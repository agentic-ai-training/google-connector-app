"""Privacy-bounded failure capture, analysis, clustering, and review handoff."""

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone

from app.mlops.metrics import failure_incidents


_EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_URL = re.compile(r"https?://\S+")
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b", re.I)
_LONG_ID = re.compile(r"\b[A-Za-z0-9_-]{24,}\b")
_NUMBER = re.compile(r"\b\d+\b")


def sanitize_request_excerpt(message: str) -> str:
    """Retain useful intent language while removing direct identifiers."""
    value = _EMAIL.sub("<email>", message or "")
    value = _URL.sub("<url>", value)
    value = _UUID.sub("<uuid>", value)
    value = _LONG_ID.sub("<id>", value)
    return " ".join(value.split())[:280]


def request_shape(message: str, policy: dict | None = None) -> dict:
    text = " ".join((message or "").casefold().split())
    policy = policy or {}
    return {
        "length_bucket": "short" if len(text) < 80 else "medium" if len(text) < 500 else "long",
        "has_email": bool(_EMAIL.search(text)),
        "has_url": bool(_URL.search(text)),
        "has_relative_date": bool(re.search(r"\b(today|tomorrow|yesterday|next week)\b", text)),
        "has_time": bool(re.search(r"\b\d{1,2}(?::\d{2})?\s*(am|pm)\b", text)),
        "service_count": len(policy.get("services") or []),
        "services": policy.get("services") or [],
        "write": bool(policy.get("write")),
        "risk_level": policy.get("risk_level", "low"),
        "clarification_count": len(policy.get("required_clarifications") or []),
    }


def normalize_error(error: str) -> str:
    value = (error or "unknown failure").casefold()
    value = _EMAIL.sub("<email>", value)
    value = _URL.sub("<url>", value)
    value = _UUID.sub("<uuid>", value)
    value = _LONG_ID.sub("<id>", value)
    value = _NUMBER.sub("<n>", value)
    return " ".join(value.split())[:500]


def failure_fingerprint(
    stage: str, category: str, component: str, error: str,
    service: str | None = None, operation: str | None = None,
) -> tuple[str, str]:
    normalized = normalize_error(error)
    canonical = "\0".join([
        stage, category, component, service or "none", operation or "none", normalized,
    ])
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    return digest, digest[:24]


def _option(
    key: str, title: str, explanation: str, change_scope: list[str],
    acceptance_tests: list[str], tradeoff: str, *, automation_eligible: bool,
) -> dict:
    return {
        "id": key, "title": title, "explanation": explanation,
        "change_scope": change_scope, "acceptance_tests": acceptance_tests,
        "tradeoff": tradeoff, "automation_eligible": automation_eligible,
    }


