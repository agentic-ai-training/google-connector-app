import json
import asyncio
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from app.db.connection import get_pool
from app.db.prompt_service import (
    activate_experiment, create_experiment, conclude_experiment,
)
from app.runs.schemas import (
    ImprovementCandidateRegistration,
    ImprovementDecision,
    ImprovementDeploymentEvidence,
)
from app.runs.repository import search_runs
from app.config.settings import get_settings
from app.db.google_clients import request_google_credentials
from app.db.oauth_credentials import load_google_credentials
from app.improvements.publisher import publish_github_draft, send_proposal_email
from app.improvements.candidates import (
    candidate_digest, file_digest, validate_candidate_files,
)
router=APIRouter(prefix="/admin")
class ExperimentIn(BaseModel):
    name:str; prompt_name:str; control_id:str; variant_id:str; traffic_split:float=.5
    notes:str|None=None; selection_policy:str="ab"
class ConcludeIn(BaseModel): winner:str
class ActivateExperimentIn(BaseModel):
    confirmation: str
    evidence: dict
class ExternalPublicationDecision(BaseModel):
    proposal_hash: str
    confirmation: str
class PromptIn(BaseModel):
    name:str; content:str; model_target:str="groq/llama-3.3-70b"; temperature:float=.3; max_tokens:int=1000; notes:str|None=None
class FeatureFlagIn(BaseModel):
    enabled: bool
    config: dict = Field(default_factory=dict)


@router.get("/runs")
async def admin_run_history(
    user_id: str | None = None, session_id: str | None = None,
    status: str | None = None, service: str | None = None,
    model: str | None = None, failure: str | None = None,
    deployment_version: str | None = None, started_after: datetime | None = None,
    started_before: datetime | None = None, limit: int = 100, offset: int = 0,
):
    rows = await search_runs(
        await get_pool(), user_id=user_id, session_id=session_id, status=status,
        service=service, model=model, failure=failure,
        deployment_version=deployment_version, started_after=started_after,
        started_before=started_before, limit=limit, offset=offset,
    )
    return {"runs": rows}
@router.get("/experiments/{name}/summary")
async def summary(name:str):
    pool=await get_pool()
    async with pool.acquire() as conn: rows=await conn.fetch("SELECT * FROM experiment_summary WHERE experiment_name=$1",name)
    return {"summary":[dict(r) for r in rows]}
@router.post("/experiments")
async def create(body:ExperimentIn): return await create_experiment(**body.model_dump())
@router.post("/experiments/{name}/activate")
async def activate(name: str, body: ActivateExperimentIn, request: Request):
    if body.confirmation != "ACTIVATE LOW RISK EXPERIMENT":
        raise HTTPException(409, "Exact low-risk experiment confirmation is required")
    try:
        return await activate_experiment(name, request.state.user_id, body.evidence)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
@router.post("/experiments/{name}/conclude")
async def conclude(name:str,body:ConcludeIn): return await conclude_experiment(name,body.winner)
@router.get("/prompts")
async def prompts():
    pool=await get_pool()
    async with pool.acquire() as conn: rows=await conn.fetch("SELECT * FROM prompts ORDER BY name,version")
    return {"prompts":[dict(r) for r in rows]}
@router.post("/prompts")
async def add_prompt(body:PromptIn):
    pool=await get_pool()
    async with pool.acquire() as conn:
        row=await conn.fetchrow("INSERT INTO prompts(name,version,content,model_target,temperature,max_tokens,notes) SELECT $1,coalesce(max(version),0)+1,$2,$3,$4,$5,$6 FROM prompts WHERE name=$1 RETURNING *",body.name,body.content,body.model_target,body.temperature,body.max_tokens,body.notes)
    return dict(row)


@router.get("/feature-flags")
async def feature_flags():
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM feature_flags ORDER BY name")
    return {"feature_flags": [dict(row) for row in rows]}


