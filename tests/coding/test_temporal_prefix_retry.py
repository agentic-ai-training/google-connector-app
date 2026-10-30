"""Actual SDK activity generations and SQL; all provider effects are synthetic."""
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

from app.agents import supervisor
from app.api.routes import runs
from app.db import oauth_credentials
from app.runs import verifier, worker
from app.runs.schemas import ExecutionPlan, PlanStep
from app.runs.temporal_contracts import FinalizeRequest
from app.runs.temporal_read_retry import current_journal
from tests.coding.test_temporal_read_retry import activity_environment, next_request
from tests.coding.test_temporal_workspace import database as workspace_database, prepared

database = workspace_database
pytestmark = pytest.mark.asyncio(loop_scope='module')


async def failed_prefix(pool, monkeypatch):
    activities, environment, reference, request = await prepared(pool)
    identity, step_id = UUID(reference.run_id), UUID(request.step_id)
    c = SimpleNamespace(pool=pool, activities=activities, environment=environment, reference=reference,
        request=request, identity=identity, step_id=step_id, resolves=[], sends=[], abort_suffix=True,
        text='SYNTHETIC PRIVATE PREFIX CONTENT', destination='synthetic@example.invalid')
    args = {'allowed_tools':['resolve_chat_destination','send_chat_message'],
        'tool_arguments':{'destination':c.destination,'text':c.text}}
    plan = ExecutionPlan(objective='Send exact synthetic Chat content', services=['chat'], steps=[PlanStep(
        id='information', title='Send requested Chat', service='chat', operation='send', read_only=False, arguments=args)])
    await pool.execute("UPDATE agent_runs SET request='Send exact synthetic Chat content',plan=$2::jsonb WHERE id=$1",
        identity, plan.model_dump())
    await pool.execute("UPDATE agent_run_steps SET read_only=FALSE,service='chat',operation='send',input_data=$2::jsonb,"
        "max_attempts=1 WHERE id=$1", step_id, args)
    @tool
    async def resolve_chat_destination(destination: str) -> dict:
        """Synthetic resolver, never Google."""
        c.resolves.append(destination)
        return {'name':'spaces/synthetic','readbackVerified':True}
    @tool
    async def send_chat_message(space_id: str, text: str) -> dict:
        """Synthetic message counter, never Google."""
        c.sends.append((space_id,text))
        return {'name':'spaces/synthetic/messages/one'}
    monkeypatch.setattr(worker,'get_toolsets',lambda:{'chat':[resolve_chat_destination,send_chat_message]})
    original = worker.execute_tool_call
    async def stop_before_suffix(tool, *args, **kwargs):
        if tool.name=='send_chat_message' and c.abort_suffix:
            raise TimeoutError('Synthetic stop before suffix admission')
        return await original(tool,*args,**kwargs)
    monkeypatch.setattr(worker,'execute_tool_call',stop_before_suffix)
    async def credentials(*args):
        return SimpleNamespace(synthetic=True)
    monkeypatch.setattr(oauth_credentials,'load_google_credentials',credentials)
    monkeypatch.setattr(runs,'load_google_credentials',credentials)
    monkeypatch.setattr(runs,'get_settings',lambda:SimpleNamespace(executor_version='unrelated',deployment_version='unrelated'))
    async def record(*args, **kwargs):
        return None
    monkeypatch.setattr(supervisor,'_record_tool_call',record)
    class Reply:
        def __init__(self,value):
            self.value=value
        def execute(self):
            return self.value
    monkeypatch.setattr(verifier.google,'chat_service',SimpleNamespace(spaces=lambda:SimpleNamespace(
        get=lambda **kw:Reply({'name':'spaces/synthetic'}),messages=lambda:SimpleNamespace(
            get=lambda **kw:Reply({'name':'spaces/synthetic/messages/one','space':{'name':'spaces/synthetic'},'text':c.text})))))
    assert await environment.run(activities.execute_step,request)=='uncertain'
    assert await environment.run(activities.finalize,FinalizeRequest(reference))=='failed'
    c.prior = dict(await current_journal(pool,identity,step_id))
    assert c.prior['execution_policy']=='prefix-safe-v1' and c.prior['completed_at'] is not None
    assert c.resolves==[c.destination] and c.sends==[]
    return c


