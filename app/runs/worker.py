import asyncio
import json
import socket
import time
from contextlib import suppress

from app.config.settings import get_settings
from app.db.google_clients import request_google_credentials
from app.db.oauth_credentials import load_google_credentials
from app.runs.incident import build_incident, completion_from_steps
from app.runs.repository import append_event
from app.runs.verifier import verify_executions
from app.mlops.metrics import run_duration, run_failures, run_transitions


def classify_error(exc: Exception) -> str:
    text = str(exc).lower()
    if "429" in text or "rate limit" in text or "quota" in text:
        return "rate_limit"
    if "oauth" in text or "credential" in text or "unauthorized" in text:
        return "authentication"
    if "permission" in text or "403" in text:
        return "permission"
    if "timeout" in text or "connection" in text:
        return "network"
    return "execution"


async def claim_run(pool, owner: str):
    lease = get_settings().worker_lease_seconds
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """SELECT * FROM agent_runs
                   WHERE (status='queued' OR
                         (status='running' AND lease_expires_at < now()))
                     AND deleted_at IS NULL
                   ORDER BY queued_at
                   FOR UPDATE SKIP LOCKED LIMIT 1"""
            )
            if not row:
                return None
            updated = await conn.fetchrow(
                """UPDATE agent_runs SET status='running',current_phase='execution',
                   started_at=COALESCE(started_at,now()),heartbeat_at=now(),
                   lease_owner=$1,lease_expires_at=now()+($2 * interval '1 second')
                   WHERE id=$3 RETURNING *""",
                owner, lease, row["id"],
            )
            return dict(updated)


