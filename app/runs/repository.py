import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone

from app.config.settings import get_settings
from app.runs.context import analyze_conversation_context
from app.runs.approval_preview import build_approval_summary
from app.runs.planner import (
    CALENDAR_DURATION_QUESTION,
    CALENDAR_END_DATE_QUESTION,
    CALENDAR_RECURRENCE_QUESTION,
    CALENDAR_START_DATE_QUESTION,
    CALENDAR_START_TIME_QUESTION,
    CALENDAR_TIMEZONE_QUESTION,
    action_hash,
    build_plan,
    parse_calendar_date,
    validate_plan,
)
from app.runs.request_analysis import EMAIL_PATTERN, RequestStatementAnalysis
from app.runs.request_analysis import analyze_request_statement
from app.tools.calendar_normalization import normalize_timezone
from app.mlops.metrics import approval_requests, run_transitions
from app.improvements.failure_intelligence import record_failure_incident
from app.improvements.routing import resolve_executor_assignment


class RunLimitExceeded(RuntimeError):
    pass


class CandidateAssignmentMismatch(RuntimeError):
    pass


class InvalidClarificationAnswers(ValueError):
    """Clarification payload did not match this run's outstanding questions."""

    def __init__(self, message: str, *, reason_code: str = "invalid_clarification",
                 suggested_value: str | None = None):
        super().__init__(message)
        self.reason_code = reason_code
        self.suggested_value = suggested_value


def _edit_distance(left: str, right: str) -> int:
    previous = list(range(len(right) + 1))
    for left_index, left_character in enumerate(left, 1):
        current = [left_index]
        for right_index, right_character in enumerate(right, 1):
            current.append(min(
                current[-1] + 1,
                previous[right_index] + 1,
                previous[right_index - 1]
                + (left_character != right_character),
            ))
        previous = current
    return previous[-1]


def _normalize_clarification_answers(run, answers: dict) -> dict:
    normalized = dict(answers)
    if any(not str(value or "").strip() for value in answers.values()):
        raise InvalidClarificationAnswers(
            "Every submitted clarification must contain a value",
            reason_code="blank_clarification",
        )
    original_emails = [
        value.casefold() for value in EMAIL_PATTERN.findall(run["request"])
    ]
    for question, raw_answer in answers.items():
        answer = str(raw_answer or "").strip()
        lowered_question = question.casefold()
        if "timezone" in lowered_question:
            try:
                normalized[question] = normalize_timezone(answer)
            except ValueError as exc:
                raise InvalidClarificationAnswers(
                    str(exc), reason_code="invalid_timezone",
                ) from exc
        if (
            "chat" not in lowered_question
            or ("email" not in lowered_question and "destination" not in lowered_question)
        ):
            continue
        answer_emails = EMAIL_PATTERN.findall(answer)
        if "@" in answer and len(answer_emails) != 1:
            raise InvalidClarificationAnswers(
                "Enter one valid direct-message email or an accessible spaces/... "
                "Google Chat resource",
                reason_code="invalid_chat_destination",
            )
        if len(answer_emails) != 1:
            continue
        supplied = answer_emails[0].casefold()
        close = min(
            original_emails,
            key=lambda candidate: _edit_distance(supplied, candidate),
            default=None,
        )
        if close and supplied != close and _edit_distance(supplied, close) <= 2:
            raise InvalidClarificationAnswers(
                f"The Chat recipient looks mistyped. Did you mean {close}?",
                reason_code="recipient_typo_suspected",
                suggested_value=close,
            )
        normalized[question] = supplied
    combined = {**(run["clarification_answers"] or {}), **normalized}
    timezone_name = str(combined.get(CALENDAR_TIMEZONE_QUESTION) or "").strip()
    for question in (CALENDAR_START_DATE_QUESTION, CALENDAR_END_DATE_QUESTION):
        if question not in answers or not timezone_name:
            continue
        if parse_calendar_date(str(normalized[question]), timezone_name) is None:
            raise InvalidClarificationAnswers(
                "Enter a complete Calendar date such as 2036-08-01, "
                "1 August 2036, or '10 years later'",
                reason_code="invalid_calendar_date",
                suggested_value="2036-08-01",
            )
    if CALENDAR_START_TIME_QUESTION in answers and not re.fullmatch(
        r"\s*\d{1,2}(?::\d{2})?\s*(?:am|pm)\s*",
        str(answers[CALENDAR_START_TIME_QUESTION]), re.IGNORECASE,
    ):
        raise InvalidClarificationAnswers(
            "Enter a start time such as 10:00 AM",
            reason_code="invalid_calendar_time",
        )
    if CALENDAR_DURATION_QUESTION in answers and not re.fullmatch(
        r"\s*[1-9]\d*\s*(?:minutes?|hours?)\s*",
        str(answers[CALENDAR_DURATION_QUESTION]), re.IGNORECASE,
    ):
        raise InvalidClarificationAnswers(
            "Enter an event duration such as 10 minutes or 1 hour",
            reason_code="invalid_calendar_duration",
        )
    if CALENDAR_RECURRENCE_QUESTION in answers and str(
        answers[CALENDAR_RECURRENCE_QUESTION]
    ).strip().casefold() not in {"daily", "weekdays", "weekly", "monthly", "yearly"}:
        raise InvalidClarificationAnswers(
            "Choose daily, weekdays, weekly, monthly, or yearly",
            reason_code="invalid_calendar_recurrence",
        )
    return normalized


