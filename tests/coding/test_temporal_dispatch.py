"""Real SQL transaction/fencing tests; SDK requests use a bounded fake service."""
import asyncio
from dataclasses import replace
import hashlib
import importlib
from types import SimpleNamespace
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

from app.runs import temporal_dispatch as dispatch
from tests.coding.test_workflow_ownership_postgres import owner_store as owner_fixture, admit, run

owner_store = owner_fixture
migration = importlib.import_module('migrations.versions.027_temporal_start_intents')


@pytest_asyncio.fixture
async def store(owner_store):
    await owner_store.execute(migration.SCHEMA_SQL)
    return owner_store


async def enable(pool, enabled=True):
    await pool.execute("UPDATE temporal_dispatch_control SET enabled=$1,evidence_reference='test:offline-service'", enabled)


async def queued(pool, kind='workspace'):
    await admit(pool, kind)
    return await run(pool, kind, temporal=True)


async def ready(pool):
    await pool.execute("UPDATE temporal_start_intents SET available_at=now()-interval '1 second'")


@pytest.mark.asyncio
async def test_atomic_capture_reference_only_and_independently_off(store):
    await run(store, 'workspace')
    assert await store.fetchval('SELECT count(*) FROM temporal_start_intents') == 0
    identity = uuid4()
    for kind in ('workspace', 'coding'):
        await admit(store, kind)
        await run(store, kind, temporal=True, identity=identity, user='PRIVATE EMAIL')
    rows = await store.fetch('SELECT * FROM temporal_start_intents ORDER BY workflow_kind')
    assert len(rows) == 2 and rows[0]['workflow_id'] != rows[1]['workflow_id']
    for row in rows:
        assert b'PRIVATE' not in bytes(row['request_bytes'])
        assert hashlib.sha256(row['request_bytes']).hexdigest() == row['request_sha256']
    assert await dispatch.claim(store) is None


@pytest.mark.asyncio
async def test_business_insert_rolls_back_if_intent_capture_fails(store):
    await admit(store, 'workspace')
    # A 2-KB reference envelope is a hard bound, not a truncation of source metadata.
    await store.execute("UPDATE workflow_ownership_control SET executor_version=$1 WHERE workflow_kind='workspace'", 'x' * 3000)
    with pytest.raises(asyncpg.CheckViolationError):
        await run(store, 'workspace', temporal=True, executor_version='x' * 3000)
    assert await store.fetchval('SELECT count(*) FROM agent_runs') == 0


@pytest.mark.asyncio
async def test_concurrent_claim_is_disjoint_and_ack_requires_live_nonce(store):
    await queued(store)
    await enable(store)
    results = await asyncio.gather(dispatch.claim(store), dispatch.claim(store))
    item = next(value for value in results if value)
    assert sum(value is not None for value in results) == 1
    receipt = dispatch.StartReceipt(item.namespace, item.workflow_id, uuid4(), item.sha256)
    assert not await dispatch.acknowledge(store, replace(item, lease_token=uuid4()), receipt)
    assert await dispatch.acknowledge(store, item, receipt)
    assert await dispatch.claim(store) is None
    assert not await dispatch.acknowledge(store, item, receipt)


@pytest.mark.asyncio
async def test_lost_start_ack_reconciles_same_identity_not_new_workflow(store):
    await queued(store)
    await enable(store)
    class LostAck:
        def __init__(self):
            self.identity = None
            self.starts = 0
            self.calls = 0
        async def ensure_started(self, item):
            self.calls += 1
            if self.identity is None:
                self.identity = uuid4()
                self.starts += 1
                raise ConnectionError('PRIVATE TRANSPORT DETAILS')
            return dispatch.StartReceipt(item.namespace, item.workflow_id, self.identity, item.sha256)
    transport = LostAck()
    assert await dispatch.dispatch_one(store, transport)
    row = await store.fetchrow('SELECT * FROM temporal_start_intents')
    assert row['state'] == 'uncertain' and row['may_have_started']
    assert row['last_error_category'] == 'transport'
    await ready(store)
    assert await dispatch.dispatch_one(store, transport)
    assert transport.starts == 1 and transport.calls == 2
    assert await store.fetchval('SELECT first_execution_run_id FROM temporal_start_intents') == transport.identity


@pytest.mark.asyncio
async def test_cancelled_parent_and_disabled_transport_cannot_admit_new_starts(store):
    row = await queued(store)
    await enable(store)
    await store.execute("UPDATE agent_runs SET status='cancelled' WHERE id=$1", row['id'])
    assert not (await dispatch.claim(store)).allow_start
    # A claimed/uncertain record can still be described while transport is paused.
    await store.execute("UPDATE temporal_start_intents SET lease_expires_at=now()-interval '1 second'")
    await enable(store, False)
    item = await dispatch.claim(store)
    assert item and not item.allow_start


@pytest.mark.asyncio
async def test_tampering_and_parent_deletion_cannot_erase_start_uncertainty(store):
    row = await queued(store)
    with pytest.raises(asyncpg.RaiseError, match='immutable'):
        await store.execute("UPDATE temporal_start_intents SET workflow_id='other'")
    await enable(store)
    item = await dispatch.claim(store)
    with pytest.raises(asyncpg.RaiseError, match='immutable'):
        await store.execute('UPDATE temporal_start_intents SET may_have_started=FALSE')
    with pytest.raises(dispatch.DispatchError, match='binding'):
        await dispatch.acknowledge(store, item, dispatch.StartReceipt('other', item.workflow_id, uuid4(), item.sha256))
    await store.execute('DELETE FROM agent_runs WHERE id=$1', row['id'])
    retained = await store.fetchrow('SELECT * FROM temporal_start_intents')
    assert retained['workspace_parent_id'] is None and retained['may_have_started']
    await store.execute("UPDATE temporal_start_intents SET lease_expires_at=now()-interval '1 second'")
    assert not (await dispatch.claim(store)).allow_start


