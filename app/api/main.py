import asyncio
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest, make_asgi_app
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from app.agents.supervisor import build_agent_graph
from app.config.settings import get_settings, validate_runtime_security
from app.db.connection import close_pool,get_pool
from app.rag.embedder import NomicEmbedder
from app.rag.sync.scheduler import scheduler,setup_scheduler
from app.api.middleware.auth import auth_middleware,router as auth_router
from app.api.middleware.metrics import metrics_middleware
from app.api.routes import admin,chat,feedback,history,runs
from app.runs.worker import worker_loop
from app.runs.retention import retention_loop
from app.rag.jobs import embedding_worker_loop
from app.improvements.analyzer import improvement_analysis_loop
from app.mlops.collector import metrics_collection_loop
from app.mlops.metrics import build_info
from app.mlops.tracing import configure_tracing
from app.okf.loader import sync_bundle
@asynccontextmanager
async def lifespan(app):
    settings=get_settings()
    validate_runtime_security(settings)
    build_info.info({"version": settings.deployment_version})
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
    if settings.okf_enabled:
        await sync_bundle(pool)
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
        worker_stop = asyncio.Event()
        retention_stop = asyncio.Event()
        worker_task = None
        retention_task = asyncio.create_task(retention_loop(pool, retention_stop))
        embedding_task = asyncio.create_task(embedding_worker_loop(pool, retention_stop))
        improvement_task = asyncio.create_task(
            improvement_analysis_loop(pool, retention_stop)
        ) if settings.governed_improvements_enabled else None
        metrics_task = asyncio.create_task(metrics_collection_loop(pool, retention_stop))
        if settings.durable_runs_enabled and settings.embedded_worker_enabled:
            worker_task = asyncio.create_task(worker_loop(app, pool, worker_stop))
        yield
        worker_stop.set()
        retention_stop.set()
        if worker_task:
            await worker_task
        await retention_task
        await embedding_task
        if improvement_task:
            await improvement_task
        await metrics_task
    if scheduler.running:
        scheduler.shutdown(wait=False)
    await close_pool()
app=FastAPI(title="Google Workspace AI Agent",lifespan=lifespan)
configure_tracing(app)
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
# Starlette executes the last registered HTTP middleware first. Keep metrics
# outermost so authentication rejections are still correlated, counted, and
# traced without exposing the requested resource identifier or query string.
app.middleware("http")(auth_middleware)
app.middleware("http")(metrics_middleware)
app.include_router(auth_router); app.include_router(chat.router); app.include_router(runs.router); app.include_router(runs.sessions_router); app.include_router(feedback.router); app.include_router(history.router); app.include_router(admin.router)
app.mount("/metrics",make_asgi_app())
@app.get("/health")
async def health(): return {"status":"ok"}


@app.get("/monitoring/metrics", include_in_schema=False)
async def monitoring_metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
