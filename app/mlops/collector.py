import asyncio
from contextlib import suppress

from app.mlops.metrics import (
    artifact_cleanup_queue,
    candidate_budget_ratio,
    candidate_build_queue,
    candidate_progress_state,
    candidate_retry_state,
    canary_routing,
    embedding_queue,
    improvement_queue,
    improvement_notifications,
    failure_notifications,
    failure_review_queue,
    failure_theme_queue,
    okf_bundle_publications,
    rag_quality,
    rag_quality_samples,
    run_queue_depth,
    stale_runs,
)

RUN_STATES = (
    "queued", "awaiting_clarification", "awaiting_approval", "running",
    "completed", "partial", "failed", "cancelled",
)
EMBEDDING_STATES = ("queued", "running", "completed", "failed", "dead_letter")
PROPOSAL_STATES = (
    "awaiting_review", "approved_for_canary", "canary_active",
    "awaiting_promotion", "approved_for_publication", "production_pending",
    "published", "rejected", "changes_requested", "expired", "rolled_back",
)
CLEANUP_STATES = (
    "awaiting_confirmation", "approved", "rejected", "executing", "completed",
    "failed", "manual_required",
)
NOTIFICATION_CHANNELS = ("admin", "grafana", "email", "github")
NOTIFICATION_STATES = ("queued", "sent", "skipped", "failed")
CANDIDATE_BUILD_STATES = (
    "queued", "investigating", "drafted", "validating", "validated", "failed", "cancelled",
)
FAILURE_THEME_STATES = ("active", "candidate_building", "resolved", "suppressed")
OKF_PUBLICATION_STATES = (
    "draft", "validated", "canary", "trusted", "rolled_back", "rejected",
)


