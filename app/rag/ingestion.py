import hashlib
import json
import uuid

from app.rag.chunking import chunks_for_source

TOOL_SOURCES = {
    "search_gmail": "gmail", "get_gmail_message": "gmail",
    "search_drive": "drive", "get_drive_file": "drive",
    "read_google_doc": "docs", "read_google_sheet": "sheets",
    "list_calendar_events": "calendar", "get_calendar_event": "calendar",
    "list_chat_spaces": "chat", "search_contacts": "contacts",
    "get_contact": "contacts", "list_tasks": "tasks",
    "list_meet_conferences": "meet", "list_meet_participants": "meet",
}

CHUNKER_VERSIONS = {
    "gmail": "gmail-v3-parent", "drive": "drive-v3-parent",
    "docs": "docs-v3-parent",
    "sheets": "sheets-v2", "calendar": "calendar-v2", "chat": "chat-v2",
    "contacts": "contacts-v2", "tasks": "tasks-v2", "meet": "meet-v2",
    "pdf": "pdf-layout-v2", "meet_transcript": "meet-transcript-v2",
}


def _items(result):
    if isinstance(result, list):
        return [item if isinstance(item, dict) else {"content": str(item)} for item in result]
    if isinstance(result, dict):
        for key in ("messages", "files", "items", "results", "values"):
            value = result.get(key)
            if isinstance(value, list):
                if key == "values":
                    return [{"values": value}]
                return [item if isinstance(item, dict) else {"content": str(item)} for item in value]
        return [result]
    return [{"content": str(result)}]


