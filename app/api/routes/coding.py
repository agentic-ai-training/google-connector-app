"""Authenticated control plane for durable hosted coding-agent runs."""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request

from app.coding.repository import (
    CodingRunConflict,
    cancel_coding_run,
    coding_run_detail,
    create_coding_run,
    decide_coding_run,
    list_coding_events,
    list_coding_runs,
)
from app.coding.schemas import CodingRunApproval, CodingRunCancel, CodingRunCreate
from app.config.settings import get_settings
from app.db.connection import get_pool


router = APIRouter(prefix="/coding/runs", tags=["coding"])


def _authorize(request: Request, repository: str | None = None) -> None:
    settings = get_settings()
    if not settings.coding_agent_enabled:
        raise HTTPException(503, "The hosted coding agent is not enabled")
    if settings.coding_agent_admin_only and not getattr(request.state, "is_admin", False):
        raise HTTPException(403, "The hosted coding-agent pilot is administrator-only")
    allowed = {
        value.strip().lower()
        for value in settings.coding_allowed_repositories.split(",")
        if value.strip()
    }
    if repository and allowed and repository.lower() not in allowed:
        raise HTTPException(403, "Repository is outside the configured coding-agent allowlist")


@router.post("")
async def create(body: CodingRunCreate, request: Request):
    _authorize(request, body.repository)
    if body.source_egress_consent is not True:
        raise HTTPException(
            422,
            "Explicit source-egress consent is required for the hosted Groq planner",
        )
    try:
        run = await create_coding_run(
            await get_pool(), user_id=request.state.user_id,
            repository=body.repository, base_ref=body.base_ref,
            request=body.request, idempotency_key=body.idempotency_key,
        )
    except CodingRunConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"run": run}


@router.get("")
async def list_runs(
    request: Request, limit: int = Query(default=50, ge=1, le=100),
):
    _authorize(request)
    return {"runs": await list_coding_runs(await get_pool(), request.state.user_id, limit)}


@router.get("/{run_id}")
async def get_run(run_id: UUID, request: Request):
    _authorize(request)
    run = await coding_run_detail(
        await get_pool(), str(run_id), request.state.user_id,
    )
    if not run:
        raise HTTPException(404, "Coding run not found")
    return {"run": run}


@router.get("/{run_id}/events")
async def events(
    run_id: UUID, request: Request,
    after_id: int = Query(default=0, ge=0),
):
    _authorize(request)
    rows = await list_coding_events(
        await get_pool(), str(run_id), request.state.user_id, after_id,
    )
    if rows is None:
        raise HTTPException(404, "Coding run not found")
    return {"events": rows}


@router.post("/{run_id}/decision")
async def decision(run_id: UUID, body: CodingRunApproval, request: Request):
    _authorize(request)
    try:
        run = await decide_coding_run(
            await get_pool(), run_id=str(run_id), user_id=request.state.user_id,
            action_hash=body.action_hash, decision=body.decision, note=body.note,
        )
    except CodingRunConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"run": run}


@router.post("/{run_id}/cancel")
async def cancel(run_id: UUID, body: CodingRunCancel, request: Request):
    _authorize(request)
    run = await cancel_coding_run(
        await get_pool(), run_id=str(run_id), user_id=request.state.user_id,
        reason=body.reason,
    )
    if not run:
        raise HTTPException(409, "Coding run cannot be cancelled")
    return {"run": run}