async def _heartbeat(pool, run_id, owner):
    while True:
        await asyncio.sleep(max(5, get_settings().worker_lease_seconds // 3))
        async with pool.acquire() as conn:
            result = await conn.execute(
                """UPDATE agent_runs SET heartbeat_at=now(),
                   lease_expires_at=now()+($1 * interval '1 second')
                   WHERE id=$2 AND lease_owner=$3 AND status='running'""",
                get_settings().worker_lease_seconds, run_id, owner,
            )
        if result.endswith("0"):
            return


def _contains_failure(value) -> bool:
    if isinstance(value, dict):
        if value.get("error") or value.get("success") is False:
            return True
        return any(_contains_failure(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_failure(item) for item in value)
    return False


def _find_artifacts(value, found=None):
    """Extract stable Google resource evidence without retaining message bodies."""
    found = found if found is not None else []
    if isinstance(value, dict):
        external_id = next((str(value[key]) for key in (
            "spreadsheetId", "documentId", "fileId", "messageId", "eventId",
            "taskId", "spaceId", "conferenceId", "id", "name",
        ) if value.get(key)), None)
        url = next((str(value[key]) for key in (
            "spreadsheetUrl", "documentUrl", "webViewLink", "htmlLink", "meetLink",
            "meetingUri", "url", "link",
        ) if value.get(key)), None)
        if external_id or url:
            found.append({"external_id": external_id, "url": url})
        for item in value.values():
            _find_artifacts(item, found)
    elif isinstance(value, list):
        for item in value:
            _find_artifacts(item, found)
    return found


def verify_step(step, result) -> tuple[bool, str, list[dict]]:
    """Legacy structural verifier retained for old graph/test compatibility."""
    tool_results = result.get("tool_results", [])
    if _contains_failure(tool_results):
        return False, "At least one tool returned explicit failure evidence", []
    artifacts = _find_artifacts(tool_results)
    if not step["read_only"] and not tool_results:
        return False, "A write step completed without any tool result", []
    if not step["read_only"] and not artifacts:
        return False, "A write step returned no stable resource ID or URL", []
    if not result.get("task_complete"):
        return False, result.get("error") or "The agent did not reach a completed state", artifacts
    return True, "Deterministic postconditions passed", artifacts


async def _claim_step(conn, run_id):
    return await conn.fetchrow(
        """UPDATE agent_run_steps SET status='running',attempt_count=attempt_count+1,
           started_at=now() WHERE id=(
             SELECT candidate.id FROM agent_run_steps candidate
             WHERE candidate.run_id=$1 AND candidate.status='pending'
               AND NOT EXISTS (
                 SELECT 1 FROM unnest(candidate.dependencies) dependency
                 LEFT JOIN agent_run_steps required
                   ON required.run_id=candidate.run_id AND required.step_key=dependency
                 WHERE required.id IS NULL OR required.status!='completed'
               )
             ORDER BY candidate.sequence_no FOR UPDATE SKIP LOCKED LIMIT 1
           ) RETURNING *""",
        run_id,
    )


async def _dependency_context(conn, step):
    if not step["dependencies"]:
        return []
    rows = await conn.fetch(
        """SELECT step_key,service,output_data FROM agent_run_steps
           WHERE run_id=$1 AND step_key=ANY($2::text[]) ORDER BY sequence_no""",
        step["run_id"], step["dependencies"],
    )
    return [dict(row) for row in rows]


async def _store_artifacts(conn, run, step, artifacts):
    for index, artifact in enumerate(artifacts):
        external_id = artifact.get("external_id") or f"url-{index}"
        await conn.execute(
            """INSERT INTO agent_artifacts
               (run_id,step_id,user_id,artifact_type,external_id,url,metadata,
                verification_status,verified_at)
               VALUES($1,$2,$3,$4,$5,$6,$7::jsonb,'verified',now())
               ON CONFLICT(run_id,artifact_type,external_id) DO UPDATE SET
                 url=COALESCE(EXCLUDED.url,agent_artifacts.url),
                 verification_status='verified',verified_at=now()""",
            run["id"], step["id"], run["user_id"], step["service"] or "google_resource",
            external_id, artifact.get("url"), json.dumps({"source": "tool_result"}),
        )


async def execute_run(app, pool, run):
    run_id = run["id"]
    user_id = run["user_id"]
    heartbeat = asyncio.create_task(_heartbeat(pool, run_id, run["lease_owner"]))
    credential_token = None
    started = time.perf_counter()
    try:
        credentials = await load_google_credentials(pool, user_id)
        if credentials is None and not get_settings().allow_dev_auth:
            raise RuntimeError("Google credentials are not connected")
        credential_token = request_google_credentials.set(credentials)
        final_output = ""
        while True:
            async with pool.acquire() as conn:
                current_status = await conn.fetchval(
                    "SELECT status FROM agent_runs WHERE id=$1", run_id
                )
                if current_status == "cancelled":
                    return
                step_row = await _claim_step(conn, run_id)
                if not step_row:
                    pending = await conn.fetchval(
                        "SELECT count(*) FROM agent_run_steps WHERE run_id=$1 AND status='pending'",
                        run_id,
                    )
                    if pending:
                        raise RuntimeError("No executable step: dependency graph is blocked")
                    break
                step = dict(step_row)
                dependencies = await _dependency_context(conn, step)
                await conn.execute(
                    "UPDATE agent_runs SET current_step_id=$1,heartbeat_at=now() WHERE id=$2",
                    step["id"], run_id,
                )
            await append_event(pool, run_id, user_id, "step_started", step_id=step["id"],
                               phase="execution", message=step["title"])
            step_started = time.perf_counter()
            dependency_text = json.dumps(dependencies, default=str)
            scoped_message = (
                f"Overall request: {run['request']}\n\n"
                f"Execute only the {step['service']} portion now. Do not repeat work from "
                f"completed dependency steps. Dependency outputs: {dependency_text}"
            )
            initial = {
                "message": scoped_message, "session_id": run["session_id"],
                "user_id": user_id, "run_id": str(run_id), "step_id": str(step["id"]),
                "forced_service": step["service"], "messages": [],
                "risk_level": run["risk_level"],
                "allow_small_fallback": (
                    run["risk_level"] == "low" and len((run["plan"] or {}).get("services", [])) <= 1
                ),
            }
            result = await app.state.agent_graph.ainvoke(
                initial, config={"configurable": {
                    "thread_id": f"{run_id}:{step['step_key']}"
                }}
            )
            final_output = result.get("output", "")
            executions = result.get("tool_executions", [])
            if executions:
                verified, evidence, artifacts = await verify_executions(executions)
                if verified and not result.get("task_complete"):
                    verified = False
                    evidence = result.get("error") or "The agent did not reach a completed state"
            else:
                verified, evidence, artifacts = verify_step(step, result)
            elapsed_ms = int((time.perf_counter() - step_started) * 1000)
            async with pool.acquire() as conn:
                await conn.execute(
                    """UPDATE agent_run_steps SET status=$1,output_data=$2::jsonb,
                       duration_ms=$3,completed_at=now(),error_category=$4,error_message=$5
                       WHERE id=$6""",
                    "completed" if verified else "failed",
                    json.dumps({"output": final_output,
                                "tool_results": result.get("tool_results", []),
                                "tool_executions": executions,
                                "verification": evidence}, default=str),
                    elapsed_ms, None if verified else "verification",
                    None if verified else evidence, step["id"],
                )
                await _store_artifacts(conn, run, step, artifacts)
            await append_event(
                pool, run_id, user_id,
                "verification_succeeded" if verified else "verification_failed",
                step_id=step["id"], phase="verification", message=evidence,
                payload={"artifacts": len(artifacts)},
            )
            if not verified:
                raise RuntimeError(evidence)
            await append_event(pool, run_id, user_id, "step_completed",
                               step_id=step["id"], phase="execution",
                               message=final_output,
                               payload={"artifact_count": len(artifacts)})

        async with pool.acquire() as conn:
            steps = [dict(row) for row in await conn.fetch(
                "SELECT * FROM agent_run_steps WHERE run_id=$1 ORDER BY sequence_no", run_id
            )]
            completion = completion_from_steps(steps)
            usage = await conn.fetchrow(
                """SELECT coalesce(array_agg(DISTINCT model) FILTER(WHERE model IS NOT NULL),'{}') AS models,
                          coalesce(sum(input_tokens),0) AS input_tokens,
                          coalesce(sum(output_tokens),0) AS output_tokens
                   FROM agent_model_calls WHERE run_id=$1""",
                run_id,
            )
            await conn.execute(
                """UPDATE agent_runs SET status='completed',current_phase='completed',
                   result=$1::jsonb,incident_summary='{}'::jsonb,
                   technical_completion=$2,functional_completion=$3,
                   user_visible_completion=$4,side_effect_integrity=$5,
                   error_category=NULL,error_message=NULL,completed_at=now(),heartbeat_at=now(),
                   current_step_id=NULL,lease_owner=NULL,lease_expires_at=NULL,
                   models_used=$6,input_tokens=$7,output_tokens=$8 WHERE id=$9""",
                json.dumps({"output": final_output}, default=str),
                completion["technical_completion"], completion["functional_completion"],
                completion["user_visible_completion"], completion["side_effect_integrity"],
                usage["models"], usage["input_tokens"], usage["output_tokens"], run_id,
            )
        await append_event(pool, run_id, user_id, "run_completed", phase="completed",
                           message=final_output, payload={"task_complete": True})
        run_transitions.labels("completed").inc()
        run_duration.labels("completed").observe(time.perf_counter() - started)
    except Exception as exc:
        category = classify_error(exc)
        retrying = False
        async with pool.acquire() as conn, conn.transaction():
            running_step = await conn.fetchrow(
                "SELECT * FROM agent_run_steps WHERE run_id=$1 AND status='running'",
                run_id,
            )
            if (
                running_step and running_step["read_only"]
                and category in {"network", "rate_limit", "worker"}
                and running_step["attempt_count"] < running_step["max_attempts"]
            ):
                await conn.execute(
                    """UPDATE agent_run_steps SET status='pending',error_category=$1,
                         error_message=$2 WHERE id=$3""",
                    category, str(exc), running_step["id"],
                )
                await conn.execute(
                    """UPDATE agent_runs SET status='queued',current_phase='retry_wait',
                         error_category=$1,error_message=$2,lease_owner=NULL,
                         lease_expires_at=NULL,current_step_id=NULL WHERE id=$3""",
                    category, str(exc), run_id,
                )
                retrying = True
        if retrying:
            await append_event(
                pool, run_id, user_id, "retry_scheduled", phase="recovery",
                message=str(exc), payload={"category": category},
            )
            run_transitions.labels("queued").inc()
            return
        async with pool.acquire() as conn:
            await conn.execute(
                """UPDATE agent_run_steps SET status='failed',error_category=$1,
                   error_message=$2,completed_at=now()
                   WHERE run_id=$3 AND status='running'""",
                category, str(exc), run_id,
            )
            steps = [dict(row) for row in await conn.fetch(
                "SELECT * FROM agent_run_steps WHERE run_id=$1 ORDER BY sequence_no", run_id
            )]
            completion = completion_from_steps(steps)
            incident = build_incident(steps, category, str(exc))
            terminal_status = (
                "partial" if any(step["status"] == "completed" for step in steps)
                else "failed"
            )
            usage = await conn.fetchrow(
                """SELECT coalesce(array_agg(DISTINCT model) FILTER(WHERE model IS NOT NULL),'{}') AS models,
                          coalesce(sum(input_tokens),0) AS input_tokens,
                          coalesce(sum(output_tokens),0) AS output_tokens
                   FROM agent_model_calls WHERE run_id=$1""",
                run_id,
            )
            await conn.execute(
                """UPDATE agent_runs SET status=$1,current_phase=$1,
                   incident_summary=$2::jsonb,technical_completion=$3,
                   functional_completion=$4,user_visible_completion=$5,
                   side_effect_integrity=$6,error_category=$7,error_message=$8,
                   completed_at=now(),lease_owner=NULL,lease_expires_at=NULL,
                   models_used=$9,input_tokens=$10,output_tokens=$11 WHERE id=$12""",
                terminal_status, json.dumps(incident), completion["technical_completion"],
                completion["functional_completion"], completion["user_visible_completion"],
                completion["side_effect_integrity"], category, str(exc),
                usage["models"], usage["input_tokens"], usage["output_tokens"], run_id,
            )
        await append_event(
            pool, run_id, user_id, f"run_{terminal_status}", phase=terminal_status,
            message=str(exc), payload={"category": category},
        )
        run_transitions.labels(terminal_status).inc()
        run_failures.labels(category).inc()
        run_duration.labels(terminal_status).observe(time.perf_counter() - started)
    finally:
        if credential_token is not None:
            request_google_credentials.reset(credential_token)
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat


async def worker_loop(app, pool, stop_event: asyncio.Event):
    owner = f"{socket.gethostname()}:{id(asyncio.current_task())}"
    while not stop_event.is_set():
        run = await claim_run(pool, owner)
        if run:
            await execute_run(app, pool, run)
            continue
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=get_settings().worker_poll_seconds
            )
        except asyncio.TimeoutError:
            pass
