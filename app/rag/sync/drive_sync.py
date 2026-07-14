import io,time
from app.db.connection import get_pool
from app.db.google_clients import drive_service
from app.rag.embedder import NomicEmbedder
from app.rag.sync.common import log_sync
async def drive_sync(pool=None,embedder=None):
    pool=pool or await get_pool(); embedder=embedder or NomicEmbedder(); started=time.monotonic(); count=0
    files=drive_service.files().list(q="trashed=false",pageSize=500,fields="files(id,name,mimeType,webViewLink,modifiedTime,createdTime,parents,owners,shared,size,trashed)").execute().get("files",[])
    for f in files:
        content=""
        if f["mimeType"]=="application/vnd.google-apps.document":
            try: content=drive_service.files().export(fileId=f["id"],mimeType="text/plain").execute().decode(errors="replace")
            except Exception: pass
        vector=await embedder.aembed_query(f["name"]+" "+content)
        async with pool.acquire() as conn: await conn.execute("""INSERT INTO drive_documents(id,name,mime_type,content,parent_folder,web_view_link,owners,size_bytes,trashed,embedding) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) ON CONFLICT(id) DO UPDATE SET name=excluded.name,content=excluded.content,embedding=excluded.embedding,synced_at=now()""",f["id"],f["name"],f["mimeType"],content,(f.get("parents") or [None])[0],f.get("webViewLink"),[x.get("emailAddress") for x in f.get("owners",[])],int(f.get("size",0)),f.get("trashed",False),vector)
        count+=1
    await log_sync(pool,"drive",count,started=started); return count
