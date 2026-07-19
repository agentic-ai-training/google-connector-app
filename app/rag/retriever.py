import json

from app.mlops.metrics import empty_context
from app.rag.embedder import NomicEmbedder


async def hybrid_retrieve(query, pool=None, filters=None, top_k=5, user_id=None):
    if not user_id:
        return []
    if pool is None:
        from app.db.connection import get_pool
        pool = await get_pool()
    filters = filters or {}
    vector = None
    try:
        vector = await NomicEmbedder().aembed_query(query)
    except Exception:
        # PostgreSQL full-text retrieval remains available while Ollama is cold.
        vector = None
    async with pool.acquire() as conn:
        dense_rows = []
        if vector is not None:
            dense_rows = await conn.fetch(
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
        lexical_rows = await conn.fetch(
            """SELECT id,source_type AS source,source_id,parent_id,content,
                      ts_rank_cd(to_tsvector('english',content),
                                 websearch_to_tsquery('english',$1)) AS score,metadata
               FROM rag_chunks
               WHERE user_id=$2 AND deleted_at IS NULL
                 AND ($3::text IS NULL OR source_type=$3)
                 AND to_tsvector('english',content) @@ websearch_to_tsquery('english',$1)
               ORDER BY score DESC LIMIT $4""",
            query, user_id, filters.get("source"), top_k * 3,
        )
    fused = {}
    for channel, rows in (("dense", dense_rows), ("lexical", lexical_rows)):
        for rank, row in enumerate(rows, 1):
            item = dict(row)
            key = str(item["id"])
            current = fused.setdefault(key, {**item, "fusion_score": 0.0, "channels": []})
            current["fusion_score"] += 1.0 / (60 + rank)
            current["channels"].append(channel)
            current["score"] = max(float(current.get("score") or 0),
                                   float(item.get("score") or 0))
    ranked = sorted(fused.values(), key=lambda item: (
        item["fusion_score"], item["score"]
    ), reverse=True)
    results = []
    seen = set()
    selected_sources = {}
    for item in ranked:
        if isinstance(item.get("metadata"), str):
            item["metadata"] = json.loads(item["metadata"])
        duplicate_key = (item["source"], item["source_id"], item["content"][:200])
        if duplicate_key in seen:
            continue
        # Preserve source diversity so one long Gmail thread cannot consume all context.
        if selected_sources.get(item["source_id"], 0) >= int(filters.get("per_source_limit", 2)):
            continue
        seen.add(duplicate_key)
        selected_sources[item["source_id"]] = selected_sources.get(item["source_id"], 0) + 1
        item["citation"] = {
            "source": item["source"], "source_id": item["source_id"],
            "chunk_id": str(item["id"]), "parent_id": item.get("parent_id"),
        }
        results.append(item)
        if len(results) >= top_k:
            break
    if not results:
        empty_context.inc()
    return results
