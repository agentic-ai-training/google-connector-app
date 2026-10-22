"""Real disposable PostgreSQL projection/privacy proofs; broker transport is fake."""
import asyncio
import importlib
import json
import os
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from app.runs import event_outbox as outbox, workflow_projection as projection


@pytest_asyncio.fixture
async def store():
    dsn = os.getenv('COST_LEDGER_TEST_DSN')
    if not dsn:
        pytest.skip('set COST_LEDGER_TEST_DSN to loopback PostgreSQL')
    target = urlsplit(dsn)
    if target.hostname not in {'localhost', '127.0.0.1', '::1'} or target.query or target.fragment:
        pytest.fail('consumer tests refuse non-loopback targets')
    name = 'workflow_consumer_test_' + uuid4().hex
    root = await asyncpg.connect(dsn)
    pool = None
    try:
        await root.execute(f'CREATE SCHEMA "{name}"')
        await root.execute(f'SET search_path TO "{name}"')
        await root.execute('''CREATE TABLE agent_runs(id UUID PRIMARY KEY,user_id TEXT NOT NULL,deleted_at TIMESTAMPTZ);
            CREATE TABLE agent_run_events(id BIGSERIAL PRIMARY KEY,run_id UUID NOT NULL REFERENCES agent_runs(id)
            ON DELETE CASCADE,user_id TEXT NOT NULL,event_type TEXT NOT NULL,phase TEXT,message TEXT,
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


async def event(pool, *, identity=None, user='alice', count=1):
    identity = identity or uuid4()
    await pool.execute('INSERT INTO agent_runs(id,user_id) VALUES($1,$2) ON CONFLICT DO NOTHING', identity, user)
    await outbox.set_capture(pool, enabled=True, evidence_reference='test:consumer-review')
    for _ in range(count):
        await pool.execute('''INSERT INTO agent_run_events(run_id,user_id,event_type,phase,message,payload)
            VALUES($1,$2,'step_started','executing','PRIVATE BODY','{"access_token":"PRIVATE TOKEN"}')''', identity, user)
    rows = await pool.fetch('SELECT wire_bytes FROM workflow_event_envelopes WHERE run_id=$1 ORDER BY sequence_no', identity)
    return identity, [bytes(row[0]) for row in rows]


def key(body):
    value = outbox.decode(body)
    return (str(value.tenant_id) + ':' + str(value.run_id)).encode()


@pytest.mark.asyncio
async def test_registered_projection_is_atomic_deduplicated_and_not_run_authority(store):
    identity, bodies = await event(store)
    results = await asyncio.gather(*(projection.apply(store, key=key(bodies[0]), body=bodies[0]) for _ in range(8)))
    assert results.count('applied') == 1 and results.count('duplicate') == 7
    row = await store.fetchrow('SELECT * FROM workflow_run_event_projection')
    assert row['run_id'] == identity and row['last_sequence'] == row['observed_event_count'] == 1
    assert 'status' not in row and 'completion' not in row
    assert 'PRIVATE' not in str(dict(row)) and 'alice' not in str(dict(row))


@pytest.mark.asyncio
async def test_out_of_order_distinct_delivery_keeps_latest_sequence_and_exact_count(store):
    _, bodies = await event(store, count=12)
    await asyncio.gather(*(projection.apply(store, key=key(body), body=body) for body in reversed(bodies)))
    row = await store.fetchrow('SELECT * FROM workflow_run_event_projection')
    assert row['last_sequence'] == 12 and row['observed_event_count'] == 12
    assert row['last_event_id'] == outbox.decode(bodies[-1]).event_id


@pytest.mark.asyncio
async def test_projection_failure_rolls_back_inbox_and_projection(store, monkeypatch):
    _, bodies = await event(store)
    original = projection.project
    async def fail(conn, value):
        await original(conn, value)
        raise RuntimeError('synthetic private failure')
    monkeypatch.setattr(projection, 'project', fail)
    with pytest.raises(RuntimeError):
        await projection.apply(store, key=key(bodies[0]), body=bodies[0])
    assert await store.fetchval('SELECT count(*) FROM workflow_event_inbox') == 0
    assert await store.fetchval('SELECT count(*) FROM workflow_run_event_projection') == 0
    monkeypatch.setattr(projection, 'project', original)
    assert await projection.apply(store, key=key(bodies[0]), body=bodies[0]) == 'applied'


@pytest.mark.asyncio
@pytest.mark.parametrize('erase', ['hard', 'soft'])
async def test_erased_source_can_advance_without_recreating_private_projection(store, erase):
    identity, bodies = await event(store)
    await projection.apply(store, key=key(bodies[0]), body=bodies[0])
    sql = 'DELETE FROM agent_runs WHERE id=$1' if erase == 'hard' else 'UPDATE agent_runs SET deleted_at=now() WHERE id=$1'
    await store.execute(sql, identity)
    assert await store.fetchval('SELECT count(*) FROM workflow_run_event_projection') == 0
    assert await projection.apply(store, key=key(bodies[0]), body=bodies[0]) == 'erased'
    assert await store.fetchval('SELECT count(*) FROM workflow_event_inbox') == 0
    rows = await store.fetch('SELECT * FROM workflow_event_revocations')
    assert len(rows) == 1 and len(rows[0]['content_sha256']) == 64
    assert set(rows[0].keys()) == {'content_sha256', 'expires_at'}


@pytest.mark.asyncio
async def test_modified_unknown_or_wrong_key_cannot_claim_erasure(store):
    identity, bodies = await event(store)
    body = bodies[0]
    with pytest.raises(projection.ProjectionDeliveryError, match='key_mismatch'):
        await projection.apply(store, key=b'wrong-tenant', body=body)
    await store.execute('DELETE FROM agent_runs WHERE id=$1', identity)
    altered = json.loads(body)
    altered['phase'] = 'changed'
    with pytest.raises(projection.ProjectionDeliveryError, match='source_unavailable'):
        await projection.apply(store, key=key(body), body=json.dumps(altered).encode())
    await store.execute("UPDATE workflow_event_revocations SET expires_at=now()-interval '1 second'")
    with pytest.raises(projection.ProjectionDeliveryError, match='source_unavailable'):
        await projection.apply(store, key=key(body), body=body)


@pytest.mark.asyncio
async def test_failure_ledger_is_bounded_sanitized_and_never_stores_raw_body(store):
    for _ in range(5):
        await projection.record_failure(store, partition=0, offset=10, body=b'PRIVATE BODY', category='record_invalid')
    row = await store.fetchrow('SELECT * FROM workflow_consumer_failures')
    assert row['attempts'] == 3 and row['broker_offset'] == 10
    assert 'PRIVATE' not in str(dict(row)) and len(row['content_sha256']) == 64
    await store.execute('''INSERT INTO workflow_consumer_failures(topic,partition_id,broker_offset,category)
        SELECT 'gca.workflow.events.v1',1,n,'record_invalid' FROM generate_series(1,9999) AS n''')
    with pytest.raises(outbox.EventDeliveryError, match='capacity'):
        await projection.record_failure(store, partition=2, offset=11, body=b'other', category='record_invalid')
    assert await store.fetchval('SELECT count(*) FROM workflow_consumer_failures') == 10000
    await projection.record_failure(store, partition=0, offset=10, body=b'PRIVATE BODY', category='record_invalid')


@pytest.mark.asyncio
async def test_diagnostic_retention_is_bounded_and_cannot_grant_offset_authority(store):
    identity, bodies = await event(store)
    await store.execute('DELETE FROM agent_runs WHERE id=$1', identity)
    await store.execute("UPDATE workflow_event_revocations SET expires_at=now()-interval '1 second'")
    await projection.record_failure(store, partition=0, offset=20, body=b'private', category='source_unavailable')
    await store.execute("UPDATE workflow_consumer_failures SET last_seen_at=now()-interval '8 days'")
    async with store.acquire() as conn, conn.transaction():
        assert await projection.prune_diagnostics(conn) == {'workflow_revocations': 1, 'workflow_consumer_failures': 1}
    with pytest.raises(projection.ProjectionDeliveryError, match='source_unavailable'):
        await projection.apply(store, key=key(bodies[0]), body=bodies[0])


@pytest.mark.asyncio
async def test_real_sql_commit_precedes_transport_commit_and_replay_deduplicates(store):
    from app.runs import kafka_consumer as transport
    from collections import namedtuple
    tp = namedtuple('TopicPartition', 'topic partition')('gca.workflow.events.v1', 0)
    identity, bodies = await event(store)
    record = SimpleNamespace(topic=tp.topic, partition=tp.partition, offset=40, key=key(bodies[0]), value=bodies[0])
    state = transport.AssignmentState()
    state.owned[tp] = 1
    calls = []
    class Consumer:
        async def getmany(self, **kwargs):
            return {tp: [record]}
        async def commit(self, offsets):
            assert await store.fetchval('SELECT observed_event_count FROM workflow_run_event_projection WHERE run_id=$1', identity) == 1
            calls.append(offsets)
            if len(calls) == 1:
                raise TimeoutError('synthetic lost commit response')
    with pytest.raises(projection.ProjectionDeliveryError, match='commit_unconfirmed'):
        await transport.consume_batches(store, Consumer(), state)
    result = await transport.consume_batches(store, Consumer(), state)
    assert result == {'applied': 0, 'duplicate': 1, 'erased': 0, 'committed': 1}
    assert calls == [{tp: 41}, {tp: 41}]
    assert await store.fetchval('SELECT count(*) FROM workflow_event_inbox') == 1


@pytest.mark.asyncio
async def test_rebalance_cancellation_rolls_back_real_sql_and_allows_safe_replay(store, monkeypatch):
    from app.runs import kafka_consumer as transport
    from collections import namedtuple
    tp = namedtuple('TopicPartition', 'topic partition')('gca.workflow.events.v1', 0)
    _, bodies = await event(store)
    record = SimpleNamespace(topic=tp.topic, partition=tp.partition, offset=50, key=key(bodies[0]), value=bodies[0])
    state = transport.AssignmentState()
    state.owned[tp] = 1
    started = asyncio.Event()
    original = projection.project
    async def waiting(conn, value):
        await original(conn, value)
        started.set()
        await conn.execute('SELECT pg_sleep(30)')
    monkeypatch.setattr(projection, 'project', waiting)
    class Consumer:
        async def getmany(self, **kwargs):
            return {tp: [record]}
        async def commit(self, offsets):
            pytest.fail('revoked SQL transaction must not commit a broker offset')
    task = asyncio.create_task(transport.consume_batches(store, Consumer(), state))
    await asyncio.wait_for(started.wait(), 2)
    state.revoke([tp])
    with pytest.raises(projection.ProjectionDeliveryError, match='assignment_lost'):
        await asyncio.wait_for(task, 2)
    assert await store.fetchval('SELECT count(*) FROM workflow_run_event_projection') == 0
    assert await store.fetchval('SELECT count(*) FROM workflow_event_inbox') == 0
    monkeypatch.setattr(projection, 'project', original)
    assert await projection.apply(store, key=key(bodies[0]), body=bodies[0]) == 'applied'
