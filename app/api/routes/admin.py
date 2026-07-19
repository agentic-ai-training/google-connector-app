import json

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from app.db.connection import get_pool
from app.db.prompt_service import create_experiment, conclude_experiment
from app.runs.schemas import ImprovementDecision
router=APIRouter(prefix="/admin")
class ExperimentIn(BaseModel):
    name:str; prompt_name:str; control_id:str; variant_id:str; traffic_split:float=.5; notes:str|None=None
class ConcludeIn(BaseModel): winner:str
class PromptIn(BaseModel):
    name:str; content:str; model_target:str="groq/llama-3.3-70b"; temperature:float=.3; max_tokens:int=1000; notes:str|None=None
class FeatureFlagIn(BaseModel):
    enabled: bool
    config: dict = Field(default_factory=dict)
@router.get("/experiments/{name}/summary")
async def summary(name:str):
    pool=await get_pool()
    async with pool.acquire() as conn: rows=await conn.fetch("SELECT * FROM experiment_summary WHERE experiment_name=$1",name)
    return {"summary":[dict(r) for r in rows]}
@router.post("/experiments")
async def create(body:ExperimentIn): return await create_experiment(**body.model_dump())
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
            "UPDATE improvement_proposals SET status='canary_active',updated_at=now() WHERE id=$1",
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
