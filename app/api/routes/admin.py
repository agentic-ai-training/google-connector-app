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
    CandidateValidationAttestation,
    CandidateDeploymentAttestation,
    CandidateBuildDraft,
    CanaryActivationDecision,
    ImprovementDecision,
    ImprovementDeploymentEvidence,
)
from app.runs.repository import search_runs
from app.config.settings import get_settings
from app.db.google_clients import request_google_credentials
from app.db.oauth_credentials import load_google_credentials
from app.improvements.publisher import (
    dispatch_candidate_deployment, publish_github_draft, send_proposal_email,
    promote_candidate_pr,
)
from app.improvements.candidates import (
    candidate_digest, file_digest, validate_candidate_files,
)
from app.improvements.builder import store_candidate_draft
from app.improvements.failure_intelligence import create_or_update_proposal
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
    confirmation: str | None = None
class FailureIncidentDecisionIn(BaseModel):
    decision: str
    note: str | None = Field(default=None, max_length=4000)


@router.get("/candidate-builds")
async def candidate_builds(status: str | None = None, limit: int = 100):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT b.*,p.proposal_key,p.title FROM candidate_builds b
               JOIN improvement_proposals p ON p.id=b.proposal_id
               WHERE ($1::text IS NULL OR b.status=$1)
               ORDER BY b.created_at DESC LIMIT $2""",
            status, max(1, min(limit, 200)),
        )
    return {"builds": [dict(row) for row in rows]}


@router.post("/candidate-builder/{build_id}/input")
async def candidate_builder_input(build_id: str):
    """Lease one sanitized build to the no-production-secrets GitHub runner."""
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            """SELECT b.*,p.proposal_key,p.risk_level FROM candidate_builds b
               JOIN improvement_proposals p ON p.id=b.proposal_id
               WHERE b.id=$1 AND b.status IN ('queued','investigating') FOR UPDATE""",
            build_id,
        )
        if not row:
            raise HTTPException(409, "Candidate build is unavailable or already finalized")
        await conn.execute(
            "UPDATE candidate_builds SET status='investigating',updated_at=now() WHERE id=$1",
            build_id,
        )
    job = dict(row)
    return {"build": {
        "id": str(job["id"]), "proposal_id": str(job["proposal_id"]),
        "proposal_key": job["proposal_key"], "risk_level": job["risk_level"],
        "mode": job["mode"], "base_commit": job["base_commit"],
        "model_name": job["model_name"], "token_budget": job["token_budget"],
        "sanitized_input": job["sanitized_input"],
    }}


@router.post("/candidate-builder/{build_id}/draft")
async def candidate_builder_draft(build_id: str, body: CandidateBuildDraft):
    try:
        return await store_candidate_draft(
            await get_pool(), build_id, body.model_dump(), body.tokens_used,
            body.roles_completed,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/candidate-builds/{build_id}/attestation")
async def attest_candidate_build(build_id: str, body: CandidateValidationAttestation):
    settings = get_settings()
    if body.repository != settings.github_proposal_repository:
        raise HTTPException(409, "CI repository does not match the configured repository")
    if not body.passed:
        raise HTTPException(422, "A passing trusted CI result is required")
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        build = await conn.fetchrow(
            """SELECT b.*,p.candidate_kind,p.candidate_version,p.exact_diff,
                      p.rollback_plan,p.validation_report
               FROM candidate_builds b JOIN improvement_proposals p ON p.id=b.proposal_id
               WHERE b.id=$1 FOR UPDATE""", build_id,
        )
        if not build or build["status"] != "drafted":
            raise HTTPException(409, "Candidate build is not awaiting trusted CI")
        files = [dict(row) for row in await conn.fetch(
            """SELECT path,change_type,content,result_hash FROM candidate_build_files
               WHERE build_id=$1 ORDER BY path""", build_id,
        )]
        expected = {item["path"]: item["result_hash"] for item in files}
        if body.file_hashes != expected:
            raise HTTPException(409, "CI file hashes do not match the frozen candidate")
        report = {
            "passed": True, "commands": body.commands, "results": body.results,
            "suite_version": body.suite_version, "commit_sha": body.commit_sha,
            "tree_sha": body.tree_sha, "workflow": body.workflow,
            "run_id": body.run_id, "log_digest": body.log_digest,
            "trusted_identity": f"github-actions:{body.repository}:{body.workflow}",
        }
        digest_files = [
            {"path": item["path"], "change_type": item["change_type"],
             "content": item["content"]} for item in files
        ]
        digest = candidate_digest(
            build["base_commit"], digest_files, report,
            candidate_kind=build["candidate_kind"],
            candidate_version=body.commit_sha, exact_diff=build["exact_diff"],
            rollback_plan=build["rollback_plan"],
        )
        await conn.execute(
            """INSERT INTO candidate_validation_runs
               (build_id,suite_version,commit_sha,status,commands,results,log_digest,
                attestation,trusted_identity,completed_at)
               VALUES($1,$2,$3,'passed',$4::jsonb,$5::jsonb,$6,$7::jsonb,$8,now())""",
            build_id, body.suite_version, body.commit_sha, json.dumps(body.commands),
            json.dumps(body.results), body.log_digest, json.dumps(body.model_dump()),
            report["trusted_identity"],
        )
        await conn.execute(
            """UPDATE candidate_builds SET status='validated',candidate_commit=$1,
               candidate_tree=$2,canonical_digest=$3,checkpoint=checkpoint||$4::jsonb,
               updated_at=now(),completed_at=now() WHERE id=$5""",
            body.commit_sha, body.tree_sha, digest,
            json.dumps({"trusted_ci": report}), build_id,
        )
        await conn.execute(
            """UPDATE improvement_proposals SET candidate_state='validated_implementation',
               candidate_version=$1,validation_report=$2::jsonb,content_hash=$3,
               candidate_manifest=candidate_manifest||$4::jsonb,updated_at=now()
               WHERE id=$5""",
            body.commit_sha, json.dumps(report), digest,
            json.dumps({"candidate_commit": body.commit_sha,
                        "candidate_tree": body.tree_sha,"canary_eligible": True}),
            build["proposal_id"],
        )
    return {"build_id": build_id, "status": "validated", "content_hash": digest}


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
    if name == "failure_improvement_automation":
        mode = body.config.get("mode", "manual")
        if mode not in {"manual", "auto_draft"}:
            raise HTTPException(422, "Only manual or auto_draft analysis is supported")
        if body.enabled and mode == "auto_draft" and body.confirmation != "ENABLE FAILURE AUTO DRAFT":
            raise HTTPException(409, "Exact auto-draft confirmation is required")
        body.config["human_approval_required"] = True
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


@router.get("/failure-incidents")
async def failure_incidents(status: str | None = "awaiting_review", limit: int = 100):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT i.*,
                      (SELECT count(*) FROM failure_incidents c
                       WHERE c.cluster_key=i.cluster_key) AS cluster_occurrences
               FROM failure_incidents i
               WHERE ($1::text IS NULL OR i.analysis_status=$1)
               ORDER BY CASE i.risk_level WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                        i.created_at DESC LIMIT $2""",
            status, max(1, min(limit, 200)),
        )
        notifications = await conn.fetch(
            """SELECT n.*,i.cluster_key,i.title FROM failure_incident_notifications n
               JOIN failure_incidents i ON i.id=n.incident_id
               ORDER BY n.created_at DESC LIMIT $1""",
            max(1, min(limit * 4, 400)),
        )
    return {"incidents": [dict(row) for row in rows],
            "notifications": [dict(row) for row in notifications]}


