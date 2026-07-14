import asyncio
import json
from app.rag.embedder import NomicEmbedder
from app.mlops.metrics import empty_context

QUERIES = {
 "gmail": "SELECT id,coalesce(subject,'')||E'\\n'||coalesce(body_plain,'') content,1-(embedding<=>$1) score,jsonb_build_object('sender',sender,'received_at',received_at) metadata FROM gmail_messages WHERE embedding IS NOT NULL ORDER BY embedding<=>$1 LIMIT $2",
 "drive": "SELECT id,coalesce(name,'')||E'\\n'||coalesce(content,'') content,1-(embedding<=>$1) score,jsonb_build_object('mime_type',mime_type,'web_view_link',web_view_link) metadata FROM drive_documents WHERE embedding IS NOT NULL ORDER BY embedding<=>$1 LIMIT $2",
 "contacts": "SELECT id,coalesce(display_name,'')||' '||coalesce(array_to_string(emails,','),'') content,1-(embedding<=>$1) score,jsonb_build_object('organization',organization) metadata FROM contacts WHERE embedding IS NOT NULL ORDER BY embedding<=>$1 LIMIT $2",
 "chat": "SELECT id,coalesce(text,'') content,1-(embedding<=>$1) score,jsonb_build_object('space_id',space_id,'sender_email',sender_email) metadata FROM chat_messages WHERE embedding IS NOT NULL ORDER BY embedding<=>$1 LIMIT $2",
}
async def hybrid_retrieve(query, pool=None, filters=None, top_k=5):
    if pool is None:
        from app.db.connection import get_pool
        pool = await get_pool()
    vector = await NomicEmbedder().aembed_query(query)
    async def fetch_table(sql):
        async with pool.acquire() as conn:
            return await conn.fetch(sql, vector, top_k)
    groups = await asyncio.gather(*(fetch_table(sql) for sql in QUERIES.values()))
    results = [{"source": source, **dict(row)} for source, rows in zip(QUERIES, groups) for row in rows]
    for result in results:
        if isinstance(result.get("metadata"), str):
            result["metadata"] = json.loads(result["metadata"])
    results = sorted(results, key=lambda r: r["score"], reverse=True)[:top_k]
    if not results:
        empty_context.inc()
    return results
