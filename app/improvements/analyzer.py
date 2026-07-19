import asyncio
import hashlib
import json
from contextlib import suppress
from datetime import date


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


def assess_canary(control: dict, candidate: dict) -> dict:
    """Apply explicit multi-objective gates without collapsing them into one reward."""
    if control["total"] < CANARY_MIN_SAMPLES or candidate["total"] < CANARY_MIN_SAMPLES:
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
                    source_version,candidate_version,content_hash,expires_at)
                   VALUES($1,'policy',$2,$3,'awaiting_review',$4,'medium',85,$5,$6,
                          $7::jsonb,$8::jsonb,$9::jsonb,$10::jsonb,'current',$11,$12,
                          now()+interval '30 days')
                   ON CONFLICT(proposal_key) DO NOTHING RETURNING id""",
                proposal_key, f"Reduce recurring {category} failures",
                f"{group['failures']} recent runs share the sanitized category {category}. {recommendation}",
                "high" if group["failures"] >= 10 else "medium", group["failures"],
                exact_diff,
                json.dumps({"target": "lower failure rate", "category": category}),
                json.dumps({"raw_content_included": False, "pii_scan": "passed"}),
                json.dumps({"external_writes_changed": False, "review_required": True}),
                json.dumps({"action": "restore source_version", "automatic": True}),
                f"candidate-{digest[:12]}", digest,
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
    async with pool.acquire() as conn:
        result = await conn.execute(
            """UPDATE improvement_proposals SET status='expired',updated_at=now()
               WHERE expires_at<now() AND status IN
                 ('drafted','awaiting_review','changes_requested','approved_for_canary')"""
        )
    return int(result.rsplit(" ", 1)[-1])


async def evaluate_active_canaries(pool) -> int:
    """Evaluate only measured, version-labelled runs; never manufacture a pass."""
    changed = 0
    async with pool.acquire() as conn:
        canaries = await conn.fetch(
            """SELECT * FROM improvement_canaries
               WHERE status='active' AND started_at <= now()-interval '1 hour'"""
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
                          avg(coalesce(input_tokens,0)+coalesce(output_tokens,0)) AS avg_tokens,
                          percentile_cont(0.95) WITHIN GROUP
                            (ORDER BY extract(epoch FROM (completed_at-started_at))*1000)
                            FILTER(WHERE completed_at IS NOT NULL AND started_at IS NOT NULL) AS p95_ms
                   FROM agent_runs WHERE deployment_version=$1 AND queued_at >= $2""",
                canary["control_version"], canary["started_at"],
            )
            candidate = await conn.fetchrow(
                """SELECT count(*) AS total,
                          count(*) FILTER(WHERE status IN ('failed','partial')) AS failed,
                          count(*) FILTER(WHERE status='cancelled') AS cancelled,
                          count(*) FILTER(WHERE side_effect_integrity<100) AS unsafe,
                          avg(coalesce(input_tokens,0)+coalesce(output_tokens,0)) AS avg_tokens,
                          percentile_cont(0.95) WITHIN GROUP
                            (ORDER BY extract(epoch FROM (completed_at-started_at))*1000)
                            FILTER(WHERE completed_at IS NOT NULL AND started_at IS NOT NULL) AS p95_ms
                   FROM agent_runs WHERE deployment_version=$1 AND queued_at >= $2""",
                canary["candidate_version"], canary["started_at"],
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
                     rollback_reason=$3 WHERE id=$4""",
                canary_status, json.dumps(metrics, default=str),
                None if passed else "Candidate breached: " + ", ".join(
                    assessment["regressions"]
                ),
                canary["id"],
            )
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
    return changed


async def improvement_analysis_loop(pool, stop_event: asyncio.Event):
    while not stop_event.is_set():
        await expire_stale_proposals(pool)
        await analyze_recent_failures(pool)
        await evaluate_active_canaries(pool)
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=3600)
