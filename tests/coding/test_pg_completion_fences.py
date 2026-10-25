"""Real PostgreSQL worker completion and metadata recovery; no remote calls."""
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.runs import worker
from app.runs.tool_admission import execution_authority
from tests.coding.test_temporal_workspace import database as workspace_database

pytestmark = pytest.mark.asyncio(loop_scope='module')
database = workspace_database


@pytest.fixture
def configured_worker(monkeypatch):
    monkeypatch.setattr(worker, 'get_settings', lambda: SimpleNamespace(
        worker_lease_seconds=60, executor_version='test-v1', deployment_version='test-v1',
        executor_role='control', worker_step_concurrency=4, allow_dev_auth=False))

    async def forbidden_credentials(*args):
        pytest.fail('information/completion-only work must not load Google credentials')
    monkeypatch.setattr(worker, 'load_google_credentials', forbidden_credentials)

    class ForbiddenGraph:
        async def ainvoke(self, *args, **kwargs):
            pytest.fail('completion-only work must not invoke a model')
    return SimpleNamespace(state=SimpleNamespace(agent_graph=ForbiddenGraph()))


async def prepared(pool, *, completed_write=False):
    identity, step_id = uuid4(), uuid4()
    await pool.execute('''INSERT INTO agent_runs(id,session_id,user_id,request,idempotency_key,executor_version)
      VALUES($1,'synthetic-session','synthetic-user','What is your name?',$2,'test-v1')''', identity, str(identity))
    await pool.execute('''INSERT INTO agent_run_steps(id,run_id,step_key,sequence_no,title,service,operation,input_data)
      VALUES($1,$2,'information',1,'Answer identity','general','answer_information',
        '{"informational_intent":"identity","capability_catalog":{}}')''', step_id, identity)
    if completed_write:
        # Represents a crash after a verified write committed, before run summary.
        # No fake provider is executed and no claim of live Google verification.
        await pool.execute('''UPDATE agent_run_steps SET service='gmail',operation='send',read_only=FALSE,
          status='completed',attempt_count=1,completed_at=now(),
          output_data='{"output":"Synthetic verified send receipt", "verification":"synthetic"}'::jsonb
          WHERE id=$1''', step_id)
    return identity, step_id


async def run_once(app, pool, identity):
    run = await worker.claim_run(pool, 'synthetic-worker')
    assert run['id'] == identity
    await worker.execute_run(app, pool, run)
    assert execution_authority.get() is None
    return dict(await pool.fetchrow('SELECT * FROM agent_runs WHERE id=$1', identity))


async def test_actual_postgres_step_run_and_events_complete(database, configured_worker):
    identity, step_id = await prepared(database)
    current = await run_once(configured_worker, database, identity)
    assert current['status'] == 'completed' and current['functional_completion'] == 100
    assert current['lease_owner'] is None and current['lease_expires_at'] is None
    assert 'Google Workspace Agent' in current['result']['output']
    assert await database.fetchval('SELECT status FROM agent_run_steps WHERE id=$1', step_id) == 'completed'
    for event in ('step_completed', 'run_completed'):
        assert await database.fetchval('SELECT count(*) FROM agent_run_events WHERE run_id=$1 AND event_type=$2',
            identity, event) == 1


async def test_failed_run_event_retries_only_metadata_and_preserves_receipts(database, configured_worker, monkeypatch):
    identity, step_id = await prepared(database, completed_write=True)

    async def forbidden_step(*args):
        pytest.fail('a committed step must never be executed during metadata recovery')
    monkeypatch.setattr(worker, '_execute_step', forbidden_step)
    await database.execute('''CREATE FUNCTION reject_run_event() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN IF NEW.event_type='run_completed' THEN RAISE EXCEPTION 'synthetic completion sink failure'; END IF;
      RETURN NEW; END $$;
      CREATE TRIGGER reject_run_completion BEFORE INSERT ON agent_run_events
        FOR EACH ROW EXECUTE FUNCTION reject_run_event();''')
    try:
        current = await run_once(configured_worker, database, identity)
        assert current['status'] == 'queued' and current['current_phase'] == 'completion_retry'
        assert current['result']['completion_retries'] == 1
        assert current['error_category'] == 'completion_persistence'
        assert current['completed_at'] is None and current['lease_owner'] is None
        assert await database.fetchval('SELECT count(*) FROM agent_run_events WHERE run_id=$1 '
            "AND event_type='run_completed'", identity) == 0
        event = await database.fetchrow('SELECT * FROM agent_run_events WHERE run_id=$1 '
            "AND event_type='completion_retry_scheduled'", identity)
        assert event['payload'] == {'attempt': 1, 'max_retries': 3, 'provider_reexecution': False}
    finally:
        await database.execute('DROP TRIGGER reject_run_completion ON agent_run_events; DROP FUNCTION reject_run_event()')
    current = await run_once(configured_worker, database, identity)
    assert current['status'] == 'completed'
    assert current['result']['output'] == 'Synthetic verified send receipt'
    assert current['error_category'] is None
    assert await database.fetchval('SELECT attempt_count FROM agent_run_steps WHERE id=$1', step_id) == 1
    assert await database.fetchval('SELECT count(*) FROM agent_run_events WHERE run_id=$1 '
        "AND event_type='run_completed'", identity) == 1


async def test_persistent_completion_failure_has_a_bounded_retry_budget(database, configured_worker, monkeypatch):
    identity, step_id = await prepared(database, completed_write=True)

    async def forbidden_step(*args):
        pytest.fail('metadata retry exhaustion must not re-execute completed steps')
    monkeypatch.setattr(worker, '_execute_step', forbidden_step)
    await database.execute('''CREATE FUNCTION reject_run_event() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN IF NEW.event_type='run_completed' THEN RAISE EXCEPTION 'synthetic completion sink failure'; END IF;
      RETURN NEW; END $$;
      CREATE TRIGGER reject_run_completion BEFORE INSERT ON agent_run_events
        FOR EACH ROW EXECUTE FUNCTION reject_run_event();''')
    try:
        for attempt in range(1, 4):
            current = await run_once(configured_worker, database, identity)
            assert current['status'] == 'queued' and current['result']['completion_retries'] == attempt
        current = await run_once(configured_worker, database, identity)
        assert current['status'] == 'partial' and current['lease_owner'] is None
        assert await worker.claim_run(database, 'synthetic-worker') is None
        assert await database.fetchval('SELECT attempt_count FROM agent_run_steps WHERE id=$1', step_id) == 1
        assert await database.fetchval('SELECT count(*) FROM agent_run_events WHERE run_id=$1 '
            "AND event_type='completion_retry_scheduled'", identity) == 3
        assert await database.fetchval('SELECT count(*) FROM agent_run_events WHERE run_id=$1 '
            "AND event_type='run_completed'", identity) == 0
    finally:
        await database.execute('DROP TRIGGER reject_run_completion ON agent_run_events; DROP FUNCTION reject_run_event()')
