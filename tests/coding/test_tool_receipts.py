"""Actual tool wrapper and recovery using private disposable PostgreSQL only."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import importlib
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit

import asyncpg
from langchain_core.tools import tool
import pytest
import pytest_asyncio
from uuid import UUID, uuid4

from app.agents import supervisor
from app.runs import worker
from app.runs.tool_admission import ExecutionAuthority, execution_authority
from app.runs.tool_receipts import ToolReceiptFailure, begin_receipt, finish_receipt, receipt_evidence
from tests.coding.test_pg_completion_fences import prepared
from tests.coding.test_temporal_workspace import database as workspace_database

database = workspace_database
pytestmark = pytest.mark.asyncio(loop_scope='module')


@pytest_asyncio.fixture(loop_scope='module')
async def case(database, monkeypatch):
    monkeypatch.setattr(worker, 'get_settings', lambda: SimpleNamespace(worker_lease_seconds=60,
        executor_version='test-v1', deployment_version='test-v1', executor_role='control'))
    identity, step_id = await prepared(database)
    run = await worker.claim_run(database, 'receipt-worker')
    assert run['id'] == identity
    await database.execute("UPDATE agent_run_steps SET status='running',read_only=FALSE,attempt_count=1 WHERE id=$1", step_id)
    authority = ExecutionAuthority('postgres', identity, 'synthetic-user', 'test-v1', 'receipt-worker')
    token = execution_authority.set(authority)
    records = []
    async def record(*args, **kwargs):
        records.append('legacy-record')
    monkeypatch.setattr(supervisor, '_record_tool_call', record)
    state = {'run_id': str(identity), 'step_id': str(step_id), 'user_id': 'synthetic-user', 'session_id': 'synthetic'}
    value = SimpleNamespace(pool=database, identity=identity, step_id=step_id, state=state, calls=[], records=records,
        call={'id':'synthetic-call','args':{'text':'SYNTHETIC PRIVATE INPUT'}})
    @tool
    async def send_gmail(text: str) -> dict:
        """Local synthetic provider counter, never Gmail."""
        value.calls.append('provider')
        assert await database.fetchval("SELECT state FROM agent_tool_receipts WHERE run_id=$1", identity) == 'dispatched'
        return {'id': 'synthetic-message', 'text': text}
    value.tool = send_gmail
    try:
        yield value
    finally:
        execution_authority.reset(token)
        await database.execute('DELETE FROM agent_runs WHERE id=$1', identity)


async def evidence(c, user='synthetic-user'):
    async with c.pool.acquire() as conn:
        return await receipt_evidence(conn, c.identity, c.step_id, user)


async def reject_event(c, event):
    await c.pool.execute('''CREATE FUNCTION reject_receipt_event() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN IF NEW.event_type=TG_ARGV[0] THEN RAISE EXCEPTION 'synthetic receipt sink failure'; END IF;
      RETURN NEW; END $$;''')
    # Identifier/arguments are fixed test constants, not user-supplied SQL.
    assert event in {'tool_receipt_dispatched', 'tool_receipt_returned'}
    await c.pool.execute(f"CREATE TRIGGER reject_receipt_event BEFORE INSERT ON agent_run_events "
        f"FOR EACH ROW EXECUTE FUNCTION reject_receipt_event('{event}')")


async def drop_reject(c):
    await c.pool.execute('DROP TRIGGER reject_receipt_event ON agent_run_events; DROP FUNCTION reject_receipt_event()')


async def test_receipt_precedes_provider_and_preserves_encrypted_exact_response(case):
    c = case
    _, result, _ = await supervisor.execute_tool_call(c.tool, c.call, c.state, c.pool)
    assert result == {'id':'synthetic-message', 'text':'SYNTHETIC PRIVATE INPUT'}
    assert c.calls == ['provider'] and c.records == ['legacy-record']
    row = await c.pool.fetchrow('SELECT * FROM agent_tool_receipts WHERE run_id=$1', c.identity)
    assert row['state'] == 'returned' and row['completed_at'] is not None
    assert 'PRIVATE' not in str(dict(row)) and 'receipt-worker' not in str(dict(row))
    assert not (await evidence(c)).blocked
    assert (await evidence(c)).executions == [{'tool':'send_gmail', 'arguments':c.call['args'], 'result':result}]
    assert (await evidence(c, 'another-user')).executions == []
    assert await c.pool.fetchval('SELECT status FROM agent_run_steps WHERE id=$1', c.step_id) == 'running'


async def test_duplicate_call_id_cannot_dispatch_again(case):
    c = case
    await supervisor.execute_tool_call(c.tool, c.call, c.state, c.pool)
    with pytest.raises(ToolReceiptFailure) as error:
        await supervisor.execute_tool_call(c.tool, c.call, c.state, c.pool)
    assert error.value.evidence['provider_called'] is False
    assert c.calls == ['provider']
    assert await c.pool.fetchval('SELECT count(*) FROM agent_tool_receipts WHERE run_id=$1', c.identity) == 1


async def test_reservation_and_event_failure_roll_back_before_provider(case):
    c = case
    await reject_event(c, 'tool_receipt_dispatched')
    try:
        with pytest.raises(ToolReceiptFailure) as error:
            await supervisor.execute_tool_call(c.tool, c.call, c.state, c.pool)
        assert error.value.evidence['provider_called'] is False and c.calls == []
        assert await c.pool.fetchval('SELECT count(*) FROM agent_tool_receipts WHERE run_id=$1', c.identity) == 0
    finally:
        await drop_reject(c)


async def test_response_persistence_failure_is_uncertain_not_fictional_provider_failure(case):
    c = case
    await reject_event(c, 'tool_receipt_returned')
    try:
        with pytest.raises(ToolReceiptFailure) as error:
            await supervisor.execute_tool_call(c.tool, c.call, c.state, c.pool)
        assert error.value.category == 'worker_reconciliation' and 'provider_called' not in error.value.evidence
        assert c.calls == ['provider'] and c.records == []
        assert (await evidence(c)).blocked and (await evidence(c)).executions == []
        assert await c.pool.fetchval('SELECT state FROM agent_tool_receipts WHERE run_id=$1', c.identity) == 'dispatched'
    finally:
        await drop_reject(c)


@pytest.mark.parametrize('cancel', [False, True])
async def test_unknown_provider_acceptance_is_recorded_without_assuming_no_effect(case, cancel):
    c = case
    @tool
    async def failing() -> dict:
        """Synthetic response loss, with no real provider."""
        c.calls.append('provider')
        if cancel:
            raise asyncio.CancelledError()
        raise TimeoutError('SYNTHETIC PRIVATE PROVIDER ERROR')
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await supervisor.execute_tool_call(failing, {'id':'unknown', 'args':{}}, c.state, c.pool)
    else:
        _, result, _ = await supervisor.execute_tool_call(failing, {'id':'unknown', 'args':{}}, c.state, c.pool)
        assert result.get('error')
    row = await c.pool.fetchrow('SELECT * FROM agent_tool_receipts WHERE run_id=$1', c.identity)
    assert row['state'] == 'unknown' and 'PRIVATE' not in str(dict(row))
    assert (await evidence(c)).blocked


async def test_late_response_records_evidence_without_mutating_replacement_owner(case):
    c = case
    receipt = await begin_receipt(c.pool, c.state, c.tool.name, c.call)
    await c.pool.execute("UPDATE agent_runs SET lease_owner='replacement' WHERE id=$1", c.identity)
    await finish_receipt(c.pool, receipt, returned=True, result={'id':'synthetic-message'})
    assert await c.pool.fetchval('SELECT lease_owner FROM agent_runs WHERE id=$1', c.identity) == 'replacement'
    assert await c.pool.fetchval('SELECT status FROM agent_run_steps WHERE id=$1', c.step_id) == 'running'
    assert not (await evidence(c)).blocked
    with pytest.raises(ToolReceiptFailure):
        await finish_receipt(c.pool, receipt, returned=True, result={'id':'replacement-message'})


async def test_wrong_tenant_finish_and_immutable_binding_are_rejected(case):
    c = case
    receipt = await begin_receipt(c.pool, c.state, c.tool.name, c.call)
    with pytest.raises(ToolReceiptFailure):
        await finish_receipt(c.pool, replace(receipt, user_id='another-user'), returned=True, result={})
    with pytest.raises(asyncpg.RaiseError, match='immutable'):
        await c.pool.execute("UPDATE agent_tool_receipts SET tool_name='other' WHERE run_id=$1", c.identity)
    assert (await evidence(c)).blocked
    migration = importlib.import_module('migrations.versions.031_tool_invocation_receipts')
    with pytest.raises(asyncpg.RaiseError, match='retained'):
        await c.pool.execute(migration.DOWNGRADE_SQL)


async def test_hard_erasure_removes_private_receipts_and_prevents_late_finish(case):
    c = case
    receipt = await begin_receipt(c.pool, c.state, c.tool.name, c.call)
    await c.pool.execute('DELETE FROM agent_runs WHERE id=$1', c.identity)
    assert await c.pool.fetchval('SELECT count(*) FROM agent_tool_receipts WHERE id=$1', receipt.identity) == 0
    with pytest.raises(ToolReceiptFailure):
        await finish_receipt(c.pool, receipt, returned=True, result={})


async def failed_sheet(c, monkeypatch, *, composite=False, unknown=False, skip_invocation=False):
    @tool
    async def create_google_sheet(title: str) -> dict:
        """Synthetic Sheets result, not a real Google call."""
        c.calls.append('provider')
        return {'spreadsheetId':'synthetic-sheet'}
    call = {'id':'sheet-create', 'args':{'title':'SYNTHETIC PRIVATE TITLE'}}
    if skip_invocation:
        pass  # Used only after the real subprocess below already committed its receipt.
    elif unknown:
        c.pending_receipt = await begin_receipt(c.pool, c.state, create_google_sheet.name, call)
    else:
        await supervisor.execute_tool_call(create_google_sheet, call, c.state, c.pool)
    operation = 'create_and_write' if composite else 'create'
    tools = ['create_google_sheet','write_google_sheet'] if composite else ['create_google_sheet']
    await c.pool.execute('''UPDATE agent_runs SET status='failed',request='Create requested Sheet',
      error_category='worker_reconciliation' WHERE id=$1''', c.identity)
    await c.pool.execute('''UPDATE agent_run_steps SET status='failed',service='sheets',operation=$2,
      input_data=$3::jsonb,error_category='worker_reconciliation',output_data='{}' WHERE id=$1''',
      c.step_id, operation, {'allowed_tools':tools})
    class Reply:
        def execute(self):
            return {'spreadsheetId':'synthetic-sheet'}
    from app.runs import verifier
    from app.api.routes import runs
    monkeypatch.setattr(verifier.google, 'sheets_service', SimpleNamespace(spreadsheets=lambda:
        SimpleNamespace(get=lambda **kwargs: Reply())))
    async def credentials(*args):
        return None  # Readback service is the synthetic double above.
    monkeypatch.setattr(runs, 'load_google_credentials', credentials)
    monkeypatch.setattr(runs, 'get_settings', lambda: SimpleNamespace(executor_version='test-v1', deployment_version='test-v1'))


async def test_actual_resume_recovers_response_lost_before_step_commit_without_recreating_sheet(case, monkeypatch):
    c = case
    await failed_sheet(c, monkeypatch)
    from app.api.routes.runs import _reconcile_and_resume
    decision = await _reconcile_and_resume(c.pool, c.identity, 'synthetic-user', c.step_id)
    assert decision.state == 'already_completed' and c.calls == ['provider']
    step = await c.pool.fetchrow('SELECT * FROM agent_run_steps WHERE id=$1', c.step_id)
    assert step['status'] == 'completed'
    assert step['output_data']['tool_executions'][0]['result']['spreadsheetId'] == 'synthetic-sheet'
    assert 'https://docs.google.com/spreadsheets/d/synthetic-sheet/edit' in step['output_data']['output']


async def test_partial_composite_does_not_become_false_success_or_recreate_existing_sheet(case, monkeypatch):
    c = case
    await failed_sheet(c, monkeypatch, composite=True)
    from app.api.routes.runs import _reconcile_and_resume
    decision = await _reconcile_and_resume(c.pool, c.identity, 'synthetic-user', c.step_id)
    assert decision.state == 'manual_required' and decision.reason_code == 'write_contract_incomplete'
    assert c.calls == ['provider']
    assert await c.pool.fetchval('SELECT status FROM agent_run_steps WHERE id=$1', c.step_id) == 'failed'


async def test_unknown_receipt_blocks_resume_even_without_legacy_attempt_log(case, monkeypatch):
    c = case
    await failed_sheet(c, monkeypatch, unknown=True)
    from app.api.routes.runs import _reconcile_and_resume
    decision = await _reconcile_and_resume(c.pool, c.identity, 'synthetic-user', c.step_id)
    assert decision.state == 'manual_required' and decision.reason_code == 'tool_receipt_write_outcome_uncertain'
    assert c.calls == [] and c.records == []


async def test_receipt_arriving_during_readback_cannot_be_silently_accepted(case, monkeypatch):
    c = case
    await failed_sheet(c, monkeypatch, unknown=True)
    from app.api.routes import runs
    original = runs.reconcile_failed_step
    async def race(*args, **kwargs):
        decision = await original(*args, **kwargs)
        await finish_receipt(c.pool, c.pending_receipt, returned=True, result={'spreadsheetId':'synthetic-sheet'})
        return decision
    monkeypatch.setattr(runs, 'reconcile_failed_step', race)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as error:
        await runs._reconcile_and_resume(c.pool, c.identity, 'synthetic-user', c.step_id)
    assert error.value.status_code == 409 and 'evidence changed' in str(error.value.detail)
    assert c.calls == []


async def test_soft_erasure_clears_ciphertext_without_erasing_uncertainty_marker(case):
    c = case
    await supervisor.execute_tool_call(c.tool, c.call, c.state, c.pool)
    await c.pool.execute('UPDATE agent_runs SET deleted_at=now() WHERE id=$1', c.identity)
    from app.runs.retention import apply_retention
    report = await apply_retention(c.pool)
    assert report['expired_tool_receipt_payloads'] >= 1
    row = await c.pool.fetchrow('SELECT * FROM agent_tool_receipts WHERE run_id=$1', c.identity)
    assert row['encrypted_payload'] is None and row['state'] == 'returned'
    assert (await evidence(c)).executions == []


async def test_temporal_resume_binds_receipt_readback_to_original_journal_without_repeating(database, monkeypatch):
    from tests.coding.test_temporal_workspace import prepared as temporal_prepared
    from app.runs.schemas import ExecutionPlan, PlanStep
    activities, environment, reference, request = await temporal_prepared(database)
    identity, step_id, nonce = UUID(reference.run_id), UUID(request.step_id), uuid4()
    args = {'allowed_tools':['create_google_sheet']}
    plan = ExecutionPlan(objective='Create requested Sheet', services=['sheets'], steps=[PlanStep(
        id='information', title='Create Sheet', service='sheets', operation='create', read_only=False, arguments=args)])
    await database.execute("UPDATE agent_runs SET status='running',plan=$2::jsonb WHERE id=$1", identity, plan.model_dump())
    await database.execute("UPDATE agent_run_steps SET status='running',attempt_count=1,read_only=FALSE,service='sheets',"
        "operation='create',input_data=$2::jsonb WHERE id=$1", step_id, args)
    await database.execute('''INSERT INTO temporal_workspace_steps(run_id,step_id,run_parent_id,step_parent_id,
      workflow_execution_id,activity_id,execution_token,read_only) VALUES($1,$2,$1,$2,$3,$4,$5,FALSE)''',
      identity, step_id, UUID(environment.info.workflow_run_id), environment.info.activity_id, nonce)
    token = execution_authority.set(ExecutionAuthority('temporal',identity,'synthetic-user','test-v1',str(nonce),
        step_id, UUID(environment.info.workflow_run_id), datetime.now(timezone.utc)+timedelta(minutes=1)))
    c = SimpleNamespace(pool=database,identity=identity,step_id=step_id,calls=[],state={
        'run_id':str(identity),'step_id':str(step_id),'user_id':'synthetic-user','session_id':'synthetic'})
    async def record(*args, **kwargs):
        return None
    monkeypatch.setattr(supervisor, '_record_tool_call', record)
    try:
        await failed_sheet(c, monkeypatch)
        await database.execute("UPDATE temporal_workspace_steps SET state='uncertain',completed_at=now() WHERE run_id=$1", identity)
        from app.api.routes.runs import _reconcile_and_resume
        decision = await _reconcile_and_resume(database, identity, 'synthetic-user', step_id)
        assert decision.state == 'already_completed' and c.calls == ['provider']
        assert await database.fetchval('SELECT temporal_step_resolved($1,$2)', identity, step_id)
        assert await database.fetchval('SELECT state FROM temporal_workspace_steps WHERE run_id=$1', identity) == 'uncertain'
        from app.runs.temporal_contracts import FinalizeRequest
        assert await environment.run(activities.finalize, FinalizeRequest(reference)) == 'completed'
    finally:
        execution_authority.reset(token)
        await database.execute('DELETE FROM agent_runs WHERE id=$1', identity)


async def test_real_process_death_after_receipt_recovers_without_a_second_effect(case, monkeypatch):
    c = case
    await c.pool.execute('''CREATE TABLE IF NOT EXISTS synthetic_receipt_effects(
      run_id UUID PRIMARY KEY REFERENCES agent_runs(id) ON DELETE CASCADE,calls INTEGER NOT NULL DEFAULT 0)''')
    await c.pool.execute('INSERT INTO synthetic_receipt_effects(run_id) VALUES($1)', c.identity)
    name = await c.pool.fetchval('SELECT current_database()')
    dsn = urlunsplit(urlsplit(os.environ['COST_LEDGER_TEST_DSN'])._replace(path='/'+name))
    # A fresh process, no forked SDK threads and no inherited provider/cloud keys.
    source = '''
import asyncio,os,sys
from uuid import UUID
from app.config import settings
configured=settings.Settings(_env_file=None,jwt_secret_key='synthetic-crash-secret-at-least-32-bytes',
    oauth_encryption_keys='synthetic-receipt-crash-key')
settings.get_settings=lambda: configured
import asyncpg
from langchain_core.tools import tool
from app.db.connection import _init_connection
from app.runs.tool_admission import ExecutionAuthority,execution_authority
from app.agents import supervisor
async def main():
    pool=await asyncpg.create_pool(os.environ['DATABASE_URL'],init=_init_connection)
    run_id,step_id=UUID(sys.argv[1]),UUID(sys.argv[2])
    execution_authority.set(ExecutionAuthority('postgres',run_id,'synthetic-user','test-v1','receipt-worker'))
    async def record(*args,**kwargs): pass
    supervisor._record_tool_call=record
    @tool
    async def create_google_sheet(title: str)->dict:
        """Synthetic durable effect counter; never Google."""
        await pool.execute('UPDATE synthetic_receipt_effects SET calls=calls+1 WHERE run_id=$1',run_id)
        return {'spreadsheetId':'synthetic-sheet'}
    await supervisor.execute_tool_call(create_google_sheet,{'id':'process-crash','args':{'title':'Synthetic'}},
        {'run_id':str(run_id),'step_id':str(step_id),'user_id':'synthetic-user','session_id':'synthetic'},pool)
    os._exit(41)
asyncio.run(main())
'''
    process = await asyncio.create_subprocess_exec(sys.executable, '-c', source, str(c.identity), str(c.step_id),
        cwd=Path(__file__).resolve().parents[2], env={'PATH':os.environ.get('PATH',''), 'DATABASE_URL':dsn,
            'PYTHONDONTWRITEBYTECODE':'1','LANGCHAIN_TRACING_V2':'false'},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        await asyncio.wait_for(process.communicate(), timeout=30)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        pytest.fail('synthetic receipt crash probe timed out')
    assert process.returncode == 41, 'synthetic subprocess did not reach its post-receipt crash boundary'
    assert await c.pool.fetchval('SELECT calls FROM synthetic_receipt_effects WHERE run_id=$1', c.identity) == 1
    from app.db import oauth_credentials
    monkeypatch.setattr(oauth_credentials, 'get_settings', lambda: SimpleNamespace(
        oauth_encryption_keys='synthetic-receipt-crash-key',jwt_secret_key='synthetic-crash-secret-at-least-32-bytes'))
    assert (await evidence(c)).executions[0]['result']['spreadsheetId'] == 'synthetic-sheet'
    await failed_sheet(c, monkeypatch, skip_invocation=True)
    from app.api.routes.runs import _reconcile_and_resume
    assert (await _reconcile_and_resume(c.pool, c.identity, 'synthetic-user', c.step_id)).state == 'already_completed'
    assert await c.pool.fetchval('SELECT calls FROM synthetic_receipt_effects WHERE run_id=$1', c.identity) == 1


async def test_invocation_capacity_is_bounded_before_any_provider_call(case):
    c = case
    for number in range(64):
        await begin_receipt(c.pool, c.state, c.tool.name, {'id':str(number),'args':{}})
    with pytest.raises(ToolReceiptFailure) as error:
        await supervisor.execute_tool_call(c.tool, c.call, c.state, c.pool)
    assert error.value.evidence['provider_called'] is False and c.calls == []
    assert await c.pool.fetchval('SELECT count(*) FROM agent_tool_receipts WHERE run_id=$1', c.identity) == 64


@pytest.mark.parametrize('oversize_output', [False, True])
async def test_private_payload_bound_does_not_turn_unrecorded_response_into_success(case, oversize_output):
    c = case
    @tool
    async def large_response() -> dict:
        """Synthetic oversized return, never an external provider."""
        c.calls.append('provider')
        return {'body':'x' * 2_000_000}
    selected = large_response if oversize_output else c.tool
    call = {'id':'oversize','args':{}} if oversize_output else {'id':'oversize','args':{'text':'x'*2_000_000}}
    with pytest.raises(ToolReceiptFailure) as error:
        await supervisor.execute_tool_call(selected, call, c.state, c.pool)
    assert (error.value.category == 'worker_reconciliation') is oversize_output
    assert len(c.calls) == int(oversize_output)
    assert await c.pool.fetchval('SELECT count(*) FROM agent_tool_receipts WHERE run_id=$1', c.identity) == int(oversize_output)


async def test_expired_payload_cannot_authorize_readback_and_is_erased_with_marker_retained(case):
    c = case
    await supervisor.execute_tool_call(c.tool, c.call, c.state, c.pool)
    # Seed an already-aged record through valid INSERT constraints, rather than
    # weakening production immutability or changing the machine/database clock.
    async with c.pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow('DELETE FROM agent_tool_receipts WHERE run_id=$1 RETURNING *', c.identity)
        await conn.execute('''INSERT INTO agent_tool_receipts(id,run_id,step_id,step_attempt,sequence_no,tool_name,
          call_hash,authority_hash,read_only,state,encrypted_payload,payload_hash,created_at,completed_at,expires_at)
          VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,clock_timestamp()-interval '2 hours',
            clock_timestamp()-interval '2 hours',clock_timestamp()-interval '1 hour')''',
          *[row[k] for k in ('id','run_id','step_id','step_attempt','sequence_no','tool_name','call_hash',
                            'authority_hash','read_only','state','encrypted_payload','payload_hash')])
    assert (await evidence(c)).blocked and (await evidence(c)).executions == []
    from app.runs.retention import apply_retention, export_user_data
    assert (await apply_retention(c.pool))['expired_tool_receipt_payloads'] >= 1
    record = await c.pool.fetchrow('SELECT * FROM agent_tool_receipts WHERE run_id=$1', c.identity)
    assert record['encrypted_payload'] is None and record['state'] == 'returned'
    assert (await evidence(c)).blocked
    exported = await export_user_data(c.pool, 'synthetic-user')
    assert exported['private_tool_receipts_excluded'] is True
