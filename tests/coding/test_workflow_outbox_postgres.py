"""Real transactional outbox/inbox proofs on disposable loopback schemas only."""
import asyncio
from dataclasses import replace
import importlib
import json
import os
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from app.runs import event_outbox as outbox


@pytest_asyncio.fixture
async def store():
    dsn = os.getenv('COST_LEDGER_TEST_DSN')
    if not dsn:
        pytest.skip('set COST_LEDGER_TEST_DSN to loopback PostgreSQL')
    target = urlsplit(dsn)
    if target.hostname not in {'localhost', '127.0.0.1', '::1'} or target.query or target.fragment:
        pytest.fail('workflow tests refuse non-loopback targets')
    name = 'workflow_outbox_test_' + uuid4().hex
    root = await asyncpg.connect(dsn)
    pool = None
    try:
        await root.execute(f'CREATE SCHEMA "{name}"')
        await root.execute(f'SET search_path TO "{name}"')
        await root.execute('''CREATE TABLE agent_runs(id UUID PRIMARY KEY,user_id TEXT NOT NULL,deleted_at TIMESTAMPTZ);
            CREATE TABLE agent_run_events(id BIGSERIAL PRIMARY KEY,run_id UUID NOT NULL REFERENCES agent_runs(id)
            ON DELETE CASCADE,user_id TEXT NOT NULL,event_type TEXT NOT NULL,phase TEXT,message TEXT,
            payload JSONB NOT NULL DEFAULT '{}',created_at TIMESTAMPTZ NOT NULL DEFAULT now());
            CREATE TABLE projected_events(event_id UUID PRIMARY KEY,tenant_id UUID NOT NULL);''')
        migration = importlib.import_module('migrations.versions.023_workflow_event_outbox')
        await root.execute(migration.SCHEMA_SQL)
        pool = await asyncpg.create_pool(dsn, min_size=2, max_size=10, server_settings={'search_path': name})
        yield pool
    finally:
        if pool:
            await pool.close()
        await root.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
        await root.close()


async def run(pool, user='alice'):
    identity = uuid4()
    await pool.execute('INSERT INTO agent_runs(id,user_id) VALUES($1,$2)', identity, user)
    return identity


async def event(pool, identity, user='alice', *, kind='step_started'):
    return await pool.fetchval('''INSERT INTO agent_run_events(run_id,user_id,event_type,phase,message,payload)
        VALUES($1,$2,$3,'executing','PRIVATE EMAIL BODY',
        '{"access_token":"synthetic-private-token","body":"PRIVATE TOOL RESULT"}') RETURNING id''', identity, user, kind)


async def enable(pool):
    await outbox.set_capture(pool, enabled=True, evidence_reference='test:operator-review')


async def ack(pool, item):
    return await outbox.acknowledge(pool, item, receipt=outbox.BrokerAcknowledgement(
        item.event_id, 'gca.workflow.events.v1', 0, 123))  # Synthetic broker, not live proof.


async def projection(conn, value):
    await conn.execute('INSERT INTO projected_events VALUES($1,$2)', value.event_id, value.tenant_id)


@pytest.mark.asyncio
async def test_capture_is_off_by_default_and_does_not_backfill_history(store):
    identity = await run(store)
    await event(store, identity)
    assert await store.fetchval('SELECT count(*) FROM workflow_event_envelopes') == 0
    await enable(store)
    await event(store, identity)
    rows = await outbox.claim(store, owner='publisher')
    assert len(rows) == 1 and outbox.decode(rows[0].body).sequence == 1
    for private in (b'PRIVATE', b'access_token', b'synthetic-private-token', b'alice', b'payload'):
        assert private not in rows[0].body


@pytest.mark.asyncio
async def test_business_event_and_outbox_rollback_together(store):
    identity = await run(store)
    await enable(store)
    async with store.acquire() as conn:
        with pytest.raises(RuntimeError):
            async with conn.transaction():
                await event(conn, identity)
                raise RuntimeError('synthetic business transaction rollback')
    assert await store.fetchval('SELECT count(*) FROM agent_run_events') == 0
    assert await store.fetchval('SELECT count(*) FROM workflow_event_envelopes') == 0
    assert await store.fetchval('SELECT pending_count FROM workflow_delivery_control') == 0


@pytest.mark.asyncio
async def test_concurrent_run_events_have_unique_committed_order(store):
    identity = await run(store)
    await enable(store)
    await asyncio.gather(*(event(store, identity) for _ in range(12)))
    seq = await store.fetch('SELECT sequence_no FROM workflow_event_envelopes ORDER BY sequence_no')
    assert [row[0] for row in seq] == list(range(1, 13))
    first = await outbox.claim(store, owner='first')
    assert len(first) == 1 and outbox.decode(first[0].body).sequence == 1
    assert await outbox.claim(store, owner='second') == []
    assert await ack(store, first[0])
    next_ = await outbox.claim(store, owner='second')
    assert outbox.decode(next_[0].body).sequence == 2


