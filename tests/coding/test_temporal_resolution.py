"""Verified readback resumes the actual API/Temporal ledger, never a new write."""
from dataclasses import replace
from datetime import timedelta
import importlib
from types import SimpleNamespace
from uuid import UUID, uuid4
import asyncio

import asyncpg
import pytest
import pytest_asyncio

pytest.importorskip('temporalio')
from app.runs.temporal_contracts import FinalizeRequest
from app.runs.temporal_resolution import ResolutionConflict, bind_decision, snapshot
from app.runs.reconciliation import ReconciliationDecision, reconcile_failed_step
from tests.coding.test_temporal_workspace import database as database_fixture, prepared

database = database_fixture
pytestmark = pytest.mark.asyncio(loop_scope='module')


class Reply:
    def execute(self):
        return {'spreadsheetId': 'synthetic-sheet'}


@pytest_asyncio.fixture(loop_scope='module')
async def case(database, monkeypatch):
    activities, environment, reference, request = await prepared(database)
    identity, step_id = UUID(reference.run_id), UUID(request.step_id)
    from app.runs.schemas import ExecutionPlan, PlanStep
    plan = ExecutionPlan(objective='Create requested Sheet', services=['sheets'], steps=[PlanStep(
        id='information', title='Create Sheet', service='sheets', operation='create', read_only=False,
        arguments={'allowed_tools': ['create_google_sheet']})])
    await database.execute('''UPDATE agent_runs SET status='failed',side_effect_integrity=0,
      request='Create requested Sheet',plan=$2::jsonb,error_category='worker_reconciliation' WHERE id=$1''', identity, plan.model_dump())
    await database.execute('''UPDATE agent_run_steps SET status='failed',read_only=FALSE,service='sheets',operation='create',
      error_category='worker_reconciliation',input_data='{"allowed_tools":["create_google_sheet"]}',
      output_data='{"output":"Old failure message","tool_executions":[{"tool":"create_google_sheet",
        "arguments":{"title":"SYNTHETIC PRIVATE TITLE"},"result":{"spreadsheetId":"synthetic-sheet"}}]}'
      WHERE id=$1''', step_id)
    await database.execute('''INSERT INTO temporal_workspace_steps(run_id,step_id,run_parent_id,step_parent_id,
      workflow_execution_id,activity_id,read_only,state,completed_at)
      VALUES($1,$2,$1,$2,$3,$4,FALSE,'uncertain',now())''', identity, step_id,
      UUID(environment.info.workflow_run_id), environment.info.activity_id)
    from app.runs import verifier
    monkeypatch.setattr(verifier.google, 'sheets_service', SimpleNamespace(spreadsheets=lambda:
        SimpleNamespace(get=lambda **kwargs: Reply())))
    from app.api.routes import runs
    async def credentials(*args):
        return None  # Google API is a local readback double, never an OAuth call.
    monkeypatch.setattr(runs, 'load_google_credentials', credentials)
    monkeypatch.setattr(runs, 'get_settings', lambda: SimpleNamespace(executor_version='other-version',
        deployment_version='other-version'))
    return database, activities, environment, reference, request


async def rows(pool, reference, request):
    return (dict(await pool.fetchrow('SELECT * FROM agent_runs WHERE id=$1', UUID(reference.run_id))),
            dict(await pool.fetchrow('SELECT * FROM agent_run_steps WHERE id=$1', UUID(request.step_id))))


async def test_actual_resume_uses_real_readback_and_retains_original_uncertainty(case):
    pool, activities, environment, reference, request = case
    from app.api.routes.runs import _reconcile_and_resume
    decision = await _reconcile_and_resume(pool, reference.run_id, 'synthetic-user', request.step_id)
    assert decision.state == 'already_completed'
    journal = await pool.fetchrow('SELECT * FROM temporal_workspace_steps WHERE run_id=$1', UUID(reference.run_id))
    assert journal['state'] == 'uncertain'
    assert await pool.fetchval('SELECT executor_version FROM agent_runs WHERE id=$1', UUID(reference.run_id)) == 'test-v1'
    assert await pool.fetchval('SELECT temporal_step_resolved($1,$2)', UUID(reference.run_id), UUID(request.step_id))
    assert await environment.run(activities.finalize, FinalizeRequest(reference)) == 'completed'
    run, _ = await rows(pool, reference, request)
    assert run['side_effect_integrity'] == 100 and run['functional_completion'] == 100
    assert 'Old failure message' not in run['result']['output']
    assert 'https://docs.google.com/spreadsheets/d/synthetic-sheet/edit' in run['result']['output']
    proof = await pool.fetchrow('SELECT * FROM temporal_step_resolutions WHERE run_id=$1', UUID(reference.run_id))
    assert 'PRIVATE TITLE' not in str(proof) and 'synthetic-user' not in str(proof)


