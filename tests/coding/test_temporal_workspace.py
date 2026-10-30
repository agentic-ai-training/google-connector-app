"""Actual Workspace executor through SDK ActivityEnvironment and disposable SQL."""
import asyncio
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

pytest.importorskip('temporalio')
from temporalio import workflow
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

from app.runs import temporal_dispatch as dispatch
from app.runs.temporal_activities import WorkspaceActivities
from app.runs.temporal_contracts import FinalizeRequest, StepRequest, WorkflowReference
from app.runs.temporal_workflow import WorkspaceWorkflow

pytestmark = pytest.mark.asyncio(loop_scope='module')


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def database():
    dsn = os.getenv('COST_LEDGER_TEST_DSN')
    if not dsn:
        pytest.skip('set COST_LEDGER_TEST_DSN to loopback PostgreSQL')
    target = urlsplit(dsn)
    if target.hostname not in {'localhost', '127.0.0.1', '::1'} or target.query or target.fragment:
        pytest.fail('Temporal tests refuse non-loopback targets')
    name = 'temporal_workspace_test_' + uuid4().hex
    temporary_dsn = urlunsplit(target._replace(path='/' + name))
    admin = await asyncpg.connect(dsn)
    pool = None
    created = False
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        created = True
        env = {'PATH': os.environ.get('PATH', ''), 'DATABASE_URL': temporary_dsn,
               'PYTHONDONTWRITEBYTECODE': '1', 'LANGCHAIN_TRACING_V2': 'false'}
        process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'alembic', 'upgrade', 'head',
            cwd=Path(__file__).resolve().parents[2], env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            pytest.fail('disposable migration timed out')
        if process.returncode:
            pytest.fail('disposable migration failed: ' + stderr.decode(errors='replace')[-1000:].replace(temporary_dsn, '[local]'))
        from app.db.connection import _init_connection
        pool = await asyncpg.create_pool(temporary_dsn, min_size=2, max_size=5, init=_init_connection)
        yield pool
    finally:
        if pool:
            await pool.close()
        if created:
            await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()


async def prepared(pool, *, receipt=True):
    await pool.execute('''UPDATE workflow_ownership_control SET admission_enabled=TRUE,namespace='test-namespace',
      task_queue='test-queue',workflow_version='test-workflow-v1',executor_version='test-v1',
      evidence_reference='test:private-no-provider',expires_at=now()+interval '1 hour' WHERE workflow_kind='workspace' ''')
    identity, step_id, execution = uuid4(), uuid4(), uuid4()
    from app.runs.schemas import ExecutionPlan, PlanStep
    plan = ExecutionPlan(objective='Answer identity', services=['general'], steps=[
        PlanStep(id='information', title='Answer identity', service='general', operation='answer_information')])
    await pool.execute('''INSERT INTO agent_runs(id,session_id,user_id,request,idempotency_key,executor_version,
      orchestration_owner,temporal_namespace,temporal_workflow_id,temporal_task_queue,temporal_workflow_version,plan)
      VALUES($1,'synthetic-session','synthetic-user','What is your name?',$2,'test-v1',
        'temporal','test-namespace',$3,'test-queue','test-workflow-v1',$4::jsonb)''',
      identity, str(identity), 'gca.workspace.' + str(identity), plan.model_dump())
    await pool.execute('''INSERT INTO agent_run_steps(id,run_id,step_key,sequence_no,title,service,operation,input_data)
      VALUES($1,$2,'information',1,'Answer identity','general','answer_information',
        '{"informational_intent":"identity","capability_catalog":{}}')''', step_id, identity)
    if receipt:
        # Bind through the real start claim/ack contract, not a disconnected test flag.
        await pool.execute("UPDATE temporal_dispatch_control SET enabled=TRUE,evidence_reference='test:local-sdk' ")
        item = await dispatch.claim(pool)
        assert item.run_id == identity
        assert await dispatch.acknowledge(pool, item,
            dispatch.StartReceipt(item.namespace, item.workflow_id, execution, item.sha256))
    reference = WorkflowReference('gca.temporal.start.v1', 'workspace', str(identity), 'test-workflow-v1', 'test-v1')
    environment = ActivityEnvironment()
    environment.info = replace(environment.info, namespace='test-namespace', workflow_namespace='test-namespace',
        workflow_id='gca.workspace.' + str(identity), workflow_run_id=str(execution), workflow_type='gca.workspace.v1',
        task_queue='test-queue', activity_id='step.' + str(step_id), started_time=datetime.now(timezone.utc),
        start_to_close_timeout=timedelta(minutes=5))
    class ForbiddenGraph:
        async def ainvoke(self, *args, **kwargs):
            pytest.fail('informational execution must not call a model')
    app = SimpleNamespace(state=SimpleNamespace(agent_graph=ForbiddenGraph()))
    activities = WorkspaceActivities(pool, app, namespace='test-namespace', task_queue='test-queue',
        workflow_version='test-workflow-v1', executor_version='test-v1')
    return activities, environment, reference, StepRequest(reference, str(step_id))