@pytest.mark.asyncio
async def test_cross_tenant_event_rolls_back_without_broker_leak(store):
    identity = await run(store)
    await enable(store)
    with pytest.raises(asyncpg.RaiseError, match='tenant/run mismatch'):
        await event(store, identity, 'mallory')
    assert await store.fetchval('SELECT count(*) FROM agent_run_events') == 0


@pytest.mark.asyncio
async def test_expired_lease_fences_old_ack_and_keeps_event_identity(store):
    identity = await run(store)
    await enable(store)
    await event(store, identity)
    first = (await outbox.claim(store, owner='first'))[0]
    await store.execute("UPDATE workflow_event_outbox SET lease_expires_at=now()-interval '1 second'")
    second = (await outbox.claim(store, owner='second'))[0]
    assert first.event_id == second.event_id and first.body == second.body
    assert first.lease_token != second.lease_token
    assert not await ack(store, first)
    assert await ack(store, second)
    assert not await ack(store, second)
    assert await store.fetchval('SELECT pending_count FROM workflow_delivery_control') == 0


@pytest.mark.asyncio
async def test_consumer_effects_and_deduplication_are_atomic(store):
    identity = await run(store)
    await enable(store)
    await event(store, identity)
    item = (await outbox.claim(store, owner='publisher'))[0]
    async def failing(conn, value):
        await projection(conn, value)
        raise RuntimeError('synthetic projection failure')
    with pytest.raises(RuntimeError):
        await outbox.consume(store, consumer_name='projection', body=item.body, handler=failing)
    assert await store.fetchval('SELECT count(*) FROM workflow_event_inbox') == 0
    assert await store.fetchval('SELECT count(*) FROM projected_events') == 0
    results = await asyncio.gather(*(outbox.consume(store, consumer_name='projection', body=item.body,
                                                   handler=projection) for _ in range(8)))
    assert results.count(True) == 1 and results.count(False) == 7
    assert await store.fetchval('SELECT count(*) FROM projected_events') == 1


@pytest.mark.asyncio
async def test_privacy_deletion_invalidates_old_broker_messages(store):
    identity = await run(store)
    await enable(store)
    await event(store, identity)
    item = (await outbox.claim(store, owner='publisher'))[0]
    await store.execute('DELETE FROM agent_runs WHERE id=$1', identity)
    assert await store.fetchval('SELECT count(*) FROM workflow_event_envelopes') == 0
    assert await store.fetchval('SELECT count(*) FROM workflow_event_tenants') == 0
    assert await store.fetchval('SELECT pending_count FROM workflow_delivery_control') == 0
    with pytest.raises(outbox.EventDeliveryError, match='unknown, changed or deleted'):
        await outbox.consume(store, consumer_name='projection', body=item.body, handler=projection)


@pytest.mark.asyncio
async def test_capacity_failure_is_atomic_and_does_not_drop_events(store):
    identity = await run(store)
    await enable(store)
    await store.execute('UPDATE workflow_delivery_control SET pending_limit=1')
    await event(store, identity)
    with pytest.raises(asyncpg.RaiseError, match='global capacity'):
        await event(store, identity)
    assert await store.fetchval('SELECT count(*) FROM agent_run_events') == 1
    assert await store.fetchval('SELECT workflow_event_sequence FROM agent_runs WHERE id=$1', identity) == 1


@pytest.mark.asyncio
async def test_dead_letter_blocks_later_events_in_same_run(store):
    identity = await run(store)
    await enable(store)
    await event(store, identity)
    await event(store, identity)
    item = (await outbox.claim(store, owner='publisher'))[0]
    await store.execute('UPDATE workflow_event_outbox SET attempts=12 WHERE event_id=$1', item.event_id)
    assert await outbox.failed(store, item, category='broker_rejected')
    assert await outbox.claim(store, owner='publisher') == []
    assert await store.fetchval('SELECT pending_count FROM workflow_delivery_control') == 2


@pytest.mark.asyncio
async def test_changed_tenant_or_body_is_rejected_by_consumer(store):
    identity = await run(store)
    await enable(store)
    await event(store, identity)
    item = (await outbox.claim(store, owner='publisher'))[0]
    changed = json.loads(item.body)
    changed['tenant_id'] = str(uuid4())
    with pytest.raises(outbox.EventDeliveryError):
        await outbox.consume(store, consumer_name='projection', body=json.dumps(changed).encode(), handler=projection)
    assert not await ack(store, replace(item, lease_token=uuid4()))


