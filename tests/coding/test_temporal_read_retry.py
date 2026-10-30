"""Append read retry generations through real SDK activities and private SQL."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import importlib
from types import SimpleNamespace
from uuid import UUID, uuid4

import asyncpg
from langchain_core.tools import tool
import pytest
import pytest_asyncio
from temporalio.testing import ActivityEnvironment

from app.runs import worker
from app.runs.schemas import ExecutionPlan, PlanStep
from app.runs.temporal_contracts import FinalizeRequest, StepRequest
from app.runs.temporal_read_retry import current_journal
from app.runs.tool_admission import ExecutionAuthority, ToolAdmissionDenied, execution_authority, require_tool_admission
from tests.coding.test_temporal_workspace import database as workspace_database, prepared

database = workspace_database
pytestmark = pytest.mark.asyncio(loop_scope='module')


async def failed_read(pool, monkeypatch, *, gmail=False):
    activities, environment, reference, request = await prepared(pool)
    identity, step_id = UUID(reference.run_id), UUID(request.step_id)
    c = SimpleNamespace(pool=pool, activities=activities, environment=environment, reference=reference, request=request,
        identity=identity, step_id=step_id, calls=[])
    args = ({'allowed_tools':['list_recent_gmail_senders'], 'tool_arguments':{'max_results':20}} if gmail else
            {'informational_intent':'identity','capability_catalog':{}})
    plan = ExecutionPlan(objective='Read current data', services=['gmail' if gmail else 'general'], steps=[PlanStep(
        id='information',title='Read current data',service='gmail' if gmail else 'general',
        operation='recent_senders' if gmail else 'answer_information',arguments=args)])
    await pool.execute('UPDATE agent_runs SET plan=$2::jsonb WHERE id=$1', identity, plan.model_dump())
    await pool.execute('''UPDATE agent_run_steps SET service=$2,operation=$3,input_data=$4::jsonb,max_attempts=3
      WHERE id=$1''', step_id, plan.steps[0].service, plan.steps[0].operation, args)
    original = worker._execute_step
    if gmail:
        @tool
        async def list_recent_gmail_senders(max_results: int = 20) -> dict:
            """Local synthetic Gmail read counter, never Google."""
            c.calls.append('read')
            if len(c.calls) == 1:
                raise TimeoutError('SYNTHETIC PRIVATE READ ERROR')
            return {'senders':[{'sender_name':'Synthetic','sender_email':'synthetic@example.invalid'}]}
        monkeypatch.setattr(worker, 'get_toolsets', lambda: {'gmail':[list_recent_gmail_senders]})
        from app.db import oauth_credentials
        async def credentials(*args):
            return SimpleNamespace(synthetic=True)
        monkeypatch.setattr(oauth_credentials, 'load_google_credentials', credentials)
    else:
        async def fail(*args):
            c.calls.append('executor')
            raise TimeoutError('SYNTHETIC PRIVATE READ ERROR')
        monkeypatch.setattr(worker, '_execute_step', fail)
    assert await environment.run(activities.execute_step, request) in {'failed','uncertain'}
    assert await environment.run(activities.finalize, FinalizeRequest(reference)) == 'failed'
    if not gmail:
        monkeypatch.setattr(worker, '_execute_step', original)
    c.prior = dict(await current_journal(pool, identity, step_id))
    return c


@pytest_asyncio.fixture(loop_scope='module')
async def case(database, monkeypatch):
    c = await failed_read(database, monkeypatch)
    from app.api.routes import runs
    monkeypatch.setattr(runs, 'get_settings', lambda: SimpleNamespace(executor_version='other',deployment_version='other'))
    try:
        yield c
    finally:
        await database.execute('DELETE FROM agent_runs WHERE id=$1', c.identity)


async def authorize(c):
    from app.api.routes.runs import _reconcile_and_resume
    return await _reconcile_and_resume(c.pool, c.identity, 'synthetic-user', c.step_id)


async def next_request(c):
    snapshot = await c.environment.run(c.activities.inspect, c.reference)
    ref = snapshot.ready[0]
    return StepRequest(c.reference, ref.step_id, ref.generation, ref.permit_id)


def activity_environment(c, request):
    environment = ActivityEnvironment()
    environment.info = replace(c.environment.info,activity_id=request.activity_id,started_time=datetime.now(timezone.utc),attempt=1)
    return environment


async def test_actual_resume_appends_generation_and_preserves_original_uncertainty(case):
    c = case
    assert (await authorize(c)).state == 'safe_to_retry'
    request = await next_request(c)
    assert request.generation == 1 and request.permit_id and request.activity_id != c.request.activity_id
    environment = activity_environment(c, request)
    assert await environment.run(c.activities.execute_step, request) == 'succeeded'
    assert await environment.run(c.activities.execute_step, request) == 'succeeded'
    rows = await c.pool.fetch('SELECT * FROM temporal_workspace_steps WHERE run_id=$1 ORDER BY generation', c.identity)
    assert len(rows) == 2 and rows[0]['state'] == c.prior['state'] == 'uncertain'
    assert rows[0]['execution_token'] == c.prior['execution_token'] != rows[1]['execution_token']
    assert rows[1]['generation'] == 1 and rows[1]['state'] == 'succeeded'
    assert await c.pool.fetchval('SELECT temporal_read_superseded($1,$2,$3)', c.identity, c.step_id, rows[0]['execution_token'])
    assert await environment.run(c.activities.finalize, FinalizeRequest(c.reference)) == 'completed'
    run = await c.pool.fetchrow('SELECT * FROM agent_runs WHERE id=$1', c.identity)
    assert run['functional_completion'] == 100 and run['side_effect_integrity'] == 100 and run['executor_version'] == 'test-v1'
    assert 'Google Workspace Agent' in run['result']['output']


async def test_real_shared_gmail_read_path_retries_without_model_or_write(database, monkeypatch):
    c = await failed_read(database, monkeypatch, gmail=True)
    try:
        from app.api.routes import runs
        monkeypatch.setattr(runs, 'get_settings', lambda: SimpleNamespace(executor_version='other',deployment_version='other'))
        assert (await authorize(c)).state == 'safe_to_retry'
        request = await next_request(c)
        environment = activity_environment(c, request)
        assert await environment.run(c.activities.execute_step, request) == 'succeeded'
        assert await environment.run(c.activities.finalize, FinalizeRequest(c.reference)) == 'completed'
        assert c.calls == ['read','read']
        assert await c.pool.fetchval('SELECT count(*) FROM agent_tool_receipts WHERE run_id=$1', c.identity) == 2
        assert not await c.pool.fetchval('SELECT EXISTS(SELECT 1 FROM agent_tool_receipts WHERE run_id=$1 AND NOT read_only)', c.identity)
        assert await c.pool.fetchval('SELECT count(*) FROM agent_model_calls WHERE run_id=$1', c.identity) == 0
    finally:
        await database.execute('DELETE FROM agent_runs WHERE id=$1', c.identity)


async def test_old_redelivery_cannot_poison_pending_or_running_new_generation(case, monkeypatch):
    c = case
    assert (await authorize(c)).state == 'safe_to_retry'
    request = await next_request(c)
    assert await c.environment.run(c.activities.execute_step, c.request) == 'superseded'
    assert await c.pool.fetchval('SELECT status FROM agent_run_steps WHERE id=$1', c.step_id) == 'pending'
    entered, release = asyncio.Event(), asyncio.Event()
    original = worker._execute_step
    async def delayed(*args):
        entered.set()
        await release.wait()
        return await original(*args)
    monkeypatch.setattr(worker, '_execute_step', delayed)
    environment = activity_environment(c, request)
    task = asyncio.create_task(environment.run(c.activities.execute_step, request))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert await c.environment.run(c.activities.execute_step, c.request) == 'superseded'
        assert await c.pool.fetchval('SELECT status FROM agent_run_steps WHERE id=$1', c.step_id) == 'running'
        token = execution_authority.set(ExecutionAuthority('temporal',c.identity,'synthetic-user','test-v1',
            str(c.prior['execution_token']),c.step_id,c.prior['workflow_execution_id'],datetime.now(timezone.utc)+timedelta(minutes=5)))
        try:
            with pytest.raises(ToolAdmissionDenied):
                await require_tool_admission(c.pool, {'run_id':str(c.identity),'step_id':str(c.step_id),'user_id':'synthetic-user'})
        finally:
            execution_authority.reset(token)
    finally:
        release.set()
        await task
    assert task.result() == 'succeeded'


async def test_changed_plan_after_permit_is_not_executed(case):
    c = case
    assert (await authorize(c)).state == 'safe_to_retry'
    request = await next_request(c)
    await c.pool.execute("UPDATE agent_run_steps SET input_data=input_data||'{\"changed\":true}' WHERE id=$1", c.step_id)
    assert await activity_environment(c, request).run(c.activities.execute_step, request) == 'not_admitted'
    assert await c.pool.fetchval('SELECT count(*) FROM temporal_workspace_steps WHERE run_id=$1', c.identity) == 1
    assert await c.pool.fetchval('SELECT status FROM agent_run_steps WHERE id=$1', c.step_id) == 'failed'


async def test_retry_budget_is_not_reset_by_user_resume(case, monkeypatch):
    c = case
    async def fail(*args):
        c.calls.append('retry')
        raise TimeoutError('synthetic retry failure')
    monkeypatch.setattr(worker, '_execute_step', fail)
    for generation in (1,2):
        assert (await authorize(c)).state == 'safe_to_retry'
        request = await next_request(c)
        assert request.generation == generation
        environment = activity_environment(c, request)
        assert await environment.run(c.activities.execute_step, request) == 'uncertain'
        assert await environment.run(c.activities.finalize, FinalizeRequest(c.reference)) == 'failed'
    decision = await authorize(c)
    assert decision.state == 'manual_required' and decision.reason_code == 'temporal_read_retry_budget_exhausted'
    assert await c.pool.fetchval('SELECT attempt_count FROM agent_run_steps WHERE id=$1', c.step_id) == 3
    assert await c.pool.fetchval('SELECT count(*) FROM temporal_read_retry_permits WHERE run_id=$1', c.identity) == 2
    assert c.calls == ['executor','retry','retry']


async def test_nonquiescent_prior_activity_is_not_restarted(case):
    c = case
    await c.pool.execute('UPDATE temporal_workspace_steps SET completed_at=NULL WHERE run_id=$1', c.identity)
    decision = await authorize(c)
    assert decision.state == 'manual_required' and decision.reason_code == 'temporal_activity_quiescence_required'
    assert await c.pool.fetchval('SELECT count(*) FROM temporal_read_retry_permits WHERE run_id=$1', c.identity) == 0


async def test_legacy_write_evidence_cannot_hide_behind_a_read_label(case):
    c = case
    await c.pool.execute('''INSERT INTO agent_tool_attempts(run_id,step_id,tool_name,attempt_no,status)
      VALUES($1,$2,'send_gmail',1,'error')''', c.identity, c.step_id)
    decision = await authorize(c)
    assert decision.state == 'manual_required' and decision.reason_code == 'temporal_read_retry_write_evidence'


async def test_permit_metadata_is_immutable_and_downgrade_refuses_evidence(case):
    c = case
    assert (await authorize(c)).state == 'safe_to_retry'
    with pytest.raises(asyncpg.RaiseError, match='immutable'):
        await c.pool.execute("UPDATE temporal_read_retry_permits SET source_hash=$2 WHERE run_id=$1", c.identity, 'f'*64)
    migration = importlib.import_module('migrations.versions.032_temporal_read_retry_generations')
    with pytest.raises(asyncpg.RaiseError, match='retained'):
        await c.pool.execute(migration.DOWNGRADE_SQL)
    await c.pool.execute('DELETE FROM agent_runs WHERE id=$1', c.identity)
    row = await c.pool.fetchrow('SELECT * FROM temporal_read_retry_permits WHERE run_id=$1', c.identity)
    assert row['run_parent_id'] is None and row['step_parent_id'] is None
    assert (await c.environment.run(c.activities.inspect, c.reference)).status == 'deleted'


async def test_registry_inventory_is_explicit_and_disjoint():
    from app.tools.contracts import READ_TOOLS, WRITE_TOOLS
    from app.tools.registry import registered_tool_names
    assert not READ_TOOLS & WRITE_TOOLS
    assert READ_TOOLS | WRITE_TOOLS == registered_tool_names()


async def test_database_guard_rejects_a_forged_execution_token(case):
    c = case
    assert (await authorize(c)).state == 'safe_to_retry'
    request = await next_request(c)
    with pytest.raises(asyncpg.RaiseError, match='not admitted'):
        await c.pool.execute('''INSERT INTO temporal_workspace_steps(run_id,step_id,run_parent_id,step_parent_id,
          workflow_execution_id,activity_id,execution_token,read_only,generation,execution_policy,retry_permit_id)
          VALUES($1,$2,$1,$2,$3,'synthetic-forged-token',$4,TRUE,1,'read-safe-v1',$5)''',
          c.identity,c.step_id,c.prior['workflow_execution_id'],uuid4(),UUID(request.permit_id))
    assert await c.pool.fetchval('SELECT count(*) FROM temporal_workspace_steps WHERE run_id=$1', c.identity) == 1


async def test_pre_policy_activity_is_not_retroactively_given_read_retry_authority(case):
    c = case
    # Seed a real pre-policy generation-zero row through normal INSERT defaults.
    # Runtime code must never perform this replacement/reset of history.
    async with c.pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow('DELETE FROM temporal_workspace_steps WHERE run_id=$1 RETURNING *', c.identity)
        await conn.execute('''INSERT INTO temporal_workspace_steps(run_id,step_id,run_parent_id,step_parent_id,
          workflow_execution_id,activity_id,execution_token,read_only,state,started_at,completed_at)
          VALUES($1,$2,$1,$2,$3,$4,$5,TRUE,'uncertain',$6,$7)''',
          c.identity,c.step_id,row['workflow_execution_id'],row['activity_id'],row['execution_token'],row['started_at'],row['completed_at'])
    decision = await authorize(c)
    assert decision.state == 'manual_required' and decision.reason_code == 'temporal_read_retry_provenance_required'


async def test_expired_permit_is_rejected_and_explicit_resume_appends_a_new_reference(case):
    c = case
    assert (await authorize(c)).state == 'safe_to_retry'
    old_request = await next_request(c)
    # Seed a legitimately aged immutable record via INSERT constraints; do not
    # disable triggers or change the host/database clock.
    async with c.pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow('DELETE FROM temporal_read_retry_permits WHERE id=$1 RETURNING *', UUID(old_request.permit_id))
        await conn.execute('''INSERT INTO temporal_read_retry_permits(id,run_id,step_id,run_parent_id,step_parent_id,
          actor_tenant_id,generation,prior_token,execution_token,source_hash,issued_at,expires_at)
          VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,clock_timestamp()-interval '2 minutes',clock_timestamp()-interval '1 minute')''',
          *[row[key] for key in ('id','run_id','step_id','run_parent_id','step_parent_id','actor_tenant_id','generation',
                                'prior_token','execution_token','source_hash')])
    env = activity_environment(c, old_request)
    assert await env.run(c.activities.execute_step, old_request) == 'not_admitted'
    assert await env.run(c.activities.finalize, FinalizeRequest(c.reference)) == 'failed'
    assert (await authorize(c)).state == 'safe_to_retry'
    new_request = await next_request(c)
    assert new_request.generation == 1 and new_request.permit_id != old_request.permit_id
    assert await env.run(c.activities.execute_step, old_request) == 'superseded'
    assert await c.pool.fetchval('SELECT status FROM agent_run_steps WHERE id=$1', c.step_id) == 'pending'
    assert await activity_environment(c, new_request).run(c.activities.execute_step, new_request) == 'succeeded'
    assert await c.pool.fetchval('SELECT count(*) FROM temporal_read_retry_permits WHERE run_id=$1', c.identity) == 2


async def test_read_approval_must_still_be_current_when_retry_is_consumed(case):
    c = case
    from app.runs.planner import action_hash
    plan = ExecutionPlan.model_validate(await c.pool.fetchval('SELECT plan FROM agent_runs WHERE id=$1', c.identity))
    plan.steps[0].requires_approval = True
    await c.pool.execute('UPDATE agent_runs SET plan=$2::jsonb WHERE id=$1', c.identity, plan.model_dump())
    await c.pool.execute('UPDATE agent_run_steps SET requires_approval=TRUE WHERE id=$1', c.step_id)
    assert (await authorize(c)).reason_code == 'temporal_read_retry_approval_required'
    await c.pool.execute('''INSERT INTO run_approvals(run_id,requested_from,action_hash,action_summary,status,expires_at,decided_at)
      VALUES($1,'synthetic-user',$2,'{}','approved',now()+interval '1 hour',now())''', c.identity, action_hash(plan))
    assert (await authorize(c)).state == 'safe_to_retry'
    request = await next_request(c)
    await c.pool.execute("UPDATE run_approvals SET expires_at=now()-interval '1 minute' WHERE run_id=$1", c.identity)
    assert await activity_environment(c, request).run(c.activities.execute_step, request) == 'not_admitted'
    assert await c.pool.fetchval('SELECT count(*) FROM temporal_workspace_steps WHERE run_id=$1', c.identity) == 1


async def test_expired_permit_renewals_cannot_grow_history_without_bound(case):
    c = case
    assert (await authorize(c)).state == 'safe_to_retry'
    request = await next_request(c)
    # Eight valid-but-aged metadata rows exercise the lifetime permit cap without
    # waiting hours or lowering production expiry/immutability checks.
    async with c.pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow('DELETE FROM temporal_read_retry_permits WHERE id=$1 RETURNING *', UUID(request.permit_id))
        for _ in range(8):
            await conn.execute('''INSERT INTO temporal_read_retry_permits(run_id,step_id,run_parent_id,step_parent_id,
              actor_tenant_id,generation,prior_token,source_hash,issued_at,expires_at)
              VALUES($1,$2,$1,$2,$3,1,$4,$5,clock_timestamp()-interval '2 minutes',clock_timestamp()-interval '1 minute')''',
              c.identity,c.step_id,row['actor_tenant_id'],row['prior_token'],row['source_hash'])
    aged = await next_request(c)
    assert await activity_environment(c, aged).run(c.activities.execute_step, aged) == 'not_admitted'
    await c.environment.run(c.activities.finalize, FinalizeRequest(c.reference))
    decision = await authorize(c)
    assert decision.state == 'manual_required' and decision.reason_code == 'temporal_read_retry_permit_limit'
    assert await c.pool.fetchval('SELECT count(*) FROM temporal_read_retry_permits WHERE run_id=$1', c.identity) == 8


@pytest.mark.parametrize('generation,permit', [(True,None),(-1,None),(4,str(uuid4())),(0,str(uuid4())),(1,None)])
async def test_generation_history_contract_rejects_malformed_references(case, generation, permit):
    request = StepRequest(case.reference,str(case.step_id),generation,permit)
    with pytest.raises(ValueError):
        request.validate()