async def test_real_sdk_sandbox_prepares_without_database_or_provider_imports():
    SandboxedWorkflowRunner().prepare_workflow(workflow._Definition.must_from_class(WorkspaceWorkflow))


async def test_actual_informational_executor_and_completion_without_provider(database, monkeypatch):
    activities, environment, reference, request = await prepared(database)
    from app.db import oauth_credentials
    async def forbidden(*args):
        pytest.fail('Google credentials must not be loaded')
    monkeypatch.setattr(oauth_credentials, 'load_google_credentials', forbidden)
    snapshot = await environment.run(activities.inspect, reference)
    assert snapshot.ready[0].step_id == request.step_id
    assert await environment.run(activities.execute_step, request) == 'succeeded'
    assert await environment.run(activities.execute_step, request) == 'succeeded'
    assert await environment.run(activities.finalize, FinalizeRequest(reference)) == 'completed'
    row = await database.fetchrow('SELECT * FROM agent_runs WHERE id=$1', UUID(reference.run_id))
    assert row['functional_completion'] == 100 and row['side_effect_integrity'] == 100
    assert 'Google Workspace Agent' in row['result']['output']
    assert row['lease_owner'] is None and row['lease_expires_at'] is None


async def test_receipt_and_wrong_execution_are_fenced_before_tools(database):
    activities, environment, reference, request = await prepared(database, receipt=False)
    assert (await environment.run(activities.inspect, reference)).status == 'awaiting_start_receipt'
    assert await environment.run(activities.execute_step, request) == 'not_admitted'
    await database.execute('''UPDATE temporal_start_intents SET state='started',first_execution_run_id=$1
      WHERE run_id=$2''', uuid4(), UUID(reference.run_id))
    with pytest.raises(ApplicationError, match='lineage'):
        await environment.run(activities.execute_step, request)


async def test_unknown_failure_and_redelivery_never_repeat_step(database, monkeypatch):
    activities, environment, reference, request = await prepared(database)
    from app.runs import worker
    calls = []
    async def uncertain(*args):
        calls.append('possible-effect')
        raise ConnectionError('PRIVATE PROVIDER BODY')
    monkeypatch.setattr(worker, '_execute_step', uncertain)
    assert await environment.run(activities.execute_step, request) == 'uncertain'
    assert await environment.run(activities.execute_step, request) == 'uncertain'
    assert calls == ['possible-effect']
    assert await environment.run(activities.finalize, FinalizeRequest(reference)) == 'failed'
    row = await database.fetchrow('SELECT * FROM agent_runs WHERE id=$1', UUID(reference.run_id))
    assert row['side_effect_integrity'] == 0 and row['functional_completion'] == 0
    assert 'PRIVATE' not in str(row['incident_summary'])
    await environment.run(activities.inspect, reference)
    assert await database.fetchval('SELECT count(*) FROM failure_incidents WHERE run_id=$1', UUID(reference.run_id)) == 1
    assert await database.fetchval('SELECT count(*) FROM workflow_evaluations WHERE run_id=$1', UUID(reference.run_id)) == 1