@pytest_asyncio.fixture(loop_scope='module')
async def case(database, monkeypatch):
    c = await failed_prefix(database,monkeypatch)
    try:
        yield c
    finally:
        await database.execute('DELETE FROM agent_runs WHERE id=$1',c.identity)


async def authorize(c):
    return await runs._reconcile_and_resume(c.pool,c.identity,'synthetic-user',c.step_id)


async def test_real_activity_continuation_reuses_prefix_and_sends_once(case):
    c=case
    assert (await authorize(c)).state=='continue_verified_prefix'
    request=await next_request(c)
    assert request.generation==1 and request.permit_id and request.activity_id!=c.request.activity_id
    assert await c.environment.run(c.activities.execute_step,c.request)=='superseded'
    c.abort_suffix=False
    environment=activity_environment(c,request)
    assert await environment.run(c.activities.execute_step,request)=='succeeded'
    assert await environment.run(c.activities.execute_step,request)=='succeeded'
    rows=await c.pool.fetch('SELECT * FROM temporal_workspace_steps WHERE run_id=$1 ORDER BY generation',c.identity)
    assert len(rows)==2 and rows[0]['state']=='uncertain' and rows[0]['execution_token']==c.prior['execution_token']
    assert rows[1]['generation']==1 and rows[1]['prefix_permit_id']==UUID(request.permit_id)
    assert rows[1]['retry_permit_id'] is None and rows[1]['execution_token']!=rows[0]['execution_token']
    assert await c.pool.fetchval('SELECT temporal_prefix_superseded($1,$2,$3)',c.identity,c.step_id,rows[0]['execution_token'])
    assert await environment.run(c.activities.finalize,FinalizeRequest(c.reference))=='completed'
    run=await c.pool.fetchrow('SELECT * FROM agent_runs WHERE id=$1',c.identity)
    assert run['functional_completion']==100 and run['side_effect_integrity']==100 and c.text in run['result']['output']
    assert c.resolves==[c.destination] and c.sends==[('spaces/synthetic',c.text)]
    assert await c.pool.fetchval('SELECT count(*) FROM agent_tool_receipts WHERE run_id=$1',c.identity)==2
    assert await c.pool.fetchval('SELECT count(*) FROM agent_model_calls WHERE run_id=$1',c.identity)==0


@pytest.mark.parametrize('mutation',['executing','not_finished','legacy','attempt','unrelated_plan','unknown_receipt'])
async def test_unproven_activity_or_evidence_cannot_get_a_generation(case,mutation):
    c=case
    if mutation in {'executing','legacy'}:
        # Test-only corruption simulates pre-policy/unfinished records. The real
        # immutable trigger is independently checked below and never disabled in app code.
        await c.pool.execute('ALTER TABLE temporal_workspace_steps DISABLE TRIGGER temporal_workspace_step_binding')
        try:
            column,value=('state','executing') if mutation=='executing' else ('execution_policy','read-safe-v1')
            assert column in {'state','execution_policy'}
            await c.pool.execute(f'UPDATE temporal_workspace_steps SET {column}=$2 WHERE run_id=$1',c.identity,value)
        finally:
            await c.pool.execute('ALTER TABLE temporal_workspace_steps ENABLE TRIGGER temporal_workspace_step_binding')
    elif mutation=='not_finished':
        await c.pool.execute('UPDATE temporal_workspace_steps SET completed_at=NULL WHERE run_id=$1',c.identity)
    elif mutation=='attempt':
        await c.pool.execute('UPDATE agent_run_steps SET attempt_count=2 WHERE id=$1',c.step_id)
    elif mutation=='unrelated_plan':
        await c.pool.execute("UPDATE agent_runs SET plan='{}' WHERE id=$1",c.identity)
    else:
        # Erasure removes private evidence, not the uncertainty marker.
        await c.pool.execute('ALTER TABLE agent_tool_receipts DISABLE TRIGGER tool_receipt_binding')
        try:
            await c.pool.execute("UPDATE agent_tool_receipts SET state='unknown' WHERE run_id=$1",c.identity)
        finally:
            await c.pool.execute('ALTER TABLE agent_tool_receipts ENABLE TRIGGER tool_receipt_binding')
    decision=await authorize(c)
    assert decision.state=='manual_required'
    assert await c.pool.fetchval('SELECT count(*) FROM temporal_prefix_generations WHERE run_id=$1',c.identity)==0
    assert c.sends==[]


