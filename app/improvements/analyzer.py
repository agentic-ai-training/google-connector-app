import asyncio
import hashlib
import json
import logging
from contextlib import suppress
from datetime import date

from app.improvements.failure_intelligence import record_failure_incident


logger = logging.getLogger(__name__)

FAILURE_RECOMMENDATIONS = {
    "rate_limit": "Add quota-aware deferral and reserve the quality model for complex execution.",
    "network": "Add bounded retry with jitter and resume from the first incomplete step.",
    "authentication": "Improve OAuth scope diagnostics and reconnect guidance.",
    "permission": "Validate required scopes and sharing policy before execution.",
    "verification": "Strengthen tool-specific resource and read-after-write postconditions.",
    "embedding": "Tune embedding backpressure, batching, and dead-letter recovery.",
    "execution": "Add a golden replay case and constrain the planner/tool policy.",
}

CANARY_MIN_SAMPLES = 5
CANARY_LATENCY_RATIO = 1.25
CANARY_TOKEN_RATIO = 1.25


def _json_object(value) -> dict:
    """Normalize JSON/JSONB values returned by pools without a JSON codec."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _number(value, default: float) -> float:
    """Return a JSON-safe telemetry number for Decimal-backed database columns."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def assess_canary(control: dict, candidate: dict) -> dict:
    """Apply explicit multi-objective gates without collapsing them into one reward."""
    if control["total"] < CANARY_MIN_SAMPLES or candidate["total"] < CANARY_MIN_SAMPLES:
        if candidate.get("unsafe"):
            return {"ready": True, "passed": False,
                    "regressions": ["side_effect_integrity"],
                    "safety_tripwire": True}
        return {"ready": False, "passed": False, "regressions": ["insufficient_samples"]}
    control_failure = control["failed"] / control["total"]
    candidate_failure = candidate["failed"] / candidate["total"]
    control_cancel = control["cancelled"] / control["total"]
    candidate_cancel = candidate["cancelled"] / candidate["total"]
    regressions = []
    if candidate_failure > control_failure:
        regressions.append("failure_rate")
    if candidate["unsafe"]:
        regressions.append("side_effect_integrity")
    if candidate_cancel > control_cancel + 0.05:
        regressions.append("cancellation_rate")
    control_latency = float(control.get("p95_ms") or 0)
    candidate_latency = float(candidate.get("p95_ms") or 0)
    if control_latency and candidate_latency and candidate_latency > max(
        control_latency * CANARY_LATENCY_RATIO, control_latency + 1000
    ):
        regressions.append("p95_latency")
    control_tokens = float(control.get("avg_tokens") or 0)
    candidate_tokens = float(candidate.get("avg_tokens") or 0)
    if control_tokens and candidate_tokens > control_tokens * CANARY_TOKEN_RATIO:
        regressions.append("average_tokens")
    for metric in ("technical", "functional", "user_visible"):
        control_value = float(control.get(f"avg_{metric}") or 0)
        candidate_value = float(candidate.get(f"avg_{metric}") or 0)
        if candidate_value + 1 < control_value:
            regressions.append(f"{metric}_completion")
    return {
        "ready": True, "passed": not regressions, "regressions": regressions,
        "control_failure_rate": control_failure,
        "candidate_failure_rate": candidate_failure,
        "control_cancellation_rate": control_cancel,
        "candidate_cancellation_rate": candidate_cancel,
    }


def _proposal_diff(category: str, recommendation: str) -> str:
    return (
        "--- policy/current\n+++ policy/candidate\n"
        f"@@ failure:{category} @@\n"
        "- handling: generic_failure\n"
        f"+ handling: {recommendation}\n"
        "+ publication: human_canary_then_human_promotion\n"
    )


