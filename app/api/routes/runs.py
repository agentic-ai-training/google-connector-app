import asyncio
import hashlib
import json
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import httpx
import jwt
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.config.settings import get_settings
from app.config.feature_flags import feature_enabled, get_feature_flag
from app.db.connection import get_pool
from app.db.google_clients import request_google_credentials
from app.db import google_clients as google
from app.db.oauth_credentials import load_google_credentials
from app.runs.repository import (
    CandidateAssignmentMismatch,
    InvalidClarificationAnswers,
    RunLimitExceeded,
    append_event,
    cancel_run,
    clarify_run,
    create_run,
    decide_run,
    get_run,
    list_events,
    search_runs,
)
from app.runs.context import analyze_conversation_context
from app.runs.request_analysis import (
    RequestStatementAnalysis,
    analyze_request_statement,
)
from app.runs.schemas import (
    ArtifactCleanupDecision,
    ArtifactCleanupRequest,
    RunClarification,
    RunCreate,
    RunDecision,
    RunResume,
)
from app.improvements.failure_intelligence import record_failure_incident
from app.mlops.metrics import write_reconciliation
from app.runs.planner import classify_request, infer_operation
from app.runs.reconciliation import (
    ReconciliationDecision,
    reconcile_failed_step,
)
from app.improvements.routing import (
    resolve_candidate_api_target,
    resolve_run_candidate_api_target,
)

router = APIRouter(prefix="/runs", tags=["runs"])
sessions_router = APIRouter(prefix="/sessions", tags=["runs"])


def _serializable(value):
    return json.loads(json.dumps(value, default=str))


def _decision_payload(decision: ReconciliationDecision) -> dict:
    return _serializable(asdict(decision))


def _routing_plan(
    message: str, timezone_name: str | None = None,
    authority_message: str | None = None,
    request_analysis: RequestStatementAnalysis | None = None,
) -> dict:
    policy = classify_request(
        message, timezone_name, authority_message=authority_message,
        request_analysis=request_analysis,
    )
    services = policy.get("services") or []
    return {
        "services": services,
        "rag_mode": policy.get("rag_mode", "none"),
        "steps": [
            {"operation": infer_operation(service, message, policy.get("write", False))}
            for service in services
        ],
    }