async def test_cancellation_preserves_uncertainty_instead_of_repeating(database, monkeypatch):
    activities, environment, reference, request = await prepared(database)
    from app.runs import worker
    async def cancelled(*args):
        raise asyncio.CancelledError()
    monkeypatch.setattr(worker, '_execute_step', cancelled)
    with pytest.raises(asyncio.CancelledError):
        await environment.run(activities.execute_step, request)
    assert await environment.run(activities.execute_step, request) == 'uncertain'
    assert await environment.run(activities.finalize, FinalizeRequest(reference, 'cancelled')) == 'cancelled'


async def test_deleted_parent_is_not_reconstructed(database):
    activities, environment, reference, request = await prepared(database)
    await database.execute('DELETE FROM agent_runs WHERE id=$1', UUID(reference.run_id))
    assert (await environment.run(activities.inspect, reference)).status == 'deleted'
    assert await environment.run(activities.execute_step, request) == 'cancelled'
    assert await environment.run(activities.finalize, FinalizeRequest(reference)) == 'deleted'


async def test_actual_workflow_drives_private_ledger_without_private_history(database, monkeypatch):
    activities, environment, reference, request = await prepared(database)
    calls = []
    async def execute(name, argument, **options):
        calls.append((name, asdict(argument), options))
        if name == 'gca.workspace.inspect':
            return await environment.run(activities.inspect, argument)
        if name == 'gca.workspace.step':
            assert options['retry_policy'].maximum_attempts == 1
            return await environment.run(activities.execute_step, argument)
        return await environment.run(activities.finalize, argument)
    monkeypatch.setattr(workflow, 'execute_activity', execute)
    monkeypatch.setattr(workflow, 'now', lambda: datetime.now(timezone.utc))
    assert await WorkspaceWorkflow().run(reference) == 'completed'
    assert [item[0] for item in calls] == ['gca.workspace.inspect','gca.workspace.step',
        'gca.workspace.inspect','gca.workspace.finalize','gca.workspace.inspect']
    assert 'What is your name' not in str(calls) and 'synthetic-user' not in str(calls)


async def test_duplicate_inflight_delivery_cannot_overwrite_uncertainty(database, monkeypatch):
    activities, environment, reference, request = await prepared(database)
    from app.runs import worker
    original = worker._execute_step
    entered, release = asyncio.Event(), asyncio.Event()
    async def delayed(*args):
        entered.set()
        await release.wait()
        return await original(*args)
    monkeypatch.setattr(worker, '_execute_step', delayed)
    first = asyncio.create_task(environment.run(activities.execute_step, request))
    await asyncio.wait_for(entered.wait(), timeout=5)
    duplicate = ActivityEnvironment()
    duplicate.info = replace(environment.info, attempt=2)
    assert await duplicate.run(activities.execute_step, request) == 'uncertain'
    release.set()
    assert await first == 'uncertain'
    assert await environment.run(activities.finalize, FinalizeRequest(reference)) == 'failed'
    row = await database.fetchrow('SELECT * FROM agent_runs WHERE id=$1', UUID(reference.run_id))
    assert row['side_effect_integrity'] == 0 and row['functional_completion'] < 100


async def test_duplicate_after_atomic_step_commit_does_not_invent_uncertainty(database, monkeypatch):
    activities, environment, reference, request = await prepared(database)
    from app.runs import worker
    original = worker._execute_step
    committed, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def delayed_return(*args):
        calls.append('executor')
        result = await original(*args)
        committed.set()
        await release.wait()
        return result
    monkeypatch.setattr(worker, '_execute_step', delayed_return)
    first = asyncio.create_task(environment.run(activities.execute_step, request))
    await asyncio.wait_for(committed.wait(), timeout=5)
    duplicate = ActivityEnvironment()
    duplicate.info = replace(environment.info, attempt=2)
    assert await duplicate.run(activities.execute_step, request) == 'succeeded'
    release.set()
    assert await first == 'succeeded' and calls == ['executor']
    assert await environment.run(activities.finalize, FinalizeRequest(reference)) == 'completed'


