import asyncio
from contextlib import suppress

from app.mlops.metrics import (
    embedding_queue,
    improvement_queue,
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


async def collect_operational_metrics(pool):
    for state in RUN_STATES:
        run_queue_depth.labels(state).set(0)
    for state in EMBEDDING_STATES:
        embedding_queue.labels(state).set(0)
    for state in PROPOSAL_STATES:
        improvement_queue.labels(state).set(0)
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


async def metrics_collection_loop(pool, stop_event: asyncio.Event):
    while not stop_event.is_set():
        await collect_operational_metrics(pool)
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=5)