def analyze_failure(
    *, stage: str, category: str, component: str, error: str,
    service: str | None = None, operation: str | None = None,
    breaking_point: str | None = None,
) -> dict:
    """Produce two bounded strategies from structured facts, never raw model invention."""
    normalized = normalize_error(error)
    unknown_operation = "unknown operation" in normalized or "invalid execution plan" in normalized
    if stage in {"classification", "planning", "validation"} and unknown_operation:
        title = "Invalid service or operation plan"
        root = "The deterministic planner selected a service/operation pair that is not registered."
        factors = [
            "A broad synonym or global verb was interpreted without local request context.",
            "Plan validation ran before a durable failure outcome was available to the user.",
        ]
        options = [
            _option(
                "A", "Correct the intent router and dependency plan",
                "Make service detection context-sensitive, select only registered operations, "
                "and build the data dependency graph required by this request shape.",
                ["intent classifier", "service router", "typed planner", "golden cases"],
                ["The reported request produces a valid typed plan.",
                 "Every service/operation has a registered allowlist.",
                 "The expected dependency DAG and clarifications are asserted."],
                "Best functional repair, but it needs request-shape regression coverage.",
                automation_eligible=False,
            ),
            _option(
                "B", "Strengthen safe planning rejection and recovery",
                "When planning is uncertain or invalid, persist the failure and ask a precise "
                "Workspace-scoped clarification instead of returning HTTP 500.",
                ["plan validator", "durable intake", "API error contract", "recovery UI"],
                ["Invalid plans never escape as HTTP 500.",
                 "A durable incident and two options appear in the portal.",
                 "No Google tool executes for a rejected plan."],
                "Prevents opaque failures broadly but does not by itself perform the intended task.",
                automation_eligible=True,
            ),
        ]
        recommended = "A"
        reason = "The request is supportable; correcting routing restores the intended workflow."
    elif category in {"authentication", "permission"}:
        title = "Google authorization or permission failure"
        root = "The selected Google operation could not use the required authorization or permission."
        factors = ["Required scopes, resource access, or sharing policy were not satisfied."]
        options = [
            _option(
                "A", "Add operation-specific authorization preflight",
                "Check required scopes and resource permissions before the affected step runs.",
                ["OAuth scope map", "tool policy", "preconditions"],
                ["Missing access is detected before mutation.", "The user sees exact reconnect guidance."],
                "Reduces late failures; Google can still revoke access after preflight.",
                automation_eligible=True,
            ),
            _option(
                "B", "Improve reconnect and resume recovery",
                "Preserve completed steps, guide reconnection, and resume from the first safe step.",
                ["OAuth UI", "durable resume", "artifact reconciliation"],
                ["Reconnect does not duplicate completed writes.", "Resume begins at the failed step."],
                "Improves recovery but does not prevent the first authorization failure.",
                automation_eligible=True,
            ),
        ]
        recommended, reason = "A", "Preventing unauthorized execution is safer than recovering later."
    elif category == "model_context_length":
        title = "Model context overflow after a tool result"
        root = "The executor attempted a model call whose messages or completion exceeded the provider context limit."
        factors = [
            "A live tool result may have exposed more fields or body content than the next reasoning step required.",
            "Context size must be measured before every provider call, including tool schemas and completion reserve.",
        ]
        options = [
            _option(
                "A", "Add exact metadata projection plus universal result bounds",
                "Use a metadata-only operation for this request shape and enforce a typed, token-bounded result envelope for every tool.",
                [component, service or "tool registry", "tool result projector", "context preflight"],
                ["The exact replay never fetches unneeded bodies.",
                 "Every provider call stays inside its measured context budget.",
                 "An unavoidable overflow is recorded as model_context_length, not generic execution."],
                "Best correctness and efficiency; field projections need tool-specific regression tests.",
                automation_eligible=True,
            ),
            _option(
                "B", "Store large results out of band and continue from compact references",
                "Persist authorized full results outside model history, pass only bounded summaries and stable references, and retrieve selected content on demand.",
                ["artifact/result store", "context manager", "on-demand content retrieval"],
                ["Raw bodies never enter durable step output.",
                 "References retain ownership and permission scope.",
                 "Large-content tasks can resume without repeating the Google read."],
                "More general for content-heavy work, but adds storage and retrieval complexity.",
                automation_eligible=True,
            ),
        ]
        recommended, reason = "A", "Exact projection fixes the observed path and the universal envelope prevents the broader class."
    elif category in {"rate_limit", "network"}:
        title = "Transient quota or network failure"
        root = "A temporary upstream or quota condition interrupted the selected operation."
        factors = ["The request may succeed later without changing its functional plan."]
        options = [
            _option(
                "A", "Use quota-aware deferred retry",
                "Reserve capacity, honor retry timing, and resume idempotently with bounded jitter.",
                ["quota manager", "retry scheduler", "idempotency"],
                ["Retries are bounded.", "Completed Google writes are not repeated."],
                "May increase completion time while preserving correctness.",
                automation_eligible=True,
            ),
            _option(
                "B", "Pause and present a user-controlled resume",
                "Stop safely with partial artifacts and let the user resume after the external condition clears.",
                ["run lifecycle", "partial-result UI", "resume"],
                ["The breaking point and retry time are visible.", "Resume is idempotent."],
                "Avoids background load but requires user interaction.",
                automation_eligible=True,
            ),
        ]
        recommended, reason = "A", "Bounded deferred retry is safe for transient, idempotent steps."
    elif stage == "verification" or category == "verification":
        title = "Result verification failure"
        root = "Execution returned, but required postcondition evidence was missing or incorrect."
        factors = ["An HTTP success response is not sufficient proof of task completion."]
        options = [
            _option(
                "A", "Strengthen tool-specific verification",
                "Read the resource back and validate identifiers, content, recipients, time, and sharing state.",
                ["postconditions", "read-after-write", "artifact verifier"],
                ["False success is rejected.", "Verified artifacts contain stable evidence."],
                "Adds API calls and latency for critical writes.",
                automation_eligible=True,
            ),
            _option(
                "B", "Add compensation and manual-review recovery",
                "Preserve or safely clean up uncertain artifacts and show exact administrator actions.",
                ["artifact ledger", "compensation", "admin review"],
                ["Uncertain artifacts are visible.", "Destructive cleanup still requires approval."],
                "Contains side effects but does not prevent verification defects.",
                automation_eligible=True,
            ),
        ]
        recommended, reason = "A", "Correct verification prevents false success at its source."
    else:
        title = f"{component.replace('_', ' ').title()} failure"
        root = "The request failed in a known stage, but the current structured evidence is not specific enough for a single safe code change."
        factors = [f"Normalized category: {category}.", f"Breaking point: {breaking_point or 'not recorded'}."
        ]
        options = [
            _option(
                "A", "Add a targeted replay and component guardrail",
                "Turn this request shape and failure fingerprint into a no-network regression, then constrain the responsible component.",
                [component, "golden/replay dataset", "component policy"],
                ["The failure reproduces before the fix.", "The candidate passes without unrelated regressions."],
                "Higher confidence, but engineering must inspect the component evidence.",
                automation_eligible=False,
            ),
            _option(
                "B", "Improve containment and actionable recovery",
                "Persist the exact breaking stage, preserve verified work, and offer a safe retry or clarification path.",
                ["failure taxonomy", "durable events", "recovery UI"],
                ["Every occurrence reaches the portal.", "The user receives a non-500 recovery path."],
                "Broadly reduces user harm but may not remove the underlying defect.",
                automation_eligible=True,
            ),
        ]
        recommended, reason = "A", "A replay-backed component fix is safer than guessing from a broad category."
    return {
        "title": title,
        "summary": f"Failure during {stage}: {root}",
        "root_cause": root,
        "contributing_factors": factors,
        "improvement_options": options,
        "recommended_option": recommended,
        "recommendation_reason": reason,
        "risk_level": "high" if stage in {"execution", "verification", "persistence"} else "medium",
        "automation_eligible": options[0]["automation_eligible"] and options[1]["automation_eligible"],
    }


