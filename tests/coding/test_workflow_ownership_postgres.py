"""Real business-worker claims under immutable owner bindings; loopback only."""
import asyncio
import importlib
import os
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from app.runs import workflow_ownership as ownership

migration = importlib.import_module('migrations.versions.026_workflow_ownership')


@pytest_asyncio.fixture
async def owner_store():
    dsn = os.getenv('COST_LEDGER_TEST_DSN')
    if not dsn:
        pytest.skip('set COST_LEDGER_TEST_DSN to local PostgreSQL')
    target = urlsplit(dsn)
    if target.hostname not in {'localhost', '127.0.0.1', '::1'} or target.query or target.fragment:
        pytest.fail('ownership tests refuse non-loopback targets')
    schema = 'workflow_owner_test_' + uuid4().hex
    conn = await asyncpg.connect(dsn)
    pool = None
    try:
        await conn.execute(f'CREATE SCHEMA "{schema}"')
        await conn.execute(f'SET search_path TO "{schema}"')
        await conn.execute('''CREATE TABLE agent_runs(
          id UUID PRIMARY KEY,user_id TEXT NOT NULL,deleted_at TIMESTAMPTZ,
          status TEXT NOT NULL DEFAULT 'queued',executor_version TEXT NOT NULL DEFAULT 'test-v1',
          cohort_assignment TEXT NOT NULL DEFAULT 'control',canary_id UUID,
          queued_at TIMESTAMPTZ NOT NULL DEFAULT now(),current_phase TEXT,
          started_at TIMESTAMPTZ,heartbeat_at TIMESTAMPTZ,lease_owner TEXT,lease_expires_at TIMESTAMPTZ);
          CREATE TABLE coding_runs(LIKE agent_runs INCLUDING ALL);
          ALTER TABLE coding_runs ADD COLUMN available_at TIMESTAMPTZ DEFAULT now(),
            ADD COLUMN created_at TIMESTAMPTZ DEFAULT now(),ADD COLUMN updated_at TIMESTAMPTZ,
            ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0;
          CREATE TABLE candidate_builds(coding_run_id UUID);
          CREATE TABLE coding_run_events(run_id UUID,user_id TEXT,event_type TEXT,phase TEXT,message TEXT,payload JSONB);''')
        await conn.execute(migration.SCHEMA_SQL)
        pool = await asyncpg.create_pool(dsn, min_size=2, max_size=5, server_settings={'search_path': schema})
        yield pool
    finally:
        if pool:
            await pool.close()
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await conn.close()


TABLES = {'workspace': 'agent_runs', 'coding': 'coding_runs'}


async def admit(pool, kind, *, enabled=True, interval='1 hour'):
    await pool.execute('''UPDATE workflow_ownership_control SET admission_enabled=$1,
      namespace='test-namespace',task_queue='test-queue',workflow_version='test-workflow-v1',
      executor_version='test-v1',evidence_reference='test:reviewed-local-only',
      expires_at=now()+$2::text::interval WHERE workflow_kind=$3''', enabled, interval, kind)


async def run(pool, kind, *, temporal=False, identity=None, user='alice', **overrides):
    identity = identity or uuid4()
    fields = {'id': identity, 'user_id': user}
    if temporal:
        fields.update(orchestration_owner='temporal', temporal_namespace='test-namespace',
            temporal_task_queue='test-queue', temporal_workflow_version='test-workflow-v1',
            temporal_workflow_id=ownership.workflow_id(kind, identity))
    fields.update(overrides)
    # Closed test-owned identifiers only. Values remain parameterized.
    keys = ','.join(fields)
    placeholders = ','.join('$' + str(i + 1) for i in range(len(fields)))
    return await pool.fetchrow('INSERT INTO ' + TABLES[kind] + '(' + keys + ') VALUES(' + placeholders +
        ') RETURNING *', *fields.values())


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['workspace', 'coding'])
async def test_default_postgres_and_temporal_admission_closed(owner_store, kind):
    row = await run(owner_store, kind)
    assert row['orchestration_owner'] == 'postgres' and row['temporal_workflow_id'] is None
    with pytest.raises(asyncpg.RaiseError, match='reviewed scope'):
        await run(owner_store, kind, temporal=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['workspace', 'coding'])
