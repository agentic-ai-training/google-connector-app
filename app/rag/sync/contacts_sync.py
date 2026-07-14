import time
from app.db.connection import get_pool
from app.db.google_clients import people_service
from app.rag.embedder import NomicEmbedder
from app.rag.sync.common import log_sync
async def contacts_sync(pool=None,embedder=None):
    pool=pool or await get_pool(); embedder=embedder or NomicEmbedder(); started=time.monotonic()
    people=people_service.people().connections().list(resourceName="people/me",personFields="names,emailAddresses,phoneNumbers,organizations,biographies,photos",pageSize=1000).execute().get("connections",[])
    for p in people:
        name=(p.get("names") or [{}])[0].get("displayName"); emails=[x.get("value") for x in p.get("emailAddresses",[])]; phones=[x.get("value") for x in p.get("phoneNumbers",[])]; org=(p.get("organizations") or [{}])[0]
        vector=await embedder.aembed_query(f"{name or ''} {' '.join(emails)} {org.get('name','')}")
        async with pool.acquire() as conn: await conn.execute("""INSERT INTO contacts(id,display_name,emails,phone_numbers,organization,job_title,embedding) VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT(id) DO UPDATE SET display_name=excluded.display_name,emails=excluded.emails,embedding=excluded.embedding,synced_at=now()""",p["resourceName"],name,emails,phones,org.get("name"),org.get("title"),vector)
    await log_sync(pool,"contacts",len(people),started=started); return len(people)
