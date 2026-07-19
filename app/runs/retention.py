import asyncio
import json
from contextlib import suppress
from datetime import datetime, timezone

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
        deletion_queries = (
            ("rag_chunks", "DELETE FROM rag_chunks WHERE user_id=$1"),
            ("feedback", "DELETE FROM feedback WHERE user_id=$1"),
            ("conversation_history", "DELETE FROM conversation_history WHERE user_id=$1"),
            ("agent_runs", "DELETE FROM agent_runs WHERE user_id=$1"),
            ("google_oauth_credentials", "DELETE FROM google_oauth_credentials WHERE user_id=$1"),
        )
        for table, query in deletion_queries:
            status = await conn.execute(query, user_id)
            report[table] = int(status.rsplit(" ", 1)[-1])
        await conn.execute(
            """UPDATE deletion_requests SET status='completed',completed_at=now(),report=$1::jsonb
               WHERE id=$2""",
            __import__("json").dumps(report), request_id,
        )
    return report


async def export_user_data(pool, user_id: str) -> dict:
    """Return a tenant-scoped portable export without OAuth secrets or embeddings."""
    async with pool.acquire() as conn:
        runs = [dict(row) for row in await conn.fetch(
            """SELECT * FROM agent_runs WHERE user_id=$1 ORDER BY queued_at
               LIMIT 10000""", user_id,
        )]
        run_ids = [row["id"] for row in runs]
        conversations = [dict(row) for row in await conn.fetch(
            """SELECT * FROM conversation_history WHERE user_id=$1
               ORDER BY created_at LIMIT 50000""", user_id,
        )]
        feedback = [dict(row) for row in await conn.fetch(
            "SELECT * FROM feedback WHERE user_id=$1 ORDER BY created_at LIMIT 10000",
            user_id,
        )]
        rag = [dict(row) for row in await conn.fetch(
            """SELECT id,source_type,source_id,parent_id,chunk_index,heading,content,
                      content_hash,metadata,acl,embedding_version,chunker_version,
                      sync_version,source_modified_at,indexed_at,deleted_at
               FROM rag_chunks WHERE user_id=$1 ORDER BY indexed_at LIMIT 50000""",
            user_id,
        )]
        steps = events = artifacts = trajectories = []
        if run_ids:
            steps = [dict(row) for row in await conn.fetch(
                "SELECT * FROM agent_run_steps WHERE run_id=ANY($1::uuid[]) ORDER BY run_id,sequence_no",
                run_ids,
            )]
            events = [dict(row) for row in await conn.fetch(
                "SELECT * FROM agent_run_events WHERE run_id=ANY($1::uuid[]) ORDER BY id",
                run_ids,
            )]
            artifacts = [dict(row) for row in await conn.fetch(
                "SELECT * FROM agent_artifacts WHERE run_id=ANY($1::uuid[]) ORDER BY created_at",
                run_ids,
            )]
            trajectories = [dict(row) for row in await conn.fetch(
                """SELECT * FROM learning_trajectories
                   WHERE run_id=ANY($1::uuid[]) ORDER BY created_at""", run_ids,
            )]
        google_connected = bool(await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM google_oauth_credentials WHERE user_id=$1)", user_id,
        ))
    payload = {
        "format": "google-connector-user-export-v1",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "user_id": user_id,
        "google_connected": google_connected,
        "oauth_credentials_excluded": True,
        "runs": runs, "steps": steps, "events": events, "artifacts": artifacts,
        "conversations": conversations, "feedback": feedback,
        "rag_chunks_without_embeddings": rag, "learning_trajectories": trajectories,
    }
    # Normalize UUID/datetime/Decimal values before the API serializes the payload.
    return json.loads(json.dumps(payload, default=str))
