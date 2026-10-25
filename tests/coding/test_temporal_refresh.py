"""Real transactional wake delivery with SDK-bound service doubles, no server."""
import asyncio
import importlib
from types import SimpleNamespace
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

pytest.importorskip('temporalio')
from app.runs import temporal_refresh as refresh
from tests.coding.test_temporal_workspace import database as database_fixture, prepared

database = database_fixture
pytestmark = pytest.mark.asyncio(loop_scope='module')


@pytest_asyncio.fixture(loop_scope='module')
async def case(database):
    await database.execute('UPDATE temporal_dispatch_control SET notifications_enabled=FALSE')
    _, _, reference, _ = await prepared(database)
    return database, UUID(reference.run_id)


async def enable(pool):
    await pool.execute("UPDATE temporal_dispatch_control SET notifications_enabled=TRUE,"
        "notification_evidence_reference='test:local-refresh-only'")


async def update(pool, identity, status='awaiting_approval'):
    await pool.execute('UPDATE agent_runs SET status=$2 WHERE id=$1', identity, status)


async def test_default_off_no_backfill_and_only_temporal_business_changes_capture(case):
    pool, identity = case
    await update(pool, identity)
    assert await pool.fetchval('SELECT count(*) FROM temporal_refresh_outbox WHERE run_id=$1', identity) == 0
    await enable(pool)
    await pool.execute('UPDATE agent_runs SET heartbeat_at=now() WHERE id=$1', identity)
    assert await pool.fetchval('SELECT count(*) FROM temporal_refresh_outbox WHERE run_id=$1', identity) == 0
    await update(pool, identity, 'queued')
    row = await pool.fetchrow('SELECT * FROM temporal_refresh_outbox WHERE run_id=$1', identity)
    assert row['desired_generation'] == 1 and row['parent_id'] == identity
    assert 'synthetic-user' not in str(row) and 'What is your name' not in str(row)
    ordinary = uuid4()
    await pool.execute('''INSERT INTO agent_runs(id,user_id,session_id,request,idempotency_key)
      VALUES($1,'ordinary','ordinary','ordinary',$2)''', ordinary, str(ordinary))
    await update(pool, ordinary, 'cancelled')
    await pool.execute('DELETE FROM agent_runs WHERE id=$1', ordinary)
    assert not await pool.fetchval('SELECT EXISTS(SELECT 1 FROM agent_runs WHERE id=$1)', ordinary)
    assert not await pool.fetchval('SELECT EXISTS(SELECT 1 FROM temporal_refresh_outbox WHERE run_id=$1)', ordinary)


async def test_current_generation_during_inflight_ack_is_not_lost(case):
    pool, identity = case
    await enable(pool)
    await update(pool, identity)
    first = await refresh.claim(pool, run_id=identity)
    await update(pool, identity, 'queued')
    assert await refresh.acknowledge(pool, first)
    row = await pool.fetchrow('SELECT * FROM temporal_refresh_outbox WHERE run_id=$1', identity)
    assert row['acknowledged_generation'] == 1 and row['desired_generation'] == 2
    second = await refresh.claim(pool, run_id=identity)
    assert second.generation == 2 and second.lease_token != first.lease_token
    assert not await refresh.acknowledge(pool, first)
    assert await refresh.acknowledge(pool, second)
    assert await refresh.claim(pool, run_id=identity) is None


async def test_concurrent_claims_and_expired_nonce_fencing(case):
    pool, identity = case
    await enable(pool)
    await update(pool, identity)
    claims = await asyncio.gather(refresh.claim(pool, run_id=identity), refresh.claim(pool, run_id=identity))
    first = next(value for value in claims if value)
    assert sum(value is not None for value in claims) == 1
    await pool.execute("UPDATE temporal_refresh_outbox SET lease_expires_at=now()-interval '1 second' WHERE run_id=$1", identity)
    second = await refresh.claim(pool, run_id=identity)
    assert not await refresh.acknowledge(pool, first)
    assert await refresh.acknowledge(pool, second)


async def test_lost_response_repeats_only_a_hint_and_keeps_private_decision_in_db(case):
    pool, identity = case
    await enable(pool)
    await update(pool, identity)
    class Lost:
        def __init__(self):
            self.calls = []
        async def send_refresh(self, item):
            self.calls.append(item.generation)
            if len(self.calls) == 1:
                raise ConnectionError('PRIVATE TRANSPORT ERROR')
    transport = Lost()
    assert await refresh.deliver_one(pool, transport, run_id=identity)
    row = await pool.fetchrow('SELECT * FROM temporal_refresh_outbox WHERE run_id=$1', identity)
    assert row['error_category'] == 'transport' and not row['blocked']
    await pool.execute("UPDATE temporal_refresh_outbox SET available_at=now()-interval '1 second' WHERE run_id=$1", identity)
    assert await refresh.deliver_one(pool, transport, run_id=identity)
    assert transport.calls == [1, 1]
    assert await pool.fetchval('SELECT status FROM agent_runs WHERE id=$1', identity) == 'awaiting_approval'


async def test_hard_erasure_preserves_only_refresh_reference_and_does_not_suppress_delete(case):
    pool, identity = case
    await enable(pool)
    await pool.execute('DELETE FROM agent_runs WHERE id=$1', identity)
    assert not await pool.fetchval('SELECT EXISTS(SELECT 1 FROM agent_runs WHERE id=$1)', identity)
    row = await pool.fetchrow('SELECT * FROM temporal_refresh_outbox WHERE run_id=$1', identity)
    assert row['parent_id'] is None and row['desired_generation'] == 1
    assert (await refresh.claim(pool, run_id=identity)).run_id == identity


