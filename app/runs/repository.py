import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

from app.config.settings import get_settings
from app.runs.planner import action_hash, build_plan, validate_plan
from app.mlops.metrics import approval_requests, run_transitions
from app.improvements.failure_intelligence import record_failure_incident


class RunLimitExceeded(RuntimeError):
    pass


def _json(value):
    return json.dumps(value, default=str)


async def append_event(pool, run_id, user_id, event_type, *, step_id=None,
                       phase=None, message=None, payload=None):
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """INSERT INTO agent_run_events
               (run_id,step_id,user_id,event_type,phase,message,payload)
               VALUES($1,$2,$3,$4,$5,$6,$7::jsonb) RETURNING id""",
            run_id, step_id, user_id, event_type, phase, message,
            _json(payload or {}),
        )


async def create_run(pool, user_id, message, session_id, idempotency_key=None):
    settings = get_settings()
    if len(message) > settings.max_request_chars:
        raise RunLimitExceeded(
            f"Request exceeds the {settings.max_request_chars}-character safety limit"
        )
    plan, policy = build_plan(message)
    plan_errors = validate_plan(plan)
    if plan_errors:
        error = "Invalid execution plan: " + "; ".join(plan_errors)
        key = idempotency_key or str(uuid.uuid4())
        retention = datetime.now(timezone.utc) + timedelta(
            days=settings.workflow_retention_days
        )
        async with pool.acquire() as conn, conn.transaction():
            existing = await conn.fetchrow(
                "SELECT * FROM agent_runs WHERE user_id=$1 AND idempotency_key=$2",
                user_id, key,
            )
            if existing:
                return dict(existing), False
            run = await conn.fetchrow(
                """INSERT INTO agent_runs
                   (session_id,user_id,request,objective,status,current_phase,plan,
                    risk_level,requires_approval,approval_bypassed,idempotency_key,
                    chunker_version,okf_version,deployment_version,retention_until,
                    clarification_questions,intent_kind,intent_evidence,
                    planning_diagnostics,error_category,error_message,failed_at,
                    technical_completion,functional_completion,user_visible_completion,
                    side_effect_integrity)
                   VALUES($1,$2,$3,$3,'failed','validation',$4::jsonb,$5,FALSE,$6,$7,
                          $8,$9,$10,$11,$12::jsonb,$13,$14::jsonb,$15::jsonb,
                          'planning',$16,now(),0,0,0,100) RETURNING *""",
                session_id, user_id, message, _json(plan.model_dump()),
                policy["risk_level"], policy["approval_bypassed"], key,
                "source-aware-v1", "v0.1", settings.deployment_version, retention,
                _json(policy["required_clarifications"]), policy["intent_kind"],
                _json(policy["intent_evidence"]), _json({"validation_errors": plan_errors}),
                error,
            )
            await conn.execute(
                """INSERT INTO agent_run_events
                   (run_id,user_id,event_type,phase,message,payload)
                   VALUES($1,$2,'planning_failed','validation',$3,$4::jsonb)""",
                run["id"], user_id, error, _json({"validation_errors": plan_errors}),
            )
        incident = await record_failure_incident(
            pool, occurrence_key=f"run:{run['id']}:planning", run_id=run["id"],
            session_id=session_id, user_id=user_id, message=message,
            intent_kind=policy["intent_kind"], stage="validation", category="planning",
            component="typed_planner", error=error, breaking_point="Plan validation",
            completion={"technical": 0, "functional": 0, "user_visible": 0,
                        "side_effect_integrity": 100},
            evidence={"validation_errors": plan_errors}, policy=policy,
        )
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE agent_runs SET failure_fingerprint=$1 WHERE id=$2",
                incident["failure_fingerprint"], run["id"],
            )
        result = dict(run)
        result["failure_incident_id"] = incident["id"]
        return result, True
    key = idempotency_key or str(uuid.uuid4())
    status = (
        "awaiting_clarification" if policy["required_clarifications"]
        else ("awaiting_approval" if policy["requires_approval"] else "queued")
    )
    retention = datetime.now(timezone.utc) + timedelta(
        days=settings.workflow_retention_days
    )
    async with pool.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchrow(
                "SELECT * FROM agent_runs WHERE user_id=$1 AND idempotency_key=$2",
                user_id, key,
            )
            if existing:
                return dict(existing), False
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", user_id)
            active = await conn.fetchval(
                """SELECT count(*) FROM agent_runs WHERE user_id=$1 AND deleted_at IS NULL
                   AND status IN ('queued','running','awaiting_approval','awaiting_clarification')""",
                user_id,
            )
            if active >= settings.max_active_runs_per_user:
                raise RunLimitExceeded("Too many active runs; finish or cancel one first")
            recent = await conn.fetchval(
                "SELECT count(*) FROM agent_runs WHERE user_id=$1 AND queued_at>=now()-interval '1 hour'",
                user_id,
            )
            if recent >= settings.max_runs_per_user_hour:
                raise RunLimitExceeded("Hourly run limit reached; retry later")
            global_active = await conn.fetchval(
                """SELECT count(*) FROM agent_runs WHERE deleted_at IS NULL
                   AND status IN ('queued','running')"""
            )
            if global_active >= settings.max_active_runs_global:
                raise RunLimitExceeded("The service is at its active-run capacity; retry later")
            used_tokens = await conn.fetchval(
                """SELECT coalesce(sum(coalesce(input_tokens,0)+coalesce(output_tokens,0)),0)
                   FROM agent_model_calls WHERE created_at>=date_trunc('day',now())"""
            )
            estimated = plan.estimated_max_tokens
            remaining_after = settings.groq_daily_token_budget - used_tokens - estimated
            if policy["write"] and remaining_after < settings.groq_quality_reserve_tokens:
                raise RunLimitExceeded(
                    "Quality-model token reserve is too low for a mutating workflow; "
                    "retry after quota resets or increase the configured budget"
                )
            run = await conn.fetchrow(
                """INSERT INTO agent_runs
                   (session_id,user_id,request,objective,status,current_phase,plan,
                    risk_level,requires_approval,approval_bypassed,idempotency_key,
                    chunker_version,okf_version,deployment_version,retention_until,
                    clarification_questions,intent_kind,intent_evidence)
                   VALUES($1,$2,$3,$3,$4,'planned',$5::jsonb,$6,$7,$8,$9,$10,$11,$12,$13,$14::jsonb,$15,$16::jsonb)
                   RETURNING *""",
                session_id, user_id, message, status, _json(plan.model_dump()),
                policy["risk_level"], policy["requires_approval"],
                policy["approval_bypassed"], key, "source-aware-v1", "v0.1",
                settings.deployment_version, retention,
                _json(policy["required_clarifications"]), policy["intent_kind"],
                _json(policy["intent_evidence"]),
            )
            run_id = run["id"]
            for sequence_no, step in enumerate(plan.steps, 1):
                step_status = "awaiting_approval" if step.requires_approval else "pending"
                await conn.execute(
                    """INSERT INTO agent_run_steps
                       (run_id,step_key,sequence_no,title,service,operation,dependencies,
                        read_only,risk_level,requires_approval,weight,status,preconditions,
                        postconditions,input_data,retry_policy,max_attempts)
                       VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::jsonb,$14::jsonb,$15::jsonb,$16::jsonb,$17)""",
                    run_id, step.id, sequence_no, step.title, step.service,
                    step.operation, step.dependencies, step.read_only, step.risk_level,
                    step.requires_approval, step.weight, step_status,
                    _json(step.preconditions), _json(step.postconditions),
                    _json(step.arguments),
                    _json({"retry": ["network", "rate_limit", "worker"]
                           if step.read_only else [], "backoff_seconds": [2, 10]}),
                    3 if step.read_only else 1,
                )
            await conn.execute(
                """INSERT INTO agent_run_events
                   (run_id,user_id,event_type,phase,message,payload)
                   VALUES($1,$2,'run_created','planned','Durable run created',$3::jsonb),
                         ($1,$2,'plan_produced','planned','Execution plan produced',$4::jsonb)""",
                run_id, user_id, _json({"status": status}), _json(plan.model_dump()),
            )
            if policy["required_clarifications"]:
                await conn.execute(
                    """INSERT INTO agent_run_events
                       (run_id,user_id,event_type,phase,message,payload)
                       VALUES($1,$2,'clarification_required','clarification',
                              'Material information is required before execution',$3::jsonb)""",
                    run_id, user_id,
                    _json({"questions": policy["required_clarifications"]}),
                )
            elif policy["requires_approval"]:
                approval_requests.labels(policy["risk_level"]).inc()
                digest = action_hash(plan)
                await conn.execute(
                    """INSERT INTO run_approvals
                       (run_id,requested_from,action_hash,action_summary,expires_at)
                       VALUES($1,$2,$3,$4::jsonb,now()+interval '30 minutes')""",
                    run_id, user_id, digest,
                    _json({"objective": message, "risk": policy["risk_level"],
                           "services": policy["services"]}),
                )
                await conn.execute(
                    """INSERT INTO agent_run_events
                       (run_id,user_id,event_type,phase,message,payload)
                       VALUES($1,$2,'approval_required','approval',
                              'Confirmation is required before the high-risk external write',
                              $3::jsonb)""",
                    run_id, user_id, _json({"action_hash": digest}),
                )
            run_transitions.labels(status).inc()
            return dict(run), True


