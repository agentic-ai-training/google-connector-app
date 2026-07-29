"""Explicit, tenant-scoped source-aware RAG backfill.

OAuth access is not treated as blanket training consent.  Callers must invoke
the authenticated sync endpoint, which creates only tenant-owned embedding jobs.
The jobs reuse the same source-aware chunkers as live tool-result ingestion.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from datetime import datetime, timedelta, timezone

from app.db import google_clients as google
from app.db.google_clients import request_google_credentials
from app.db.oauth_credentials import load_google_credentials
from app.rag.jobs import enqueue_tool_result
from app.tools.registry import _gmail


ALLOWED_SOURCES = frozenset({"gmail", "drive", "calendar"})


def _gmail_items(limit: int) -> list[tuple[str, dict, dict]]:
    ids = google.gmail_service.users().messages().list(
        userId="me", maxResults=limit,
    ).execute().get("messages", [])
    return [
        (
            "get_gmail_message",
            {"message_id": item["id"]},
            _gmail(
                google.gmail_service.users().messages().get(
                    userId="me", id=item["id"], format="full",
                ).execute()
            ),
        )
        for item in ids
    ]


def _drive_items(limit: int) -> list[tuple[str, dict, dict]]:
    files = google.drive_service.files().list(
        q="trashed=false",
        pageSize=limit,
        orderBy="modifiedTime desc",
        fields=(
            "files(id,name,mimeType,webViewLink,modifiedTime,createdTime,"
            "parents,owners,shared,size,trashed)"
        ),
    ).execute().get("files", [])
    items = []
    for metadata in files:
        content = ""
        mime_type = metadata.get("mimeType")
        try:
            if mime_type == "application/vnd.google-apps.document":
                content = google.drive_service.files().export(
                    fileId=metadata["id"], mimeType="text/plain",
                ).execute().decode(errors="replace")
            elif mime_type == "application/vnd.google-apps.spreadsheet":
                content = google.drive_service.files().export(
                    fileId=metadata["id"], mimeType="text/csv",
                ).execute().decode(errors="replace")
            elif str(mime_type).startswith("text/"):
                content = google.drive_service.files().get_media(
                    fileId=metadata["id"],
                ).execute().decode(errors="replace")
        except Exception:
            # Metadata remains useful and a single unexportable file must not
            # suppress the remainder of the explicitly requested sync.
            content = ""
        items.append((
            "get_drive_file",
            {"file_id": metadata["id"]},
            {"metadata": metadata, "content": content},
        ))
    return items


def _calendar_items(limit: int) -> list[tuple[str, dict, dict]]:
    now = datetime.now(timezone.utc)
    events = google.calendar_service.events().list(
        calendarId="primary",
        timeMin=(now - timedelta(days=365)).isoformat(),
        timeMax=(now + timedelta(days=365)).isoformat(),
        singleEvents=True,
        orderBy="updated",
        maxResults=limit,
    ).execute().get("items", [])
    return [
        (
            "get_calendar_event",
            {"event_id": event["id"], "calendar_id": "primary"},
            event,
        )
        for event in events
    ]


COLLECTORS = {
    "gmail": _gmail_items,
    "drive": _drive_items,
    "calendar": _calendar_items,
}


async def sync_user_sources(
    pool,
    *,
    user_id: str,
    sources: list[str],
    max_items_per_source: int,
) -> dict:
    """Read bounded recent sources and enqueue tenant-owned source-aware indexing."""
    selected = list(dict.fromkeys(source.casefold() for source in sources))
    invalid = sorted(set(selected) - ALLOWED_SOURCES)
    if invalid:
        raise ValueError(f"Unsupported RAG sources: {', '.join(invalid)}")
    limit = max(1, min(int(max_items_per_source), 100))
    credentials = await load_google_credentials(pool, user_id)
    if credentials is None:
        raise ValueError("Google authorization is unavailable")
    token = request_google_credentials.set(credentials)
    try:
        collected = {
            source: await asyncio.to_thread(COLLECTORS[source], limit)
            for source in selected
        }
    finally:
        request_google_credentials.reset(token)

    queued = {}
    skipped = {}
    for source, items in collected.items():
        queued[source] = 0
        skipped[source] = 0
        for tool_name, arguments, result in items:
            accepted = await enqueue_tool_result(
                tool_name, arguments, result, pool, user_id,
            )
            if accepted:
                queued[source] += 1
            else:
                skipped[source] += 1
    return {
        "sources": selected,
        "collected": {
            source: len(items) for source, items in collected.items()
        },
        "queued": queued,
        "skipped_existing_or_oversize": skipped,
        "max_items_per_source": limit,
    }


async def requeue_timestamp_failed_jobs(pool, user_id: str) -> int:
    """Requeue only the known pre-fix asyncpg timestamp failure for this tenant."""
    async with pool.acquire() as conn:
        status = await conn.execute(
            """UPDATE embedding_jobs
               SET status='failed',attempt_count=0,available_at=now(),
                   lease_expires_at=NULL,error_message=NULL
               WHERE user_id=$1 AND status='dead_letter'
                 AND error_message LIKE
                   '%expected a datetime.date or datetime.datetime instance%'""",
            user_id,
        )
    return int(status.rsplit(" ", 1)[-1])


async def enqueue_user_sync(
    pool,
    *,
    user_id: str,
    sources: list[str],
    max_items_per_source: int,
    requeue_known_failures: bool,
) -> dict:
    """Persist explicit indexing consent and return without holding an HTTP request."""
    selected = list(dict.fromkeys(source.casefold() for source in sources))
    invalid = sorted(set(selected) - ALLOWED_SOURCES)
    if invalid:
        raise ValueError(f"Unsupported RAG sources: {', '.join(invalid)}")
    limit = max(1, min(int(max_items_per_source), 100))
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext($1))",
            f"rag-source-sync:{user_id}",
        )
        existing = await conn.fetchrow(
            """SELECT id,status,sources,max_items_per_source,created_at
               FROM rag_source_sync_jobs
               WHERE user_id=$1 AND status IN ('queued','running','failed')
               ORDER BY created_at DESC LIMIT 1""",
            user_id,
        )
        if existing:
            return {**dict(existing), "created": False}
        row = await conn.fetchrow(
            """INSERT INTO rag_source_sync_jobs
               (user_id,sources,max_items_per_source,requeue_known_failures)
               VALUES($1,$2,$3,$4)
               RETURNING id,status,sources,max_items_per_source,created_at""",
            user_id, selected, limit, requeue_known_failures,
        )
    return {**dict(row), "created": True}


async def _claim_user_sync(pool):
    async with pool.acquire() as conn, conn.transaction():
        return await conn.fetchrow(
            """UPDATE rag_source_sync_jobs
               SET status='running',attempt_count=attempt_count+1,
                   started_at=COALESCE(started_at,now()),
                   lease_expires_at=now()+interval '12 minutes'
               WHERE id=(
                 SELECT id FROM rag_source_sync_jobs
                 WHERE (
                   status IN ('queued','failed') AND available_at<=now()
                 ) OR (
                   status='running' AND lease_expires_at<now()
                 )
                 ORDER BY available_at,created_at
                 FOR UPDATE SKIP LOCKED LIMIT 1
               )
               RETURNING *"""
        )


async def rag_source_sync_worker_loop(pool, stop_event: asyncio.Event):
    """Run explicit source backfills durably and independently of the browser."""
    while not stop_event.is_set():
        job = await _claim_user_sync(pool)
        if not job:
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=2)
            continue
        try:
            async with asyncio.timeout(10 * 60):
                requeued = (
                    await requeue_timestamp_failed_jobs(pool, job["user_id"])
                    if job["requeue_known_failures"] else 0
                )
                result = await sync_user_sources(
                    pool,
                    user_id=job["user_id"],
                    sources=list(job["sources"]),
                    max_items_per_source=job["max_items_per_source"],
                )
                result["requeued_known_failures"] = requeued
            async with pool.acquire() as conn:
                await conn.execute(
                    """UPDATE rag_source_sync_jobs
                       SET status='completed',result=$1::jsonb,completed_at=now(),
                           lease_expires_at=NULL,error_message=NULL
                       WHERE id=$2""",
                    json.dumps(result, default=str), job["id"],
                )
        except Exception as exc:
            exhausted = job["attempt_count"] >= job["max_attempts"]
            async with pool.acquire() as conn:
                await conn.execute(
                    """UPDATE rag_source_sync_jobs
                       SET status=$1,error_message=$2,
                           available_at=now()+((attempt_count * 60) * interval '1 second'),
                           lease_expires_at=NULL
                       WHERE id=$3""",
                    "dead_letter" if exhausted else "failed",
                    str(exc)[:2000], job["id"],
                )


async def user_rag_status(pool, user_id: str) -> dict:
    async with pool.acquire() as conn:
        sources = await conn.fetch(
            """SELECT source_type,count(*)::integer AS chunks,
                      count(embedding)::integer AS embedded_chunks,
                      array_agg(DISTINCT chunker_version ORDER BY chunker_version)
                        AS chunker_versions
               FROM rag_chunks
               WHERE user_id=$1 AND deleted_at IS NULL
               GROUP BY source_type ORDER BY source_type""",
            user_id,
        )
        jobs = await conn.fetch(
            """SELECT status,count(*)::integer AS count
               FROM embedding_jobs WHERE user_id=$1 GROUP BY status""",
            user_id,
        )
        parents = await conn.fetchval(
            """SELECT count(*) FROM rag_parent_sections
               WHERE user_id=$1 AND deleted_at IS NULL""",
            user_id,
        )
        sync_job = await conn.fetchrow(
            """SELECT id,status,sources,max_items_per_source,result,error_message,
                      created_at,started_at,completed_at
               FROM rag_source_sync_jobs
               WHERE user_id=$1 ORDER BY created_at DESC LIMIT 1""",
            user_id,
        )
    return {
        "ready": bool(sources),
        "sources": [dict(item) for item in sources],
        "jobs": {item["status"]: item["count"] for item in jobs},
        "parent_sections": int(parents or 0),
        "latest_sync": dict(sync_job) if sync_job else None,
    }
