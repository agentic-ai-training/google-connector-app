import hashlib
import time

from app.db.connection import get_pool


async def retrieve_operational_knowledge(
    query: str, *, run_id: str | None = None, step_id: str | None = None,
    limit: int = 4, include_private: bool = False,
) -> list[dict]:
    """Retrieve trusted operational knowledge separately from tenant content."""
    started = time.perf_counter()
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT d.id,d.title,d.concept_type,d.version,c.heading,c.content,
                      ts_rank_cd(
                        to_tsvector('english',coalesce(d.title,'') || ' ' ||
                          coalesce(c.heading,'') || ' ' || c.content),
                        websearch_to_tsquery('english',$1)
                      ) AS score
               FROM okf_chunks c JOIN okf_documents d ON d.id=c.document_id
               WHERE d.trusted=TRUE
                 AND (d.visibility='public' OR ($3=TRUE AND d.visibility='private'))
                 AND to_tsvector('english',coalesce(d.title,'') || ' ' ||
                       coalesce(c.heading,'') || ' ' || c.content)
                     @@ websearch_to_tsquery('english',$1)
               ORDER BY score DESC,d.id,c.chunk_index LIMIT $2""",
            query, limit, include_private,
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
    return documents


def pack_operational_knowledge(documents: list[dict]) -> str:
    return "\n\n".join(
        f"[OKF {item['id']} v{item['version']} — {item['heading']}]\n{item['content']}"
        for item in documents
    )
