import datetime
import json
import time

from app.db.connection import get_pool
from app.db.google_clients import calendar_service
from app.rag.embedder import NomicEmbedder
from app.rag.sync.common import log_sync


def _event_time(value):
    if value.get("dateTime"):
        return datetime.datetime.fromisoformat(value["dateTime"])
    if value.get("date"):
        date = datetime.date.fromisoformat(value["date"])
        return datetime.datetime.combine(date, datetime.time(), datetime.timezone.utc)
    return None


async def calendar_sync(pool=None, embedder=None):
    pool = pool or await get_pool()
    embedder = embedder or NomicEmbedder()
    started = time.monotonic()
    now = datetime.datetime.now(datetime.timezone.utc)
    page_token = None
    events = []
    while True:
        page = calendar_service.events().list(
            calendarId="primary",
            timeMin=(now - datetime.timedelta(days=30)).isoformat(),
            timeMax=(now + datetime.timedelta(days=90)).isoformat(),
            singleEvents=True,
            pageToken=page_token,
        ).execute()
        events.extend(page.get("items", []))
        page_token = page.get("nextPageToken")
        if not page_token:
            break
    for event in events:
        vector = await embedder.aembed_query(
            f"{event.get('summary', '')} {event.get('description', '')}"
        )
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO calendar_events
                   (id,title,description,location,start_time,end_time,is_all_day,
                    attendees,organizer_email,meet_link,status,recurrence,embedding)
                   VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10,$11,$12,$13)
                   ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title,description=excluded.description,
                    location=excluded.location,start_time=excluded.start_time,
                    end_time=excluded.end_time,is_all_day=excluded.is_all_day,
                    attendees=excluded.attendees,
                    organizer_email=excluded.organizer_email,
                    meet_link=excluded.meet_link,status=excluded.status,
                    recurrence=excluded.recurrence,embedding=excluded.embedding,
                    synced_at=now()""",
                event["id"], event.get("summary"), event.get("description"),
                event.get("location"), _event_time(event.get("start", {})),
                _event_time(event.get("end", {})),
                "date" in event.get("start", {}),
                json.dumps(event.get("attendees", [])),
                event.get("organizer", {}).get("email"), event.get("hangoutLink"),
                event.get("status"), event.get("recurrence"), vector,
            )
    await log_sync(pool, "calendar", len(events), started=started)
    return len(events)
