"""Opt-in, isolated real-PostgreSQL fixture shared by cost/deployment tests."""
import importlib
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from app.coding import cost_ledger
from app.coding.cloud_budget import CloudCreditSnapshot


@pytest_asyncio.fixture
async def db():
    dsn = os.getenv("COST_LEDGER_TEST_DSN")
    if not dsn:
        pytest.skip("set COST_LEDGER_TEST_DSN to local PostgreSQL")
    parsed = urlsplit(dsn)
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1"} or parsed.query or parsed.fragment:
        pytest.fail("cost-ledger tests refuse non-loopback database targets")
    schema = "cost_ledger_test_" + uuid4().hex
    conn = await asyncpg.connect(dsn)
    pool = None
    try:
        await conn.execute(f'CREATE SCHEMA "{schema}"')
        await conn.execute(f'SET search_path TO "{schema}"')
        migration = importlib.import_module("migrations.versions.016_cloud_cost_ledger")
        await conn.execute(migration.SCHEMA_SQL)
        pool = await asyncpg.create_pool(
            dsn, min_size=2, max_size=10, server_settings={"search_path": schema},
        )
        now = datetime.now(timezone.utc)
        snapshot = CloudCreditSnapshot(
            project_id="test-cost-project", remaining_credit_paise=2_850_000, observed_at=now,
            expires_at=now + timedelta(days=60),
        )
        end = now + timedelta(days=30)
        await cost_ledger.register_account(pool, snapshot, hosting_ends_at=end,
                                          evidence_reference="test:verified-credit")
        yield pool, snapshot, end
    finally:
        if pool:
            await pool.close()
        # Only the unique schema made above is dropped. Existing schemas/data stay intact.
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await conn.close()