@pytest.mark.parametrize('mutation',['source','expiry','receipt','cancel','approval'])
async def test_changed_authority_denies_new_activity_before_suffix(case,mutation):
    c=case
    assert (await authorize(c)).state=='continue_verified_prefix'
    request=await next_request(c)
    if mutation=='source':
        await c.pool.execute("UPDATE agent_run_steps SET input_data=input_data||'{\"changed\":true}'::jsonb WHERE id=$1",c.step_id)
    elif mutation=='expiry':
        await c.pool.execute('ALTER TABLE temporal_prefix_generations DISABLE TRIGGER temporal_prefix_generation_binding')
        try:
            await c.pool.execute("UPDATE temporal_prefix_generations SET issued_at=now()-interval '20 minutes',"
                "verification_started_at=now()-interval '20 minutes',expires_at=now()-interval '5 minutes' WHERE run_id=$1",c.identity)
        finally:
            await c.pool.execute('ALTER TABLE temporal_prefix_generations ENABLE TRIGGER temporal_prefix_generation_binding')
    elif mutation=='receipt':
        await c.pool.execute('ALTER TABLE agent_tool_receipts DISABLE TRIGGER tool_receipt_binding')
        try:
            await c.pool.execute('UPDATE agent_tool_receipts SET encrypted_payload=NULL WHERE run_id=$1',c.identity)
        finally:
            await c.pool.execute('ALTER TABLE agent_tool_receipts ENABLE TRIGGER tool_receipt_binding')
    elif mutation=='cancel':
        await c.pool.execute("UPDATE agent_runs SET status='cancelled' WHERE id=$1",c.identity)
    else:
        await c.pool.execute('UPDATE agent_run_steps SET requires_approval=TRUE WHERE id=$1',c.step_id)
    c.abort_suffix=False
    result=await activity_environment(c,request).run(c.activities.execute_step,request)
    assert result in {'not_admitted','cancelled'} and c.sends==[]
    assert await c.pool.fetchval('SELECT count(*) FROM temporal_workspace_steps WHERE run_id=$1',c.identity)==1


async def test_generations_and_continuations_are_bounded_without_resetting_history(case):
    c=case
    for generation in range(1,4):
        assert (await authorize(c)).state=='continue_verified_prefix'
        request=await next_request(c)
        assert request.generation==generation
        environment=activity_environment(c,request)
        assert await environment.run(c.activities.execute_step,request)=='uncertain'
        assert await environment.run(c.activities.finalize,FinalizeRequest(c.reference))=='failed'
    blocked=await authorize(c)
    assert blocked.reason_code=='temporal_prefix_generation_budget_exhausted'
    assert await c.pool.fetchval('SELECT attempt_count FROM agent_run_steps WHERE id=$1',c.step_id)==4
    assert await c.pool.fetchval('SELECT count(*) FROM temporal_workspace_steps WHERE run_id=$1',c.identity)==4
    assert c.resolves==[c.destination] and c.sends==[]


async def test_raw_sql_cannot_substitute_token_or_read_permission(case):
    c=case
    assert (await authorize(c)).state=='continue_verified_prefix'
    request=await next_request(c)
    permit=await c.pool.fetchrow('SELECT * FROM temporal_prefix_generations WHERE run_id=$1',c.identity)
    for read_only,token in ((False,uuid4()),(True,permit['execution_token'])):
        with pytest.raises(asyncpg.RaiseError,match='not admitted'):
            await c.pool.execute('''INSERT INTO temporal_workspace_steps(run_id,step_id,run_parent_id,step_parent_id,
              workflow_execution_id,activity_id,read_only,execution_token,generation,execution_policy,prefix_permit_id)
              VALUES($1,$2,$1,$2,$3,$4,$5,$6,1,'prefix-safe-v1',$7)''',c.identity,c.step_id,
              permit['workflow_execution_id'],request.activity_id,read_only,token,permit['id'])
    assert c.sends==[]