async def clarify_run(pool, run_id, user_id, answers):
    async with pool.acquire() as conn, conn.transaction():
        run = await conn.fetchrow(
            """SELECT * FROM agent_runs WHERE id=$1 AND user_id=$2
               AND status='awaiting_clarification' FOR UPDATE""",
            run_id, user_id,
        )
        if not run:
            return None
        augmented = run["request"] + "\n\nUser clarifications:\n" + "\n".join(
            f"{key}: {value}" for key, value in sorted(answers.items())
        )
        plan, policy = build_plan(augmented)
        if policy["required_clarifications"]:
            await conn.execute(
                """UPDATE agent_runs SET request=$1,plan=$2::jsonb,
                   clarification_questions=$3::jsonb,clarification_answers=$4::jsonb
                   WHERE id=$5""",
                augmented, _json(plan.model_dump()),
                _json(policy["required_clarifications"]), _json(answers), run_id,
            )
            return "awaiting_clarification"
        await conn.execute("DELETE FROM agent_run_steps WHERE run_id=$1", run_id)
        for sequence_no, step in enumerate(plan.steps, 1):
            await conn.execute(
                """INSERT INTO agent_run_steps
                   (run_id,step_key,sequence_no,title,service,operation,dependencies,
                    read_only,risk_level,requires_approval,weight,status,preconditions,
                    postconditions,input_data,retry_policy,max_attempts)
                   VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,'pending',$12::jsonb,$13::jsonb,$14::jsonb,$15::jsonb,$16)""",
                run_id, step.id, sequence_no, step.title, step.service, step.operation,
                step.dependencies, step.read_only, step.risk_level,
                step.requires_approval, step.weight, _json(step.preconditions),
                _json(step.postconditions), _json(step.arguments),
                _json({"retry": ["network", "rate_limit", "worker"]
                       if step.read_only else [], "backoff_seconds": [2, 10]}),
                3 if step.read_only else 1,
            )
        status = "awaiting_approval" if policy["requires_approval"] else "queued"
        await conn.execute(
            """UPDATE agent_runs SET request=$1,objective=$1,status=$2,current_phase='planned',
               plan=$3::jsonb,risk_level=$4,requires_approval=$5,
               clarification_questions='[]'::jsonb,clarification_answers=$6::jsonb
               WHERE id=$7""",
            augmented, status, _json(plan.model_dump()), policy["risk_level"],
            policy["requires_approval"], _json(answers), run_id,
        )
        await conn.execute(
            """INSERT INTO agent_run_events
               (run_id,user_id,event_type,phase,message,payload)
               VALUES($1,$2,'clarification_received','planning',
                      'Clarifications were applied and the plan was rebuilt',$3::jsonb)""",
            run_id, user_id, _json({"answer_keys": sorted(answers)}),
        )
        if policy["requires_approval"]:
            digest = action_hash(plan)
            await conn.execute(
                """INSERT INTO run_approvals
                   (run_id,requested_from,action_hash,action_summary,expires_at)
                   VALUES($1,$2,$3,$4::jsonb,now()+interval '30 minutes')""",
                run_id, user_id, digest,
                _json({"objective": augmented, "risk": policy["risk_level"],
                       "services": policy["services"]}),
            )
        return status