async def test_late_output_or_plan_change_invalidates_verified_proof(case):
    pool, activities, environment, reference, request = case
    from app.api.routes.runs import _reconcile_and_resume
    await _reconcile_and_resume(pool, reference.run_id, 'synthetic-user', request.step_id)
    await pool.execute("UPDATE agent_run_steps SET output_data=output_data||'{\"output\":\"late result\"}'::jsonb WHERE id=$1",
        UUID(request.step_id))
    assert not await pool.fetchval('SELECT temporal_step_resolved($1,$2)', UUID(reference.run_id), UUID(request.step_id))
    assert await environment.run(activities.finalize, FinalizeRequest(reference)) == 'partial'
    run, _ = await rows(pool, reference, request)
    assert run['side_effect_integrity'] == 0 and run['functional_completion'] < 100


async def test_source_race_expired_observation_and_wrong_actor_are_rejected(case):
    pool, _, _, reference, request = case
    run, step = await rows(pool, reference, request)
    async with pool.acquire() as conn:
        source = await snapshot(conn, run, step)
        decision = await reconcile_failed_step(run, step)
        assert decision.state == 'already_completed'
        for bad_source, actor in ((replace(source, observed_at=source.observed_at-timedelta(hours=1)), 'synthetic-user'),
                                  (source, 'another-user')):
            with pytest.raises(ResolutionConflict):
                await bind_decision(conn, run, step, decision, bad_source, actor)
        await conn.execute("UPDATE agent_run_steps SET input_data=input_data||'{\"changed\":true}'::jsonb WHERE id=$1", step['id'])
        with pytest.raises(ResolutionConflict, match='changed'):
            await bind_decision(conn, run, step, decision, source, 'synthetic-user')


async def test_nonquiescent_activity_and_retry_request_do_not_reset_journal(case):
    pool, _, _, reference, request = case
    run, step = await rows(pool, reference, request)
    async with pool.acquire() as conn:
        source = await snapshot(conn, run, step)
        decision = await reconcile_failed_step(run, step)
        await conn.execute('UPDATE temporal_workspace_steps SET completed_at=NULL WHERE run_id=$1', run['id'])
        blocked = await bind_decision(conn, run, step, decision, source, 'synthetic-user')
        assert blocked.state == 'manual_required' and blocked.reason_code == 'temporal_activity_quiescence_required'
        retry = await bind_decision(conn, run, step, ReconciliationDecision('safe_to_retry','read_only_operation',str(step['id'])),
            source, 'synthetic-user')
        assert retry.reason_code == 'temporal_retry_generation_required'
    assert await pool.fetchval('SELECT state FROM temporal_workspace_steps WHERE run_id=$1', run['id']) == 'uncertain'


async def test_private_erasure_nulls_actor_mapping_and_preserves_only_proof_metadata(case):
    pool, _, _, reference, request = case
    from app.api.routes.runs import _reconcile_and_resume
    await _reconcile_and_resume(pool, reference.run_id, 'synthetic-user', request.step_id)
    await pool.execute('DELETE FROM agent_runs WHERE id=$1', UUID(reference.run_id))
    assert not await pool.fetchval('SELECT temporal_step_resolved($1,$2)', UUID(reference.run_id), UUID(request.step_id))
    # The module contains other runs for this synthetic user; erase just its mapping
    # to exercise the same FK privacy action without removing other fixture cases.
    await pool.execute("DELETE FROM workflow_event_tenants WHERE user_id='synthetic-user'")
    proof = await pool.fetchrow('SELECT * FROM temporal_step_resolutions WHERE run_id=$1', UUID(reference.run_id))
    assert proof['actor_tenant_id'] is None
    with pytest.raises(asyncpg.RaiseError, match='immutable'):
        await pool.execute('UPDATE temporal_step_resolutions SET source_hash=$2 WHERE run_id=$1', UUID(reference.run_id), 'f'*64)
    migration = importlib.import_module('migrations.versions.030_temporal_verified_resolutions')
    with pytest.raises(asyncpg.RaiseError, match='retained'):
        await pool.execute(migration.DOWNGRADE_SQL)


async def test_expired_proof_can_be_renewed_only_by_a_new_readback_observation(case):
    pool, _, _, reference, request = case
    run, step = await rows(pool, reference, request)
    async with pool.acquire() as conn:
        source = await snapshot(conn, run, step)
        token = await conn.fetchval('SELECT execution_token FROM temporal_workspace_steps WHERE run_id=$1', run['id'])
        await conn.execute('''INSERT INTO temporal_step_resolutions(run_id,step_id,execution_token,source_hash,
          verification_hash,verification_started_at,verified_at,expires_at)
          VALUES($1,$2,$3,$4,$5,now()-interval '30 minutes',now()-interval '29 minutes',now()-interval '15 minutes')''',
          run['id'], step['id'], token, source.source_hash, 'f'*64)
        assert not await conn.fetchval('SELECT temporal_step_resolved($1,$2)', run['id'], step['id'])
        decision = await reconcile_failed_step(run, step)
        await bind_decision(conn, run, step, decision, source, 'synthetic-user')
        assert await conn.fetchval('SELECT temporal_step_resolved($1,$2)', run['id'], step['id'])
    assert await pool.fetchval('SELECT count(*) FROM temporal_step_resolutions WHERE run_id=$1', run['id']) == 2