@router.put("/feature-flags/{name}")
async def update_feature_flag(name: str, body: FeatureFlagIn, request: Request):
    if name == "live_rl" and body.enabled:
        raise HTTPException(409, "Live RL is safety-locked; only offline evaluation is allowed")
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO feature_flags(name,enabled,config,updated_by,updated_at)
               VALUES($1,$2,$3::jsonb,$4,now())
               ON CONFLICT(name) DO UPDATE SET enabled=excluded.enabled,
                 config=excluded.config,updated_by=excluded.updated_by,updated_at=now()
               RETURNING *""",
            name, body.enabled, json.dumps(body.config), request.state.user_id,
        )
    return dict(row)


@router.get("/improvements")
async def improvements(status: str | None = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT p.*,c.status AS canary_status,c.metrics AS canary_metrics
               FROM improvement_proposals p
               LEFT JOIN LATERAL (
                 SELECT status,metrics FROM improvement_canaries
                 WHERE proposal_id=p.id ORDER BY started_at DESC NULLS LAST LIMIT 1
               ) c ON TRUE
               WHERE ($1::text IS NULL OR p.status=$1)
               ORDER BY CASE p.severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 ELSE 2 END,
                        p.created_at DESC""",
            status,
        )
    return {"proposals": [dict(row) for row in rows]}


@router.put("/improvements/{proposal_key}/candidate")
async def register_improvement_candidate(
    proposal_key: str, body: ImprovementCandidateRegistration, request: Request,
):
    files = [item.model_dump() for item in body.files]
    errors = validate_candidate_files(files)
    if body.validation_report.get("passed") is not True:
        errors.append("Validation report must explicitly record passed=true")
    if not body.validation_report.get("commands"):
        errors.append("Validation report must list the commands that were run")
    if errors:
        raise HTTPException(422, errors)
    digest = candidate_digest(body.base_version, files, body.validation_report)
    manifest = {
        "file_count": len(files), "candidate_digest": digest,
        "registered_by": request.state.user_id,
        "canary_eligible": True,
    }
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        proposal = await conn.fetchrow(
            "SELECT * FROM improvement_proposals WHERE proposal_key=$1 FOR UPDATE",
            proposal_key,
        )
        if not proposal or proposal["status"] not in {"awaiting_review", "changes_requested"}:
            raise HTTPException(409, "Proposal cannot accept a candidate in its current state")
        await conn.execute("DELETE FROM improvement_candidate_files WHERE proposal_id=$1", proposal["id"])
        for item in files:
            await conn.execute(
                """INSERT INTO improvement_candidate_files
                   (proposal_id,path,change_type,content,content_hash)
                   VALUES($1,$2,$3,$4,$5)""",
                proposal["id"], item["path"], item["change_type"], item.get("content"),
                file_digest(item.get("content")),
            )
        await conn.execute(
            """UPDATE improvement_proposals SET candidate_kind=$1,
               candidate_state='validated_implementation',source_version=$2,
               candidate_version=$3,exact_diff=$4,candidate_manifest=$5::jsonb,
               validation_report=$6::jsonb,rollback_plan=$7::jsonb,
               deployment_evidence='{}'::jsonb,content_hash=$8,status='awaiting_review',
               updated_at=now() WHERE id=$9""",
            body.candidate_kind, body.base_version, body.candidate_version,
            body.exact_diff, json.dumps(manifest), json.dumps(body.validation_report),
            json.dumps(body.rollback_plan), digest, proposal["id"],
        )
    return {"proposal_key": proposal_key, "candidate_state": "validated_implementation",
            "content_hash": digest}


@router.put("/improvements/{proposal_key}/deployment-evidence")
async def register_candidate_deployment(
    proposal_key: str, body: ImprovementDeploymentEvidence, request: Request,
):
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        proposal = await conn.fetchrow(
            "SELECT * FROM improvement_proposals WHERE proposal_key=$1 FOR UPDATE",
            proposal_key,
        )
        if not proposal or proposal["candidate_state"] != "validated_implementation":
            raise HTTPException(409, "A validated implementation candidate is required")
        if proposal["candidate_version"] != body.candidate_version:
            raise HTTPException(409, "Deployment version does not match the frozen candidate")
        if not body.verified or body.smoke_tests.get("passed") is not True:
            raise HTTPException(422, "Verified deployment and passing smoke tests are required")
        evidence = {**body.model_dump(), "verified_by": request.state.user_id}
        await conn.execute(
            "UPDATE improvement_proposals SET deployment_evidence=$1::jsonb,updated_at=now() WHERE id=$2",
            json.dumps(evidence), proposal["id"],
        )
    return {"proposal_key": proposal_key, "deployment_verified": True}