@pytest.mark.asyncio
async def test_downgrade_refuses_pending_delivery_and_preserves_legacy_events(store):
    identity = await run(store)
    await enable(store)
    await event(store, identity)
    migration = importlib.import_module('migrations.versions.023_workflow_event_outbox')
    with pytest.raises(asyncpg.RaiseError, match='drain or reconcile'):
        await store.execute(migration.DOWNGRADE_SQL)
    assert await ack(store, (await outbox.claim(store, owner='publisher'))[0])
    await store.execute(migration.DOWNGRADE_SQL)
    assert await store.fetchval('SELECT count(*) FROM agent_run_events') == 1


@pytest.mark.asyncio
async def test_broker_nonack_cannot_be_marked_published(store):
    identity = await run(store)
    await enable(store)
    await event(store, identity)
    item = (await outbox.claim(store, owner='publisher'))[0]
    with pytest.raises(outbox.EventDeliveryError, match='acknowledgement'):
        await outbox.acknowledge(store, item, receipt=None)
    assert await store.fetchval('SELECT state FROM workflow_event_outbox') == 'leased'


@pytest.mark.asyncio
async def test_publisher_records_ack_and_keeps_lost_ack_retryable(store):
    identity = await run(store)
    await enable(store)
    await event(store, identity)
    class Producer:
        async def publish(self, *, key, body):
            value = outbox.decode(body)
            return outbox.BrokerAcknowledgement(value.event_id, 'gca.workflow.events.v1', 2, 456)
    assert (await outbox.publish_once(store, Producer(), owner='publisher'))['published'] == 1
    assert tuple(await store.fetchrow('SELECT broker_partition,broker_offset FROM workflow_event_outbox')) == (2, 456)
    await event(store, identity)
    class LostAck:
        async def publish(self, **kwargs):
            raise TimeoutError('synthetic private error must not be stored')
    assert (await outbox.publish_once(store, LostAck(), owner='publisher'))['unconfirmed'] == 1
    assert await store.fetchval("SELECT count(*) FROM workflow_event_outbox WHERE state='pending'") == 1
    assert await store.fetchval("SELECT failure_category FROM workflow_event_outbox WHERE state='pending'") == 'delivery_unconfirmed'


@pytest.mark.asyncio
async def test_deleting_one_of_two_runs_keeps_correct_tenant_counter(store):
    first, second = await run(store), await run(store)
    await enable(store)
    await event(store, first)
    await event(store, second)
    await store.execute('DELETE FROM agent_runs WHERE id=$1', first)
    assert await store.fetchval('SELECT count(*) FROM workflow_event_tenants') == 1
    assert await store.fetchval('SELECT pending_count FROM workflow_event_tenants') == 1
    assert await store.fetchval('SELECT pending_count FROM workflow_delivery_control') == 1


@pytest.mark.asyncio
async def test_retention_prunes_only_published_and_does_not_replay_old_messages(store):
    identity = await run(store)
    await enable(store)
    for _ in range(2):
        await store.execute("""INSERT INTO agent_run_events(run_id,user_id,event_type,created_at)
            VALUES($1,'alice','run_created',now()-interval '91 days')""", identity)
    item = (await outbox.claim(store, owner='publisher'))[0]
    await ack(store, item)
    async with store.acquire() as conn, conn.transaction():
        assert await outbox.prune_published_in_transaction(conn) == 1
    assert await store.fetchval('SELECT count(*) FROM workflow_event_outbox') == 1
    assert await store.fetchval('SELECT pending_count FROM workflow_delivery_control') == 1
    with pytest.raises(outbox.EventDeliveryError, match='unknown'):
        await outbox.consume(store, consumer_name='projection', body=item.body, handler=projection)


@pytest.mark.asyncio
async def test_soft_erasure_cancels_metadata_and_fences_an_inflight_ack(store):
    identity = await run(store)
    await enable(store)
    await event(store, identity)
    item = (await outbox.claim(store, owner='publisher'))[0]
    await store.execute('UPDATE agent_runs SET deleted_at=now() WHERE id=$1', identity)
    assert not await ack(store, item)
    assert await store.fetchval('SELECT pending_count FROM workflow_delivery_control') == 0
    assert await store.fetchval('SELECT count(*) FROM workflow_event_tenants') == 0
    with pytest.raises(outbox.EventDeliveryError, match='deleted'):
        await outbox.consume(store, consumer_name='projection', body=item.body, handler=projection)
