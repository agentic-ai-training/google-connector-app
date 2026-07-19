import asyncio
from contextlib import suppress

from app.config.settings import get_settings


async def apply_retention(pool) -> dict[str, int]:
    """Apply approved privacy windows without touching active OAuth credentials."""
    settings = get_settings()
    statements = [
        ("raw_task_logs", "task_log", "DELETE FROM task_log WHERE executed_at < now()-($1 * interval '1 day')", settings.raw_telemetry_retention_days),
        ("raw_conversations", "conversation_history", "DELETE FROM conversation_history WHERE created_at < now()-($1 * interval '1 day')", settings.raw_telemetry_retention_days),
        ("expired_run_events", "agent_run_events", "DELETE FROM agent_run_events WHERE created_at < now()-($1 * interval '1 day')", settings.workflow_retention_days),
        ("raw_embedding_payloads", "embedding_jobs", "DELETE FROM embedding_jobs WHERE completed_at IS NOT NULL AND completed_at < now()-($1 * interval '1 day')", settings.raw_telemetry_retention_days),
        ("expired_rag", "rag_chunks", "UPDATE rag_chunks SET deleted_at=now() WHERE deleted_at IS NULL AND indexed_at < now()-($1 * interval '1 day')", settings.workflow_retention_days),
        ("expired_runs", "agent_runs", "UPDATE agent_runs SET deleted_at=now() WHERE deleted_at IS NULL AND retention_until < now() AND status NOT IN ('queued','running','awaiting_approval')", 0),
    ]
    report = {}
    async with pool.acquire() as conn, conn.transaction():
        for policy, table, sql, days in statements:
            status = (
                await conn.execute(sql, days)
                if "$1" in sql else await conn.execute(sql)
            )
            affected = int(status.rsplit(" ", 1)[-1])
            report[policy] = affected
            await conn.execute(
                """INSERT INTO retention_audit(policy_name,table_name,affected_rows,action)
                   VALUES($1,$2,$3,$4)""",
                policy, table, affected, sql.split(" ", 1)[0].lower(),
            )
    return report


async def retention_loop(pool, stop_event: asyncio.Event):
    while not stop_event.is_set():
        await apply_retention(pool)
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=3600)


async def delete_user_data(pool, user_id: str) -> dict[str, int]:
    """Delete tenant content while preserving a minimal deletion audit record."""
    report = {}
    async with pool.acquire() as conn, conn.transaction():
        request_id = await conn.fetchval(
            "INSERT INTO deletion_requests(user_id,status) VALUES($1,'running') RETURNING id",
            user_id,
        )
        for table in (
            "rag_chunks", "feedback", "conversation_history", "agent_runs",
            "google_oauth_credentials",
        ):
            status = await conn.execute(f"DELETE FROM {table} WHERE user_id=$1", user_id)
            report[table] = int(status.rsplit(" ", 1)[-1])
        await conn.execute(
            """UPDATE deletion_requests SET status='completed',completed_at=now(),report=$1::jsonb
               WHERE id=$2""",
            __import__("json").dumps(report), request_id,
        )
    return report
