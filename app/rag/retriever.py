import json
from datetime import datetime, timezone

from app.mlops.metrics import empty_context
from app.rag.embedder import NomicEmbedder


def _recency_bonus(item: dict) -> float:
    """Small bounded tie-breaker; relevance channels remain authoritative."""
    value = item.get("source_modified_at")
    metadata = item.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (TypeError, ValueError):
            metadata = {}
    value = value or metadata.get("received_at") or metadata.get("modified_time")
    if not value:
        return 0.0
    try:
        moment = value if isinstance(value, datetime) else datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        age_days = max(0.0, (datetime.now(timezone.utc) - moment).total_seconds() / 86400)
    except (TypeError, ValueError):
        return 0.0
    return 0.003 / (1.0 + age_days / 30.0)


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
                          1-(embedding<=>$1) AS score,metadata,source_modified_at,
                          chunker_version
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
                                 websearch_to_tsquery('english',$1)) AS score,metadata,
                      source_modified_at,chunker_version
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
    for item in fused.values():
        item["recency_bonus"] = _recency_bonus(item)
        item["ranking_score"] = item["fusion_score"] + item["recency_bonus"]
    ranked = sorted(fused.values(), key=lambda item: (
        item["ranking_score"], item["score"]
    ), reverse=True)
    results = []
    seen = set()
    selected_sources = {}
    for item in ranked:
        if isinstance(item.get("metadata"), str):
            item["metadata"] = json.loads(item["metadata"])
        duplicate_key = (
            item["source"], item["source_id"],
            item.get("parent_id") or item["content"][:200],
        )
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
    wanted = [item for item in results if item.get("parent_id")]
    if wanted and filters.get("expand_parents", True):
        async with pool.acquire() as conn:
            parent_rows = await conn.fetch(
                """WITH wanted(source_type,source_id,parent_id,chunker_version) AS (
                     SELECT * FROM unnest($2::text[],$3::text[],$4::text[],$5::text[])
                   )
                   SELECT p.source_type,p.source_id,p.parent_id,p.chunker_version,
                          p.heading,p.content,p.content_hash,p.metadata
                   FROM rag_parent_sections p JOIN wanted w
                     ON w.source_type=p.source_type AND w.source_id=p.source_id
                    AND w.parent_id=p.parent_id AND w.chunker_version=p.chunker_version
                   WHERE p.user_id=$1 AND p.deleted_at IS NULL""",
                user_id, [item["source"] for item in wanted],
                [item["source_id"] for item in wanted],
                [item["parent_id"] for item in wanted],
                [item["chunker_version"] for item in wanted],
            )
        parents = {
            (row["source_type"], row["source_id"], row["parent_id"],
             row["chunker_version"]): dict(row)
            for row in parent_rows
        }
        for item in wanted:
            parent = parents.get((
                item["source"], item["source_id"], item["parent_id"],
                item["chunker_version"],
            ))
            if not parent:
                continue
            item["child_content"] = item["content"]
            item["content"] = parent["content"]
            item["parent_expanded"] = True
            item["parent_heading"] = parent["heading"]
            item["parent_content_hash"] = parent["content_hash"]
            item["citation"]["context_level"] = "parent"
            item["citation"]["matched_child_id"] = str(item["id"])
    if not results:
        empty_context.inc()
    return results
