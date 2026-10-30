"""Both business event sources through one real disposable PostgreSQL outbox."""
import asyncio
import importlib
import json
import os
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from app.coding.repository import append_coding_event
from app.runs import event_outbox as outbox, workflow_projection as projection


@pytest_asyncio.fixture
async def base_store():
    dsn = os.getenv('COST_LEDGER_TEST_DSN')
    if not dsn:
        pytest.skip('set COST_LEDGER_TEST_DSN to loopback PostgreSQL')
    target = urlsplit(dsn)
    if target.hostname not in {'localhost', '127.0.0.1', '::1'} or target.query or target.fragment:
        pytest.fail('coding event tests refuse non-loopback targets')
    name = 'coding_outbox_test_' + uuid4().hex
    root = await asyncpg.connect(dsn)
    pool = None
    try:
        await root.execute(f'CREATE SCHEMA "{name}"')
        await root.execute(f'SET search_path TO "{name}"')
        await root.execute('''CREATE TABLE agent_runs(id UUID PRIMARY KEY,user_id TEXT NOT NULL,deleted_at TIMESTAMPTZ);
            CREATE TABLE coding_runs(id UUID PRIMARY KEY,user_id TEXT NOT NULL,deleted_at TIMESTAMPTZ);
            CREATE TABLE agent_run_events(id BIGSERIAL PRIMARY KEY,run_id UUID NOT NULL REFERENCES agent_runs(id)
              ON DELETE CASCADE,user_id TEXT NOT NULL,event_type TEXT NOT NULL,phase TEXT,message TEXT,
              payload JSONB NOT NULL DEFAULT '{}',created_at TIMESTAMPTZ NOT NULL DEFAULT now());
            CREATE TABLE coding_run_events(id BIGSERIAL PRIMARY KEY,run_id UUID NOT NULL REFERENCES coding_runs(id)
              ON DELETE CASCADE,user_id TEXT NOT NULL,event_type TEXT NOT NULL,phase TEXT NOT NULL,message TEXT,
              payload JSONB NOT NULL DEFAULT '{}',created_at TIMESTAMPTZ NOT NULL DEFAULT now());''')
        for version in ('023_workflow_event_outbox', '024_workflow_consumer_projection'):
            await root.execute(importlib.import_module('migrations.versions.' + version).SCHEMA_SQL)
        pool = await asyncpg.create_pool(dsn, min_size=2, max_size=10, server_settings={'search_path': name})
        yield pool
    finally:
        if pool:
            await pool.close()
        await root.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
        await root.close()


@pytest_asyncio.fixture
async def store(base_store):
    await base_store.execute(importlib.import_module('migrations.versions.025_coding_workflow_events').SCHEMA_SQL)
    return base_store


async def runs(pool, *, workspace_user='alice', coding_user='alice'):
    identity = uuid4()  # Deliberately identical UUIDs in distinct business tables.
    await pool.execute('INSERT INTO agent_runs VALUES($1,$2,NULL,0)', identity, workspace_user)
    if await pool.fetchval("SELECT to_regclass('workflow_coding_event_projection') IS NOT NULL"):
        await pool.execute('INSERT INTO coding_runs VALUES($1,$2,NULL,0)', identity, coding_user)
    else:
        await pool.execute('INSERT INTO coding_runs VALUES($1,$2,NULL)', identity, coding_user)
    return identity


async def capture(pool, kind, enabled=True):
    await outbox.set_capture(pool, enabled=enabled, evidence_reference='test:reviewed-' + kind, workflow_kind=kind)


async def workspace_event(pool, identity, user='alice'):
    return await pool.fetchval('''INSERT INTO agent_run_events(run_id,user_id,event_type,phase,message,payload)
        VALUES($1,$2,'run_created','intake','PRIVATE WORKSPACE BODY','{"token":"PRIVATE"}') RETURNING id''', identity, user)


async def coding_event(pool, identity, user='alice'):
    # Exercise the actual existing repository entry point, not a disconnected helper.
    await append_coding_event(pool, run_id=identity, user_id=user, event_type='coding_worker_claimed',
        phase='planning', message='PRIVATE SOURCE CONTENT', payload={'api_key': 'PRIVATE TOKEN', 'source': 'PRIVATE CODE'})


async def acknowledge(pool, item):
    return await outbox.acknowledge(pool, item,
        receipt=outbox.BrokerAcknowledgement(item.event_id, 'gca.workflow.events.v1', 0, 100))


@pytest.mark.asyncio
async def test_coding_capture_is_independently_default_off_without_history_backfill(store):
    identity = await runs(store)
    await coding_event(store, identity)
    await capture(store, 'workspace')
    await coding_event(store, identity)
    assert await store.fetchval('SELECT count(*) FROM workflow_event_envelopes') == 0
    await capture(store, 'coding')
    await coding_event(store, identity)
    item = (await outbox.claim(store, owner='publisher'))[0]
    value = outbox.decode(item.body)
    assert value.schema_name == 'gca.workflow.event.v2' and value.kind == 'coding'
    assert value.sequence == 1 and value.source_event_id == 3
    assert item.key == (str(value.tenant_id) + ':coding:' + str(identity)).encode()
    assert b'PRIVATE' not in item.body and b'alice' not in item.body and b'api_key' not in item.body
    assert await store.fetchval("SELECT count(*) FROM workflow_delivery_audit WHERE workflow_kind='coding'") == 1