async def analyze_recent_failures(pool) -> int:
    """Backfill any missed terminal run once; live failures are recorded immediately."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT r.* FROM agent_runs r
               WHERE r.status IN ('failed','partial')
                 AND r.completed_at >= now()-interval '7 days'
                 AND NOT EXISTS (
                   SELECT 1 FROM failure_incidents i WHERE i.run_id=r.id)
               ORDER BY r.completed_at LIMIT 200"""
        )
    created = 0
    for row in rows:
        run = dict(row)
        incident = _json_object(run.get("incident_summary"))
        try:
            await record_failure_incident(
                pool, occurrence_key=f"run:{run['id']}:backfill", run_id=run["id"],
                session_id=run["session_id"], user_id=run["user_id"],
                message=run["request"], intent_kind=run.get("intent_kind") or "workspace_action",
                stage="execution", category=run.get("error_category") or "execution",
                component="durable_worker", error=run.get("error_message") or "unknown failure",
                breaking_point=incident.get("breaking_point"),
                completion={
                    "technical": _number(run.get("technical_completion"), 0),
                    "functional": _number(run.get("functional_completion"), 0),
                    "user_visible": _number(run.get("user_visible_completion"), 0),
                    "side_effect_integrity": _number(
                        run.get("side_effect_integrity"), 100
                    ),
                },
                evidence={"source": "terminal_run_backfill"},
                policy=_json_object(run.get("plan")),
            )
            created += 1
        except Exception as exc:
            # The next analysis pass retries; one malformed historical row cannot block others.
            logger.warning(
                "Failure-incident backfill skipped run_id=%s error_type=%s",
                run.get("id"), type(exc).__name__,
            )
            continue
    return created


async def analyze_cross_cluster_themes(pool) -> int:
    """Create systemic trends only from multiple specific, durable clusters."""
    async with pool.acquire() as conn:
        clusters = [dict(row) for row in await conn.fetch(
            """SELECT * FROM failure_clusters WHERE status='active'
               AND occurrence_count>0 ORDER BY last_seen DESC LIMIT 500"""
        )]
    groups = {}
    for cluster in clusters:
        mechanism = cluster.get("mechanism") or "unknown"
        component = (cluster.get("component") or "unknown").casefold()
        if mechanism == "context_budget_overflow":
            theme = (
                "unbounded-tool-context",
                "Bound every tool-to-model data boundary",
                "Several specific tool paths can exceed the provider context unless projection and preflight are universal.",
            )
        elif mechanism in {"provider_quota", "transient_transport"}:
            theme = (
                "transient-upstream-resilience",
                "Make upstream interruption recovery idempotent",
                "Repeated provider and network clusters require shared quota, retry, and resume policy.",
            )
        elif mechanism == "planner_contract_rejection" or (
            cluster["category"] == "planning" and "planner" in component
        ):
            theme = (
                "planner-tool-contract",
                "Align planning and executable tool contracts",
                "Several concrete planning/execution clusters indicate an architectural contract gap.",
            )
        else:
            continue
        groups.setdefault(theme, []).append(cluster)
    changed = 0
    for (theme_key, title, cause), members in groups.items():
        # A policy trend requires either multiple specific clusters or repeated
        # evidence in one cluster; a broad category alone is never sufficient.
        if len(members) < 2 and sum(row["occurrence_count"] for row in members) < 3:
            continue
        options = [
            {"id": "A", "title": f"Implement the shared {title.lower()} policy",
             "explanation": "Generate one replay-backed candidate covering all linked clusters."},
            {"id": "B", "title": "Keep cluster-specific fixes isolated",
             "explanation": "Build separate candidates until shared behavior is proven by replay."},
        ]
        async with pool.acquire() as conn, conn.transaction():
            theme_id = await conn.fetchval(
                """INSERT INTO failure_themes
                   (theme_key,title,systemic_cause,strategy_options,recommended_option,
                    occurrence_count,first_seen,last_seen,metadata)
                   VALUES($1,$2,$3,$4::jsonb,'A',$5,now(),now(),$6::jsonb)
                   ON CONFLICT(theme_key) DO UPDATE SET
                     occurrence_count=excluded.occurrence_count,last_seen=now(),
                     strategy_options=excluded.strategy_options,metadata=excluded.metadata
                   RETURNING id""",
                theme_key, title, cause, json.dumps(options),
                sum(row["occurrence_count"] for row in members),
                json.dumps({"cluster_count": len(members), "category_only": False}),
            )
            for member in members:
                await conn.execute(
                    """INSERT INTO failure_theme_clusters(theme_id,cluster_key,confidence)
                       VALUES($1,$2,90) ON CONFLICT DO NOTHING""",
                    theme_id, member["cluster_key"],
                )
        changed += 1
    return changed


