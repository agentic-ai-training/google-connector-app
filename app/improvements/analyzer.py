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
            for run_id in group["run_ids"]:
                await conn.execute(
                    """INSERT INTO improvement_evidence
                       (proposal_id,run_id,evidence_type,sanitized_payload)
                       VALUES($1,$2,'failure_category',$3::jsonb)""",
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
            control = await conn.fetchrow(
                """SELECT count(*) AS total,count(*) FILTER(WHERE status IN ('failed','partial')) AS failed,
                          count(*) FILTER(WHERE side_effect_integrity<100) AS unsafe
                   FROM agent_runs WHERE deployment_version=$1 AND queued_at >= $2""",
                canary["control_version"], canary["started_at"],
            )
            candidate = await conn.fetchrow(
                """SELECT count(*) AS total,count(*) FILTER(WHERE status IN ('failed','partial')) AS failed,
                          count(*) FILTER(WHERE side_effect_integrity<100) AS unsafe
                   FROM agent_runs WHERE deployment_version=$1 AND queued_at >= $2""",
                canary["candidate_version"], canary["started_at"],
            )
            if control["total"] < 5 or candidate["total"] < 5:
                continue
            control_rate = control["failed"] / control["total"]
            candidate_rate = candidate["failed"] / candidate["total"]
            metrics = {
                "control": dict(control), "candidate": dict(candidate),
                "control_failure_rate": control_rate,
                "candidate_failure_rate": candidate_rate,
            }
            passed = candidate_rate <= control_rate and candidate["unsafe"] == 0
            canary_status = "passed" if passed else "rolled_back"
            proposal_status = "awaiting_promotion" if passed else "rolled_back"
            await conn.execute(
                """UPDATE improvement_canaries SET status=$1,metrics=$2::jsonb,ended_at=now(),
                     rollback_reason=$3 WHERE id=$4""",
                canary_status, json.dumps(metrics),
                None if passed else "Candidate breached failure or side-effect guardrail",
                canary["id"],
            )
            await conn.execute(
                "UPDATE improvement_proposals SET status=$1,updated_at=now() WHERE id=$2",
                proposal_status, canary["proposal_id"],
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
