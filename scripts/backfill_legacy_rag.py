#!/usr/bin/env python3
import argparse
import asyncio
import os

import asyncpg


SOURCES = {
    "gmail": """
      SELECT id AS source_id,thread_id AS parent_id,subject AS heading,
        concat_ws(E'\n','Subject: '||coalesce(subject,''),'From: '||coalesce(sender,''),
                  'Received: '||coalesce(received_at::text,''),coalesce(body_plain,snippet,'')) AS content,
        jsonb_build_object('sender',sender,'received_at',received_at,'labels',labels) AS metadata,
        embedding,received_at AS modified_at FROM gmail_messages WHERE embedding IS NOT NULL
    """,
    "drive": """
      SELECT id AS source_id,parent_folder AS parent_id,name AS heading,
        concat_ws(E'\n','Title: '||coalesce(name,''),coalesce(content,'')) AS content,
        jsonb_build_object('mime_type',mime_type,'web_view_link',web_view_link,
                           'modified_at',modified_at) AS metadata,
        embedding,modified_at FROM drive_documents WHERE embedding IS NOT NULL
    """,
    "contacts": """
      SELECT id AS source_id,NULL::text AS parent_id,display_name AS heading,
        concat_ws(E'\n','Name: '||coalesce(display_name,''),
                  'Emails: '||coalesce(array_to_string(emails,', '),''),
                  'Organization: '||coalesce(organization,''),'Role: '||coalesce(job_title,'')) AS content,
        jsonb_build_object('emails',emails,'organization',organization,'job_title',job_title) AS metadata,
        embedding,synced_at AS modified_at FROM contacts WHERE embedding IS NOT NULL
    """,
    "chat": """
      SELECT id AS source_id,thread_id AS parent_id,space_name AS heading,
        concat_ws(E'\n','Sender: '||coalesce(sender_email,''),
                  'Time: '||coalesce(created_at::text,''),coalesce(text,'')) AS content,
        jsonb_build_object('space_id',space_id,'thread_id',thread_id,'created_at',created_at) AS metadata,
        embedding,created_at AS modified_at FROM chat_messages WHERE embedding IS NOT NULL
    """,
    "calendar": """
      SELECT id AS source_id,NULL::text AS parent_id,title AS heading,
        concat_ws(E'\n','Title: '||coalesce(title,''),'Start: '||coalesce(start_time::text,''),
                  'End: '||coalesce(end_time::text,''),coalesce(description,'')) AS content,
        jsonb_build_object('start_time',start_time,'end_time',end_time,'attendees',attendees,
                           'meet_link',meet_link,'status',status) AS metadata,
        embedding,start_time AS modified_at FROM calendar_events WHERE embedding IS NOT NULL
    """,
}


async def run(user_id: str, apply: bool, rollback: bool) -> int:
    dsn = os.getenv("NEON_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("NEON_DATABASE_URL or DATABASE_URL is required")
    dsn = dsn.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn)
    try:
        if rollback:
            result = await conn.execute(
                """DELETE FROM rag_chunks WHERE user_id=$1
                   AND chunker_version LIKE 'legacy-import-%'""",
                user_id,
            )
            print(f"rollback_rows={result.rsplit(' ', 1)[-1]}")
            return 0
        total = 0
        for source_type, source_sql in SOURCES.items():
            available = await conn.fetchval(
                f"SELECT count(*) FROM ({source_sql}) source WHERE btrim(content)<>''"
            )
            print(f"source={source_type} available={available}")
            if not apply:
                continue
            result = await conn.execute(
                f"""WITH source AS ({source_sql})
                    INSERT INTO rag_chunks
                      (user_id,source_type,source_id,parent_id,chunk_index,heading,content,
                       content_hash,metadata,acl,embedding,embedding_version,chunker_version,
                       source_modified_at)
                    SELECT $1::text,$2::text,source_id,parent_id,0,heading,content,
                           md5(content),metadata,jsonb_build_object('owner',$1::text),
                           embedding,'nomic-embed-text',
                           'legacy-import-'||$2::text||'-v1',modified_at
                    FROM source WHERE btrim(content)<>''
                    ON CONFLICT(user_id,source_type,source_id,chunker_version,chunk_index)
                    DO NOTHING""",
                user_id, source_type,
            )
            inserted = int(result.rsplit(" ", 1)[-1])
            total += inserted
            print(f"source={source_type} inserted={inserted}")
        if apply:
            owned = await conn.fetchval(
                """SELECT count(*) FROM rag_chunks WHERE user_id=$1
                   AND chunker_version LIKE 'legacy-import-%'""",
                user_id,
            )
            print(f"total_inserted={total} owner_rows={owned}")
        else:
            print("dry_run=true; pass --apply to write or --rollback to remove this import")
        return 0
    finally:
        await conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Assign legacy single-user embeddings to one explicit tenant."
    )
    parser.add_argument("--user-id", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--rollback", action="store_true")
    args = parser.parse_args()
    return asyncio.run(run(args.user_id.lower(), args.apply, args.rollback))


if __name__ == "__main__":
    raise SystemExit(main())