async def analyze_recent_failure_categories_legacy(pool) -> int:
    """Draft sanitized proposals; never change trusted runtime policy or OKF."""
    async with pool.acquire() as conn:
        groups = await conn.fetch(
            """SELECT coalesce(error_category,'unknown') AS category,count(*) AS failures,
                      (array_agg(id ORDER BY completed_at DESC))[1:10] AS run_ids
               FROM agent_runs WHERE status IN ('failed','partial')
                 AND completed_at >= now()-interval '7 days'
               GROUP BY 1 HAVING count(*) >= 3"""
        )
    created = 0
    for group in groups:
        category = group["category"]
        recommendation = FAILURE_RECOMMENDATIONS.get(
            category, "Create a verified replay case and inspect the first breaking point."
        )
        proposal_key = f"incident-{category}-{date.today().isoformat()}"
        exact_diff = _proposal_diff(category, recommendation)
        digest = hashlib.sha256(exact_diff.encode()).hexdigest()
        async with pool.acquire() as conn, conn.transaction():
            proposal_id = await conn.fetchval(
                """INSERT INTO improvement_proposals
                   (proposal_key,proposal_type,title,sanitized_summary,status,severity,
                    risk_level,root_cause_confidence,affected_sessions,exact_diff,
                    expected_impact,privacy_report,security_report,rollback_plan,
                    source_version,candidate_version,content_hash,expires_at,
                    candidate_kind,candidate_state,candidate_manifest)
                   VALUES($1,'policy',$2,$3,'awaiting_review',$4,'medium',85,$5,$6,
                          $7::jsonb,$8::jsonb,$9::jsonb,$10::jsonb,'current',$11,$12,
                          now()+interval '30 days','diagnosis','diagnosis_only',$13::jsonb)
                   ON CONFLICT(proposal_key) DO NOTHING RETURNING id""",
                proposal_key, f"Reduce recurring {category} failures",
                f"{group['failures']} recent runs share the sanitized category {category}. {recommendation}",
                "high" if group["failures"] >= 10 else "medium", group["failures"],
                exact_diff,
                json.dumps({"target": "lower failure rate", "category": category}),
                json.dumps({"raw_content_included": False, "pii_scan": "passed"}),
                json.dumps({"external_writes_changed": False, "review_required": True}),
                json.dumps({"action": "restore source_version", "automatic": True}),
                None, digest,
                json.dumps({
                    "kind": "diagnosis",
                    "canary_eligible": False,
                    "next_action": "Attach concrete changed files and passing validation evidence",
                }),
            )
            if not proposal_id:
                continue
            notification_payload = json.dumps({
                "proposal_key": proposal_key, "title": f"Reduce recurring {category} failures",
                "severity": "high" if group["failures"] >= 10 else "medium",
                "contains_private_evidence": False,
            })
            await conn.execute(
                """INSERT INTO improvement_notifications
                   (proposal_id,channel,event_type,status,sanitized_payload,error_message)
                   SELECT $1,channel,'review_required',
                          CASE WHEN channel IN ('admin','grafana') THEN 'sent' ELSE 'skipped' END,
                          $2::jsonb,
                          CASE WHEN channel IN ('email','github')
                               THEN 'requires separately configured credentials and an approved external write'
                          END
                   FROM unnest(ARRAY['admin','grafana','email','github']) channel
                   ON CONFLICT(proposal_id,channel,event_type) DO NOTHING""",
                proposal_id, notification_payload,
            )
            for run_id in group["run_ids"]:
                await conn.execute(
                    """INSERT INTO improvement_evidence
                       (proposal_id,run_id,evidence_type,sanitized_payload)
                       SELECT $1,id,'failure_category',$3::jsonb
                       FROM agent_runs WHERE id=$2""",
                    proposal_id, run_id,
                    json.dumps({"category": category, "contains_user_content": False}),
                )
            created += 1
    return created


async def expire_stale_proposals(pool) -> int:
    from app.improvements.failure_intelligence import release_theme_for_proposal
    async with pool.acquire() as conn, conn.transaction():
        rows = await conn.fetch(
            """UPDATE improvement_proposals SET status='expired',updated_at=now()
               WHERE expires_at<now() AND status IN
                 ('drafted','awaiting_review','changes_requested','approved_for_canary')
               RETURNING *"""
        )
        for proposal in rows:
            await release_theme_for_proposal(conn, dict(proposal))
    return len(rows)