@pytest.mark.asyncio
async def test_stale_ack_and_cancelled_rpc_preserve_same_intent_for_reconciliation(store):
    await queued(store)
    await enable(store)
    class Cancelled:
        async def ensure_started(self, item):
            raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await dispatch.dispatch_one(store, Cancelled())
    row = await store.fetchrow('SELECT * FROM temporal_start_intents')
    assert row['state'] == 'claimed' and row['may_have_started']
    await store.execute("UPDATE temporal_start_intents SET lease_expires_at=now()-interval '1 second'")
    item = await dispatch.claim(store)
    assert item.lease_token != row['lease_token']
    assert not await dispatch.acknowledge(store, replace(item, lease_token=row['lease_token']),
        dispatch.StartReceipt(item.namespace, item.workflow_id, uuid4(), item.sha256))


@pytest.mark.asyncio
async def test_attempt_limit_blocks_without_resetting_uncertainty(store):
    await queued(store)
    await enable(store)
    await store.execute("UPDATE temporal_start_intents SET attempt_count=10,may_have_started=TRUE,state='uncertain'")
    assert await dispatch.claim(store) is None
    row = await store.fetchrow('SELECT * FROM temporal_start_intents')
    assert row['state'] == 'blocked' and row['may_have_started'] and row['attempt_count'] == 10


@pytest.mark.asyncio
async def test_downgrade_requires_disable_and_reconciliation_even_for_acknowledged_starts(store):
    await queued(store)
    with pytest.raises(asyncpg.RaiseError, match='reconcile retained'):
        await store.execute(migration.DOWNGRADE_SQL)


def service(item, *, existing=False, wrong=False, lost=False, concurrent=False):
    from temporalio.exceptions import WorkflowAlreadyStartedError
    from temporalio.service import RPCError, RPCStatusCode
    class Client:
        namespace = item.namespace
        def __init__(self):
            self.exists = existing
            self.starts = 0
            self.describe_calls = 0
            self.options = None
            self.first = str(uuid4())
        def get_workflow_handle(self, identity):
            assert identity == item.workflow_id
            return self
        async def describe(self, **kwargs):
            self.describe_calls += 1
            assert kwargs['rpc_timeout'].total_seconds() == 10
            if not self.exists:
                raise RPCError('PRIVATE NOT FOUND', RPCStatusCode.NOT_FOUND, b'')
            async def memo():
                return {'gca_dispatch_sha256': 'wrong' if wrong else item.sha256,
                        'gca_dispatch_contract': 'gca.temporal.start.v1'}
            return SimpleNamespace(namespace=item.namespace, id=item.workflow_id, task_queue=item.task_queue,
                workflow_type='gca.' + item.kind + '.v1', memo=memo,
                raw_info=SimpleNamespace(first_run_id=self.first))
        async def start_workflow(self, name, payload, **kwargs):
            self.options = kwargs
            self.starts += 1
            self.exists = True
            assert payload == item.payload()
            if concurrent:
                raise WorkflowAlreadyStartedError(item.workflow_id, name)
            if lost:
                raise ConnectionError('PRIVATE LOST ACK')
    return Client()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['workspace', 'coding'])
async def test_real_sdk_policy_enums_and_bound_service_receipt(store, kind):
    pytest.importorskip('temporalio')
    from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
    await queued(store, kind)
    await enable(store)
    item = await dispatch.claim(store)
    client = service(item)
    receipt = await dispatch.TemporalStartTransport(client).ensure_started(item)
    assert receipt.first_execution_run_id == UUID(client.first)
    assert client.options['id_conflict_policy'] is WorkflowIDConflictPolicy.FAIL
    assert client.options['id_reuse_policy'] is WorkflowIDReusePolicy.REJECT_DUPLICATE
    assert client.options['execution_timeout'].total_seconds() == 86400
    assert not any(key in client.options for key in ('start_signal', 'cron_schedule', 'retry_policy'))
    assert await dispatch.acknowledge(store, item, receipt)


@pytest.mark.asyncio
async def test_sdk_recovers_lost_start_response_and_concurrent_start(store):
    pytest.importorskip('temporalio')
    await queued(store)
    await enable(store)
    item = await dispatch.claim(store)
    for concurrent in (False, True):
        client = service(item, lost=not concurrent, concurrent=concurrent)
        transport = dispatch.TemporalStartTransport(client)
        if not concurrent:
            with pytest.raises(dispatch.DispatchError, match='transport'):
                await transport.ensure_started(item)
        receipt = await transport.ensure_started(item)
        assert client.starts == 1 and str(receipt.first_execution_run_id) == client.first


@pytest.mark.asyncio
async def test_existing_wrong_memo_fails_closed_and_paused_missing_workflow_is_not_started(store):
    pytest.importorskip('temporalio')
    await queued(store)
    await enable(store)
    item = await dispatch.claim(store)
    wrong = service(item, existing=True, wrong=True)
    with pytest.raises(dispatch.DispatchError, match='binding'):
        await dispatch.TemporalStartTransport(wrong).ensure_started(item)
    paused = service(item)
    with pytest.raises(dispatch.DispatchError, match='not_admissible'):
        await dispatch.TemporalStartTransport(paused).ensure_started(replace(item, allow_start=False))
    assert wrong.starts == paused.starts == 0
