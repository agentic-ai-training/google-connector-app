import asyncio
import hashlib
import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.config.settings import get_settings
from app.config.feature_flags import feature_enabled, get_feature_flag
from app.db.connection import get_pool
from app.db.google_clients import request_google_credentials
from app.db import google_clients as google
from app.db.oauth_credentials import load_google_credentials
from app.runs.repository import (
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
from app.runs.schemas import (
    ArtifactCleanupDecision,
    ArtifactCleanupRequest,
    RunClarification,
    RunCreate,
    RunDecision,
    RunResume,
)
from app.improvements.failure_intelligence import record_failure_incident
from app.runs.planner import classify_request

router = APIRouter(prefix="/runs", tags=["runs"])
sessions_router = APIRouter(prefix="/sessions", tags=["runs"])


def _serializable(value):
    return json.loads(json.dumps(value, default=str))


def _cleanup_hash(user_id: str, artifact_id: str, external_id: str, action: str) -> str:
    value = f"{user_id}\0{artifact_id}\0{external_id}\0{action}"
    return hashlib.sha256(value.encode()).hexdigest()


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
    if not get_settings().durable_runs_enabled:
        raise HTTPException(503, "Durable runs are disabled")
    pool = await get_pool()
    if not await feature_enabled(pool, "durable_runs", request.state.user_id):
        raise HTTPException(503, "Durable runs are disabled by the runtime feature flag")
    pilot = await get_feature_flag(pool, "pilot_cohorts")
    if pilot and pilot["enabled"] and not await feature_enabled(
        pool, "pilot_cohorts", request.state.user_id
    ):
        raise HTTPException(403, "This account is not in the active pilot cohort")
    try:
        run, created = await create_run(
            pool, request.state.user_id, body.message,
            body.session_id, body.idempotency_key,
        )
    except RunLimitExceeded as exc:
        try:
            policy = classify_request(body.message)
            incident = await record_failure_incident(
                pool, occurrence_key=f"intake:{body.idempotency_key or uuid.uuid4()}:admission",
                session_id=body.session_id, user_id=request.state.user_id,
                message=body.message, intent_kind=policy["intent_kind"],
                stage="admission", category="rate_limit", component="run_admission",
                error=str(exc), breaking_point="Run admission policy", policy=policy,
            )
        except Exception:
            incident = None
        detail = {"message": str(exc), "stage": "admission",
                  "incident_id": str(incident["id"]) if incident else None}
        raise HTTPException(429, detail) from exc
    except (ValueError, KeyError, TypeError) as exc:
        try:
            policy = classify_request(body.message)
            incident = await record_failure_incident(
                pool, occurrence_key=f"intake:{body.idempotency_key or uuid.uuid4()}:planning",
                session_id=body.session_id, user_id=request.state.user_id,
                message=body.message, intent_kind=policy["intent_kind"],
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
    status = await clarify_run(
        await get_pool(), run_id, request.state.user_id, body.answers,
    )
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
    async with pool.acquire() as conn:
        result = await conn.execute(
            """UPDATE agent_runs SET status='queued',current_phase='queued',
               completed_at=NULL,error_category=NULL,error_message=NULL,
               lease_owner=NULL,lease_expires_at=NULL
               WHERE id=$1 AND user_id=$2 AND status IN ('failed','partial')""",
            run_id, request.state.user_id,
        )
        if result.endswith("0"):
            raise HTTPException(409, "Run cannot be resumed")
        if body.retry_failed_step:
            await conn.execute(
                """UPDATE agent_run_steps SET status='pending',error_category=NULL,
                   error_message=NULL WHERE run_id=$1 AND status='failed'""",
                run_id,
            )
    return {"run_id": run_id, "status": "queued"}


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
            async with pool.acquire() as conn, conn.transaction():
                await conn.execute(
                    """UPDATE agent_run_steps SET status='pending',error_category=NULL,
                       error_message=NULL WHERE run_id=$1 AND status='failed'""",
                    run_id,
                )
                await conn.execute(
                    """UPDATE agent_runs SET status='queued',current_phase='queued',
                       completed_at=NULL,error_category=NULL,error_message=NULL,
                       lease_owner=NULL,lease_expires_at=NULL WHERE id=$1 AND user_id=$2""",
                    run_id, request.state.user_id,
                )
            result = {"run_id": run_id, "queued": True}
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