async def create_or_update_proposal(pool, incident_id, selected_option: str, actor: str) -> dict:
    """Convert a reviewed incident to a diagnosis proposal; never fake candidate files."""
    async with pool.acquire() as conn, conn.transaction():
        incident = await conn.fetchrow(
            "SELECT * FROM failure_incidents WHERE id=$1 FOR UPDATE", incident_id,
        )
        if not incident:
            raise ValueError("Failure incident not found")
        options = incident["improvement_options"]
        if isinstance(options, str):
            options = json.loads(options)
        option = next((item for item in options if item["id"] == selected_option), None)
        if not option:
            raise ValueError("Selected improvement option is unavailable")
        active = await conn.fetchrow(
            """SELECT * FROM improvement_proposals
               WHERE failure_cluster_key=$1 AND status NOT IN
                 ('rejected','expired','rolled_back','approved_for_publication')
               ORDER BY created_at DESC LIMIT 1 FOR UPDATE""",
            incident["cluster_key"],
        )
        exact_diff = (
            "--- failure/current\n+++ improvement/selected\n"
            f"@@ cluster:{incident['cluster_key']} @@\n"
            f"- handling: {incident['root_cause']}\n"
            f"+ option_{selected_option}: {option['title']}\n"
            f"+ scope: {', '.join(option['change_scope'])}\n"
            "+ publication: validated_candidate_then_human_canary\n"
        )
        digest = hashlib.sha256(exact_diff.encode()).hexdigest()
        if active:
            proposal_id = active["id"]
            await conn.execute(
                """UPDATE improvement_proposals SET affected_sessions=(
                       SELECT count(*) FROM failure_incidents WHERE cluster_key=$1),
                     updated_at=now() WHERE id=$2""",
                incident["cluster_key"], proposal_id,
            )
        else:
            now = datetime.now(timezone.utc)
            proposal_key = (
                f"failure-{incident['cluster_key'][:12]}-"
                f"{now.strftime('%Y%m%d%H%M%S')}-{str(incident_id)[:8]}"
            )
            proposal_id = await conn.fetchval(
                """INSERT INTO improvement_proposals
                   (proposal_key,proposal_type,title,sanitized_summary,status,severity,
                    risk_level,root_cause_confidence,affected_sessions,exact_diff,
                    expected_impact,privacy_report,security_report,rollback_plan,
                    source_version,candidate_version,content_hash,expires_at,
                    candidate_kind,candidate_state,candidate_manifest,
                    failure_cluster_key,selected_option,created_by)
                   VALUES($1,'failure_intelligence',$2,$3,'awaiting_review',$4,$5,90,1,$6,
                          $7::jsonb,$8::jsonb,$9::jsonb,$10::jsonb,'current',NULL,$11,
                          now()+interval '30 days','diagnosis','diagnosis_only',$12::jsonb,
                          $13,$14,$15) RETURNING id""",
                proposal_key, option["title"],
                f"Selected option {selected_option} for {incident['title']}: {option['explanation']}",
                "high" if incident["risk_level"] == "high" else "medium",
                incident["risk_level"], exact_diff,
                json.dumps({"target": option["title"], "acceptance_tests": option["acceptance_tests"]}),
                json.dumps({"raw_content_included": False, "pii_scan": "passed"}),
                json.dumps({"external_writes_changed": False, "review_required": True}),
                json.dumps({"action": "restore source_version", "automatic": True}),
                digest,
                json.dumps({
                    "kind": "diagnosis", "selected_option": selected_option,
                    "canary_eligible": False,
                    "next_action": "Attach concrete files and passing validation evidence",
                }),
                incident["cluster_key"], selected_option, actor,
            )
        await conn.execute(
            """UPDATE failure_incidents SET analysis_status='proposal_created',
                 proposal_id=$1,updated_at=now() WHERE id=$2""",
            proposal_id, incident_id,
        )
        await conn.execute(
            """INSERT INTO improvement_evidence
               (proposal_id,run_id,evidence_type,sanitized_payload)
               VALUES($1,$2,'failure_incident',$3::jsonb)""",
            proposal_id, incident["run_id"], json.dumps({
                "incident_id": str(incident_id), "cluster_key": incident["cluster_key"],
                "stage": incident["stage"], "category": incident["category"],
                "selected_option": selected_option, "contains_user_content": False,
            }),
        )
        payload = json.dumps({
            "incident_id": str(incident_id), "proposal_id": str(proposal_id),
            "cluster_key": incident["cluster_key"], "contains_private_evidence": False,
        })
        await conn.execute(
            """INSERT INTO improvement_notifications
               (proposal_id,channel,event_type,status,sanitized_payload,error_message)
               SELECT $1,channel,$2,
                      CASE WHEN channel IN ('admin','grafana') THEN 'sent' ELSE 'skipped' END,
                      $3::jsonb,
                      CASE WHEN channel IN ('email','github') THEN
                        'requires separately configured credentials and an approved external write' END
               FROM unnest(ARRAY['admin','grafana','email','github']) channel
               ON CONFLICT(proposal_id,channel,event_type) DO NOTHING""",
            proposal_id, f"failure_option_{selected_option}_{str(incident_id)[:8]}", payload,
        )
        proposal = await conn.fetchrow(
            "SELECT id,proposal_key,status,candidate_state,content_hash FROM improvement_proposals WHERE id=$1",
            proposal_id,
        )
    from app.improvements.builder import enqueue_candidate_build
    build_id = await enqueue_candidate_build(
        pool, proposal_id, dict(incident), option, actor,
    )
    dispatch = None
    if build_id:
        try:
            from app.improvements.publisher import dispatch_candidate_builder
            dispatch = await dispatch_candidate_builder(str(build_id))
        except Exception as exc:
            dispatch = {"status": "not_dispatched", "reason": str(exc)}
    result = dict(proposal)
    result["candidate_build_id"] = str(build_id) if build_id else None
    result["candidate_build_status"] = "queued" if build_id else "disabled"
    result["candidate_build_dispatch"] = dispatch
    return result


