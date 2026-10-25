"""Tiny real SQL fixture plus provider-call counters; no cloud/model calls."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import os
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio
from langchain_core.tools import tool

from app.runs.tool_admission import (
    ExecutionAuthority, ToolAdmissionDenied, execution_authority, require_tool_admission,
    CompletionFenced, fenced_postgres_commit, fenced_step_commit,
)


@pytest_asyncio.fixture
async def admitted():
    dsn = os.getenv('COST_LEDGER_TEST_DSN')
    if not dsn:
        pytest.skip('set COST_LEDGER_TEST_DSN to local PostgreSQL')
    target = urlsplit(dsn)
    if target.hostname not in {'localhost', '127.0.0.1', '::1'} or target.query or target.fragment:
        pytest.fail('tool fence tests refuse non-loopback targets')
    schema = 'tool_admission_test_' + uuid4().hex
    admin = await asyncpg.connect(dsn)
    pool = None
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        await admin.execute(f'SET search_path TO "{schema}"')
        await admin.execute('''CREATE TABLE agent_runs(id UUID PRIMARY KEY,user_id TEXT,status TEXT,deleted_at TIMESTAMPTZ,
          executor_version TEXT,orchestration_owner TEXT,lease_owner TEXT,lease_expires_at TIMESTAMPTZ);
          CREATE TABLE agent_run_steps(id UUID PRIMARY KEY,run_id UUID REFERENCES agent_runs(id),status TEXT);
          CREATE TABLE temporal_workspace_steps(run_id UUID,step_id UUID,execution_token UUID,workflow_execution_id UUID,state TEXT);
          CREATE TABLE temporal_start_intents(run_id UUID,workflow_kind TEXT,state TEXT,first_execution_run_id UUID);''')
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3, server_settings={'search_path': schema})
        run_id, step_id = uuid4(), uuid4()
        await pool.execute("INSERT INTO agent_runs VALUES($1,'alice','running',NULL,'version-1','postgres','lease-one',now()+interval '1 minute')", run_id)
        await pool.execute("INSERT INTO agent_run_steps VALUES($1,$2,'running')", step_id, run_id)
        authority = ExecutionAuthority('postgres', run_id, 'alice', 'version-1', 'lease-one')
        state = {'run_id': str(run_id), 'step_id': str(step_id), 'user_id': 'alice'}
        yield pool, authority, state
    finally:
        if pool:
            await pool.close()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


@pytest.mark.asyncio
async def test_legacy_path_without_durable_scope_is_not_reinterpreted_as_a_run():
    await require_tool_admission(None, {'session_id': 'legacy'})


@pytest.mark.asyncio
async def test_actual_tool_wrapper_stops_between_calls_after_cancel(admitted, monkeypatch):
    pool, authority, state = admitted
    from app.agents import supervisor
    calls = []
    @tool
    async def mutation_probe() -> dict:
        """Synthetic provider counter, never an external service."""
        calls.append('provider')
        return {'ok': True}
    async def record(*args, **kwargs):
        calls.append('recorded')
    monkeypatch.setattr(supervisor, '_record_tool_call', record)
    import importlib
    await pool.execute('ALTER TABLE agent_run_steps ADD COLUMN attempt_count INTEGER DEFAULT 1, '
        'ADD COLUMN read_only BOOLEAN DEFAULT FALSE')
    await pool.execute(importlib.import_module('migrations.versions.031_tool_invocation_receipts').SCHEMA_SQL)
    await event_table(pool)
    token = execution_authority.set(authority)
    try:
        _, result, _ = await supervisor.execute_tool_call(mutation_probe, {'id': 'call-one','args': {}}, state, pool)
        assert result == {'ok': True}
        await pool.execute("UPDATE agent_runs SET status='cancelled' WHERE id=$1", authority.run_id)
        with pytest.raises(ToolAdmissionDenied) as error:
            await supervisor.execute_tool_call(mutation_probe, {'id': 'call-two','args': {}}, state, pool)
        assert error.value.category == 'cancelled' and error.value.evidence['provider_called'] is False
        assert calls == ['provider', 'recorded']
    finally:
        execution_authority.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize('assignment,reason', [
    ("deleted_at=now()", 'run_or_step_not_visible'),
    ("lease_owner='other-worker'", 'postgres_lease_lost'),
    ("lease_expires_at=now()-interval '1 second'", 'postgres_lease_lost'),
    ("executor_version='other-version'", 'live_execution_binding_changed'),
    ("orchestration_owner='temporal'", 'live_execution_binding_changed'),
])
async def test_current_database_state_fences_stale_context(admitted, assignment, reason):
    pool, authority, state = admitted
    await pool.execute('UPDATE agent_runs SET ' + assignment + ' WHERE id=$1', authority.run_id)
    token = execution_authority.set(authority)
    try:
        with pytest.raises(ToolAdmissionDenied) as error:
            await require_tool_admission(pool, state)
        assert error.value.evidence['reason_code'] == reason
    finally:
        execution_authority.reset(token)


@pytest.mark.asyncio
async def test_missing_scope_tenant_step_and_step_state_are_fail_closed(admitted):
    pool, authority, state = admitted
    with pytest.raises(ToolAdmissionDenied):
        await require_tool_admission(pool, state)
    token = execution_authority.set(authority)
    try:
        for changed in ({**state, 'user_id': 'bob'}, {**state, 'step_id': str(uuid4())},
                        {**state, 'run_id': 'not-a-uuid'}, {**state, 'run_id': None}):
            with pytest.raises(ToolAdmissionDenied):
                await require_tool_admission(pool, changed)
        await pool.execute("UPDATE agent_run_steps SET status='completed' WHERE id=$1", UUID(state['step_id']))
        with pytest.raises(ToolAdmissionDenied):
            await require_tool_admission(pool, state)
    finally:
        execution_authority.reset(token)


@pytest.mark.asyncio
async def test_context_is_inherited_by_parallel_step_tasks_and_reset_after_scope(admitted):
    pool, authority, state = admitted
    token = execution_authority.set(authority)
    try:
        await asyncio.gather(require_tool_admission(pool, state), require_tool_admission(pool, state))
    finally:
        execution_authority.reset(token)
    assert execution_authority.get() is None


@pytest.mark.asyncio
async def test_temporal_nonce_lineage_uncertainty_and_deadline_are_checked(admitted):
    pool, authority, state = admitted
    nonce, execution = uuid4(), uuid4()
    await pool.execute("UPDATE agent_runs SET orchestration_owner='temporal',lease_owner=NULL,lease_expires_at=NULL WHERE id=$1", authority.run_id)
    step_id = UUID(state['step_id'])
    await pool.execute("INSERT INTO temporal_workspace_steps VALUES($1,$2,$3,$4,'executing')", authority.run_id, step_id, nonce, execution)
    await pool.execute("INSERT INTO temporal_start_intents VALUES($1,'workspace','started',$2)", authority.run_id, execution)
    scope = ExecutionAuthority('temporal', authority.run_id, 'alice', 'version-1', str(nonce), step_id, execution,
        datetime.now(timezone.utc)+timedelta(minutes=1))
    for current, allowed in ((scope, True), (replace(scope, token=str(uuid4())), False),
                             (replace(scope, workflow_execution_id=uuid4()), False),
                             (replace(scope, token='malformed'), False),
                             (replace(scope, deadline=None), False),
                             (replace(scope, deadline=datetime.now()), False),
                             (replace(scope, deadline=datetime.now(timezone.utc)-timedelta(seconds=1)), False)):
        token = execution_authority.set(current)
        try:
            if allowed:
                await require_tool_admission(pool, state)
            else:
                with pytest.raises(ToolAdmissionDenied):
                    await require_tool_admission(pool, state)
        finally:
            execution_authority.reset(token)
    await pool.execute("UPDATE temporal_workspace_steps SET state='uncertain'")
    token = execution_authority.set(scope)
    try:
        with pytest.raises(ToolAdmissionDenied):
            await require_tool_admission(pool, state)
    finally:
        execution_authority.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize('read_only,integrity', [(True, 100), (False, 0)])
async def test_actual_postgres_worker_preserves_cancellation_and_clears_only_its_lease(admitted, monkeypatch, read_only, integrity):
    pool, authority, state = admitted
    from app.runs import worker
    await pool.execute('''ALTER TABLE agent_runs ADD COLUMN current_step_id UUID,ADD COLUMN heartbeat_at TIMESTAMPTZ,
      ADD COLUMN side_effect_integrity INTEGER NOT NULL DEFAULT 100;
      ALTER TABLE agent_run_steps ADD COLUMN read_only BOOLEAN NOT NULL DEFAULT TRUE,
        ADD COLUMN error_category TEXT,ADD COLUMN error_message TEXT,ADD COLUMN completed_at TIMESTAMPTZ;
      CREATE TABLE agent_run_events(run_id UUID,user_id TEXT,event_type TEXT,phase TEXT,message TEXT,payload JSONB);''')
    await pool.execute('UPDATE agent_run_steps SET read_only=$1', read_only)
    claimed = False
    async def claim(*args):
        nonlocal claimed
        if claimed:
            return None
        claimed = True
        return {'id': UUID(state['step_id']), 'operation': 'answer_information'}
    async def dependencies(*args):
        return []
    async def cancelled(*args):
        assert execution_authority.get().token == 'lease-one'
        await pool.execute("UPDATE agent_runs SET status='cancelled' WHERE id=$1", authority.run_id)
        await require_tool_admission(pool, state)
    monkeypatch.setattr(worker, '_claim_step', claim)
    monkeypatch.setattr(worker, '_dependency_context', dependencies)
    monkeypatch.setattr(worker, '_execute_step', cancelled)
    run = dict(await pool.fetchrow('SELECT * FROM agent_runs WHERE id=$1', authority.run_id))
    run['plan'] = {'steps': [{'operation': 'answer_information'}]}
    await worker.execute_run(None, pool, run)
    current = await pool.fetchrow('SELECT * FROM agent_runs WHERE id=$1', authority.run_id)
    assert current['status'] == 'cancelled' and current['lease_owner'] is None
    assert current['side_effect_integrity'] == integrity
    assert await pool.fetchval('SELECT status FROM agent_run_steps') == 'cancelled'
    assert await pool.fetchval('SELECT event_type FROM agent_run_events') == 'execution_cancelled'
    assert execution_authority.get() is None


@pytest.mark.asyncio
async def test_actual_worker_does_not_finalize_a_replacement_owners_run(admitted, monkeypatch):
    pool, authority, state = admitted
    from app.runs import worker
    await pool.execute('ALTER TABLE agent_runs ADD COLUMN current_step_id UUID,ADD COLUMN heartbeat_at TIMESTAMPTZ')
    claimed = False
    async def claim(*args):
        nonlocal claimed
        if claimed:
            return None
        claimed = True
        return {'id': UUID(state['step_id']), 'operation': 'answer_information'}
    async def dependencies(*args):
        return []
    async def stolen(*args):
        await pool.execute("UPDATE agent_runs SET lease_owner='replacement' WHERE id=$1", authority.run_id)
        await require_tool_admission(pool, state)
    monkeypatch.setattr(worker, '_claim_step', claim)
    monkeypatch.setattr(worker, '_dependency_context', dependencies)
    monkeypatch.setattr(worker, '_execute_step', stolen)
    run = dict(await pool.fetchrow('SELECT * FROM agent_runs WHERE id=$1', authority.run_id))
    run['plan'] = {'steps': [{'operation': 'answer_information'}]}
    await worker.execute_run(None, pool, run)
    current = await pool.fetchrow('SELECT * FROM agent_runs WHERE id=$1', authority.run_id)
    assert current['status'] == 'running' and current['lease_owner'] == 'replacement'
    assert execution_authority.get() is None


@pytest.mark.asyncio
async def test_database_failure_is_sanitized_and_does_not_admit_a_call(admitted):
    _, authority, state = admitted
    from app.agents.errors import ExecutionFailure
    class BrokenPool:
        def acquire(self, **kwargs):
            raise RuntimeError('PRIVATE CONNECTION DETAILS')
    token = execution_authority.set(authority)
    try:
        with pytest.raises(ExecutionFailure) as error:
            await require_tool_admission(BrokenPool(), state)
        assert error.value.category == 'network' and 'PRIVATE' not in str(error.value)
        assert error.value.evidence['provider_called'] is False
    finally:
        execution_authority.reset(token)


@pytest.mark.asyncio
async def test_temporal_cancelled_workflow_finalizes_uncertainty_before_return(monkeypatch):
    pytest.importorskip('temporalio')
    from temporalio import workflow
    from app.runs.temporal_workflow import WorkspaceWorkflow
    from app.runs.temporal_contracts import RunSnapshot, WorkflowReference
    calls = []
    async def execute(name, argument, **kwargs):
        calls.append(name)
        if name == 'gca.workspace.inspect':
            return RunSnapshot('cancelled')
        assert argument.reason == 'cancelled'
        return 'cancelled'
    monkeypatch.setattr(workflow, 'execute_activity', execute)
    monkeypatch.setattr(workflow, 'now', lambda: datetime.now(timezone.utc))
    reference = WorkflowReference('gca.temporal.start.v1','workspace',str(uuid4()),'workflow-v1','executor-v1')
    assert await WorkspaceWorkflow().run(reference) == 'cancelled'
    assert calls == ['gca.workspace.inspect', 'gca.workspace.finalize']


async def event_table(pool):
    await pool.execute('''CREATE TABLE agent_run_events(id BIGSERIAL PRIMARY KEY,run_id UUID,step_id UUID,user_id TEXT,
      event_type TEXT,phase TEXT,message TEXT,payload JSONB)''')


@pytest.mark.asyncio
async def test_step_and_completion_event_share_one_transaction(admitted):
    pool, authority, state = admitted
    from app.runs.repository import append_event_conn
    await event_table(pool)
    run = dict(await pool.fetchrow('SELECT * FROM agent_runs WHERE id=$1', authority.run_id))
    step = {'id': UUID(state['step_id'])}
    token = execution_authority.set(authority)
    try:
        await pool.execute('''CREATE FUNCTION reject_event() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN RAISE EXCEPTION 'synthetic event failure'; END $$;
          CREATE TRIGGER reject_completion BEFORE INSERT ON agent_run_events FOR EACH ROW EXECUTE FUNCTION reject_event();''')
        with pytest.raises(asyncpg.RaiseError, match='synthetic event failure'):
            async with fenced_step_commit(pool, run, step, succeeded=True) as conn:
                await conn.execute("UPDATE agent_run_steps SET status='completed' WHERE id=$1", step['id'])
                await append_event_conn(conn, run['id'], run['user_id'], 'step_completed', step_id=step['id'])
        assert await pool.fetchval('SELECT status FROM agent_run_steps') == 'running'
        assert await pool.fetchval('SELECT count(*) FROM agent_run_events') == 0
    finally:
        execution_authority.reset(token)


@pytest.mark.asyncio
async def test_late_completion_cannot_write_after_lease_transfer(admitted):
    pool, authority, state = admitted
    run = dict(await pool.fetchrow('SELECT * FROM agent_runs WHERE id=$1', authority.run_id))
    await pool.execute("UPDATE agent_runs SET lease_owner='replacement' WHERE id=$1", authority.run_id)
    token = execution_authority.set(authority)
    try:
        with pytest.raises(CompletionFenced) as error:
            async with fenced_step_commit(pool, run, {'id': UUID(state['step_id'])}, succeeded=True):
                pytest.fail('stale completion must not enter the commit section')
        assert 'provider_called' not in error.value.evidence
        assert error.value.evidence['prior_effects'] == 'requires_reconciliation'
        assert await pool.fetchval('SELECT status FROM agent_run_steps') == 'running'
        assert await pool.fetchval('SELECT lease_owner FROM agent_runs') == 'replacement'
    finally:
        execution_authority.reset(token)


@pytest.mark.asyncio
async def test_run_completion_serializes_with_actual_cancellation_predicate(admitted):
    pool, authority, _ = admitted
    run = dict(await pool.fetchrow('SELECT * FROM agent_runs WHERE id=$1', authority.run_id))
    token = execution_authority.set(authority)
    cancellation = None
    try:
        async with pool.acquire() as cancel_conn:
            pid = cancel_conn.get_server_pid()
            async with fenced_postgres_commit(pool, run) as conn:
                cancellation = asyncio.create_task(cancel_conn.execute("UPDATE agent_runs SET status='cancelled' WHERE id=$1 "
                    "AND status IN ('queued','awaiting_approval','running')", authority.run_id))
                async def wait_for_row_lock():
                    while not await pool.fetchval("SELECT wait_event_type='Lock' FROM pg_stat_activity WHERE pid=$1", pid):
                        await asyncio.sleep(0.01)
                await asyncio.wait_for(wait_for_row_lock(), timeout=2)
                assert not cancellation.done()
                await conn.execute("UPDATE agent_runs SET status='completed' WHERE id=$1", authority.run_id)
            assert await cancellation == 'UPDATE 0'
        assert await pool.fetchval('SELECT status FROM agent_runs') == 'completed'
    finally:
        execution_authority.reset(token)
        if cancellation is not None and not cancellation.done():
            await cancellation


@pytest.mark.asyncio
@pytest.mark.parametrize('reject_event', [False, True])
async def test_temporal_step_and_journal_completion_are_atomic(admitted, reject_event):
    pool, original, state = admitted
    from app.runs.repository import append_event_conn
    nonce, execution = uuid4(), uuid4()
    step_id = UUID(state['step_id'])
    await pool.execute("UPDATE agent_runs SET orchestration_owner='temporal',lease_owner=NULL,lease_expires_at=NULL")
    await pool.execute('ALTER TABLE temporal_workspace_steps ADD COLUMN completed_at TIMESTAMPTZ,ADD COLUMN error_category TEXT')
    await pool.execute("INSERT INTO temporal_workspace_steps VALUES($1,$2,$3,$4,'executing',NULL,NULL)",
        original.run_id, step_id, nonce, execution)
    await pool.execute("INSERT INTO temporal_start_intents VALUES($1,'workspace','started',$2)", original.run_id, execution)
    await event_table(pool)
    authority = ExecutionAuthority('temporal', original.run_id, 'alice','version-1',str(nonce),step_id,execution,
        datetime.now(timezone.utc)+timedelta(minutes=1))
    run = dict(await pool.fetchrow('SELECT * FROM agent_runs'))
    token = execution_authority.set(authority)
    try:
        async def complete():
            async with fenced_step_commit(pool, run, {'id': step_id}, succeeded=True) as conn:
                await conn.execute("UPDATE agent_run_steps SET status='completed' WHERE id=$1", step_id)
                await append_event_conn(conn, run['id'], 'alice','step_completed',step_id=step_id)
        if reject_event:
            await pool.execute('''CREATE FUNCTION reject_event() RETURNS trigger LANGUAGE plpgsql AS $$
              BEGIN RAISE EXCEPTION 'synthetic event failure'; END $$;
              CREATE TRIGGER reject_completion BEFORE INSERT ON agent_run_events FOR EACH ROW EXECUTE FUNCTION reject_event();''')
            with pytest.raises(asyncpg.RaiseError):
                await complete()
            assert await pool.fetchval('SELECT status FROM agent_run_steps') == 'running'
            assert await pool.fetchval('SELECT state FROM temporal_workspace_steps') == 'executing'
            assert await pool.fetchval('SELECT count(*) FROM agent_run_events') == 0
        else:
            await complete()
            assert await pool.fetchval('SELECT status FROM agent_run_steps') == 'completed'
            assert await pool.fetchval('SELECT state FROM temporal_workspace_steps') == 'succeeded'
            assert await pool.fetchval('SELECT completed_at IS NOT NULL FROM temporal_workspace_steps')
            assert await pool.fetchval('SELECT count(*) FROM agent_run_events') == 1
    finally:
        execution_authority.reset(token)