async def test_write_grants_cannot_be_consumed_as_read_retries(case):
    c=case
    assert (await authorize(c)).state=='continue_verified_prefix'
    request=await next_request(c)
    permit=await c.pool.fetchrow('SELECT * FROM temporal_prefix_generations WHERE run_id=$1',c.identity)
    with pytest.raises(asyncpg.RaiseError,match='read retry generation'):
        await c.pool.execute('''INSERT INTO temporal_workspace_steps(run_id,step_id,run_parent_id,step_parent_id,
          workflow_execution_id,activity_id,read_only,execution_token,generation,execution_policy,retry_permit_id)
          VALUES($1,$2,$1,$2,$3,$4,TRUE,$5,1,'read-safe-v1',$6)''',c.identity,c.step_id,
          permit['workflow_execution_id'],request.activity_id,permit['execution_token'],permit['id'])


async def test_old_delivery_during_new_execution_is_fenced(case,monkeypatch):
    c=case
    assert (await authorize(c)).state=='continue_verified_prefix'
    request=await next_request(c)
    entered,release=asyncio.Event(),asyncio.Event()
    original=worker._execute_step
    async def delayed(*args):
        entered.set()
        await release.wait()
        return await original(*args)
    monkeypatch.setattr(worker,'_execute_step',delayed)
    c.abort_suffix=False
    environment=activity_environment(c,request)
    task=asyncio.create_task(environment.run(c.activities.execute_step,request))
    try:
        await asyncio.wait_for(entered.wait(),timeout=5)
        assert await c.environment.run(c.activities.execute_step,c.request)=='superseded'
        assert await c.pool.fetchval('SELECT status FROM agent_run_steps WHERE id=$1',c.step_id)=='running'
    finally:
        release.set()
        await asyncio.wait_for(task,timeout=10)
    assert c.resolves==[c.destination] and len(c.sends)==1


async def test_immutable_metadata_erasure_and_downgrade_guard(case):
    c=case
    assert (await authorize(c)).state=='continue_verified_prefix'
    with pytest.raises(asyncpg.RaiseError,match='immutable'):
        await c.pool.execute("UPDATE temporal_prefix_generations SET verification_hash=repeat('0',64) WHERE run_id=$1",c.identity)
    migration=importlib.import_module('migrations.versions.034_temporal_prefix_generations')
    with pytest.raises(asyncpg.RaiseError,match='retain'):
        await c.pool.execute(migration.DOWNGRADE_SQL)
    await c.pool.execute('DELETE FROM agent_runs WHERE id=$1',c.identity)
    row=await c.pool.fetchrow('SELECT * FROM temporal_prefix_generations WHERE run_id=$1',c.identity)
    assert row and row['run_parent_id'] is None and row['step_parent_id'] is None and row['prefix_grant_id'] is None
    assert c.text not in str(dict(row)) and c.destination not in str(dict(row))


async def test_prefix_expiry_after_consumption_still_blocks_every_tool(case,monkeypatch):
    c=case
    assert (await authorize(c)).state=='continue_verified_prefix'
    request=await next_request(c)
    original=worker.execute_tool_call
    async def expire_before_call(*args,**kwargs):
        # The activity was admitted and consumed its shared grant. Only the
        # narrower Temporal proof expires; its later-lived shared grant is fresh.
        await c.pool.execute('ALTER TABLE temporal_prefix_generations DISABLE TRIGGER temporal_prefix_generation_binding')
        try:
            await c.pool.execute("UPDATE temporal_prefix_generations SET issued_at=now()-interval '20 minutes',"
                "verification_started_at=now()-interval '20 minutes',expires_at=now()-interval '5 minutes' WHERE run_id=$1",c.identity)
        finally:
            await c.pool.execute('ALTER TABLE temporal_prefix_generations ENABLE TRIGGER temporal_prefix_generation_binding')
        return await original(*args,**kwargs)
    monkeypatch.setattr(worker,'execute_tool_call',expire_before_call)
    c.abort_suffix=False
    assert await activity_environment(c,request).run(c.activities.execute_step,request)=='uncertain'
    assert c.resolves==[c.destination] and c.sends==[]
    assert await c.pool.fetchval('SELECT consumed_at IS NOT NULL FROM tool_prefix_continuations WHERE run_id=$1',c.identity)


