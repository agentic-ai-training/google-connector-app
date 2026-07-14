import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import make_asgi_app
from app.config.settings import get_settings
from app.db.connection import close_pool,get_pool
from app.rag.embedder import NomicEmbedder
from app.rag.sync.scheduler import scheduler,setup_scheduler
from app.api.middleware.auth import auth_middleware,router as auth_router
from app.api.routes import admin,chat,feedback,history
@asynccontextmanager
async def lifespan(app):
    settings=get_settings()
    valid_langsmith = settings.langchain_api_key and "your_" not in settings.langchain_api_key
    os.environ.update({
        "LANGCHAIN_TRACING_V2": settings.langchain_tracing_v2 if valid_langsmith else "false",
        "LANGCHAIN_PROJECT": settings.langchain_project,
    })
    if valid_langsmith:
        os.environ["LANGCHAIN_API_KEY"] = settings.langchain_api_key
    pool=await get_pool(); setup_scheduler(pool,NomicEmbedder())
    yield
    if scheduler.running: scheduler.shutdown(wait=False)
    await close_pool()
app=FastAPI(title="Google Workspace AI Agent",lifespan=lifespan)
app.add_middleware(CORSMiddleware,allow_origins=["http://localhost:3000","http://127.0.0.1:3000"],allow_credentials=True,allow_methods=["*"],allow_headers=["*"])
app.middleware("http")(auth_middleware)
app.include_router(auth_router); app.include_router(chat.router); app.include_router(feedback.router); app.include_router(history.router); app.include_router(admin.router)
app.mount("/metrics",make_asgi_app())
@app.get("/health")
async def health(): return {"status":"ok"}
