import datetime,time,json
from app.db.connection import get_pool
from app.db.google_clients import calendar_service
from app.rag.embedder import NomicEmbedder
from app.rag.sync.common import log_sync
async def calendar_sync(pool=None,embedder=None):
    pool=pool or await get_pool(); embedder=embedder or NomicEmbedder(); started=time.monotonic(); now=datetime.datetime.now(datetime.timezone.utc)
    events=calendar_service.events().list(calendarId="primary",timeMin=(now-datetime.timedelta(days=30)).isoformat(),timeMax=(now+datetime.timedelta(days=90)).isoformat(),singleEvents=True).execute().get("items",[])
    for e in events:
        vector=await embedder.aembed_query(f"{e.get('summary','')} {e.get('description','')}")
        async with pool.acquire() as conn: await conn.execute("""INSERT INTO calendar_events(id,title,description,location,start_time,end_time,attendees,organizer_email,meet_link,status,recurrence,embedding) VALUES($1,$2,$3,$4,$5,$6,$7::jsonb,$8,$9,$10,$11,$12) ON CONFLICT(id) DO UPDATE SET title=excluded.title,embedding=excluded.embedding,synced_at=now()""",e["id"],e.get("summary"),e.get("description"),e.get("location"),e.get("start",{}).get("dateTime"),e.get("end",{}).get("dateTime"),json.dumps(e.get("attendees",[])),e.get("organizer",{}).get("email"),e.get("hangoutLink"),e.get("status"),e.get("recurrence"),vector)
    await log_sync(pool,"calendar",len(events),started=started); return len(events)