def _candidate_assertion(user_id: str, target) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    return jwt.encode({
        "purpose": "candidate-api-forward", "sub": user_id,
        "candidate_version": target.candidate_version,
        "canary_id": target.canary_id, "iat": now,
        "exp": now + timedelta(seconds=60),
    }, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def _validated_candidate_assertion(request: Request) -> dict:
    settings = get_settings()
    token = request.headers.get("x-candidate-forward", "")
    if not token:
        raise HTTPException(403, "Direct candidate API ingress is forbidden")
    try:
        claims = jwt.decode(
            token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm],
            options={"require": ["sub", "exp", "purpose", "candidate_version", "canary_id"]},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(403, "Invalid candidate forwarding assertion") from exc
    if (
        claims.get("purpose") != "candidate-api-forward"
        or claims.get("sub") != request.state.user_id
        or claims.get("candidate_version") != settings.executor_version
    ):
        raise HTTPException(403, "Candidate forwarding assertion does not match this runtime")
    return claims


async def _forward_to_candidate(target, path: str, request: Request, payload: dict):
    authorization = request.headers.get("authorization", "")
    headers = {
        "Authorization": authorization,
        "X-Candidate-Forward": _candidate_assertion(request.state.user_id, target),
    }
    try:
        async with httpx.AsyncClient(timeout=get_settings().candidate_api_request_timeout_seconds) as client:
            response = await client.post(target.url + path, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        raise HTTPException(503, "The selected candidate runtime is unavailable; the run was not sent to control") from exc
    try:
        content = response.json()
    except ValueError:
        content = {"detail": "Candidate runtime returned an invalid response"}
    if response.status_code >= 400:
        raise HTTPException(response.status_code, content.get("detail", content))
    return content


def _cleanup_hash(user_id: str, artifact_id: str, external_id: str, action: str) -> str:
    value = f"{user_id}\0{artifact_id}\0{external_id}\0{action}"
    return hashlib.sha256(value.encode()).hexdigest()


async def _reconcile_and_resume(
    pool, run_id: str, user_id: str, step_id: str | None = None,
) -> ReconciliationDecision:
    """Reconcile one failed step, then mutate only the proven-safe resume point."""
    async with pool.acquire() as conn, conn.transaction():
        run_row = await conn.fetchrow(
            """SELECT * FROM agent_runs WHERE id=$1 AND user_id=$2
               AND status IN ('failed','partial') FOR UPDATE""",
            run_id, user_id,
        )
        if not run_row:
            raise HTTPException(409, "Run cannot be resumed")
        if step_id:
            step_row = await conn.fetchrow(
                """SELECT * FROM agent_run_steps
                   WHERE id=$1 AND run_id=$2 AND status='failed' FOR UPDATE""",
                step_id, run_id,
            )
        else:
            step_row = await conn.fetchrow(
                """SELECT * FROM agent_run_steps
                   WHERE run_id=$1 AND status='failed'
                   ORDER BY sequence_no LIMIT 1 FOR UPDATE""",
                run_id,
            )
        if not step_row:
            raise HTTPException(409, "No matching failed step can be resumed")
        artifacts = await conn.fetch(
            "SELECT * FROM agent_artifacts WHERE run_id=$1 AND step_id=$2",
            run_id, step_row["id"],
        )
        historical_tool_attempts = await conn.fetch(
            """SELECT tool_name,status,error_category FROM agent_tool_attempts
               WHERE run_id=$1 AND step_id=$2 ORDER BY attempt_no""",
            run_id, step_row["id"],
        )
        await conn.execute(
            """UPDATE agent_runs SET current_phase='reconciling',
               current_step_id=$1 WHERE id=$2""",
            step_row["id"], run_id,
        )

    run = dict(run_row)
    step = dict(step_row)
    step["historical_tool_attempts"] = [
        dict(item) for item in historical_tool_attempts
    ]
    prior_executions = (
        (step.get("output_data") or {}).get("tool_executions", [])
        if isinstance(step.get("output_data"), dict) else []
    )
    credentials_token = None
    if prior_executions and not step.get("read_only"):
        credentials = await load_google_credentials(pool, user_id)
        if credentials is not None:
            credentials_token = request_google_credentials.set(credentials)
    try:
        decision = await reconcile_failed_step(
            run, step, [dict(item) for item in artifacts],
        )
    finally:
        if credentials_token is not None:
            request_google_credentials.reset(credentials_token)

    settings = get_settings()
    resume_executor_version = (
        settings.executor_version or settings.deployment_version
    )
    previous_executor_version = run.get("executor_version")
    async with pool.acquire() as conn, conn.transaction():
        locked = await conn.fetchrow(
            """SELECT status,error_category FROM agent_run_steps
               WHERE id=$1 AND run_id=$2 FOR UPDATE""",
            step_row["id"], run_id,
        )
        if not locked or locked["status"] != "failed":
            raise HTTPException(409, "The failed step changed during reconciliation")
        if decision.state == "already_completed":
            await conn.execute(
                """UPDATE agent_run_steps SET status='completed',
                   error_category=NULL,error_message=NULL,completed_at=COALESCE(completed_at,now())
                   WHERE id=$1""",
                step_row["id"],
            )
        elif decision.state == "safe_to_retry":
            await conn.execute(
                """UPDATE agent_run_steps SET status='pending',started_at=NULL,
                   completed_at=NULL,error_category=NULL,error_message=NULL WHERE id=$1""",
                step_row["id"],
            )
        else:
            await conn.execute(
                """UPDATE agent_runs SET current_phase='reconciliation',
                   current_step_id=$1,error_category='worker_reconciliation',
                   error_message=$2 WHERE id=$3""",
                step_row["id"], decision.reason_code, run_id,
            )
        if decision.state != "manual_required":
            await conn.execute(
                """UPDATE agent_runs SET status='queued',current_phase='queued',
                   completed_at=NULL,error_category=NULL,error_message=NULL,
                   current_step_id=NULL,lease_owner=NULL,lease_expires_at=NULL,
                   side_effect_integrity=100,executor_version=$3
                   WHERE id=$1 AND user_id=$2""",
                run_id, user_id, resume_executor_version,
            )
    await append_event(
        pool, run_id, user_id, "run_reconciled",
        step_id=step_row["id"], phase="reconciliation",
        message=f"Resume decision: {decision.reason_code}",
        payload={
            "state": decision.state,
            "reason_code": decision.reason_code,
            "resume_step_id": decision.resume_step_id,
            "evidence": decision.evidence,
            "previous_executor_version": previous_executor_version,
            "resume_executor_version": (
                resume_executor_version
                if decision.state != "manual_required" else previous_executor_version
            ),
        },
    )
    write_reconciliation.labels(
        step.get("service") or "unknown",
        step.get("operation") or "unknown",
        decision.state,
    ).inc()
    return decision


def _google_cleanup(artifact: dict, action: str) -> dict:
    external_id = artifact["external_id"]
    metadata = artifact.get("metadata") or {}
    identifiers = metadata.get("identifiers") or {}
    if action == "delete":
        result = google.drive_service.files().update(
            fileId=external_id, body={"trashed": True}, fields="id,trashed"
        ).execute()
        if not result.get("trashed"):
            raise RuntimeError("Drive resource was not moved to trash")
        return {"external_id": external_id, "trashed": True}
    if action == "cancel_event":
        calendar_id = identifiers.get("calendar_id", "primary")
        google.calendar_service.events().delete(
            calendarId=calendar_id, eventId=external_id, sendUpdates="all"
        ).execute()
        return {"external_id": external_id, "cancelled": True}
    if action == "rollback_sharing":
        permission_id = metadata.get("permission_id")
        if not permission_id:
            raise RuntimeError("The verified permission ID is unavailable; manual cleanup is required")
        google.drive_service.permissions().delete(
            fileId=external_id, permissionId=permission_id
        ).execute()
        return {"external_id": external_id, "permission_id": permission_id,
                "sharing_rolled_back": True}
    raise RuntimeError(f"Unsupported Google cleanup action: {action}")


@router.post("", status_code=202)
async def start_run(body: RunCreate, request: Request):
    settings = get_settings()
    if not settings.durable_runs_enabled:
        raise HTTPException(503, "Durable runs are disabled")
    pool = await get_pool()
    if not await feature_enabled(pool, "durable_runs", request.state.user_id):
        raise HTTPException(503, "Durable runs are disabled by the runtime feature flag")
    pilot = await get_feature_flag(pool, "pilot_cohorts")
    if pilot and pilot["enabled"] and not await feature_enabled(
        pool, "pilot_cohorts", request.state.user_id
    ):
        raise HTTPException(403, "This account is not in the active pilot cohort")
    request_analysis = analyze_request_statement(body.message)
    context_analysis = await analyze_conversation_context(
        pool,
        user_id=request.state.user_id,
        session_id=body.session_id,
        message=body.message,
        request_analysis=request_analysis,
    )
    effective_message = context_analysis.effective_message
    candidate_claims = None
    if settings.executor_role == "candidate":
        candidate_claims = _validated_candidate_assertion(request)
    elif settings.executor_role == "control":
        async with pool.acquire() as conn, conn.transaction():
            target = await resolve_candidate_api_target(
                conn, request.state.user_id,
                _routing_plan(
                    effective_message, body.timezone, body.message,
                    request_analysis,
                ),
            )
        if target:
            return await _forward_to_candidate(
                target, "/runs", request, body.model_dump(mode="json"),
            )
    try:
        run, created = await create_run(
            pool, request.state.user_id, body.message,
            body.session_id, body.idempotency_key,
            required_executor_version=(candidate_claims or {}).get("candidate_version"),
            timezone_name=body.timezone,
            planning_message=effective_message,
            context_diagnostics=context_analysis.diagnostics(),
            request_analysis=request_analysis,
        )
    except RunLimitExceeded as exc:
        try:
            policy = classify_request(
                effective_message, body.timezone, authority_message=body.message,
                request_analysis=request_analysis,
            )
            incident = await record_failure_incident(
                pool, occurrence_key=f"intake:{body.idempotency_key or uuid.uuid4()}:admission",
                session_id=body.session_id, user_id=request.state.user_id,
                message=effective_message, intent_kind=policy["intent_kind"],
                stage="admission", category="rate_limit", component="run_admission",
                error=str(exc), breaking_point="Run admission policy", policy=policy,
            )
        except Exception:
            incident = None
        detail = {"message": str(exc), "stage": "admission",
                  "incident_id": str(incident["id"]) if incident else None}
        raise HTTPException(429, detail) from exc
    except (CandidateAssignmentMismatch, ValueError, KeyError, TypeError) as exc:
        try:
            policy = classify_request(
                effective_message, body.timezone, authority_message=body.message,
                request_analysis=request_analysis,
            )
            incident = await record_failure_incident(
                pool, occurrence_key=f"intake:{body.idempotency_key or uuid.uuid4()}:planning",
                session_id=body.session_id, user_id=request.state.user_id,
                message=effective_message, intent_kind=policy["intent_kind"],
                stage="planning", category="planning", component="request_planner",
                error=str(exc), breaking_point="Request planning", policy=policy,
            )
        except Exception:
            incident = None
        raise HTTPException(422, {
            "message": "The request could not be converted into a safe Workspace plan.",
            "stage": "planning", "reason": str(exc),
            "incident_id": str(incident["id"]) if incident else None,
        }) from exc
    except Exception as exc:
        try:
            policy = classify_request(body.message)
            incident = await record_failure_incident(
                pool, occurrence_key=f"intake:{body.idempotency_key or uuid.uuid4()}:api",
                session_id=body.session_id, user_id=request.state.user_id,
                message=body.message, intent_kind=policy["intent_kind"], stage="api",
                category="persistence", component="runs_api", error=str(exc),
                breaking_point="Creating the durable run", policy=policy,
            )
        except Exception:
            incident = None
        raise HTTPException(500, {
            "message": "The request could not be durably accepted.", "stage": "api",
            "incident_id": str(incident["id"]) if incident else None,
        }) from exc
    return {
        "run_id": str(run["id"]), "status": run["status"],
        "created": created, "requires_approval": run["requires_approval"],
    }


@router.get("")
async def run_history(
    request: Request, session_id: str | None = None, status: str | None = None,
    service: str | None = None, model: str | None = None,
    failure: str | None = None, deployment_version: str | None = None,
    started_after: datetime | None = None, started_before: datetime | None = None,
    limit: int = 100, offset: int = 0,
):
    rows = await search_runs(
        await get_pool(), user_id=request.state.user_id, session_id=session_id,
        status=status, service=service, model=model, failure=failure,
        deployment_version=deployment_version, started_after=started_after,
        started_before=started_before, limit=limit, offset=offset,
    )
    return {"runs": _serializable(rows)}


@router.get("/{run_id}")
async def read_run(run_id: str, request: Request):
    run = await get_run(await get_pool(), run_id, request.state.user_id)
    if not run:
        raise HTTPException(404, "Run not found")
    return _serializable(run)


@router.get("/{run_id}/events")
async def read_events(run_id: str, request: Request, after_id: int = 0):
    events = await list_events(
        await get_pool(), run_id, request.state.user_id, after_id
    )
    if events is None:
        raise HTTPException(404, "Run not found")
    return {"events": _serializable(events)}


@router.get("/{run_id}/stream")
async def stream_events(run_id: str, request: Request, after_id: int = 0):
    pool = await get_pool()
    if await list_events(pool, run_id, request.state.user_id, after_id) is None:
        raise HTTPException(404, "Run not found")

    async def events():
        cursor = after_id
        while True:
            rows = await list_events(pool, run_id, request.state.user_id, cursor)
            for row in rows or []:
                cursor = row["id"]
                yield f"id: {cursor}\nevent: {row['event_type']}\ndata: {json.dumps(row, default=str)}\n\n"
            run = await get_run(pool, run_id, request.state.user_id)
            if not run or run["status"] in {"completed", "failed", "partial", "cancelled"}:
                yield f"event: end\ndata: {json.dumps({'status': run['status'] if run else 'missing'})}\n\n"
                return
            yield ": heartbeat\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@router.post("/{run_id}/approve")
async def approve_run(run_id: str, body: RunDecision, request: Request):
    status = await decide_run(
        await get_pool(), run_id, request.state.user_id,
        body.approved, body.action_hash, body.note,
    )
    if not status:
        raise HTTPException(409, "Approval is missing, stale, expired, or changed")
    return {"run_id": run_id, "status": status}


@router.post("/{run_id}/clarify")
async def submit_clarification(run_id: str, body: RunClarification, request: Request):
    settings = get_settings()
    pool = await get_pool()
    if settings.executor_role == "candidate":
        _validated_candidate_assertion(request)
    else:
        async with pool.acquire() as conn:
            target = await resolve_run_candidate_api_target(
                conn, run_id, request.state.user_id,
            )
        if target:
            return await _forward_to_candidate(
                target, f"/runs/{run_id}/clarify", request,
                body.model_dump(mode="json"),
            )
    try:
        status = await clarify_run(
            pool, run_id, request.state.user_id, body.answers,
        )
    except InvalidClarificationAnswers as exc:
        raise HTTPException(422, {
            "message": str(exc),
            "reason_code": exc.reason_code,
            "suggested_value": exc.suggested_value,
        }) from exc
    if not status:
        raise HTTPException(409, "Run is not awaiting clarification")
    return {"run_id": run_id, "status": status}


@router.post("/{run_id}/cancel")
async def stop_run(run_id: str, request: Request):
    if not await cancel_run(await get_pool(), run_id, request.state.user_id):
        raise HTTPException(409, "Run cannot be cancelled")
    return {"run_id": run_id, "status": "cancelled"}


@router.post("/{run_id}/resume")
async def resume_run(run_id: str, body: RunResume, request: Request):
    pool = await get_pool()
    if not body.retry_failed_step:
        raise HTTPException(409, "A failed step must be selected for safe resume")
    decision = await _reconcile_and_resume(
        pool, run_id, request.state.user_id, body.step_id,
    )
    if decision.state == "manual_required":
        raise HTTPException(409, {
            "message": "Automatic resume requires manual reconciliation.",
            "reason_code": decision.reason_code,
            "resume_step_id": decision.resume_step_id,
        })
    return {
        "run_id": run_id, "status": "queued",
        "reconciliation": _decision_payload(decision),
    }


@router.post("/{run_id}/artifacts/{artifact_id}/cleanup-request")
async def request_artifact_cleanup(
    run_id: str, artifact_id: str, body: ArtifactCleanupRequest, request: Request
):
    """Prepare an exact compensation action; no external write happens here."""
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        artifact = await conn.fetchrow(
            """SELECT a.* FROM agent_artifacts a JOIN agent_runs r ON r.id=a.run_id
               WHERE a.id=$1 AND a.run_id=$2 AND a.user_id=$3 AND r.user_id=$3
               FOR UPDATE""",
            artifact_id, run_id, request.state.user_id,
        )
        if not artifact:
            raise HTTPException(404, "Artifact not found")
        if body.action == "delete" and (
            artifact["artifact_type"] not in {"drive", "docs", "sheets"}
            or not artifact["safe_to_delete"]
        ):
            raise HTTPException(
                409, "Only resources created by this run and marked safe may be deleted"
            )
        if body.action == "cancel_event" and artifact["artifact_type"] != "calendar":
            raise HTTPException(409, "Only a Calendar artifact can be cancelled")
        if body.action == "rollback_sharing" and artifact["artifact_type"] != "drive":
            raise HTTPException(409, "Only a Drive sharing artifact can be rolled back")
        digest = _cleanup_hash(
            request.state.user_id, artifact_id, artifact["external_id"] or "", body.action
        )
        if body.action == "preserve":
            status = "completed"
            cleanup_id = await conn.fetchval(
                """INSERT INTO artifact_cleanup_requests
                   (artifact_id,run_id,user_id,action,status,action_hash,completed_at)
                   VALUES($1,$2,$3,$4,$5,$6,now()) RETURNING id""",
                artifact_id, run_id, request.state.user_id, body.action, status, digest,
            )
        else:
            status = "awaiting_confirmation"
            cleanup_id = await conn.fetchval(
                """INSERT INTO artifact_cleanup_requests
                   (artifact_id,run_id,user_id,action,status,action_hash)
                   VALUES($1,$2,$3,$4,$5,$6) RETURNING id""",
                artifact_id, run_id, request.state.user_id, body.action, status, digest,
            )
        if body.action == "preserve":
            await conn.execute(
                "UPDATE agent_artifacts SET cleanup_state='retained' WHERE id=$1",
                artifact_id,
            )
    await append_event(
        pool, run_id, request.state.user_id, "compensation_requested",
        phase="compensation", message=f"Artifact action requested: {body.action}",
        payload={"cleanup_id": str(cleanup_id), "action": body.action,
                 "requires_confirmation": body.action != "preserve"},
    )
    return {
        "cleanup_id": str(cleanup_id), "status": status, "action": body.action,
        "action_hash": digest if body.action != "preserve" else None,
    }


@router.post("/{run_id}/artifacts/{artifact_id}/cleanup-decision")
async def decide_artifact_cleanup(
    run_id: str, artifact_id: str, body: ArtifactCleanupDecision, request: Request
):
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        cleanup = await conn.fetchrow(
            """SELECT c.*,a.artifact_type,a.external_id,a.metadata,a.safe_to_delete
                   ,a.step_id
               FROM artifact_cleanup_requests c
               JOIN agent_artifacts a ON a.id=c.artifact_id
               WHERE c.artifact_id=$1 AND c.run_id=$2 AND c.user_id=$3
                 AND c.status='awaiting_confirmation'
               ORDER BY c.requested_at DESC LIMIT 1 FOR UPDATE OF c""",
            artifact_id, run_id, request.state.user_id,
        )
        if not cleanup or cleanup["action_hash"] != body.action_hash:
            raise HTTPException(409, "Cleanup request is missing, stale, or changed")
        if cleanup["expires_at"] <= datetime.now(timezone.utc):
            await conn.execute(
                "UPDATE artifact_cleanup_requests SET status='rejected',error_message='expired' WHERE id=$1",
                cleanup["id"],
            )
            raise HTTPException(409, "Cleanup confirmation expired")
        if not body.approved:
            await conn.execute(
                """UPDATE artifact_cleanup_requests SET status='rejected',decided_at=now()
                   WHERE id=$1""",
                cleanup["id"],
            )
            return {"cleanup_id": str(cleanup["id"]), "status": "rejected"}
        await conn.execute(
            """UPDATE artifact_cleanup_requests SET status='executing',decided_at=now()
               WHERE id=$1""",
            cleanup["id"],
        )

    action = cleanup["action"]
    try:
        if action == "retry_population":
            decision = await _reconcile_and_resume(
                pool, run_id, request.state.user_id,
                str(cleanup["step_id"]) if cleanup["step_id"] else None,
            )
            if decision.state == "manual_required":
                result = {
                    "run_id": run_id, "queued": False,
                    "reason_code": decision.reason_code,
                }
                cleanup_state = "manual_required"
                raise RuntimeError(
                    f"Manual reconciliation required: {decision.reason_code}"
                )
            result = {
                "run_id": run_id, "queued": True,
                "reconciliation": _decision_payload(decision),
            }
            cleanup_state = "population_retried"
        else:
            credentials = await load_google_credentials(pool, request.state.user_id)
            if credentials is None:
                raise RuntimeError("Google authorization is missing or lacks required scopes")
            token = request_google_credentials.set(credentials)
            try:
                result = await asyncio.to_thread(_google_cleanup, dict(cleanup), action)
            finally:
                request_google_credentials.reset(token)
            cleanup_state = {
                "delete": "deleted", "cancel_event": "cancelled",
                "rollback_sharing": "sharing_rolled_back",
            }[action]
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """UPDATE artifact_cleanup_requests SET status='completed',completed_at=now(),
                   result=$1::jsonb WHERE id=$2""",
                json.dumps(result), cleanup["id"],
            )
            await conn.execute(
                "UPDATE agent_artifacts SET cleanup_state=$1 WHERE id=$2",
                cleanup_state, artifact_id,
            )
        status = "completed"
    except Exception as exc:
        manual = action == "rollback_sharing" and "permission ID" in str(exc)
        status = "manual_required" if manual else "failed"
        async with pool.acquire() as conn:
            await conn.execute(
                """UPDATE artifact_cleanup_requests SET status=$1,error_message=$2,
                   completed_at=now() WHERE id=$3""",
                status, str(exc), cleanup["id"],
            )
        result = {"error": str(exc)}
    await append_event(
        pool, run_id, request.state.user_id, "compensation_completed",
        phase="compensation", message=f"Artifact action {action}: {status}",
        payload={"cleanup_id": str(cleanup["id"]), "action": action, "status": status},
    )
    return {"cleanup_id": str(cleanup["id"]), "status": status, "result": result}


@sessions_router.get("/{session_id}/runs")
async def session_runs(session_id: str, request: Request):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id,status,current_phase,technical_completion,
                      functional_completion,user_visible_completion,risk_level,
                      error_category,queued_at,started_at,completed_at
               FROM agent_runs WHERE session_id=$1 AND user_id=$2 AND deleted_at IS NULL
               ORDER BY queued_at DESC LIMIT 100""",
            session_id, request.state.user_id,
        )
    return {"runs": _serializable([dict(row) for row in rows])}