@pytest.mark.parametrize('interval', ['-1 second', '25 hours'])
async def test_expired_or_overlong_admission_does_not_take_new_runs(owner_store, kind, interval):
    await admit(owner_store, kind, interval=interval)
    with pytest.raises(asyncpg.RaiseError, match='reviewed scope'):
        await run(owner_store, kind, temporal=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['workspace', 'coding'])
async def test_temporal_bindings_cannot_be_transferred_or_leased_by_old_workers(owner_store, kind):
    await admit(owner_store, kind)
    row = await run(owner_store, kind, temporal=True)
    for assignment in ("lease_owner='old-worker'", "lease_expires_at=now()", "executor_version='other'",
                       "user_id='bob'", "temporal_task_queue='other'", "orchestration_owner='postgres'"):
        with pytest.raises(asyncpg.RaiseError):
            await owner_store.execute('UPDATE ' + TABLES[kind] + ' SET ' + assignment + ' WHERE id=$1', row['id'])
    # Safe operational projection updates do not transfer ownership.
    await owner_store.execute('UPDATE ' + TABLES[kind] + " SET current_phase='awaiting_signal' WHERE id=$1", row['id'])
    assert await owner_store.fetchval('SELECT lease_owner FROM ' + TABLES[kind] + ' WHERE id=$1', row['id']) is None


@pytest.mark.asyncio
async def test_existing_postgres_run_cannot_be_hot_moved(owner_store):
    row = await run(owner_store, 'workspace')
    await admit(owner_store, 'workspace')
    with pytest.raises(asyncpg.RaiseError, match='immutable'):
        await owner_store.execute("UPDATE agent_runs SET orchestration_owner='temporal' WHERE id=$1", row['id'])


@pytest.mark.asyncio
async def test_bound_scope_and_exact_id_are_mandatory(owner_store):
    await admit(owner_store, 'workspace')
    for changes in ({'temporal_namespace': 'other'}, {'executor_version': 'other'},
                    {'status': 'running'}, {'temporal_workflow_id': 'gca.workspace.wrong'}):
        with pytest.raises(asyncpg.RaiseError):
            await run(owner_store, 'workspace', temporal=True, **changes)
    await admit(owner_store, 'coding')
    with pytest.raises(asyncpg.RaiseError):
        await run(owner_store, 'coding', temporal=True, attempt_count=1)


@pytest.mark.asyncio
async def test_actual_workers_skip_temporal_even_when_older_and_keep_claims_disjoint(owner_store, monkeypatch):
    from app.runs import worker as workspace
    from app.coding import worker as coding
    settings = SimpleNamespace(worker_lease_seconds=60, executor_version='test-v1',
        deployment_version='test-v1', executor_role='control', coding_worker_lease_seconds=60)
    monkeypatch.setattr(workspace, 'get_settings', lambda: settings)
    monkeypatch.setattr(coding, 'get_settings', lambda: settings)
    # Same UUID across business kinds must still map to distinct workflows.
    identity = uuid4()
    for kind in TABLES:
        await admit(owner_store, kind)
        await run(owner_store, kind, temporal=True, identity=identity)
    regular = [await run(owner_store, 'workspace') for _ in range(2)]
    claims = await asyncio.gather(workspace.claim_run(owner_store, 'one'), workspace.claim_run(owner_store, 'two'))
    assert {row['id'] for row in claims} == {row['id'] for row in regular}
    assert await workspace.claim_run(owner_store, 'three') is None
    ordinary_coding = await run(owner_store, 'coding')
    claimed = await coding._claim(owner_store)
    assert claimed['id'] == ordinary_coding['id'] and await coding._claim(owner_store) is None
    for row in claims:
        await ownership.require_postgres_lease(owner_store, 'workspace', row)
    await ownership.require_postgres_lease(owner_store, 'coding', claimed)
    assert ownership.workflow_id('workspace', identity) != ownership.workflow_id('coding', identity)


@pytest.mark.asyncio
async def test_external_calls_are_fenced_before_heartbeat_or_credentials(owner_store, monkeypatch):
    from app.runs import worker
    await admit(owner_store, 'workspace')
    row = await run(owner_store, 'workspace', temporal=True)
    async def forbidden(*args, **kwargs):
        pytest.fail('credentials must not be loaded for another execution owner')
    monkeypatch.setattr(worker, 'load_google_credentials', forbidden)
    with pytest.raises(ownership.WorkflowOwnershipError):
        await worker.execute_run(None, owner_store, row)
    assert await owner_store.fetchval('SELECT heartbeat_at FROM agent_runs WHERE id=$1', row['id']) is None


