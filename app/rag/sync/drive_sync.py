import datetime,time
from app.db.connection import get_pool
from app.db.google_clients import drive_activity_service, drive_service
from app.rag.embedder import NomicEmbedder
from app.rag.sync.common import log_sync
async def drive_sync(pool=None,embedder=None,full=False):
    pool=pool or await get_pool(); embedder=embedder or NomicEmbedder(); started=time.monotonic(); count=0
    async with pool.acquire() as conn:
        last=await conn.fetchval("SELECT max(last_synced_at) FROM sync_log WHERE source='drive' AND status='success'")
    files=[]
    activity_failed=False
    if last and not full:
        ids=set(); page_token=None
        try:
            while True:
                page=drive_activity_service.activity().query(body={"filter":f'time > "{last.isoformat()}"',"pageSize":100,"pageToken":page_token}).execute()
                for activity in page.get("activities",[]):
                    for target in activity.get("targets",[]):
                        name=target.get("driveItem",{}).get("name","")
                        if name.startswith("items/"): ids.add(name.split("/",1)[1])
                page_token=page.get("nextPageToken")
                if not page_token: break
            for file_id in ids:
                try: files.append(drive_service.files().get(fileId=file_id,fields="id,name,mimeType,webViewLink,modifiedTime,createdTime,parents,owners,shared,size,trashed").execute())
                except Exception: continue
        except Exception:
            files=[]; activity_failed=True
    if not last or full or activity_failed:
        page_token=None
        query="trashed=false"
        if last and not full:
            query += f" and modifiedTime > '{last.astimezone(datetime.timezone.utc).isoformat().replace('+00:00', 'Z')}'"
        while True:
            page=drive_service.files().list(q=query,pageSize=500,pageToken=page_token,fields="nextPageToken,files(id,name,mimeType,webViewLink,modifiedTime,createdTime,parents,owners,shared,size,trashed)").execute()
            files.extend(page.get("files",[])); page_token=page.get("nextPageToken")
            if not page_token: break
    for f in files:
        content=""
        if f["mimeType"]=="application/vnd.google-apps.document":
            try: content=drive_service.files().export(fileId=f["id"],mimeType="text/plain").execute().decode(errors="replace")
            except Exception: pass
        elif f["mimeType"]=="application/vnd.google-apps.spreadsheet":
            try: content=drive_service.files().export(fileId=f["id"],mimeType="text/csv").execute().decode(errors="replace")
            except Exception: pass
        vector=await embedder.aembed_query(f["name"]+" "+content)
        modified=datetime.datetime.fromisoformat(f["modifiedTime"].replace("Z","+00:00")) if f.get("modifiedTime") else None
        created=datetime.datetime.fromisoformat(f["createdTime"].replace("Z","+00:00")) if f.get("createdTime") else None
        async with pool.acquire() as conn: await conn.execute("""INSERT INTO drive_documents(id,name,mime_type,content,parent_folder,web_view_link,owners,shared_with,size_bytes,modified_at,created_at,trashed,embedding) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13) ON CONFLICT(id) DO UPDATE SET name=excluded.name,mime_type=excluded.mime_type,content=excluded.content,parent_folder=excluded.parent_folder,web_view_link=excluded.web_view_link,owners=excluded.owners,size_bytes=excluded.size_bytes,modified_at=excluded.modified_at,created_at=excluded.created_at,trashed=excluded.trashed,embedding=excluded.embedding,synced_at=now()""",f["id"],f["name"],f["mimeType"],content,(f.get("parents") or [None])[0],f.get("webViewLink"),[x.get("emailAddress") for x in f.get("owners",[])],[],int(f.get("size",0)),modified,created,f.get("trashed",False),vector)
        count+=1
    await log_sync(pool,"drive",count,started=started); return count
