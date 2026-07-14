import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from app.config.settings import get_settings
scheduler=AsyncIOScheduler()
async def ping_self():
    if get_settings().railway_url:
        async with httpx.AsyncClient() as client: await client.get(f"{get_settings().railway_url}/health")
def setup_scheduler(pool,embedder):
    from app.rag.sync.gmail_sync import gmail_sync
    from app.rag.sync.drive_sync import drive_sync
    from app.rag.sync.calendar_sync import calendar_sync
    from app.rag.sync.contacts_sync import contacts_sync
    jobs=[(gmail_sync,2,0),(drive_sync,2,15),(calendar_sync,2,30),(contacts_sync,2,45)]
    for fn,hour,minute in jobs: scheduler.add_job(fn,"cron",hour=hour,minute=minute,args=[pool,embedder],replace_existing=True,id=fn.__name__)
    if get_settings().railway_url: scheduler.add_job(ping_self,"interval",minutes=10,id="keepalive",replace_existing=True)
    scheduler.start()
