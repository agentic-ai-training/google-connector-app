import json

from app.mlops.metrics import empty_context
from app.rag.embedder import NomicEmbedder


async def hybrid_retrieve(query, pool=None, filters=None, top_k=5, user_id=None):
    if not user_id:
        return []
    if pool is None:
        from app.db.connection import get_pool
        pool = await get_pool()
    vector = await NomicEmbedder().aembed_query(query)
    filters = filters or {}
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id,source_type AS source,source_id,parent_id,content,
                      1-(embedding<=>$1) AS score,metadata
               FROM rag_chunks
               WHERE user_id=$2 AND embedding IS NOT NULL AND deleted_at IS NULL
                 AND ($3::text IS NULL OR source_type=$3)
                 AND 1-(embedding<=>$1) >= $4
               ORDER BY embedding<=>$1 LIMIT $5""",
            vector, user_id, filters.get("source"),
            float(filters.get("minimum_score", 0.35)), top_k * 3,
        )
    results = []
    seen = set()
    for row in rows:
        item = dict(row)
        if isinstance(item.get("metadata"), str):
            item["metadata"] = json.loads(item["metadata"])
        duplicate_key = (item["source"], item["source_id"], item["content"][:200])
        if duplicate_key in seen:
            continue
        seen.add(duplicate_key)
        results.append(item)
        if len(results) >= top_k:
            break
    if not results:
        empty_context.inc()
    return results
