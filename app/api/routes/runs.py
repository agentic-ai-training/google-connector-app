import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.config.settings import get_settings
from app.config.feature_flags import feature_enabled, get_feature_flag
from app.db.connection import get_pool
from app.runs.repository import (
    RunLimitExceeded,
    cancel_run,
    clarify_run,
    create_run,
    decide_run,
    get_run,
    list_events,
)
from app.runs.schemas import RunClarification, RunCreate, RunDecision, RunResume

router = APIRouter(prefix="/runs", tags=["runs"])
sessions_router = APIRouter(prefix="/sessions", tags=["runs"])


def _serializable(value):
    return json.loads(json.dumps(value, default=str))


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
        raise HTTPException(429, str(exc)) from exc
    return {
        "run_id": str(run["id"]), "status": run["status"],
        "created": created, "requires_approval": run["requires_approval"],
    }


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