@pytest.mark.asyncio
async def test_stale_or_wrong_tenant_lease_cannot_execute(owner_store):
    row = dict(await run(owner_store, 'workspace', status='running', lease_owner='one'))
    with pytest.raises(ownership.WorkflowOwnershipError):
        await ownership.require_postgres_lease(owner_store, 'workspace', row)
    await owner_store.execute("UPDATE agent_runs SET lease_expires_at=now()+interval '1 minute' WHERE id=$1", row['id'])
    for changes in ({'user_id': 'bob'}, {'executor_version': 'other'}, {'lease_owner': 'two'}):
        with pytest.raises(ownership.WorkflowOwnershipError):
            await ownership.require_postgres_lease(owner_store, 'workspace', {**row, **changes})
    await ownership.require_postgres_lease(owner_store, 'workspace', row)
    await owner_store.execute("UPDATE agent_runs SET lease_expires_at=now()-interval '1 second' WHERE id=$1", row['id'])
    with pytest.raises(ownership.WorkflowOwnershipError):
        await ownership.require_postgres_lease(owner_store, 'workspace', row)


@pytest.mark.asyncio
async def test_rollback_disables_only_new_admission_not_existing_ownership(owner_store):
    await admit(owner_store, 'workspace')
    row = await run(owner_store, 'workspace', temporal=True)
    await admit(owner_store, 'workspace', enabled=False)
    with pytest.raises(asyncpg.RaiseError):
        await run(owner_store, 'workspace', temporal=True)
    assert (await run(owner_store, 'workspace'))['orchestration_owner'] == 'postgres'
    with pytest.raises(asyncpg.RaiseError, match='reconcile retained'):
        await owner_store.execute(migration.DOWNGRADE_SQL)
    await owner_store.execute('DELETE FROM agent_runs WHERE id=$1', row['id'])
    # Only test-owned fixture data is deleted. Production artifacts need retention/reconciliation.
    await owner_store.execute(migration.DOWNGRADE_SQL)
    async with owner_store.acquire() as conn:
        assert await ownership.postgres_claim_predicate(conn) == 'TRUE'


@pytest.mark.asyncio
async def test_enabled_admission_blocks_downgrade_even_before_first_run(owner_store):
    await admit(owner_store, 'coding')
    with pytest.raises(asyncpg.RaiseError, match='disable Temporal'):
        await owner_store.execute(migration.DOWNGRADE_SQL)


@pytest.mark.asyncio
async def test_coding_entrypoint_cannot_call_provider_for_another_owner(owner_store, monkeypatch):
    from app.coding import worker
    await admit(owner_store, 'coding')
    row = await run(owner_store, 'coding', temporal=True)
    async def injected_claim(*args):
        return row
    async def forbidden(*args):
        pytest.fail('provider must not be called for another owner')
    monkeypatch.setattr(worker, '_claim', injected_claim)
    monkeypatch.setattr(worker, '_plan', forbidden)
    with pytest.raises(ownership.WorkflowOwnershipError):
        await worker.process_one_coding_run(owner_store)


@pytest.mark.asyncio
async def test_policy_audit_survives_rollback_and_forward_repair(owner_store):
    await admit(owner_store, 'workspace')
    await admit(owner_store, 'workspace', enabled=False)
    assert await owner_store.fetchval('SELECT count(*) FROM workflow_ownership_audit') == 2
    await owner_store.execute(migration.DOWNGRADE_SQL)
    await owner_store.execute(migration.SCHEMA_SQL)
    assert await owner_store.fetchval('SELECT count(*) FROM workflow_ownership_audit') == 2
    assert await owner_store.fetchval('SELECT count(*) FROM workflow_ownership_control WHERE admission_enabled') == 0


@pytest.mark.asyncio
async def test_rejected_stale_claim_does_not_kill_workspace_worker_loop(monkeypatch):
    from app.runs import worker
    stop = asyncio.Event()
    calls = []
    async def claim(*args):
        return {'id': uuid4()}
    async def fenced(*args):
        calls.append('fenced')
        stop.set()
        raise ownership.WorkflowOwnershipError('stale lease')
    monkeypatch.setattr(worker, 'claim_run', claim)
    monkeypatch.setattr(worker, 'execute_run', fenced)
    await worker.worker_loop(None, None, stop)
    assert calls == ['fenced']