async def collect_operational_metrics(pool):
    candidate_budget_ratio.clear()
    candidate_progress_state.clear()
    candidate_retry_state.clear()
    for state in RUN_STATES:
        run_queue_depth.labels(state).set(0)
    for state in EMBEDDING_STATES:
        embedding_queue.labels(state).set(0)
    for state in PROPOSAL_STATES:
        improvement_queue.labels(state).set(0)
    for state in CLEANUP_STATES:
        artifact_cleanup_queue.labels(state).set(0)
    for channel in NOTIFICATION_CHANNELS:
        for state in NOTIFICATION_STATES:
            improvement_notifications.labels(channel, state).set(0)
            failure_notifications.labels(channel, state).set(0)
    for state in CANDIDATE_BUILD_STATES:
        candidate_build_queue.labels(state).set(0)
    for state in FAILURE_THEME_STATES:
        failure_theme_queue.labels(state).set(0)
    for state in OKF_PUBLICATION_STATES:
        okf_bundle_publications.labels(state).set(0)
    for stage in ("intake", "classification", "planning", "validation", "admission",
                  "approval", "execution", "verification", "recovery", "persistence", "api"):
        for risk in ("low", "medium", "high"):
            failure_review_queue.labels(stage, risk).set(0)
    async with pool.acquire() as conn:
        for row in await conn.fetch(
            "SELECT status,count(*) AS count FROM agent_runs WHERE deleted_at IS NULL GROUP BY status"
        ):
            run_queue_depth.labels(row["status"]).set(row["count"])
        stale_runs.set(await conn.fetchval(
            "SELECT count(*) FROM agent_runs WHERE status='running' AND lease_expires_at<now()"
        ))
        for row in await conn.fetch(
            "SELECT status,count(*) AS count FROM embedding_jobs GROUP BY status"
        ):
            embedding_queue.labels(row["status"]).set(row["count"])
        for row in await conn.fetch(
            "SELECT status,count(*) AS count FROM improvement_proposals GROUP BY status"
        ):
            improvement_queue.labels(row["status"]).set(row["count"])
        for row in await conn.fetch(
            "SELECT status,count(*) AS count FROM artifact_cleanup_requests GROUP BY status"
        ):
            artifact_cleanup_queue.labels(row["status"]).set(row["count"])
        for row in await conn.fetch(
            """SELECT channel,status,count(*) AS count FROM improvement_notifications
               GROUP BY channel,status"""
        ):
            improvement_notifications.labels(row["channel"], row["status"]).set(row["count"])
        for row in await conn.fetch(
            """SELECT stage,risk_level,count(*) AS count FROM failure_incidents
               WHERE analysis_status='awaiting_review' GROUP BY stage,risk_level"""
        ):
            failure_review_queue.labels(row["stage"], row["risk_level"]).set(row["count"])
        for row in await conn.fetch(
            """SELECT channel,status,count(*) AS count FROM failure_incident_notifications
               GROUP BY channel,status"""
        ):
            failure_notifications.labels(row["channel"], row["status"]).set(row["count"])
        for row in await conn.fetch(
            "SELECT status,count(*) AS count FROM candidate_builds GROUP BY status"
        ):
            candidate_build_queue.labels(row["status"]).set(row["count"])
        for row in await conn.fetch(
            """SELECT mode,status,
                      sum(tokens_used)::float /
                      greatest(sum(coalesce(
                        nullif(checkpoint#>>'{generation_checkpoint,effective_token_budget}','')::numeric,
                        token_budget)),1)::float AS ratio
               FROM candidate_builds GROUP BY mode,status"""
        ):
            candidate_budget_ratio.labels(row["mode"], row["status"]).set(
                float(row["ratio"] or 0)
            )
        for row in await conn.fetch(
            """SELECT coalesce(
                        checkpoint#>>'{generation_checkpoint,active_role}','none') AS role,
                      coalesce(
                        checkpoint#>>'{generation_checkpoint,progress_gate}','none') AS gate,
                      count(*) AS count
               FROM candidate_builds
               WHERE status IN ('queued','investigating')
               GROUP BY role,gate"""
        ):
            candidate_progress_state.labels(row["role"], row["gate"]).set(row["count"])
        for row in await conn.fetch(
            """SELECT coalesce(
                        checkpoint#>>'{last_runner_failure,retryable}','false') AS eligible,
                      coalesce(
                        checkpoint#>>'{last_runner_failure,retry_reason}','none') AS reason,
                      count(*) AS count
               FROM candidate_builds
               WHERE checkpoint ? 'last_runner_failure'
               GROUP BY eligible,reason"""
        ):
            candidate_retry_state.labels(
                row["eligible"], row["reason"],
            ).set(row["count"])
        for row in await conn.fetch(
            "SELECT status,count(*) AS count FROM failure_themes GROUP BY status"
        ):
            failure_theme_queue.labels(row["status"]).set(row["count"])
        for row in await conn.fetch(
            """SELECT publication_status AS status,count(*) AS count
               FROM okf_bundle_versions GROUP BY publication_status"""
        ):
            okf_bundle_publications.labels(row["status"]).set(row["count"])
        for row in await conn.fetch(
            """SELECT status,routing_enabled,count(*) AS count
               FROM improvement_canaries GROUP BY status,routing_enabled"""
        ):
            canary_routing.labels(
                row["status"], str(row["routing_enabled"]).lower()
            ).set(row["count"])
        quality = await conn.fetchrow(
            """SELECT avg(faithfulness) AS faithfulness,
                      avg(answer_relevancy) AS answer_relevancy,
                      avg(context_recall) AS context_recall,
                      count(*) FILTER (WHERE faithfulness IS NOT NULL
                                       OR answer_relevancy IS NOT NULL
                                       OR context_recall IS NOT NULL) AS samples
               FROM prompt_metrics
               WHERE recorded_at >= now()-interval '7 days'
                 AND metric_source='rag_evaluation'"""
        )
        rag_quality_samples.set(int(quality["samples"] or 0))
        for metric in ("faithfulness", "answer_relevancy", "context_recall"):
            if quality[metric] is not None:
                rag_quality.labels(metric).set(float(quality[metric]))


async def metrics_collection_loop(pool, stop_event: asyncio.Event):
    while not stop_event.is_set():
        await collect_operational_metrics(pool)
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=5)