@router.get("/improvements-pending/count")
async def pending_improvement_count():
    pool = await get_pool()
    async with pool.acquire() as conn:
        counts = await conn.fetchrow(
            """SELECT
                 count(*) FILTER(WHERE status='awaiting_review') AS review,
                 count(*) FILTER(WHERE status='approved_for_canary') AS activation,
                 count(*) FILTER(WHERE status='awaiting_promotion') AS promotion
               FROM improvement_proposals"""
        )
    values = dict(counts)
    return {**values, "total": sum(values.values())}


@router.get("/improvement-notifications")
async def improvement_notifications(limit: int = 100):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT n.*,p.proposal_key,p.title FROM improvement_notifications n
               JOIN improvement_proposals p ON p.id=n.proposal_id
               ORDER BY n.created_at DESC LIMIT $1""",
            max(1, min(limit, 200)),
        )
    return {"notifications": [dict(row) for row in rows]}


async def _reserve_external_notification(
    proposal_key: str, proposal_hash: str, channel: str, event_type: str,
    required_status: str,
):
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        proposal = await conn.fetchrow(
            "SELECT * FROM improvement_proposals WHERE proposal_key=$1 FOR UPDATE",
            proposal_key,
        )
        if not proposal or proposal["status"] != required_status:
            raise HTTPException(409, f"Proposal must be {required_status}")
        if proposal["content_hash"] != proposal_hash:
            raise HTTPException(409, "Proposal changed; review the new frozen hash")
        notification_id = await conn.fetchval(
            """INSERT INTO improvement_notifications
               (proposal_id,channel,event_type,status,sanitized_payload)
               VALUES($1,$2,$3,'queued',$4::jsonb)
               ON CONFLICT(proposal_id,channel,event_type) DO NOTHING RETURNING id""",
            proposal["id"], channel, event_type,
            json.dumps({"proposal_key": proposal_key, "content_hash": proposal_hash,
                        "contains_private_evidence": False}),
        )
        if not notification_id:
            existing = await conn.fetchrow(
                """SELECT * FROM improvement_notifications
                   WHERE proposal_id=$1 AND channel=$2 AND event_type=$3""",
                proposal["id"], channel, event_type,
            )
            if existing["status"] == "sent":
                return pool, dict(proposal), dict(existing)
            if existing["status"] == "failed":
                await conn.execute(
                    """UPDATE improvement_notifications SET status='queued',
                       error_message=NULL,created_at=now() WHERE id=$1""",
                    existing["id"],
                )
                return pool, dict(proposal), {
                    "id": existing["id"], "status": "queued"
                }
            raise HTTPException(409, "Publication is already queued or previously failed")
    return pool, dict(proposal), {"id": notification_id, "status": "queued"}


async def _finish_notification(pool, notification_id, *, reference=None, error=None):
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE improvement_notifications SET status=$1,external_reference=$2,
                 error_message=$3,sent_at=CASE WHEN $1='sent' THEN now() END
               WHERE id=$4""",
            "failed" if error else "sent", reference, error, notification_id,
        )