async def test_current_plan_mismatch_cannot_be_accepted_as_success(case):
    pool, _, _, reference, request = case
    run, step = await rows(pool, reference, request)
    await pool.execute("UPDATE agent_runs SET plan=jsonb_set(plan,'{steps,0,operation}','\"write\"') WHERE id=$1", run['id'])
    run, step = await rows(pool, reference, request)
    async with pool.acquire() as conn:
        source = await snapshot(conn, run, step)
        decision = await reconcile_failed_step(run, step)
        with pytest.raises(ResolutionConflict, match='typed plan'):
            await bind_decision(conn, run, step, decision, source, 'synthetic-user')


async def test_existing_postgres_resume_keeps_current_control_behavior_without_resolution_table_use(case):
    pool, _, _, reference, request = case
    original, source_step = await rows(pool, reference, request)
    identity, step_id = uuid4(), uuid4()
    await pool.execute('''INSERT INTO agent_runs(id,user_id,session_id,request,idempotency_key,status,executor_version,plan)
      VALUES($1,'synthetic-user','ordinary','Create requested Sheet',$2,'failed','old-control',$3::jsonb)''',
      identity, str(identity), original['plan'])
    await pool.execute('''INSERT INTO agent_run_steps(id,run_id,step_key,sequence_no,title,service,operation,
      read_only,status,input_data,output_data) VALUES($1,$2,'information',1,'Create Sheet','sheets','create',FALSE,'failed',$3::jsonb,$4::jsonb)''',
      step_id, identity, source_step['input_data'], source_step['output_data'])
    from app.api.routes.runs import _reconcile_and_resume
    decision = await _reconcile_and_resume(pool, str(identity), 'synthetic-user', str(step_id))
    assert decision.state == 'already_completed'
    assert await pool.fetchval('SELECT executor_version FROM agent_runs WHERE id=$1', identity) == 'other-version'
    assert not await pool.fetchval('SELECT EXISTS(SELECT 1 FROM temporal_step_resolutions WHERE run_id=$1)', identity)


async def test_actual_api_readback_race_is_a_structured_conflict_not_success(case, monkeypatch):
    pool, _, _, reference, request = case
    from app.api.routes.runs import _reconcile_and_resume
    from fastapi import HTTPException
    from app.runs import verifier
    loop = asyncio.get_running_loop()
    class Raced:
        def execute(self):
            change = pool.execute("UPDATE agent_run_steps SET input_data=input_data||'{\"changed\":true}'::jsonb WHERE id=$1",
                UUID(request.step_id))
            asyncio.run_coroutine_threadsafe(change, loop).result(timeout=5)
            return {'spreadsheetId': 'synthetic-sheet'}
    monkeypatch.setattr(verifier.google, 'sheets_service', SimpleNamespace(spreadsheets=lambda:
        SimpleNamespace(get=lambda **kwargs: Raced())))
    with pytest.raises(HTTPException) as error:
        await _reconcile_and_resume(pool, reference.run_id, 'synthetic-user', request.step_id)
    assert error.value.status_code == 409
    assert not await pool.fetchval('SELECT EXISTS(SELECT 1 FROM temporal_step_resolutions WHERE run_id=$1)', UUID(reference.run_id))
    assert await pool.fetchval('SELECT status FROM agent_runs WHERE id=$1', UUID(reference.run_id)) == 'failed'


async def test_approval_after_the_effect_cannot_retroactively_authorize_resolution(case):
    pool, _, _, reference, request = case
    run, step = await rows(pool, reference, request)
    from app.runs.planner import action_hash
    from app.runs.schemas import ExecutionPlan
    plan = ExecutionPlan.model_validate(run['plan'])
    plan.steps[0].requires_approval = True
    await pool.execute('UPDATE agent_runs SET requires_approval=TRUE,approval_bypassed=TRUE,plan=$2::jsonb WHERE id=$1',
        run['id'], plan.model_dump())
    await pool.execute('UPDATE agent_run_steps SET requires_approval=TRUE WHERE id=$1', step['id'])
    await pool.execute('''INSERT INTO run_approvals(run_id,requested_from,action_hash,action_summary,
      status,expires_at,decided_at) VALUES($1,'synthetic-user',$2,'{}','approved',now()+interval '1 hour',clock_timestamp())''',
      run['id'], action_hash(plan))
    run, step = await rows(pool, reference, request)
    async with pool.acquire() as conn:
        source = await snapshot(conn, run, step)
        decision = await reconcile_failed_step(run, step)
        blocked = await bind_decision(conn, run, step, decision, source, 'synthetic-user')
        assert blocked.state == 'manual_required' and blocked.reason_code == 'temporal_approval_evidence_required'
    assert not await pool.fetchval('SELECT EXISTS(SELECT 1 FROM temporal_step_resolutions WHERE run_id=$1)', run['id'])