async def evaluate_active_canaries(pool) -> int:
    """Evaluate only measured, version-labelled runs; never manufacture a pass."""
    changed = 0
    cleanup_requests: list[tuple[str, str]] = []
    async with pool.acquire() as conn:
        canaries = await conn.fetch(
            """SELECT * FROM improvement_canaries WHERE status='active'"""
        )
    for canary in canaries:
        async with pool.acquire() as conn, conn.transaction():
            # The API process and dedicated worker may both run this loop. Lock and
            # re-check the lifecycle so a canary is concluded exactly once.
            canary = await conn.fetchrow(
                "SELECT * FROM improvement_canaries WHERE id=$1 AND status='active' FOR UPDATE",
                canary["id"],
            )
            if not canary:
                continue
            control = await conn.fetchrow(
                """SELECT count(*) AS total,
                          count(*) FILTER(WHERE status IN ('failed','partial')) AS failed,
                          count(*) FILTER(WHERE status='cancelled') AS cancelled,
                          count(*) FILTER(WHERE side_effect_integrity<100) AS unsafe,
                          avg(technical_completion) AS avg_technical,
                          avg(functional_completion) AS avg_functional,
                          avg(user_visible_completion) AS avg_user_visible,
                          avg(coalesce(input_tokens,0)+coalesce(output_tokens,0)) AS avg_tokens,
                          percentile_cont(0.95) WITHIN GROUP
                            (ORDER BY extract(epoch FROM (completed_at-started_at))*1000)
                            FILTER(WHERE completed_at IS NOT NULL AND started_at IS NOT NULL) AS p95_ms
                   FROM agent_runs WHERE canary_id=$1 AND cohort_assignment='control'
                     AND queued_at >= $2""",
                canary["id"], canary["started_at"],
            )
            candidate = await conn.fetchrow(
                """SELECT count(*) AS total,
                          count(*) FILTER(WHERE status IN ('failed','partial')) AS failed,
                          count(*) FILTER(WHERE status='cancelled') AS cancelled,
                          count(*) FILTER(WHERE side_effect_integrity<100) AS unsafe,
                          avg(technical_completion) AS avg_technical,
                          avg(functional_completion) AS avg_functional,
                          avg(user_visible_completion) AS avg_user_visible,
                          avg(coalesce(input_tokens,0)+coalesce(output_tokens,0)) AS avg_tokens,
                          percentile_cont(0.95) WITHIN GROUP
                            (ORDER BY extract(epoch FROM (completed_at-started_at))*1000)
                            FILTER(WHERE completed_at IS NOT NULL AND started_at IS NOT NULL) AS p95_ms
                   FROM agent_runs WHERE canary_id=$1 AND cohort_assignment='candidate'
                     AND queued_at >= $2""",
                canary["id"], canary["started_at"],
            )
            control_data = dict(control)
            candidate_data = dict(candidate)
            assessment = assess_canary(control_data, candidate_data)
            if not assessment["ready"]:
                continue
            metrics = {
                "control": control_data, "candidate": candidate_data,
                **{key: value for key, value in assessment.items() if key != "ready"},
            }
            passed = assessment["passed"]
            canary_status = "passed" if passed else "rolled_back"
            proposal_status = "awaiting_promotion" if passed else "rolled_back"
            await conn.execute(
                """INSERT INTO improvement_evaluations
                   (proposal_id,suite_version,control_metrics,candidate_metrics,
                    regressions,passed)
                   VALUES($1,'canary-guardrails-v1',$2::jsonb,$3::jsonb,$4::jsonb,$5)""",
                canary["proposal_id"], json.dumps(control_data, default=str),
                json.dumps(candidate_data, default=str),
                json.dumps(assessment["regressions"]), passed,
            )
            await conn.execute(
                """UPDATE improvement_canaries SET status=$1,metrics=$2::jsonb,ended_at=now(),
                     rollback_reason=$3,routing_enabled=$5,
                     rollback_at=CASE WHEN $5=FALSE THEN now() END WHERE id=$4""",
                canary_status, json.dumps(metrics, default=str),
                None if passed else "Candidate breached: " + ", ".join(
                    assessment["regressions"]
                ),
                canary["id"], passed,
            )
            if not passed:
                await conn.execute(
                    """UPDATE agent_runs SET executor_version=$1,
                       cohort_assignment='control',assignment_reason='automatic canary rollback'
                       WHERE canary_id=$2 AND cohort_assignment='candidate' AND status='queued'
                         AND NOT EXISTS(SELECT 1 FROM agent_run_steps s
                           WHERE s.run_id=agent_runs.id AND s.status IN ('running','completed'))""",
                    canary["control_version"], canary["id"],
                )
                await conn.execute(
                    """UPDATE okf_bundle_versions SET publication_status='rolled_back'
                       WHERE bundle_hash=(SELECT candidate_manifest->>'okf_bundle_hash'
                         FROM improvement_proposals WHERE id=$1)
                         AND publication_status='canary'""",
                    canary["proposal_id"],
                )
                retired = await conn.fetchrow(
                    """SELECT proposal_key,candidate_kind,deployment_evidence
                       FROM improvement_proposals
                       WHERE id=$1""",
                    canary["proposal_id"],
                )
                if retired and retired["candidate_kind"] == "code":
                    cleanup_requests.append((
                        retired["proposal_key"],
                        "automatic measured canary rollback",
                        str((retired["deployment_evidence"] or {}).get(
                            "frontend_url"
                        ) or ""),
                    ))
            await conn.execute(
                "UPDATE improvement_proposals SET status=$1,updated_at=now() WHERE id=$2",
                proposal_status, canary["proposal_id"],
            )
            notification_payload = json.dumps({
                "proposal_id": str(canary["proposal_id"]),
                "canary_status": canary_status,
                "regressions": assessment["regressions"],
                "contains_private_evidence": False,
            })
            await conn.execute(
                """INSERT INTO improvement_notifications
                   (proposal_id,channel,event_type,status,sanitized_payload,error_message)
                   SELECT $1,channel,$2,
                          CASE WHEN channel IN ('admin','grafana') THEN 'sent' ELSE 'skipped' END,
                          $3::jsonb,
                          CASE WHEN channel IN ('email','github')
                               THEN 'requires separately configured credentials and an approved external write'
                          END
                   FROM unnest(ARRAY['admin','grafana','email','github']) channel
                   ON CONFLICT(proposal_id,channel,event_type) DO NOTHING""",
                canary["proposal_id"], f"canary_{canary_status}", notification_payload,
            )
            changed += 1
    if cleanup_requests:
        from app.improvements.publisher import dispatch_candidate_cleanup
        for proposal_key, reason, frontend_url in cleanup_requests:
            try:
                await dispatch_candidate_cleanup(
                    proposal_key, reason, frontend_url,
                )
            except Exception as exc:
                logger.warning(
                    "Candidate cleanup dispatch failed proposal=%s error_type=%s",
                    proposal_key, type(exc).__name__,
                )
    return changed