@pytest.mark.asyncio
async def test_equal_source_ids_and_run_uuids_remain_independent_streams(store):
    identity = await runs(store)
    await capture(store, 'workspace')
    await capture(store, 'coding')
    assert await workspace_event(store, identity) == 1
    await coding_event(store, identity)
    items = await outbox.claim(store, owner='publisher')
    assert len(items) == 2 and len({item.key for item in items}) == 2
    values = [outbox.decode(item.body) for item in items]
    assert {value.kind for value in values} == {'workspace', 'coding'}
    assert all(value.source_event_id == value.sequence == 1 and value.run_id == identity for value in values)
    for item in items:
        assert await projection.apply(store, key=item.key, body=item.body) == 'applied'
        assert await projection.apply(store, key=item.key, body=item.body) == 'duplicate'
    assert await store.fetchval('SELECT observed_event_count FROM workflow_run_event_projection') == 1
    assert await store.fetchval('SELECT observed_event_count FROM workflow_coding_event_projection') == 1


@pytest.mark.asyncio
async def test_two_source_kinds_share_tenant_capacity_without_losing_atomicity(store):
    identity = await runs(store)
    await capture(store, 'workspace')
    await capture(store, 'coding')
    await workspace_event(store, identity)
    await store.execute('UPDATE workflow_event_tenants SET pending_limit=1')
    with pytest.raises(asyncpg.RaiseError, match='tenant capacity'):
        await coding_event(store, identity)
    assert await store.fetchval('SELECT count(*) FROM coding_run_events') == 0
    assert await store.fetchval('SELECT workflow_event_sequence FROM coding_runs') == 0
    assert await store.fetchval('SELECT pending_count FROM workflow_delivery_control') == 1


@pytest.mark.asyncio
async def test_invalid_coding_tenant_and_business_rollback_leave_no_delivery_intent(store):
    identity = await runs(store)
    await capture(store, 'coding')
    with pytest.raises(asyncpg.RaiseError, match='tenant/run mismatch'):
        await coding_event(store, identity, 'mallory')
    async with store.acquire() as conn:
        with pytest.raises(RuntimeError):
            async with conn.transaction():
                await coding_event(conn, identity)
                raise RuntimeError('synthetic business rollback')
    assert await store.fetchval('SELECT count(*) FROM coding_run_events') == 0
    assert await store.fetchval('SELECT count(*) FROM workflow_event_outbox') == 0
    assert await store.fetchval('SELECT pending_count FROM workflow_delivery_control') == 0


@pytest.mark.asyncio
async def test_changed_business_owner_cannot_reinterpret_a_previous_tenant_envelope(store):
    identity = await runs(store)
    await capture(store, 'coding')
    await coding_event(store, identity)
    item = (await outbox.claim(store, owner='publisher'))[0]
    await store.execute("UPDATE coding_runs SET user_id='mallory' WHERE id=$1", identity)
    with pytest.raises(projection.ProjectionDeliveryError, match='source_unavailable'):
        await projection.apply(store, key=item.key, body=item.body)
    assert await store.fetchval('SELECT count(*) FROM workflow_coding_event_projection') == 0
    assert await store.fetchval('SELECT count(*) FROM workflow_event_inbox') == 0


@pytest.mark.asyncio
async def test_both_kinds_final_erasure_removes_private_tenant_map_and_all_pending_counts(store):
    identity = await runs(store)
    await capture(store, 'workspace')
    await capture(store, 'coding')
    await workspace_event(store, identity)
    await coding_event(store, identity)
    await store.execute('UPDATE coding_runs SET deleted_at=now() WHERE id=$1', identity)
    await store.execute('UPDATE agent_runs SET deleted_at=now() WHERE id=$1', identity)
    assert await store.fetchval('SELECT count(*) FROM workflow_event_tenants') == 0
    assert await store.fetchval('SELECT count(*) FROM workflow_event_outbox') == 0
    assert await store.fetchval('SELECT pending_count FROM workflow_delivery_control') == 0


