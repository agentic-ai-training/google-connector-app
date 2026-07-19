import asyncio
import hashlib
import json
from contextlib import suppress

from app.rag.embedder import NomicEmbedder
from app.rag.ingestion import TOOL_SOURCES, index_tool_result


async def enqueue_tool_result(name, args, result, pool, user_id) -> bool:
    source_type = TOOL_SOURCES.get(name)
    if not source_type or not user_id:
        return False
    serialized = json.dumps(
        {"tool": name, "args": args, "result": result}, default=str,
        separators=(",", ":"),
    )
    content_hash = hashlib.sha256(serialized.encode()).hexdigest()
    source_id = hashlib.sha256(
        json.dumps(args, sort_keys=True, default=str).encode()
    ).hexdigest()
    async with pool.acquire() as conn:
        status = await conn.execute(
            """INSERT INTO embedding_jobs
               (user_id,source_type,source_id,payload,content_hash)
               VALUES($1,$2,$3,$4::jsonb,$5)
               ON CONFLICT(user_id,source_type,source_id,content_hash) DO NOTHING""",
            user_id, source_type, source_id, serialized, content_hash,
        )
    return status.endswith("1")


async def _claim(pool):
    async with pool.acquire() as conn, conn.transaction():
        return await conn.fetchrow(
            """UPDATE embedding_jobs SET status='running',attempt_count=attempt_count+1,
                 lease_expires_at=now()+interval '2 minutes'
               WHERE id=(SELECT id FROM embedding_jobs
                 WHERE ((status IN ('queued','failed') AND available_at<=now()) OR
                        (status='running' AND lease_expires_at<now()))
                 ORDER BY available_at FOR UPDATE SKIP LOCKED LIMIT 1)
               RETURNING *"""
        )


async def embedding_worker_loop(pool, stop_event: asyncio.Event):
    embedder = NomicEmbedder()
    while not stop_event.is_set():
        job = await _claim(pool)
        if not job:
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=2)
            continue
        payload = job["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        try:
            async with asyncio.timeout(90):
                await index_tool_result(
                    payload["tool"], payload["args"], payload["result"],
                    pool, embedder, job["user_id"],
                )
            async with pool.acquire() as conn:
                await conn.execute(
                    """UPDATE embedding_jobs SET status='completed',completed_at=now(),
                       lease_expires_at=NULL,error_message=NULL WHERE id=$1""",
                    job["id"],
                )
        except Exception as exc:
            exhausted = job["attempt_count"] >= job["max_attempts"]
            async with pool.acquire() as conn:
                await conn.execute(
                    """UPDATE embedding_jobs SET status=$1,error_message=$2,
                       available_at=now()+((attempt_count * 30) * interval '1 second'),
                       lease_expires_at=NULL WHERE id=$3""",
                    "dead_letter" if exhausted else "failed", str(exc)[:2000], job["id"],
                )
