import hashlib
import time

from app.db.connection import get_pool


async def retrieve_operational_knowledge(
    query: str, *, run_id: str | None = None, step_id: str | None = None,
    limit: int = 4, include_private: bool = False,
    operational_tags: list[str] | None = None,
) -> list[dict]:
    """Retrieve trusted operational knowledge separately from tenant content."""
    started = time.perf_counter()
    tags = list(dict.fromkeys(
        str(value).casefold().strip()
        for value in (operational_tags or [])
        if str(value).strip()
    ))[:30]
    pool = await get_pool()
    async with pool.acquire() as conn:
        bundle_hash = None
        if run_id:
            bundle_hash = await conn.fetchval(
                "SELECT okf_bundle_version FROM agent_runs WHERE id=$1", run_id,
            )
        if bundle_hash:
            rows = await conn.fetch(
                """SELECT d.document_id AS id,d.title,d.concept_type,d.version,
                          c.heading,c.content,d.metadata->'tags' AS matched_tags,
                          ts_rank_cd(to_tsvector('english',d.title||' '||
                            coalesce(c.heading,'')||' '||c.content),
                            websearch_to_tsquery('english',$1)) AS score
                   FROM okf_bundle_chunks c JOIN okf_bundle_documents d
                     ON d.bundle_hash=c.bundle_hash AND d.document_id=c.document_id
                   WHERE d.bundle_hash=$2
                     AND (d.visibility='public' OR ($4=TRUE AND d.visibility='private'))
                     AND ((d.metadata->'tags') ?| $5::text[] OR
                       to_tsvector('english',d.title||' '||coalesce(c.heading,'')||' '||c.content)
                         @@ websearch_to_tsquery('english',$1))
                   ORDER BY ((d.metadata->'tags') ?| $5::text[]) DESC,
                            score DESC,d.document_id,c.chunk_index LIMIT $3""",
                query, bundle_hash, limit, include_private, tags,
            )
        else:
            rows = await conn.fetch(
            """SELECT d.id,d.title,d.concept_type,d.version,c.heading,c.content,
                      to_jsonb(d.tags) AS matched_tags,
                      ts_rank_cd(
                        to_tsvector('english',coalesce(d.title,'') || ' ' ||
                          coalesce(c.heading,'') || ' ' || c.content),
                        websearch_to_tsquery('english',$1)
                      ) AS score
               FROM okf_chunks c JOIN okf_documents d ON d.id=c.document_id
               WHERE d.trusted=TRUE
                 AND (d.visibility='public' OR ($3=TRUE AND d.visibility='private'))
                 AND (d.tags && $4::text[] OR
                   to_tsvector('english',coalesce(d.title,'') || ' ' ||
                       coalesce(c.heading,'') || ' ' || c.content)
                     @@ websearch_to_tsquery('english',$1))
               ORDER BY (d.tags && $4::text[]) DESC,
                        score DESC,d.id,c.chunk_index LIMIT $2""",
            query, limit, include_private, tags,
            )
        documents = [dict(row) for row in rows]
        if run_id:
            await conn.execute(
                """INSERT INTO okf_retrieval_events
                   (run_id,step_id,document_ids,okf_versions,query_hash,duration_ms)
                   VALUES($1,$2,$3,$4,$5,$6)""",
                run_id, step_id,
                list(dict.fromkeys(item["id"] for item in documents)),
                list(dict.fromkeys(item["version"] for item in documents)),
                hashlib.sha256(query.encode()).hexdigest(),
                int((time.perf_counter() - started) * 1000),
            )
            await conn.execute(
                """INSERT INTO agent_run_events
                   (run_id,step_id,user_id,event_type,phase,message,payload)
                   SELECT $1,$2,user_id,'okf_context_selected','knowledge',
                          'Version-pinned operational knowledge selected',
                          jsonb_build_object(
                            'document_ids',$3::text[],
                            'operational_tags',$4::text[],
                            'selection_policy','okf-structured-tags-v1'
                          )
                     FROM agent_runs WHERE id=$1""",
                run_id, step_id,
                list(dict.fromkeys(item["id"] for item in documents)),
                tags,
            )
    return documents


def pack_operational_knowledge(documents: list[dict]) -> str:
    return "\n\n".join(
        f"[OKF {item['id']} v{item['version']} — {item['heading']}]\n{item['content']}"
        for item in documents
    )
