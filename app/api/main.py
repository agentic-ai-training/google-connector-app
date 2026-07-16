import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest, make_asgi_app
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from app.agents.supervisor import build_agent_graph
from app.config.settings import get_settings
from app.db.connection import close_pool,get_pool
from app.rag.embedder import NomicEmbedder
from app.rag.sync.scheduler import scheduler,setup_scheduler
from app.api.middleware.auth import auth_middleware,router as auth_router
from app.api.middleware.metrics import metrics_middleware
from app.api.routes import admin,chat,feedback,history
@asynccontextmanager
async def lifespan(app):
    settings=get_settings()
    langsmith_key = settings.langsmith_api_key or settings.langchain_api_key
    valid_langsmith = bool(langsmith_key and "your_" not in langsmith_key)
    os.environ.update({
        "LANGCHAIN_TRACING_V2": settings.langchain_tracing_v2 if valid_langsmith else "false",
        "LANGCHAIN_PROJECT": settings.langchain_project,
        "LANGSMITH_TRACING": settings.langsmith_tracing if valid_langsmith else "false",
        "LANGSMITH_PROJECT": settings.langsmith_project,
    })
    if valid_langsmith:
        os.environ["LANGCHAIN_API_KEY"] = langsmith_key
        os.environ["LANGSMITH_API_KEY"] = langsmith_key
    pool=await get_pool()
    setup_scheduler(pool,NomicEmbedder())
    # A single long-lived connection is unsafe with serverless Postgres providers
    # such as Neon: the provider may retire it while the Railway process stays up.
    # The pool validates borrowed connections and transparently replaces stale ones.
    async with AsyncConnectionPool(
        conninfo=settings.database_url,
        min_size=1,
        max_size=5,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        check=AsyncConnectionPool.check_connection,
        max_idle=300,
        max_lifetime=1800,
        reconnect_timeout=60,
    ) as checkpoint_pool:
        checkpointer = AsyncPostgresSaver(checkpoint_pool)
        await checkpointer.setup()
        app.state.agent_graph = build_agent_graph(pool, checkpointer)
        yield
    if scheduler.running:
        scheduler.shutdown(wait=False)
    await close_pool()
app=FastAPI(title="Google Workspace AI Agent",lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        origin.strip() for origin in get_settings().cors_origins.split(",")
        if origin.strip()
    ],
    allow_origin_regex=r"https://[a-zA-Z0-9-]+\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.middleware("http")(metrics_middleware)
app.middleware("http")(auth_middleware)
app.include_router(auth_router); app.include_router(chat.router); app.include_router(feedback.router); app.include_router(history.router); app.include_router(admin.router)
app.mount("/metrics",make_asgi_app())
@app.get("/health")
async def health(): return {"status":"ok"}


@app.get("/monitoring/metrics", include_in_schema=False)
async def monitoring_metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