@pytest.mark.parametrize('cause',['expiry','metadata'])
async def test_unadmitted_generation_renews_without_reset_or_old_delivery(case,monkeypatch,cause):
    c=case
    assert (await authorize(c)).state=='continue_verified_prefix'
    old_request=await next_request(c)
    if cause=='expiry':
        await c.pool.execute('ALTER TABLE temporal_prefix_generations DISABLE TRIGGER temporal_prefix_generation_binding')
        try:
            await c.pool.execute("UPDATE temporal_prefix_generations SET issued_at=now()-interval '20 minutes',"
                "verification_started_at=now()-interval '20 minutes',expires_at=now()-interval '5 minutes' WHERE run_id=$1",c.identity)
        finally:
            await c.pool.execute('ALTER TABLE temporal_prefix_generations ENABLE TRIGGER temporal_prefix_generation_binding')
    else:
        await c.pool.execute('''UPDATE agent_artifacts SET metadata=metadata||'{"synthetic_readback_tag":true}'::jsonb
          WHERE run_id=$1''',c.identity)
    assert await activity_environment(c,old_request).run(c.activities.execute_step,old_request)=='not_admitted'
    assert await activity_environment(c,old_request).run(c.activities.finalize,FinalizeRequest(c.reference))=='failed'
    assert (await authorize(c)).state=='continue_verified_prefix'
    new_request=await next_request(c)
    assert new_request.generation==old_request.generation==1 and new_request.permit_id!=old_request.permit_id
    assert await activity_environment(c,old_request).run(c.activities.execute_step,old_request)=='superseded'
    c.abort_suffix=False
    entered,release=asyncio.Event(),asyncio.Event()
    original=worker._execute_step
    async def delayed(*args):
        entered.set()
        await release.wait()
        return await original(*args)
    monkeypatch.setattr(worker,'_execute_step',delayed)
    task=asyncio.create_task(activity_environment(c,new_request).run(c.activities.execute_step,new_request))
    try:
        await asyncio.wait_for(entered.wait(),timeout=5)
        assert await activity_environment(c,old_request).run(c.activities.execute_step,old_request)=='superseded'
        assert await c.pool.fetchval('SELECT state FROM temporal_workspace_steps WHERE run_id=$1 AND generation=1',c.identity)=='executing'
    finally:
        release.set()
    assert await asyncio.wait_for(task,timeout=10)=='succeeded'
    assert c.resolves==[c.destination] and len(c.sends)==1
    assert await c.pool.fetchval('SELECT attempt_count FROM agent_run_steps WHERE id=$1',c.step_id)==2
    assert await c.pool.fetchval('SELECT count(*) FROM tool_prefix_continuations WHERE run_id=$1',c.identity)==2


async def test_unknown_receipt_blocks_success_independently_of_journal_failure_label(case):
    c=case
    # Simulate an older layer having labelled a failed call as failed rather
    # than uncertain. The independent private receipt marker must still win.
    await c.pool.execute('ALTER TABLE temporal_workspace_steps DISABLE TRIGGER temporal_workspace_step_binding')
    await c.pool.execute('ALTER TABLE agent_tool_receipts DISABLE TRIGGER tool_receipt_binding')
    try:
        await c.pool.execute("UPDATE temporal_workspace_steps SET state='failed' WHERE run_id=$1",c.identity)
        await c.pool.execute("UPDATE agent_tool_receipts SET state='unknown' WHERE run_id=$1",c.identity)
    finally:
        await c.pool.execute('ALTER TABLE temporal_workspace_steps ENABLE TRIGGER temporal_workspace_step_binding')
        await c.pool.execute('ALTER TABLE agent_tool_receipts ENABLE TRIGGER tool_receipt_binding')
    assert await c.environment.run(c.activities.finalize,FinalizeRequest(c.reference))=='failed'
    row=await c.pool.fetchrow('SELECT * FROM agent_runs WHERE id=$1',c.identity)
    assert row['side_effect_integrity']==0 and row['error_category']=='worker_reconciliation'