async def improvement_analysis_loop(pool, stop_event: asyncio.Event):
    while not stop_event.is_set():
        await expire_stale_proposals(pool)
        await analyze_recent_failures(pool)
        await analyze_cross_cluster_themes(pool)
        await dispatch_retryable_candidate_builds(pool)
        await evaluate_active_canaries(pool)
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=60)


async def dispatch_retryable_candidate_builds(pool, limit: int = 2) -> int:
    """Re-dispatch due GitHub builders without creating duplicate candidates."""
    async with pool.acquire() as conn, conn.transaction():
        rows = await conn.fetch(
            """SELECT id FROM candidate_builds
               WHERE status='queued'
                 AND checkpoint#>>'{last_runner_failure,retryable}'='true'
                 AND updated_at + (
                   LEAST(86400, GREATEST(60, COALESCE(
                     NULLIF(checkpoint#>>'{last_runner_failure,retry_after_seconds}','')::int,
                     1800
                   ))) * interval '1 second'
                 ) <= now()
               ORDER BY updated_at,id
               FOR UPDATE SKIP LOCKED LIMIT $1""",
            max(1, min(int(limit), 10)),
        )
        build_ids = [str(row["id"]) for row in rows]
        for build_id in build_ids:
            await conn.execute(
                """UPDATE candidate_builds SET updated_at=now(),
                     checkpoint=checkpoint||$1::jsonb WHERE id=$2""",
                json.dumps({
                    "last_retry_dispatch": {
                        "state": "dispatching", "contains_private_evidence": False,
                    },
                }), build_id,
            )
    if not build_ids:
        return 0
    from app.improvements.publisher import dispatch_candidate_builder
    dispatched = 0
    for build_id in build_ids:
        try:
            await dispatch_candidate_builder(build_id)
            dispatched += 1
            async with pool.acquire() as conn:
                await conn.execute(
                    """UPDATE candidate_builds SET checkpoint=checkpoint||$1::jsonb
                         WHERE id=$2 AND status='queued'""",
                    json.dumps({
                        "last_retry_dispatch": {
                            "state": "dispatched",
                            "contains_private_evidence": False,
                        },
                    }), build_id,
                )
        except Exception as exc:
            logger.warning(
                "Candidate retry dispatch failed build=%s error_type=%s",
                build_id, type(exc).__name__,
            )
            async with pool.acquire() as conn:
                await conn.execute(
                    """UPDATE candidate_builds SET error_message=$1,updated_at=now(),
                         checkpoint=checkpoint||$2::jsonb WHERE id=$3 AND status='queued'""",
                    "Candidate retry dispatch failed; the durable build remains queued.",
                    json.dumps({
                        "last_retry_dispatch": {
                            "state": "failed", "error_type": type(exc).__name__,
                            "contains_private_evidence": False,
                        },
                    }), build_id,
                )
    return dispatched
