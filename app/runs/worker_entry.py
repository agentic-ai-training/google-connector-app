import asyncio
from types import SimpleNamespace
from prometheus_client import start_http_server

from app.agents.supervisor import build_agent_graph
from app.db.connection import close_pool, get_pool
from app.okf.loader import sync_bundle
from app.runs.worker import worker_loop
from app.rag.jobs import embedding_worker_loop


async def main():
    start_http_server(8001)
    pool = await get_pool()
    await sync_bundle(pool)
    app = SimpleNamespace(state=SimpleNamespace(agent_graph=build_agent_graph(pool)))
    stop = asyncio.Event()
    try:
        await asyncio.gather(worker_loop(app, pool, stop), embedding_worker_loop(pool, stop))
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
