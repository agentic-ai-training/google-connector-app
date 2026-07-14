import asyncpg
from pgvector.asyncpg import register_vector
from app.config.settings import get_settings

_pool = None
async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        url = get_settings().async_database_url.replace("+asyncpg", "")
        _pool = await asyncpg.create_pool(
            url, min_size=2, max_size=10, init=register_vector
        )
    return _pool

async def close_pool():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