@router.post("/improvements/{proposal_key}/publish-draft-pr")
async def publish_proposal_draft(
    proposal_key: str, body: ExternalPublicationDecision,
):
    if body.confirmation != "PUBLISH SANITIZED DRAFT PR":
        raise HTTPException(409, "Exact draft-PR confirmation is required")
    pool, proposal, notification = await _reserve_external_notification(
        proposal_key, body.proposal_hash, "github", "draft_pr_created",
        "approved_for_publication",
    )
    if notification["status"] == "sent":
        return {"status": "sent", "url": notification["external_reference"]}
    try:
        async with pool.acquire() as conn:
            candidate_files = [dict(row) for row in await conn.fetch(
                "SELECT path,change_type,content FROM improvement_candidate_files WHERE proposal_id=$1 ORDER BY path",
                proposal["id"],
            )]
        result = await publish_github_draft(proposal, candidate_files)
        await _finish_notification(pool, notification["id"], reference=result["url"])
        return {"status": "sent", **result}
    except Exception as exc:
        await _finish_notification(pool, notification["id"], error=str(exc))
        raise HTTPException(502, f"Draft PR publication failed: {exc}") from exc


@router.post("/improvements/{proposal_key}/notify-email")
async def email_proposal_review(
    proposal_key: str, body: ExternalPublicationDecision, request: Request,
):
    if body.confirmation != "SEND SANITIZED REVIEW EMAIL":
        raise HTTPException(409, "Exact review-email confirmation is required")
    recipient = get_settings().admin_notification_email
    if not recipient:
        raise HTTPException(409, "ADMIN_NOTIFICATION_EMAIL is not configured")
    pool, proposal, notification = await _reserve_external_notification(
        proposal_key, body.proposal_hash, "email", "review_email_sent",
        "awaiting_review",
    )
    if notification["status"] == "sent":
        return {"status": "sent", "message_id": notification["external_reference"]}
    credentials = await load_google_credentials(pool, request.state.user_id)
    if credentials is None:
        await _finish_notification(
            pool, notification["id"], error="Administrator Google authorization is unavailable"
        )
        raise HTTPException(409, "Connect the administrator Google account first")
    token = request_google_credentials.set(credentials)
    try:
        result = await asyncio.to_thread(send_proposal_email, proposal, recipient)
        await _finish_notification(
            pool, notification["id"], reference=result.get("message_id")
        )
        return {"status": "sent", **result}
    except Exception as exc:
        await _finish_notification(pool, notification["id"], error=str(exc))
        raise HTTPException(502, f"Review email failed: {exc}") from exc
    finally:
        request_google_credentials.reset(token)