def _json(value):
    return json.dumps(value, default=str)


def _referenced_plan_content(plan: dict | None) -> tuple[str | None, str | None, str | None]:
    """Recover already-resolved private context while a run is clarified."""
    for step in (plan or {}).get("steps") or []:
        arguments = (step.get("arguments") or {}).get("tool_arguments") or {}
        if arguments.get("text"):
            return str(arguments["text"]), None, "chat"
        if arguments.get("body"):
            return (
                str(arguments["body"]),
                str(arguments.get("subject") or "") or None,
                "gmail",
            )
        if arguments.get("reuse_text"):
            return str(arguments["reuse_text"]), None, "composition"
    return None, None, None


def _clarification_fields(questions: list[str], answers: dict) -> list[dict]:
    """Return typed UI contracts while retaining question keys for compatibility."""
    fields = []
    for question in questions:
        field = {
            "key": question, "label": question, "type": "text",
            "required": True, "value": str(answers.get(question) or ""),
            "options": [], "placeholder": "Enter the requested information",
        }
        if question in {CALENDAR_START_DATE_QUESTION, CALENDAR_END_DATE_QUESTION}:
            field.update({"type": "date", "placeholder": "YYYY-MM-DD"})
        elif question == CALENDAR_START_TIME_QUESTION:
            field["placeholder"] = "10:00 AM"
        elif question == CALENDAR_DURATION_QUESTION:
            field.update({
                "type": "select",
                "options": ["5 minutes", "10 minutes", "15 minutes", "30 minutes", "1 hour"],
                "placeholder": "Choose a duration",
            })
        elif question == CALENDAR_RECURRENCE_QUESTION:
            field.update({
                "type": "select",
                "options": ["daily", "weekdays", "weekly", "monthly", "yearly"],
                "placeholder": "Choose a recurrence",
            })
        elif question == CALENDAR_TIMEZONE_QUESTION:
            field["type"] = "timezone"
        elif "Chat space" in question or "direct-message email" in question:
            field.update({"type": "email_or_space", "placeholder": "person@example.com or spaces/..."})
        fields.append(field)
    return fields


async def resolve_contextual_request(
    pool, user_id: str, session_id: str, message: str,
    timezone_name: str | None = None,
) -> str:
    """Compatibility wrapper for callers that only need the effective text."""
    del timezone_name
    analysis = await analyze_conversation_context(
        pool, user_id=user_id, session_id=session_id, message=message,
    )
    return analysis.effective_message


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