@router.post("/failure-incidents/{incident_id}/decision")
async def decide_failure_incident(
    incident_id: str, body: FailureIncidentDecisionIn, request: Request,
):
    if body.decision not in {"choose_A", "choose_B", "acknowledged", "ignored"}:
        raise HTTPException(422, "Choose option A/B, acknowledge, or ignore")
    pool = await get_pool()
    selected = body.decision[-1] if body.decision.startswith("choose_") else None
    async with pool.acquire() as conn, conn.transaction():
        incident = await conn.fetchrow(
            "SELECT * FROM failure_incidents WHERE id=$1 FOR UPDATE", incident_id,
        )
        if not incident:
            raise HTTPException(404, "Failure incident not found")
        await conn.execute(
            """INSERT INTO failure_incident_reviews
               (incident_id,decision,selected_option,decided_by,decision_note)
               VALUES($1,$2,$3,$4,$5)""",
            incident_id, body.decision, selected, request.state.user_id, body.note,
        )
        if selected is None:
            await conn.execute(
                "UPDATE failure_incidents SET analysis_status=$1,updated_at=now() WHERE id=$2",
                "acknowledged" if body.decision == "acknowledged" else "ignored", incident_id,
            )
    proposal = None
    if selected:
        proposal = await create_or_update_proposal(
            pool, incident_id, selected, request.state.user_id,
        )
    return {"incident_id": incident_id, "decision": body.decision, "proposal": proposal}


