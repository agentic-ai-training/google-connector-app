import asyncio
from contextlib import suppress

from app.mlops.metrics import (
    artifact_cleanup_queue,
    embedding_queue,
    improvement_queue,
    improvement_notifications,
    failure_notifications,
    failure_review_queue,
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
    "awaiting_promotion", "approved_for_publication", "rolled_back",
)
CLEANUP_STATES = (
    "awaiting_confirmation", "approved", "rejected", "executing", "completed",
    "failed", "manual_required",
)
NOTIFICATION_CHANNELS = ("admin", "grafana", "email", "github")
NOTIFICATION_STATES = ("queued", "sent", "skipped", "failed")


async def collect_operational_metrics(pool):
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