async def create_run(pool, user_id, message, session_id, idempotency_key=None,
                     required_executor_version: str | None = None,
                     timezone_name: str | None = None,
                     planning_message: str | None = None,
                     context_diagnostics: dict | None = None,
                     request_analysis: RequestStatementAnalysis | None = None,
                     referenced_output: str | None = None,
                     referenced_subject: str | None = None,
                     referenced_service: str | None = None):
    settings = get_settings()
    if len(message) > settings.max_request_chars:
        raise RunLimitExceeded(
            f"Request exceeds the {settings.max_request_chars}-character safety limit"
        )
    planning_message = planning_message or message
    context_diagnostics = context_diagnostics or {
        "version": "conversation-context-v1", "mode": "standalone",
    }
    plan, policy = build_plan(
        planning_message, timezone_name, authority_message=message,
        request_analysis=request_analysis,
        referenced_output=referenced_output,
        referenced_subject=referenced_subject,
        referenced_service=referenced_service,
    )
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
                    side_effect_integrity,executor_version,cohort_assignment,
                    assignment_reason,assigned_at)
                   VALUES($1,$2,$3,$3,'failed','validation',$4::jsonb,$5,FALSE,$6,$7,
                          $8,$9,$10,$11,$12::jsonb,$13,$14::jsonb,$15::jsonb,
                          'planning',$16,now(),0,0,0,100,$17,'control',
                          'planning failed before canary assignment',now()) RETURNING *""",
                session_id, user_id, message, _json(plan.model_dump()),
                policy["risk_level"], policy["approval_bypassed"], key,
                "source-aware-v1", "v0.1", settings.deployment_version, retention,
                _json(policy["required_clarifications"]), policy["intent_kind"],
                _json(policy["intent_evidence"]), _json({
                    "validation_errors": plan_errors,
                    "request_analysis": (
                        request_analysis.diagnostics()
                        if request_analysis else None
                    ),
                    "conversation_context": context_diagnostics,
                }),
                error, settings.deployment_version,
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
            assignment = await resolve_executor_assignment(
                conn, user_id, settings.deployment_version, plan.model_dump(),
            )
            if required_executor_version and (
                assignment.cohort != "candidate"
                or assignment.executor_version != required_executor_version
            ):
                raise CandidateAssignmentMismatch(
                    "The candidate assertion no longer matches the durable routing decision"
                )
            run = await conn.fetchrow(
                """INSERT INTO agent_runs
                   (session_id,user_id,request,objective,status,current_phase,plan,
                    risk_level,requires_approval,approval_bypassed,idempotency_key,
                    chunker_version,okf_version,deployment_version,retention_until,
                    clarification_questions,intent_kind,intent_evidence,
                    planning_diagnostics,executor_version,
                    canary_id,cohort_assignment,assignment_reason,assigned_at,okf_bundle_version)
                   VALUES($1,$2,$3,$3,$4,'planned',$5::jsonb,$6,$7,$8,$9,$10,$11,$12,$13,$14::jsonb,$15,$16::jsonb,
                          $17::jsonb,$18,$19,$20,$21,now(),$22)
                   RETURNING *""",
                session_id, user_id, message, status, _json(plan.model_dump()),
                policy["risk_level"], policy["requires_approval"],
                policy["approval_bypassed"], key, "source-aware-v1", "v0.1",
                settings.deployment_version, retention,
                _json(policy["required_clarifications"]), policy["intent_kind"],
                _json(policy["intent_evidence"]),
                _json({
                    "request_analysis": (
                        request_analysis.diagnostics()
                        if request_analysis else None
                    ),
                    "conversation_context": context_diagnostics,
                }),
                assignment.executor_version,
                assignment.canary_id, assignment.cohort, assignment.reason,
                assignment.okf_bundle_version,
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
                         ($1,$2,'request_analyzed','planning',
                          'Current request statement analyzed',$4::jsonb),
                         ($1,$2,'context_analyzed','planning',
                          'Conversation context relevance evaluated',$5::jsonb),
                         ($1,$2,'plan_produced','planned','Execution plan produced',$6::jsonb)""",
                run_id, user_id, _json({"status": status}),
                _json(
                    request_analysis.diagnostics()
                    if request_analysis else {"version": "request-statement-v1"}
                ),
                _json(context_diagnostics), _json(plan.model_dump()),
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
                    _json(build_approval_summary(plan, message, policy)),
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
    settings = get_settings()
    current_executor_version = settings.executor_version or settings.deployment_version
    async with pool.acquire() as conn, conn.transaction():
        run = await conn.fetchrow(
            """SELECT * FROM agent_runs WHERE id=$1 AND user_id=$2
               AND status='awaiting_clarification' FOR UPDATE""",
            run_id, user_id,
        )
        if not run:
            return None
        requested_questions = set(run["clarification_questions"] or [])
        supplied_questions = set(answers)
        unexpected = sorted(supplied_questions - requested_questions)
        if unexpected:
            raise InvalidClarificationAnswers(
                "Clarification answers do not match this run's current questions",
                reason_code="clarification_keys_mismatch",
            )
        answers = _normalize_clarification_answers(run, answers)
        combined_answers = {
            **(run["clarification_answers"] or {}),
            **answers,
        }
        # Clarifications are typed data, not new authority-bearing prose. Reusing
        # the previous plan objective here formerly appended the same blocks on
        # every submission and allowed question wording such as "email" to invent
        # services. Always rebuild from the immutable original request.
        planning_message = run["request"]
        authority_message = run["request"]
        statement = analyze_request_statement(authority_message)
        referenced_output, referenced_subject, referenced_service = (
            _referenced_plan_content(run["plan"])
        )
        plan, policy = build_plan(
            planning_message,
            authority_message=authority_message,
            request_analysis=statement,
            referenced_output=referenced_output,
            referenced_subject=referenced_subject,
            referenced_service=referenced_service,
            clarification_answers=combined_answers,
        )
        if policy["required_clarifications"]:
            await conn.execute(
                """UPDATE agent_runs SET plan=$1::jsonb,
                   clarification_questions=$2::jsonb,clarification_answers=$3::jsonb,
                   planning_diagnostics=COALESCE(planning_diagnostics,'{}'::jsonb) ||
                       jsonb_build_object('request_analysis',$4::jsonb)
                   WHERE id=$5""",
                _json(plan.model_dump()), _json(policy["required_clarifications"]),
                _json(combined_answers), _json(statement.diagnostics()), run_id,
            )
            await conn.execute(
                """INSERT INTO agent_run_events
                   (run_id,user_id,event_type,phase,message,payload)
                   VALUES($1,$2,'clarification_received','clarification',
                          'Clarifications were saved; additional typed fields remain',
                          $3::jsonb)""",
                run_id, user_id, _json({
                    "answer_keys": sorted(answers),
                    "remaining_questions": policy["required_clarifications"],
                }),
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
            """UPDATE agent_runs SET objective=$1,status=$2,current_phase='planned',
               plan=$3::jsonb,risk_level=$4,requires_approval=$5,
               clarification_questions='[]'::jsonb,clarification_answers=$6::jsonb,
               executor_version=CASE WHEN canary_id IS NULL THEN $9 ELSE executor_version END,
               assignment_reason=CASE WHEN canary_id IS NULL
                    THEN 'clarification completed on current control deployment'
                    ELSE assignment_reason END,
               assigned_at=CASE WHEN canary_id IS NULL THEN now() ELSE assigned_at END,
               planning_diagnostics=COALESCE(planning_diagnostics,'{}'::jsonb) ||
                   jsonb_build_object('request_analysis',$7::jsonb)
               WHERE id=$8""",
            run["request"], status, _json(plan.model_dump()), policy["risk_level"],
            policy["requires_approval"], _json(combined_answers),
            _json(statement.diagnostics()), run_id, current_executor_version,
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
                _json(build_approval_summary(plan, authority_message, policy)),
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
        result["clarification_fields"] = _clarification_fields(
            list(result.get("clarification_questions") or []),
            dict(result.get("clarification_answers") or {}),
        )
        result["steps"] = [dict(item) for item in await conn.fetch(
            "SELECT * FROM agent_run_steps WHERE run_id=$1 ORDER BY sequence_no", run_id
        )]
        model_usage = await conn.fetch(
            """SELECT model,count(*)::integer AS calls,
                      coalesce(sum(input_tokens),0)::bigint AS input_tokens,
                      coalesce(sum(output_tokens),0)::bigint AS output_tokens
               FROM agent_model_calls
               WHERE run_id=$1
               GROUP BY model
               ORDER BY coalesce(sum(input_tokens),0)
                        + coalesce(sum(output_tokens),0) DESC, model""",
            run_id,
        )
        result["model_usage"] = [dict(item) for item in model_usage]
        result["model_call_count"] = sum(item["calls"] for item in model_usage)
        now = datetime.now(timezone.utc)
        elapsed_start = result.get("queued_at") or result.get("started_at")
        elapsed_end = result.get("completed_at") or now
        result["elapsed_duration_ms"] = (
            max(0, int((elapsed_end - elapsed_start).total_seconds() * 1000))
            if elapsed_start else 0
        )
        active_duration_ms = 0
        for step in result["steps"]:
            if step.get("duration_ms") is not None:
                active_duration_ms += max(0, int(step["duration_ms"]))
            elif step.get("status") == "running" and step.get("started_at"):
                active_duration_ms += max(
                    0, int((now - step["started_at"]).total_seconds() * 1000)
                )
        result["active_duration_ms"] = active_duration_ms
        result["artifacts"] = [dict(item) for item in await conn.fetch(
            "SELECT * FROM agent_artifacts WHERE run_id=$1 ORDER BY created_at", run_id
        )]
        result["rag_retrievals"] = [dict(item) for item in await conn.fetch(
            """SELECT mode,reason,returned_count,used_count,duration_ms,
                      source_types,created_at
               FROM rag_retrieval_events
               WHERE run_id=$1
               ORDER BY created_at""",
            run_id,
        )]
        result["okf_retrievals"] = [dict(item) for item in await conn.fetch(
            """SELECT document_ids,okf_versions,duration_ms,created_at
                 FROM okf_retrieval_events
                WHERE run_id=$1 ORDER BY created_at""",
            run_id,
        )]
        result["okf_selection_events"] = [dict(item) for item in await conn.fetch(
            """SELECT payload,created_at
                 FROM agent_run_events
                WHERE run_id=$1 AND event_type='okf_context_selected'
                ORDER BY id""",
            run_id,
        )]
        index_rows = await conn.fetch(
            """SELECT source_type,count(*)::integer AS chunks,
                      count(embedding)::integer AS embedded_chunks,
                      array_agg(DISTINCT chunker_version ORDER BY chunker_version)
                        AS chunker_versions
               FROM rag_chunks
               WHERE user_id=$1 AND deleted_at IS NULL
               GROUP BY source_type ORDER BY source_type""",
            user_id,
        )
        parent_count = await conn.fetchval(
            """SELECT count(*) FROM rag_parent_sections
               WHERE user_id=$1 AND deleted_at IS NULL""",
            user_id,
        )
        pending_embeddings = await conn.fetchval(
            """SELECT count(*) FROM embedding_jobs
               WHERE user_id=$1 AND status IN ('queued','running','failed')""",
            user_id,
        )
        dead_embeddings = await conn.fetchval(
            """SELECT count(*) FROM embedding_jobs
               WHERE user_id=$1 AND status='dead_letter'""",
            user_id,
        )
        latest_sync = await conn.fetchrow(
            """SELECT id,status,sources,max_items_per_source,result,error_message,
                      created_at,started_at,completed_at
               FROM rag_source_sync_jobs
               WHERE user_id=$1 ORDER BY created_at DESC LIMIT 1""",
            user_id,
        )
        result["rag_index_status"] = {
            "ready": bool(index_rows),
            "sources": [dict(item) for item in index_rows],
            "parent_sections": int(parent_count or 0),
            "pending_embedding_jobs": int(pending_embeddings or 0),
            "dead_letter_embedding_jobs": int(dead_embeddings or 0),
            "latest_sync": dict(latest_sync) if latest_sync else None,
        }
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
    settings = get_settings()
    current_executor_version = settings.executor_version or settings.deployment_version
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
                   cancellation_source=CASE WHEN $1='cancelled' THEN 'approval_rejected' END,
                   executor_version=CASE WHEN $1='queued' AND canary_id IS NULL
                       THEN $5 ELSE executor_version END,
                   assignment_reason=CASE WHEN $1='queued' AND canary_id IS NULL
                       THEN 'approval granted on current control deployment'
                       ELSE assignment_reason END,
                   assigned_at=CASE WHEN $1='queued' AND canary_id IS NULL
                       THEN now() ELSE assigned_at END
                   WHERE id=$3 AND user_id=$4""",
                next_status, "queued" if approved else "cancelled", run_id, user_id,
                current_executor_version,
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
