import asyncpg
import json
from pgvector.asyncpg import register_vector
from app.config.settings import get_settings

_pool = None


async def _init_connection(conn):
    await register_vector(conn)
    def encoder(value):
        return value if isinstance(value, str) else json.dumps(value)
    await conn.set_type_codec(
        "json", schema="pg_catalog", encoder=encoder, decoder=json.loads,
        format="text",
    )
    await conn.set_type_codec(
        "jsonb", schema="pg_catalog", encoder=encoder, decoder=json.loads,
        format="text",
    )


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        url = get_settings().async_database_url.replace("+asyncpg", "")
        _pool = await asyncpg.create_pool(
            url, min_size=2, max_size=10, init=_init_connection
        )
    return _pool

async def close_pool():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