async def get_run(pool, run_id, user_id):
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM agent_runs WHERE id=$1 AND user_id=$2 AND deleted_at IS NULL",
            run_id, user_id,
        )
        if not row:
            return None
        result = dict(row)
        result["steps"] = [dict(item) for item in await conn.fetch(
            "SELECT * FROM agent_run_steps WHERE run_id=$1 ORDER BY sequence_no", run_id
        )]
        result["artifacts"] = [dict(item) for item in await conn.fetch(
            "SELECT * FROM agent_artifacts WHERE run_id=$1 ORDER BY created_at", run_id
        )]
        result["recent_events"] = [dict(item) for item in await conn.fetch(
            """SELECT id,event_type,phase,message,payload,created_at
               FROM agent_run_events WHERE run_id=$1 ORDER BY id DESC LIMIT 25""",
            run_id,
        )][::-1]
        approval = await conn.fetchrow(
            """SELECT action_hash,action_summary,expires_at,status
               FROM run_approvals WHERE run_id=$1 ORDER BY created_at DESC LIMIT 1""",
            run_id,
        )
        result["approval"] = dict(approval) if approval else None
        return result


async def list_events(pool, run_id, user_id, after_id=0):
    async with pool.acquire() as conn:
        allowed = await conn.fetchval(
            "SELECT 1 FROM agent_runs WHERE id=$1 AND user_id=$2", run_id, user_id
        )
        if not allowed:
            return None
        return [dict(row) for row in await conn.fetch(
            """SELECT * FROM agent_run_events
               WHERE run_id=$1 AND id>$2 ORDER BY id LIMIT 1000""",
            run_id, after_id,
        )]


