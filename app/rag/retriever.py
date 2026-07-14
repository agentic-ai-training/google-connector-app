import asyncio
import json
from app.rag.embedder import NomicEmbedder
from app.mlops.metrics import empty_context

QUERIES = {
 "gmail": "SELECT id,coalesce(subject,'')||E'\\n'||coalesce(body_plain,'') content,1-(embedding<=>$1) score,jsonb_build_object('sender',sender,'received_at',received_at,'labels',labels) metadata FROM gmail_messages WHERE embedding IS NOT NULL AND ($2::timestamptz IS NULL OR received_at >= $2) AND ($3::timestamptz IS NULL OR received_at <= $3) AND ($4::text IS NULL OR sender ILIKE '%'||$4||'%') AND ($5::text IS NULL OR $5=ANY(labels)) ORDER BY embedding<=>$1 LIMIT $6",
 "drive": "SELECT id,coalesce(name,'')||E'\\n'||coalesce(content,'') content,1-(embedding<=>$1) score,jsonb_build_object('mime_type',mime_type,'web_view_link',web_view_link) metadata FROM drive_documents WHERE embedding IS NOT NULL ORDER BY embedding<=>$1 LIMIT $2",
 "contacts": "SELECT id,coalesce(display_name,'')||' '||coalesce(array_to_string(emails,','),'') content,1-(embedding<=>$1) score,jsonb_build_object('organization',organization) metadata FROM contacts WHERE embedding IS NOT NULL ORDER BY embedding<=>$1 LIMIT $2",
 "chat": "SELECT id,coalesce(text,'') content,1-(embedding<=>$1) score,jsonb_build_object('space_id',space_id,'sender_email',sender_email) metadata FROM chat_messages WHERE embedding IS NOT NULL ORDER BY embedding<=>$1 LIMIT $2",
}
async def hybrid_retrieve(query, pool=None, filters=None, top_k=5):
    if pool is None:
        from app.db.connection import get_pool
        pool = await get_pool()
    vector = await NomicEmbedder().aembed_query(query)
    filters = filters or {}
    source_filter = filters.get("source")
    async def fetch_table(source, sql):
        if source_filter and source != source_filter:
            return []
        async with pool.acquire() as conn:
            if source == "gmail":
                return await conn.fetch(
                    sql,
                    vector,
                    filters.get("after_date"),
                    filters.get("before_date"),
                    filters.get("sender"),
                    filters.get("label"),
                    top_k,
                )
            return await conn.fetch(sql, vector, top_k)
    groups = await asyncio.gather(*(
        fetch_table(source, sql) for source, sql in QUERIES.items()
    ))
    results = [{"source": source, **dict(row)} for source, rows in zip(QUERIES, groups) for row in rows]
    for result in results:
        if isinstance(result.get("metadata"), str):
            result["metadata"] = json.loads(result["metadata"])
    results = sorted(results, key=lambda r: r["score"], reverse=True)[:top_k]
    if not results:
        empty_context.inc()
    return results
