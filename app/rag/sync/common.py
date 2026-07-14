import json, time
async def log_sync(pool,source,count,status="success",error=None,started=None):
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO sync_log(source,last_synced_at,items_synced,items_embedded,status,error_message,duration_ms) VALUES($1,now(),$2,$2,$3,$4,$5)",source,count,status,error,int((time.monotonic()-(started or time.monotonic()))*1000))