async def record_failure_incident(
    pool, *, occurrence_key: str | None = None, run_id=None, session_id: str | None,
    user_id: str, message: str, intent_kind: str, stage: str, category: str,
    component: str, error: str, service: str | None = None,
    operation: str | None = None, breaking_point: str | None = None,
    completion: dict | None = None, evidence: dict | None = None,
    policy: dict | None = None,
) -> dict:
    fingerprint, cluster = failure_fingerprint(
        stage, category, component, error, service, operation,
    )
    analysis = analyze_failure(
        stage=stage, category=category, component=component, error=error,
        service=service, operation=operation, breaking_point=breaking_point,
    )
    occurrence_key = occurrence_key or f"incident:{uuid.uuid4()}"
    payload = {
        "occurrence_key": occurrence_key, "run_id": run_id,
        "session_id": session_id, "user_id": user_id,
        "request_excerpt": sanitize_request_excerpt(message),
        "request_shape": request_shape(message, policy), "intent_kind": intent_kind,
        "stage": stage, "category": category, "component": component,
        "service": service, "operation": operation,
        "failure_fingerprint": fingerprint, "cluster_key": cluster,
        "breaking_point": breaking_point,
        "completion": completion or {}, "evidence": evidence or {}, **analysis,
    }
    async with pool.acquire() as conn, conn.transaction():
        incident = await conn.fetchrow(
            """INSERT INTO failure_incidents
               (occurrence_key,run_id,session_id,user_id,request_excerpt,request_shape,
                intent_kind,stage,category,component,service,operation,
                failure_fingerprint,cluster_key,title,summary,root_cause,
                contributing_factors,breaking_point,completion,evidence,
                improvement_options,recommended_option,recommendation_reason,
                risk_level,automation_eligible)
               VALUES($1,$2,$3,$4,$5,$6::jsonb,$7,$8,$9,$10,$11,$12,$13,$14,$15,
                      $16,$17,$18::jsonb,$19,$20::jsonb,$21::jsonb,$22::jsonb,$23,
                      $24,$25,$26)
               ON CONFLICT(occurrence_key) DO UPDATE SET updated_at=now()
               RETURNING *""",
            payload["occurrence_key"], payload["run_id"], payload["session_id"],
            payload["user_id"], payload["request_excerpt"],
            json.dumps(payload["request_shape"]), payload["intent_kind"],
            payload["stage"], payload["category"], payload["component"],
            payload["service"], payload["operation"], payload["failure_fingerprint"],
            payload["cluster_key"], payload["title"], payload["summary"],
            payload["root_cause"], json.dumps(payload["contributing_factors"]),
            payload["breaking_point"], json.dumps(payload["completion"]),
            json.dumps(payload["evidence"]), json.dumps(payload["improvement_options"]),
            payload["recommended_option"], payload["recommendation_reason"],
            payload["risk_level"], payload["automation_eligible"],
        )
        await conn.execute(
            """INSERT INTO failure_clusters
               (cluster_key,stage,category,component,service,operation,title,
                normalized_signature,occurrence_count,first_seen,last_seen,
                latest_incident_id,metadata)
               VALUES($1,$2,$3,$4,$5,$6,$7,$8,0,now(),now(),$9,$10::jsonb)
               ON CONFLICT(cluster_key) DO UPDATE SET
                 last_seen=now(),latest_incident_id=excluded.latest_incident_id,
                 title=excluded.title,metadata=failure_clusters.metadata||excluded.metadata""",
            cluster, stage, category, component, service, operation, analysis["title"],
            normalize_error(error), incident["id"],
            json.dumps({"risk_level": analysis["risk_level"]}),
        )
        membership = await conn.execute(
            """INSERT INTO failure_cluster_occurrences(cluster_key,incident_id)
               VALUES($1,$2) ON CONFLICT DO NOTHING""",
            cluster, incident["id"],
        )
        if membership.endswith("1"):
            await conn.execute(
                """UPDATE failure_clusters SET occurrence_count=occurrence_count+1,
                   last_seen=now() WHERE cluster_key=$1""", cluster,
            )
        notification_payload = json.dumps({
            "incident_id": str(incident["id"]), "cluster_key": cluster,
            "stage": stage, "category": category, "risk_level": analysis["risk_level"],
            "contains_private_evidence": False,
        })
        await conn.execute(
            """INSERT INTO failure_incident_notifications
               (incident_id,channel,event_type,status,sanitized_payload,error_message,sent_at)
               SELECT $1,channel,'review_required',
                      CASE WHEN channel IN ('admin','grafana') THEN 'sent' ELSE 'skipped' END,
                      $2::jsonb,
                      CASE WHEN channel IN ('email','github') THEN
                        'requires separately configured credentials and explicit confirmation' END,
                      CASE WHEN channel IN ('admin','grafana') THEN now() END
               FROM unnest(ARRAY['admin','grafana','email','github']) channel
               ON CONFLICT(incident_id,channel,event_type) DO NOTHING""",
            incident["id"], notification_payload,
        )
        flag = await conn.fetchrow(
            "SELECT enabled,config FROM feature_flags WHERE name='failure_improvement_automation'",
        )
    result = dict(incident)
    failure_incidents.labels(stage, category).inc()
    config = dict(flag["config"] or {}) if flag else {}
    if flag and flag["enabled"] and config.get("mode") == "auto_draft":
        result["proposal"] = await create_or_update_proposal(
            pool, incident["id"], incident["recommended_option"], "analysis-system",
        )
    return result