async def search_runs(
    pool, *, user_id=None, session_id=None, status=None, service=None, model=None,
    failure=None, deployment_version=None, started_after=None, started_before=None,
    limit=100, offset=0,
):
    """Search high-cardinality run facts without placing them in metric labels."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT r.id,r.session_id,r.user_id,r.request,r.status,r.current_phase,
                      r.technical_completion,r.functional_completion,
                      r.user_visible_completion,r.side_effect_integrity,r.risk_level,
                      r.error_category,r.error_message,r.models_used,r.input_tokens,
                      r.output_tokens,r.deployment_version,r.queued_at,r.started_at,
                      r.completed_at,r.heartbeat_at,
                      coalesce((SELECT array_agg(DISTINCT s.service)
                                FROM agent_run_steps s WHERE s.run_id=r.id
                                  AND s.service IS NOT NULL),'{}') AS services
               FROM agent_runs r
               WHERE r.deleted_at IS NULL
                 AND ($1::text IS NULL OR r.user_id=$1)
                 AND ($2::text IS NULL OR r.session_id=$2)
                 AND ($3::text IS NULL OR r.status=$3)
                 AND ($4::text IS NULL OR EXISTS(
                   SELECT 1 FROM agent_run_steps s WHERE s.run_id=r.id AND s.service=$4))
                 AND ($5::text IS NULL OR $5=ANY(r.models_used))
                 AND ($6::text IS NULL OR r.error_category=$6)
                 AND ($7::text IS NULL OR r.deployment_version=$7)
                 AND ($8::timestamptz IS NULL OR r.queued_at >= $8)
                 AND ($9::timestamptz IS NULL OR r.queued_at <= $9)
               ORDER BY r.queued_at DESC LIMIT $10 OFFSET $11""",
            user_id, session_id, status, service, model, failure,
            deployment_version, started_after, started_before,
            max(1, min(int(limit), 200)), max(0, int(offset)),
        )
    return [dict(row) for row in rows]


async def decide_run(pool, run_id, user_id, approved, digest, note=None):
    decision = "approved" if approved else "rejected"
    async with pool.acquire() as conn:
        async with conn.transaction():
            approval = await conn.fetchrow(
                """SELECT * FROM run_approvals
                   WHERE run_id=$1 AND requested_from=$2 AND status='pending'
                   FOR UPDATE""",
                run_id, user_id,
            )
            if not approval or approval["action_hash"] != digest:
                return None
            if approval["expires_at"] <= datetime.now(timezone.utc):
                await conn.execute(
                    "UPDATE run_approvals SET status='expired' WHERE id=$1", approval["id"]
                )
                return None
            await conn.execute(
                """UPDATE run_approvals SET status=$1,decided_by=$2,decision_note=$3,
                   decided_at=now() WHERE id=$4""",
                decision, user_id, note, approval["id"],
            )
            next_status = "queued" if approved else "cancelled"
            await conn.execute(
                """UPDATE agent_runs SET status=$1,current_phase=$2,
                   cancellation_source=CASE WHEN $1='cancelled' THEN 'approval_rejected' END
                   WHERE id=$3 AND user_id=$4""",
                next_status, "queued" if approved else "cancelled", run_id, user_id,
            )
            await conn.execute(
                """UPDATE agent_run_steps SET status=$1
                   WHERE run_id=$2 AND status='awaiting_approval'""",
                "pending" if approved else "cancelled", run_id,
            )
            await conn.execute(
                """INSERT INTO agent_run_events
                   (run_id,user_id,event_type,phase,message,payload)
                   VALUES($1,$2,$3,'approval',$4,$5::jsonb)""",
                run_id, user_id, f"approval_{decision}",
                f"High-risk action {decision}", _json({"note": note}),
            )
            return next_status


async def cancel_run(pool, run_id, user_id):
    async with pool.acquire() as conn:
        result = await conn.execute(
            """UPDATE agent_runs SET status='cancelled',current_phase='cancelled',
               cancellation_source='user',completed_at=now()
               WHERE id=$1 AND user_id=$2 AND status IN ('queued','awaiting_approval','running')""",
            run_id, user_id,
        )
    if result.endswith("1"):
        await append_event(pool, run_id, user_id, "run_cancelled", phase="cancelled",
                           message="Run cancelled by user")
        return True
    return False


def proposal_key(proposal_type, title, exact_diff):
    return hashlib.sha256(
        f"{proposal_type}\0{title}\0{exact_diff or ''}".encode()
    ).hexdigest()[:20]