async def test_real_approval_transaction_captures_wake_intent(case, monkeypatch):
    pool, identity = case
    from app.runs import repository
    from app.runs.planner import action_hash
    from app.runs.schemas import ExecutionPlan
    await update(pool, identity)
    plan = await pool.fetchval('SELECT plan FROM agent_runs WHERE id=$1', identity)
    digest = action_hash(ExecutionPlan.model_validate(plan))
    await pool.execute('''INSERT INTO run_approvals(run_id,requested_from,action_hash,action_summary,expires_at)
      VALUES($1,'synthetic-user',$2,'{}',now()+interval '1 hour')''', identity, digest)
    await enable(pool)
    monkeypatch.setattr(repository, 'get_settings', lambda: SimpleNamespace(executor_version='unrelated-version',
        deployment_version='unrelated-version'))
    assert await repository.decide_run(pool, identity, 'synthetic-user', True, digest) == 'queued'
    assert await pool.fetchval('SELECT desired_generation FROM temporal_refresh_outbox WHERE run_id=$1', identity) == 1
    assert await pool.fetchval('SELECT executor_version FROM agent_runs WHERE id=$1', identity) == 'test-v1'


async def test_rollback_and_private_clarification_never_separate_from_wake_intent(case):
    pool, identity = case
    await enable(pool)
    with pytest.raises(RuntimeError, match='synthetic rollback'):
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute("UPDATE agent_runs SET status='awaiting_approval' WHERE id=$1", identity)
            raise RuntimeError('synthetic rollback')
    assert await pool.fetchval('SELECT status FROM agent_runs WHERE id=$1', identity) == 'queued'
    assert not await pool.fetchval('SELECT EXISTS(SELECT 1 FROM temporal_refresh_outbox WHERE run_id=$1)', identity)
    await pool.execute('''UPDATE agent_runs SET clarification_answers='{"question":"PRIVATE ANSWER"}' WHERE id=$1''', identity)
    row = await pool.fetchrow('SELECT * FROM temporal_refresh_outbox WHERE run_id=$1', identity)
    assert row['desired_generation'] == 1 and 'PRIVATE' not in str(row)
    with pytest.raises(asyncpg.RaiseError, match='discarded'):
        await pool.execute('DELETE FROM temporal_refresh_outbox WHERE run_id=$1', identity)
    with pytest.raises(asyncpg.RaiseError, match='monotone'):
        await pool.execute('UPDATE temporal_refresh_outbox SET run_id=$2,parent_id=NULL WHERE run_id=$1', identity, uuid4())


def service(item, *, closed=False, wrong=False):
    from temporalio.client import WorkflowExecutionStatus
    class Client:
        namespace = item.namespace
        def __init__(self):
            self.sent = []
        def get_workflow_handle(self, identity, **kwargs):
            assert identity == item.workflow_id and kwargs == {'first_execution_run_id': str(item.first_execution_run_id)}
            return self
        async def describe(self, **kwargs):
            async def memo():
                return {'gca_dispatch_sha256': item.request_sha256, 'gca_dispatch_contract': 'gca.temporal.start.v1'}
            return SimpleNamespace(namespace=item.namespace, id=item.workflow_id, task_queue=item.task_queue,
                workflow_type='gca.workspace.v1', raw_info=SimpleNamespace(first_run_id=str(uuid4()) if wrong else str(item.first_execution_run_id)),
                memo=memo, status=WorkflowExecutionStatus.COMPLETED if closed else WorkflowExecutionStatus.RUNNING)
        async def signal(self, *args, **kwargs):
            assert args == ('refresh',) and set(kwargs) == {'rpc_timeout'}
            self.sent.append('refresh')
    return Client()


async def test_sdk_transport_sends_no_authority_payload_and_never_starts_workflows(case):
    pool, identity = case
    await enable(pool)
    await update(pool, identity)
    item = await refresh.claim(pool, run_id=identity)
    client = service(item)
    await refresh.TemporalRefreshTransport(client).send_refresh(item)
    assert client.sent == ['refresh']
    assert await refresh.acknowledge(pool, item)


@pytest.mark.parametrize('closed,wrong,category', [(True, False, 'workflow_closed'), (False, True, 'binding')])
async def test_closed_or_foreign_execution_requires_reconciliation(case, closed, wrong, category):
    pool, identity = case
    await enable(pool)
    await update(pool, identity)
    item = await refresh.claim(pool, run_id=identity)
    client = service(item, closed=closed, wrong=wrong)
    with pytest.raises(refresh.RefreshError) as error:
        await refresh.TemporalRefreshTransport(client).send_refresh(item)
    assert error.value.category == category and not client.sent
    await refresh.fail(pool, item, category)
    assert (await pool.fetchrow('SELECT * FROM temporal_refresh_outbox WHERE run_id=$1', identity))['blocked']


async def test_enable_requires_evidence_and_pending_hint_blocks_downgrade(case):
    pool, identity = case
    await pool.execute('UPDATE temporal_dispatch_control SET notification_evidence_reference=NULL')
    with pytest.raises(asyncpg.CheckViolationError):
        await pool.execute('UPDATE temporal_dispatch_control SET notifications_enabled=TRUE')
    await enable(pool)
    await update(pool, identity)
    await pool.execute('UPDATE temporal_dispatch_control SET notifications_enabled=FALSE')
    migration = importlib.import_module('migrations.versions.029_temporal_refresh_outbox')
    with pytest.raises(asyncpg.RaiseError, match='reconcile'):
        await pool.execute(migration.DOWNGRADE_SQL)