async def index_tool_result(name, args, result, pool, embedder, user_id):
    source_type = TOOL_SOURCES.get(name)
    if not source_type or not user_id:
        return 0
    candidates = []
    parents = {}
    for item_index, item in enumerate(_items(result)):
        source_id = str(
            item.get("id") or item.get("spreadsheetId") or item.get("documentId")
            or args.get("message_id") or args.get("file_id") or args.get("document_id")
            or args.get("spreadsheet_id") or uuid.uuid5(
                uuid.NAMESPACE_URL, f"{name}:{json.dumps(args, sort_keys=True, default=str)}:{item_index}"
            )
        )
        for chunk in chunks_for_source(source_type, item):
            candidates.append((source_id, chunk))
            if chunk.parent_id and chunk.parent_content:
                key = (source_id, chunk.parent_id)
                parents[key] = {
                    "heading": chunk.parent_heading or chunk.heading,
                    "content": chunk.parent_content,
                    "content_hash": hashlib.sha256(
                        chunk.parent_content.encode()
                    ).hexdigest(),
                    "metadata": {
                        "source": source_type,
                        "parent_index": chunk.metadata.get("parent_index"),
                        "section_index": chunk.metadata.get("section_index"),
                        "thread_id": chunk.metadata.get("thread_id"),
                        "source_modified_at": (
                            chunk.metadata.get("received_at")
                            or chunk.metadata.get("modified_time")
                        ),
                    },
                }
    if not candidates:
        return 0
    chunker_version = CHUNKER_VERSIONS.get(source_type, f"{source_type}-v2")
    async with pool.acquire() as conn:
        existing = await conn.fetch(
            """SELECT id,source_id,chunk_index,content_hash FROM rag_chunks
               WHERE user_id=$1 AND source_type=$2 AND chunker_version=$3
                 AND source_id=ANY($4::text[]) AND deleted_at IS NULL""",
            user_id, source_type, chunker_version,
            list(dict.fromkeys(item[0] for item in candidates)),
        )
        existing_parents = await conn.fetch(
            """SELECT id,source_id,parent_id,content_hash FROM rag_parent_sections
               WHERE user_id=$1 AND source_type=$2 AND chunker_version=$3
                 AND source_id=ANY($4::text[]) AND deleted_at IS NULL""",
            user_id, source_type, chunker_version,
            list(dict.fromkeys(item[0] for item in candidates)),
        )
    hashes = {(row["source_id"], row["chunk_index"]): row["content_hash"] for row in existing}
    changed = [
        (source_id, chunk) for source_id, chunk in candidates
        if hashes.get((source_id, chunk.index)) != chunk.content_hash
    ]
    candidate_keys = {(source_id, chunk.index) for source_id, chunk in candidates}
    removed_chunk_ids = [
        row["id"] for row in existing
        if (row["source_id"], row["chunk_index"]) not in candidate_keys
    ]
    parent_hashes = {
        (row["source_id"], row["parent_id"]): row["content_hash"]
        for row in existing_parents
    }
    changed_parents = {
        key: value for key, value in parents.items()
        if parent_hashes.get(key) != value["content_hash"]
    }
    removed_parent_ids = [
        row["id"] for row in existing_parents
        if (row["source_id"], row["parent_id"]) not in parents
    ]
    if not changed and not changed_parents and not removed_chunk_ids and not removed_parent_ids:
        return 0
    vectors = (
        await embedder.aembed_documents([chunk.content for _, chunk in changed])
        if changed else []
    )
    rows = []
    for (source_id, chunk), vector in zip(changed, vectors, strict=True):
        rows.append((
            user_id, source_type, source_id, chunk.parent_id, chunk.index, chunk.heading,
            chunk.content, chunk.content_hash, json.dumps(chunk.metadata, default=str),
            json.dumps({"owner": user_id}), vector, "nomic-embed-text", chunker_version,
            chunk.metadata.get("received_at") or chunk.metadata.get("modified_time"),
        ))
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            """UPDATE rag_chunks SET deleted_at=now()
               WHERE user_id=$1 AND source_type=$2 AND source_id=ANY($3::text[])
                 AND chunker_version<>$4 AND deleted_at IS NULL""",
            user_id, source_type,
            list(dict.fromkeys(item[0] for item in candidates)), chunker_version,
        )
        await conn.execute(
            """UPDATE rag_parent_sections SET deleted_at=now()
               WHERE user_id=$1 AND source_type=$2 AND source_id=ANY($3::text[])
                 AND chunker_version<>$4 AND deleted_at IS NULL""",
            user_id, source_type,
            list(dict.fromkeys(item[0] for item in candidates)), chunker_version,
        )
        if removed_chunk_ids:
            await conn.execute(
                "UPDATE rag_chunks SET deleted_at=now() WHERE id=ANY($1::uuid[])",
                removed_chunk_ids,
            )
        if removed_parent_ids:
            await conn.execute(
                """UPDATE rag_parent_sections SET deleted_at=now()
                   WHERE id=ANY($1::uuid[])""",
                removed_parent_ids,
            )
        if parents:
            await conn.executemany(
                """INSERT INTO rag_parent_sections
                   (user_id,source_type,source_id,parent_id,heading,content,content_hash,
                    metadata,acl,chunker_version,source_modified_at)
                   VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9::jsonb,$10,$11::timestamptz)
                   ON CONFLICT(user_id,source_type,source_id,parent_id,chunker_version)
                   DO UPDATE SET heading=excluded.heading,content=excluded.content,
                     content_hash=excluded.content_hash,metadata=excluded.metadata,
                     acl=excluded.acl,indexed_at=now(),deleted_at=NULL""",
                [(
                    user_id, source_type, source_id, parent_id, parent["heading"],
                    parent["content"], parent["content_hash"],
                    json.dumps(parent["metadata"], default=str),
                    json.dumps({"owner": user_id}), chunker_version,
                    parent["metadata"].get("source_modified_at"),
                ) for (source_id, parent_id), parent in parents.items()],
            )
        if rows:
            await conn.executemany(
            """INSERT INTO rag_chunks
               (user_id,source_type,source_id,parent_id,chunk_index,heading,content,
                content_hash,metadata,acl,embedding,embedding_version,chunker_version,
                source_modified_at)
               VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10::jsonb,$11,$12,$13,$14::timestamptz)
               ON CONFLICT(user_id,source_type,source_id,chunker_version,chunk_index)
               DO UPDATE SET parent_id=excluded.parent_id,heading=excluded.heading,
                 content=excluded.content,content_hash=excluded.content_hash,
                 metadata=excluded.metadata,acl=excluded.acl,embedding=excluded.embedding,
                 embedding_version=excluded.embedding_version,indexed_at=now(),deleted_at=NULL""",
                rows,
            )
    return len(rows) + len(changed_parents)
