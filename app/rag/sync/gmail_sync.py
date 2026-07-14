import datetime,time
from app.db.connection import get_pool
from app.db.google_clients import gmail_service
from app.rag.embedder import NomicEmbedder
from app.tools.registry import _gmail
from app.rag.sync.common import log_sync
async def gmail_sync(pool=None,embedder=None):
    pool=pool or await get_pool(); embedder=embedder or NomicEmbedder(); started=time.monotonic(); count=0
    async with pool.acquire() as conn: last=await conn.fetchval("SELECT max(last_synced_at) FROM sync_log WHERE source='gmail' AND status='success'")
    q=f"after:{int(last.timestamp())}" if last else ""
    items=gmail_service.users().messages().list(userId="me",q=q,maxResults=500).execute().get("messages",[])
    for item in items:
        async with pool.acquire() as conn:
            if await conn.fetchval(
                "SELECT embedding IS NOT NULL FROM gmail_messages WHERE id=$1",
                item["id"],
            ):
                continue
        data=_gmail(gmail_service.users().messages().get(userId="me",id=item["id"],format="full").execute()); vector=await embedder.aembed_query(f"{data.get('subject','')} {data.get('body_plain','')}")
        async with pool.acquire() as conn: await conn.execute("""INSERT INTO gmail_messages(id,thread_id,sender,recipients,subject,body_plain,labels,snippet,embedding) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9) ON CONFLICT(id) DO UPDATE SET subject=excluded.subject,body_plain=excluded.body_plain,embedding=excluded.embedding,synced_at=now()""",data["id"],data["thread_id"],data["sender"],data["recipients"],data["subject"],data["body_plain"],data["labels"],data["snippet"],vector)
        count+=1
    await log_sync(pool,"gmail",count,started=started); return count