async def test_actual_sheet_activity_reuses_create_and_populates_exact_dependency_rows(database,monkeypatch):
    activities,environment,reference,request=await prepared(database)
    identity,step_id=UUID(reference.run_id),UUID(request.step_id)
    args={'allowed_tools':['create_google_sheet','write_google_sheet']}
    plan=ExecutionPlan(objective='Create and populate sender Sheet',services=['gmail','sheets'],steps=[
        PlanStep(id='senders',title='Read senders',service='gmail',operation='recent_senders'),
        PlanStep(id='information',title='Populate sender Sheet',service='sheets',operation='create_and_write',
            read_only=False,arguments=args,dependencies=['senders'])])
    output={'tool_executions':[{'tool':'list_recent_gmail_senders','result':{'senders':[
        {'sender_name':'Synthetic','sender_email':'synthetic@example.invalid'}]}}]}
    await database.execute("UPDATE agent_runs SET request='Create and populate sender Sheet',plan=$2::jsonb WHERE id=$1",
        identity,plan.model_dump())
    await database.execute("UPDATE agent_run_steps SET read_only=FALSE,service='sheets',operation='create_and_write',"
        "dependencies=ARRAY['senders'],input_data=$2::jsonb WHERE id=$1",step_id,args)
    await database.execute('''INSERT INTO agent_run_steps(run_id,step_key,sequence_no,title,service,operation,status,output_data)
      VALUES($1,'senders',0,'Synthetic senders','gmail','recent_senders','completed',$2::jsonb)''',identity,output)
    created,written=[],[]
    allow_write=False
    @tool
    async def create_google_sheet(title: str) -> dict:
        """Synthetic creation counter, never Google."""
        created.append(title)
        return {'spreadsheetId':'synthetic-sheet'}
    @tool
    async def write_google_sheet(spreadsheet_id: str, range: str, values: list) -> dict:
        """Synthetic population counter."""
        assert spreadsheet_id=='synthetic-sheet'
        written.append(values)
        return {'spreadsheetId':spreadsheet_id,'updatedRows':len(values)}
    monkeypatch.setattr(worker,'get_toolsets',lambda:{'sheets':[create_google_sheet,write_google_sheet]})
    original=worker.execute_tool_call
    async def stop_before_write(tool,*args,**kwargs):
        if tool.name=='write_google_sheet' and not allow_write:
            raise TimeoutError('Synthetic stop before population admission')
        return await original(tool,*args,**kwargs)
    monkeypatch.setattr(worker,'execute_tool_call',stop_before_write)
    async def credentials(*args):
        return SimpleNamespace(synthetic=True)
    async def record(*args,**kwargs):
        return None
    monkeypatch.setattr(oauth_credentials,'load_google_credentials',credentials)
    monkeypatch.setattr(runs,'load_google_credentials',credentials)
    monkeypatch.setattr(supervisor,'_record_tool_call',record)
    class Reply:
        def __init__(self,value):
            self.value=value
        def execute(self):
            return self.value
    monkeypatch.setattr(verifier.google,'sheets_service',SimpleNamespace(spreadsheets=lambda:SimpleNamespace(
        get=lambda **kw:Reply({'spreadsheetId':'synthetic-sheet'}),values=lambda:SimpleNamespace(
            get=lambda **kw:Reply({'values':written[0]})))))
    try:
        assert await environment.run(activities.execute_step,request)=='uncertain'
        assert await environment.run(activities.finalize,FinalizeRequest(reference))=='partial'
        c=SimpleNamespace(pool=database,identity=identity,step_id=step_id,environment=environment,reference=reference,
            activities=activities)
        assert (await authorize(c)).state=='continue_verified_prefix'
        new_request=await next_request(c)
        allow_write=True
        new_environment=activity_environment(c,new_request)
        assert await new_environment.run(activities.execute_step,new_request)=='succeeded'
        assert await new_environment.run(activities.finalize,FinalizeRequest(reference))=='completed'
        assert len(created)==1 and written==[[['Name','Email'],['Synthetic','synthetic@example.invalid']]]
        artifact=await database.fetchrow('SELECT * FROM agent_artifacts WHERE run_id=$1',identity)
        assert artifact['external_id']=='synthetic-sheet' and artifact['verification_status']=='verified' and artifact['safe_to_delete']
    finally:
        await database.execute('DELETE FROM agent_runs WHERE id=$1',identity)
