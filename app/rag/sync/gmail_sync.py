import datetime,time
from app.db.connection import get_pool
from app.db.google_clients import gmail_service
from app.rag.embedder import NomicEmbedder
from app.tools.registry import _gmail
from app.rag.sync.common import log_sync
async def gmail_sync(pool=None,embedder=None,full=False):
    pool=pool or await get_pool(); embedder=embedder or NomicEmbedder(); started=time.monotonic(); count=0
    async with pool.acquire() as conn: last=await conn.fetchval("SELECT max(last_synced_at) FROM sync_log WHERE source='gmail' AND status='success'")
    q=f"after:{int(last.timestamp())}" if last and not full else ""
    items = []
    page_token = None
    while True:
        page = gmail_service.users().messages().list(
            userId="me", q=q, maxResults=500, pageToken=page_token
        ).execute()
        items.extend(page.get("messages", []))
        page_token = page.get("nextPageToken")
        if not page_token:
            break
    for item in items:
        async with pool.acquire() as conn:
            existing = await conn.fetchrow(
                "SELECT embedding,received_at FROM gmail_messages WHERE id=$1",
                item["id"],
            )
            if existing and existing["embedding"] is not None and existing["received_at"]:
                continue
        data=_gmail(gmail_service.users().messages().get(userId="me",id=item["id"],format="full").execute())
        vector = existing["embedding"] if existing and existing["embedding"] is not None else await embedder.aembed_query(f"{data.get('subject','')} {data.get('body_plain','')}")
        received = datetime.datetime.fromisoformat(data["received_at"]) if data.get("received_at") else None
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO gmail_messages
                   (id,thread_id,sender,sender_name,recipients,subject,body_plain,
                    body_html,labels,has_attachments,attachment_names,received_at,
                    is_read,is_starred,snippet,embedding)
                   VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
                   ON CONFLICT(id) DO UPDATE SET
                    thread_id=excluded.thread_id,sender=excluded.sender,
                    sender_name=excluded.sender_name,recipients=excluded.recipients,
                    subject=excluded.subject,body_plain=excluded.body_plain,
                    body_html=excluded.body_html,labels=excluded.labels,
                    has_attachments=excluded.has_attachments,
                    attachment_names=excluded.attachment_names,
                    received_at=excluded.received_at,is_read=excluded.is_read,
                    is_starred=excluded.is_starred,snippet=excluded.snippet,
                    embedding=excluded.embedding,synced_at=now()""",
                data["id"], data["thread_id"], data["sender"],
                data["sender_name"], data["recipients"], data["subject"],
                data["body_plain"], data["body_html"], data["labels"],
                data["has_attachments"], data["attachment_names"], received,
                data["is_read"], data["is_starred"], data["snippet"], vector,
            )
        count+=1
    await log_sync(pool,"gmail",count,started=started); return count