@pytest.mark.asyncio
async def test_concurrent_coding_order_and_head_of_line_do_not_block_workspace_uuid_collision(store):
    identity = await runs(store)
    await capture(store, 'workspace')
    await capture(store, 'coding')
    await asyncio.gather(*(coding_event(store, identity) for _ in range(12)))
    await workspace_event(store, identity)
    first = await outbox.claim(store, owner='first')
    assert {outbox.decode(item.body).kind for item in first} == {'workspace', 'coding'}
    coding = next(item for item in first if outbox.decode(item.body).kind == 'coding')
    assert outbox.decode(coding.body).sequence == 1
    assert await outbox.claim(store, owner='second') == []
    assert await acknowledge(store, coding)
    second = (await outbox.claim(store, owner='second'))[0]
    assert outbox.decode(second.body).kind == 'coding' and outbox.decode(second.body).sequence == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,erase', [('coding', 'hard'), ('coding', 'soft'), ('workspace', 'hard'), ('workspace', 'soft')])
async def test_erasure_removes_only_matching_kind_and_keeps_other_same_uuid_tenant_state(store, kind, erase):
    identity = await runs(store)
    await capture(store, 'workspace')
    await capture(store, 'coding')
    await workspace_event(store, identity)
    await coding_event(store, identity)
    items = await outbox.claim(store, owner='publisher')
    for item in items:
        await projection.apply(store, key=item.key, body=item.body)
    if kind == 'coding':
        sql = 'DELETE FROM coding_runs WHERE id=$1' if erase == 'hard' else 'UPDATE coding_runs SET deleted_at=now() WHERE id=$1'
    else:
        sql = 'DELETE FROM agent_runs WHERE id=$1' if erase == 'hard' else 'UPDATE agent_runs SET deleted_at=now() WHERE id=$1'
    await store.execute(sql, identity)
    erased = next(item for item in items if outbox.decode(item.body).kind == kind)
    retained = next(item for item in items if outbox.decode(item.body).kind != kind)
    assert await projection.apply(store, key=erased.key, body=erased.body) == 'erased'
    assert not await acknowledge(store, erased)
    assert await acknowledge(store, retained)
    assert await store.fetchval('SELECT count(*) FROM workflow_event_tenants') == 1
    assert await store.fetchval('SELECT pending_count FROM workflow_delivery_control') == 0
    assert await projection.apply(store, key=retained.key, body=retained.body) == 'duplicate'
    assert await store.fetchval('SELECT count(*) FROM workflow_run_event_projection') == int(kind != 'workspace')
    assert await store.fetchval('SELECT count(*) FROM workflow_coding_event_projection') == int(kind != 'coding')


@pytest.mark.asyncio
async def test_different_tenants_with_equal_run_uuid_have_separate_keys_and_erasure(store):
    identity = await runs(store, workspace_user='alice', coding_user='bob')
    await capture(store, 'workspace')
    await capture(store, 'coding')
    await workspace_event(store, identity)
    await coding_event(store, identity, 'bob')
    items = await outbox.claim(store, owner='publisher')
    assert len({outbox.decode(item.body).tenant_id for item in items}) == 2
    await store.execute('DELETE FROM coding_runs WHERE id=$1', identity)
    assert await store.fetchval('SELECT count(*) FROM workflow_event_tenants') == 1
    assert await store.fetchval('SELECT user_id FROM workflow_event_tenants') == 'alice'


@pytest.mark.asyncio
async def test_upgrade_preserves_existing_workspace_bytes_and_lease(base_store):
    identity = await runs(base_store)
    await capture(base_store, 'workspace')
    await workspace_event(base_store, identity)
    before = (await outbox.claim(base_store, owner='before-upgrade'))[0]
    await base_store.execute(importlib.import_module('migrations.versions.025_coding_workflow_events').SCHEMA_SQL)
    assert await acknowledge(base_store, before)
    assert await projection.apply(base_store, key=before.key, body=before.body) == 'applied'
    assert bytes(await base_store.fetchval('SELECT wire_bytes FROM workflow_event_envelopes')) == before.body
    assert await base_store.fetchval('SELECT capture_coding_enabled FROM workflow_delivery_control') is False


@pytest.mark.asyncio
async def test_downgrade_requires_coding_disable_and_drain_preserving_business_events(store):
    identity = await runs(store)
    await capture(store, 'workspace')
    await capture(store, 'coding')
    await workspace_event(store, identity)
    await coding_event(store, identity)
    downgrade = importlib.import_module('migrations.versions.025_coding_workflow_events').DOWNGRADE_SQL
    with pytest.raises(asyncpg.RaiseError, match='disable and drain'):
        await store.execute(downgrade)
    await capture(store, 'coding', False)
    with pytest.raises(asyncpg.RaiseError, match='disable and drain'):
        await store.execute(downgrade)
    items = await outbox.claim(store, owner='publisher')
    for item in items:
        await acknowledge(store, item)
    await store.execute(downgrade)
    assert await store.fetchval('SELECT count(*) FROM coding_run_events') == 1
    assert await store.fetchval('SELECT count(*) FROM agent_run_events') == 1
    assert await store.fetchval('SELECT count(*) FROM workflow_event_envelopes') == 1
    workspace = next(item for item in items if outbox.decode(item.body).kind == 'workspace')
    assert await projection.apply(store, key=workspace.key, body=workspace.body) == 'applied'