@router.get("/improvements")
async def improvements(status: str | None = None, view: str = "active", limit: int = 100):
    if view not in {"active", "history", "all"}:
        raise HTTPException(422, "view must be active, history, or all")
    terminal = ("rejected", "expired", "rolled_back", "approved_for_publication", "published")
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
                 AND ($2='all' OR ($2='active' AND NOT p.status=ANY($3::text[]))
                      OR ($2='history' AND p.status=ANY($3::text[])))
               ORDER BY CASE p.severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 ELSE 2 END,
                        p.created_at DESC LIMIT $4""",
            status, view, terminal, max(1, min(limit, 200)),
        )
    return {"proposals": [dict(row) for row in rows]}


@router.get("/failure-themes")
async def failure_themes(status: str | None = "active", limit: int = 100):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT t.*,
                      coalesce(jsonb_agg(jsonb_build_object(
                        'cluster_key',c.cluster_key,'title',c.title,
                        'occurrence_count',c.occurrence_count,'category',c.category
                      ) ORDER BY c.last_seen DESC) FILTER(WHERE c.cluster_key IS NOT NULL),'[]')
                        AS clusters
               FROM failure_themes t
               LEFT JOIN failure_theme_clusters tc ON tc.theme_id=t.id
               LEFT JOIN failure_clusters c ON c.cluster_key=tc.cluster_key
               WHERE ($1::text IS NULL OR t.status=$1)
               GROUP BY t.id ORDER BY t.last_seen DESC LIMIT $2""",
            status, max(1, min(limit, 200)),
        )
    return {"themes": [dict(row) for row in rows]}


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
    digest = candidate_digest(
        body.base_version, files, body.validation_report,
        candidate_kind=body.candidate_kind,
        candidate_version=body.candidate_version,
        exact_diff=body.exact_diff,
        rollback_plan=body.rollback_plan,
    )
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
        if proposal["candidate_kind"] == "code":
            raise HTTPException(
                409,
                "Code deployment evidence must come from the trusted isolated deployment "
                "controller; an administrator assertion cannot activate a code canary",
            )
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