@router.get("/improvements/{proposal_key}")
async def improvement(proposal_key: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        proposal = await conn.fetchrow(
            "SELECT * FROM improvement_proposals WHERE proposal_key=$1", proposal_key
        )
        if not proposal:
            raise HTTPException(404, "Improvement proposal not found")
        evidence = await conn.fetch(
            "SELECT * FROM improvement_evidence WHERE proposal_id=$1 ORDER BY created_at",
            proposal["id"],
        )
        evaluations = await conn.fetch(
            "SELECT * FROM improvement_evaluations WHERE proposal_id=$1 ORDER BY created_at",
            proposal["id"],
        )
    return {
        "proposal": dict(proposal), "evidence": [dict(row) for row in evidence],
        "evaluations": [dict(row) for row in evaluations],
    }


@router.post("/improvements/{proposal_key}/canary-decision")
async def decide_canary(proposal_key: str, body: ImprovementDecision, request: Request):
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            proposal = await conn.fetchrow(
                "SELECT * FROM improvement_proposals WHERE proposal_key=$1 FOR UPDATE",
                proposal_key,
            )
            if not proposal or proposal["status"] != "awaiting_review":
                raise HTTPException(409, "Proposal is not awaiting review")
            if proposal["content_hash"] != body.proposal_hash:
                raise HTTPException(409, "Proposal changed; review the new version")
            if body.decision == "approved" and proposal["candidate_state"] != "validated_implementation":
                raise HTTPException(
                    409,
                    "This is a diagnosis-only finding. Attach concrete files and passing validation evidence before canary approval.",
                )
            if body.decision == "changes_requested" and not (body.note or "").strip():
                raise HTTPException(422, "A change-request note is required")
            await conn.execute(
                """INSERT INTO improvement_approvals
                   (proposal_id,stage,proposal_hash,decision,decided_by,decision_note)
                   VALUES($1,'canary',$2,$3,$4,$5)""",
                proposal["id"], body.proposal_hash, body.decision,
                request.state.user_id, body.note,
            )
            next_status = {
                "approved": "approved_for_canary", "rejected": "rejected",
                "changes_requested": "changes_requested",
            }[body.decision]
            await conn.execute(
                "UPDATE improvement_proposals SET status=$1,updated_at=now() WHERE id=$2",
                next_status, proposal["id"],
            )
            if body.decision == "approved":
                await conn.execute(
                    """INSERT INTO improvement_canaries
                       (proposal_id,cohort,status,control_version,candidate_version)
                       VALUES($1,$2::jsonb,'pending',$3,$4)""",
                    proposal["id"], json.dumps({"stage": "selected_users", "percent": 5}),
                    proposal["source_version"] or "current",
                    proposal["candidate_version"] or proposal["content_hash"][:12],
                )
    return {"proposal_key": proposal_key, "status": next_status}


@router.post("/improvements/{proposal_key}/activate-canary")
async def activate_canary(proposal_key: str, body: ImprovementDecision,
                          request: Request):
    if body.decision != "approved":
        raise HTTPException(422, "Canary activation requires an approved decision")
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        proposal = await conn.fetchrow(
            "SELECT * FROM improvement_proposals WHERE proposal_key=$1 FOR UPDATE",
            proposal_key,
        )
        if not proposal or proposal["status"] != "approved_for_canary":
            raise HTTPException(409, "Proposal is not approved for canary")
        if proposal["content_hash"] != body.proposal_hash:
            raise HTTPException(409, "Proposal changed; review the new version")
        evidence = proposal["deployment_evidence"] or {}
        if evidence.get("verified") is not True or evidence.get("candidate_version") != proposal["candidate_version"]:
            raise HTTPException(409, "Verified deployment evidence for the frozen candidate is required")
        updated = await conn.fetchval(
            """UPDATE improvement_canaries SET status='active',started_at=now()
               WHERE id=(SELECT id FROM improvement_canaries WHERE proposal_id=$1
                         AND status='pending' ORDER BY id DESC LIMIT 1)
               RETURNING id""",
            proposal["id"],
        )
        if not updated:
            raise HTTPException(409, "Pending canary record not found")
        await conn.execute(
            """UPDATE improvement_proposals SET status='canary_active',
               candidate_state='deployed_canary',updated_at=now() WHERE id=$1""",
            proposal["id"],
        )
    return {"proposal_key": proposal_key, "status": "canary_active"}


@router.post("/improvements/{proposal_key}/promotion-decision")
async def decide_promotion(proposal_key: str, body: ImprovementDecision,
                           request: Request):
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            proposal = await conn.fetchrow(
                "SELECT * FROM improvement_proposals WHERE proposal_key=$1 FOR UPDATE",
                proposal_key,
            )
            if not proposal or proposal["status"] != "awaiting_promotion":
                raise HTTPException(409, "Proposal is not awaiting promotion")
            if proposal["content_hash"] != body.proposal_hash:
                raise HTTPException(409, "Proposal changed; review the new version")
            canary_passed = await conn.fetchval(
                """SELECT EXISTS(SELECT 1 FROM improvement_canaries
                   WHERE proposal_id=$1 AND status='passed')""",
                proposal["id"],
            )
            if body.decision == "approved" and not canary_passed:
                raise HTTPException(409, "A measured passing canary is required before promotion")
            await conn.execute(
                """INSERT INTO improvement_approvals
                   (proposal_id,stage,proposal_hash,decision,decided_by,decision_note)
                   VALUES($1,'promotion',$2,$3,$4,$5)""",
                proposal["id"], body.proposal_hash, body.decision,
                request.state.user_id, body.note,
            )
            next_status = {
                "approved": "approved_for_publication", "rejected": "rejected",
                "changes_requested": "changes_requested",
            }[body.decision]
            await conn.execute(
                "UPDATE improvement_proposals SET status=$1,updated_at=now() WHERE id=$2",
                next_status, proposal["id"],
            )
    return {"proposal_key": proposal_key, "status": next_status}