async def test_approval_uses_current_plan_and_preserves_temporal_executor_pin(database, monkeypatch):
    activities, environment, reference, request = await prepared(database)
    from app.runs import repository
    from app.runs.planner import action_hash
    from app.runs.schemas import ExecutionPlan
    identity = UUID(reference.run_id)
    row = await database.fetchrow('SELECT * FROM agent_runs WHERE id=$1', identity)
    digest = action_hash(ExecutionPlan.model_validate(row['plan']))
    await database.execute("UPDATE agent_runs SET status='awaiting_approval' WHERE id=$1", identity)
    await database.execute("UPDATE agent_run_steps SET status='awaiting_approval',requires_approval=TRUE WHERE run_id=$1", identity)
    await database.execute('''INSERT INTO run_approvals(run_id,requested_from,action_hash,action_summary,expires_at)
      VALUES($1,'synthetic-user',$2,'{}',now()+interval '1 hour')''', identity, digest)
    assert (await environment.run(activities.inspect, reference)).status == 'awaiting_approval'
    assert await environment.run(activities.execute_step, request) == 'not_admitted'
    monkeypatch.setattr(repository, 'get_settings', lambda: SimpleNamespace(executor_version='other-deployment',
        deployment_version='other-deployment'))
    assert await repository.decide_run(database, identity, 'synthetic-user', True, digest) == 'queued'
    assert await database.fetchval('SELECT executor_version FROM agent_runs WHERE id=$1', identity) == 'test-v1'
    assert await environment.run(activities.execute_step, request) == 'succeeded'


async def test_missing_plan_approval_has_no_tool_attempt_or_journal(database):
    activities, environment, reference, request = await prepared(database)
    await database.execute('UPDATE agent_run_steps SET requires_approval=TRUE WHERE id=$1', UUID(request.step_id))
    assert await environment.run(activities.execute_step, request) == 'not_admitted'
    assert await database.fetchval('SELECT count(*) FROM temporal_workspace_steps WHERE run_id=$1', UUID(reference.run_id)) == 0
    assert await environment.run(activities.finalize, FinalizeRequest(reference)) == 'failed'


async def test_sdk_data_converter_round_trips_the_actual_start_contract():
    from temporalio.converter import DataConverter
    reference = WorkflowReference('gca.temporal.start.v1', 'workspace', str(uuid4()), 'workflow-v1', 'executor-v1')
    encoded = await DataConverter.default.encode([asdict(reference)])
    assert await DataConverter.default.decode(encoded, [WorkflowReference]) == [reference]


async def test_worker_registration_is_explicit_bounded_and_sdk_signature_compatible(monkeypatch):
    import inspect
    import temporalio.worker
    from app.runs.temporal_worker import build_workspace_worker
    signature = inspect.signature(temporalio.worker.Worker)
    captured = {}
    def construct(client, **kwargs):
        signature.bind_partial(client, **kwargs)
        captured.update(kwargs)
        return 'unstarted-worker'
    monkeypatch.setattr(temporalio.worker, 'Worker', construct)
    client = SimpleNamespace(namespace='test-namespace')
    assert build_workspace_worker(client, None, None, namespace='test-namespace', task_queue='test-queue',
        workflow_version='test-workflow-v1', executor_version='test-v1') == 'unstarted-worker'
    assert captured['workflows'] == [WorkspaceWorkflow]
    assert len(captured['activities']) == 3 and captured['max_concurrent_activities'] == 4
    with pytest.raises(ValueError, match='namespace'):
        build_workspace_worker(client, None, None, namespace='other', task_queue='test-queue',
            workflow_version='test-workflow-v1', executor_version='test-v1')