@router.post("/improvements/{proposal_key}/deployment-attestation")
async def attest_candidate_deployment(
    proposal_key: str, body: CandidateDeploymentAttestation,
):
    settings = get_settings()
    if not settings.railway_project_id:
        raise HTTPException(503, "RAILWAY_PROJECT_ID is not configured")
    if body.project_id != settings.railway_project_id:
        raise HTTPException(409, "Deployment project does not match the governed project")
    if body.service_name != settings.railway_candidate_worker_service:
        raise HTTPException(409, "Deployment must target the isolated candidate worker")
    if not body.verified or body.smoke_tests.get("passed") is not True:
        raise HTTPException(422, "Verified isolated deployment and smoke tests are required")
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        proposal = await conn.fetchrow(
            "SELECT * FROM improvement_proposals WHERE proposal_key=$1 FOR UPDATE",
            proposal_key,
        )
        if not proposal or proposal["status"] != "approved_for_canary":
            raise HTTPException(409, "Candidate must have human canary approval")
        if proposal["candidate_version"] != body.candidate_version:
            raise HTTPException(409, "Deployment commit does not match the frozen candidate")
        candidate_paths = await conn.fetch(
            "SELECT path FROM improvement_candidate_files WHERE proposal_id=$1",
            proposal["id"],
        )
        api_planning_paths = {
            "app/runs/planner.py", "app/runs/repository.py", "app/api/routes/runs.py",
        }
        incompatible = sorted(
            row["path"] for row in candidate_paths if row["path"] in api_planning_paths
        )
        if incompatible:
            raise HTTPException(
                409,
                "This candidate changes pre-worker planning and cannot be isolated by the "
                f"worker-only canary target: {', '.join(incompatible)}",
            )
        evidence = {
            **body.model_dump(),
            "trusted_identity": f"github-actions:{settings.github_proposal_repository}:{body.workflow}",
        }
        await conn.execute(
            """UPDATE improvement_proposals SET deployment_evidence=$1::jsonb,
               candidate_manifest=candidate_manifest||$2::jsonb,updated_at=now()
               WHERE id=$3""",
            json.dumps(evidence), json.dumps({"candidate_deployment": evidence}),
            proposal["id"],
        )
        await conn.execute(
            """UPDATE improvement_canaries SET candidate_deployment_id=$1
               WHERE id=(SELECT id FROM improvement_canaries WHERE proposal_id=$2
                         AND status='pending' ORDER BY id DESC LIMIT 1)""",
            body.deployment_id, proposal["id"],
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
        "awaiting_review",
    )
    if notification["status"] == "sent":
        return {"status": "sent", "url": notification["external_reference"]}
    try:
        async with pool.acquire() as conn:
            candidate_files = [dict(row) for row in await conn.fetch(
                "SELECT path,change_type,content,content_hash FROM improvement_candidate_files WHERE proposal_id=$1 ORDER BY path",
                proposal["id"],
            )]
        result = await publish_github_draft(proposal, candidate_files)
        await _finish_notification(pool, notification["id"], reference=result["url"])
        async with pool.acquire() as conn:
            await conn.execute(
                """UPDATE improvement_proposals SET candidate_manifest=
                   candidate_manifest||$1::jsonb,updated_at=now() WHERE id=$2""",
                json.dumps({"draft_pr": result}), proposal["id"],
            )
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
            validation = proposal["validation_report"] or {}
            if body.decision == "approved" and not validation.get("trusted_identity"):
                raise HTTPException(
                    409, "A trusted CI attestation bound to the candidate commit is required",
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
    deployment_dispatch = None
    if body.decision == "approved":
        try:
            if get_settings().allow_dev_auth:
                deployment_dispatch = {"status": "skipped_in_development"}
            else:
                deployment_dispatch = await dispatch_candidate_deployment(dict(proposal))
        except Exception as exc:
            deployment_dispatch = {"status": "not_dispatched", "reason": str(exc)}
            async with pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO improvement_notifications
                       (proposal_id,channel,event_type,status,sanitized_payload,error_message)
                       VALUES($1,'github','candidate_deployment_dispatch','failed',$2::jsonb,$3)
                       ON CONFLICT(proposal_id,channel,event_type) DO UPDATE SET
                         status='failed',error_message=excluded.error_message,created_at=now()""",
                    proposal["id"], json.dumps({
                        "proposal_key": proposal_key, "contains_private_evidence": False,
                    }), str(exc),
                )
    return {"proposal_key": proposal_key, "status": next_status,
            "deployment_dispatch": deployment_dispatch}


@router.post("/improvements/{proposal_key}/activate-canary")
async def activate_canary(proposal_key: str, body: CanaryActivationDecision,
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
        if proposal["candidate_kind"] == "code" and not evidence.get("trusted_identity"):
            raise HTTPException(
                409, "Code canaries require deployment evidence from the trusted controller",
            )
        updated = await conn.fetchval(
            """UPDATE improvement_canaries SET status='active',started_at=now(),
               routing_enabled=TRUE,traffic_percent=$2,allowed_users=$3,
               denied_users=$4,activated_by=$5
               WHERE id=(SELECT id FROM improvement_canaries WHERE proposal_id=$1
                         AND status='pending' ORDER BY id DESC LIMIT 1)
               RETURNING id""",
            proposal["id"], body.traffic_percent,
            [item.strip().lower() for item in body.allowed_users if item.strip()],
            [item.strip().lower() for item in body.denied_users if item.strip()],
            request.state.user_id,
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
    publication = None
    if body.decision == "approved":
        try:
            publication = await promote_candidate_pr(dict(proposal))
            async with pool.acquire() as conn:
                await conn.execute(
                    """UPDATE improvement_proposals SET status='published',
                       candidate_manifest=candidate_manifest||$1::jsonb,updated_at=now()
                       WHERE id=$2""",
                    json.dumps({"production_merge": publication}), proposal["id"],
                )
            next_status = "published"
        except Exception as exc:
            publication = {"status": "not_published", "reason": str(exc)}
    return {"proposal_key": proposal_key, "status": next_status,
            "publication": publication}
