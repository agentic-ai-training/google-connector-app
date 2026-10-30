"""Opt-in, real loopback Temporal/SQLite, disposable PostgreSQL, no providers."""
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from langchain_core.tools import tool
import pytest
import pytest_asyncio

pytest.importorskip('temporalio')
from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowFailureError, WorkflowHistory
from temporalio.exceptions import CancelledError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from app.runs import temporal_dispatch as dispatch, temporal_refresh as refresh, worker
from app.runs.schemas import ExecutionPlan, PlanStep
from app.runs.temporal_activities import WorkspaceActivities
from app.runs.temporal_workflow import WorkspaceWorkflow
from tests.coding.test_temporal_workspace import database as workspace_database

database = workspace_database
pytestmark = [pytest.mark.asyncio(loop_scope='module'), pytest.mark.skipif(
    os.getenv('RUN_TEMPORAL_SERVER_TESTS') != '1', reason='opt in via verify_temporal_server_local.py')]
VERSION = 'local-server-proof-v1'
PRIVATE = 'SYNTHETIC_PRIVATE_SERVER_PROOF'


@pytest_asyncio.fixture(loop_scope='module')
async def server():
    environment = await asyncio.wait_for(WorkflowEnvironment.start_local(
        namespace='gca-local-proof', ip='127.0.0.1',ui=False,identity='gca-local-proof',
        dev_server_download_version='v1.9.1',dev_server_existing_path=os.getenv('GCA_TEMPORAL_CLI'),
        dev_server_log_level='error'), timeout=60)
    assert environment.client.service_client.config.target_host.startswith('127.0.0.1:')
    try:
        yield environment
    finally:
        port = int(environment.client.service_client.config.target_host.rsplit(':',1)[1])
        await environment.shutdown()
        try:
            _,writer = await asyncio.wait_for(asyncio.open_connection('127.0.0.1',port),timeout=1)
        except OSError:
            pass
        else:
            writer.close()
            await writer.wait_closed()
            pytest.fail('local Temporal listener remained open after shutdown')
        print('GCA_TEMPORAL_SERVER_STOPPED')


@asynccontextmanager
async def proof_case(database, server, monkeypatch):
    identity, step_id = uuid4(), uuid4()
    queue = 'gca-local-proof-' + uuid4().hex
    await database.execute('''UPDATE workflow_ownership_control SET admission_enabled=TRUE,
      namespace='gca-local-proof',task_queue=$1,workflow_version=$2,executor_version=$2,
      evidence_reference='test:real-loopback-server',expires_at=now()+interval '1 hour' WHERE workflow_kind='workspace' ''',queue,VERSION)
    await database.execute("UPDATE temporal_dispatch_control SET enabled=TRUE,evidence_reference='test:real-loopback-server',"
        "notifications_enabled=TRUE,notification_evidence_reference='test:real-loopback-refresh'")
    args = {'informational_intent':'identity','capability_catalog':{}}
    plan = ExecutionPlan(objective=PRIVATE,services=['general'],steps=[PlanStep(
        id='identity',title='Answer identity',service='general',operation='answer_information',arguments=args)])
    await database.execute('''INSERT INTO agent_runs(id,user_id,session_id,request,idempotency_key,plan,
      executor_version,orchestration_owner,temporal_namespace,temporal_workflow_id,temporal_task_queue,temporal_workflow_version)
      VALUES($1,'synthetic-server-user','synthetic-session',$2,$3,$4::jsonb,$5,'temporal',
        'gca-local-proof',$6,$7,$5)''',identity,PRIVATE,str(identity),plan.model_dump(),VERSION,'gca.workspace.'+str(identity),queue)
    await database.execute('''INSERT INTO agent_run_steps(id,run_id,step_key,sequence_no,title,service,operation,input_data,max_attempts)
      VALUES($1,$2,'identity',1,'Answer identity','general','answer_information',$3::jsonb,3)''',step_id,identity,args)
    class ForbiddenGraph:
        async def ainvoke(self, *args, **kwargs):
            pytest.fail('real server information proof must not invoke a model')
    from app.db import oauth_credentials
    async def forbidden_credentials(*args):
        pytest.fail('real server information proof must not load Google credentials')
    monkeypatch.setattr(oauth_credentials,'load_google_credentials',forbidden_credentials)
    app = SimpleNamespace(state=SimpleNamespace(agent_graph=ForbiddenGraph()))
    activities = WorkspaceActivities(database,app,namespace='gca-local-proof',task_queue=queue,
        workflow_version=VERSION,executor_version=VERSION)
    c = SimpleNamespace(pool=database,server=server,client=server.client,identity=identity,step_id=step_id,queue=queue,
        activities=activities,workflow_id='gca.workspace.'+str(identity))
    try:
        yield c
    finally:
        handle = c.client.get_workflow_handle(c.workflow_id)
        try:
            description = await handle.describe(rpc_timeout=timedelta(seconds=3))
            if description.status.name == 'RUNNING':
                await handle.terminate('synthetic local proof cleanup',rpc_timeout=timedelta(seconds=3))
        except Exception:
            pass  # May never have started; server fixture owns final shutdown.
        await database.execute('DELETE FROM agent_runs WHERE id=$1',identity)


@pytest_asyncio.fixture(loop_scope='module')
async def case(database, server, monkeypatch):
    async with proof_case(database,server,monkeypatch) as c:
        yield c


@asynccontextmanager
async def running(c):
    async with Worker(c.client,task_queue=c.queue,workflows=[WorkspaceWorkflow],
            activities=[c.activities.inspect,c.activities.execute_step,c.activities.finalize],
            max_concurrent_workflow_tasks=2,max_concurrent_activities=4,
            graceful_shutdown_timeout=timedelta(seconds=2)):
        yield


async def started(c, *, lose_ack=False):
    transport = dispatch.TemporalStartTransport(c.client)
    item = await dispatch.claim(c.pool)
    assert item.run_id == c.identity
    receipt = await transport.ensure_started(item)
    if lose_ack:
        await c.pool.execute("UPDATE temporal_start_intents SET lease_expires_at=now()-interval '1 second' WHERE run_id=$1",c.identity)
        newer = await dispatch.claim(c.pool)
        recovered = await transport.ensure_started(newer)
        assert recovered.first_execution_run_id == receipt.first_execution_run_id
        assert not await dispatch.acknowledge(c.pool,item,receipt)
        item,receipt = newer,recovered
    assert await dispatch.acknowledge(c.pool,item,receipt)
    return c.client.get_workflow_handle(c.workflow_id,first_execution_run_id=str(receipt.first_execution_run_id))


async def until_status(c, handle, expected):
    async with asyncio.timeout(20):
        while True:
            observed=await handle.query('status',rpc_timeout=timedelta(seconds=3))
            business=await c.pool.fetchval('SELECT status FROM agent_runs WHERE id=$1',c.identity)
            if observed==business==expected:
                break
            await asyncio.sleep(0.05)
    assert await c.pool.fetchval('SELECT status FROM agent_runs WHERE id=$1',c.identity) == expected


async def replay(c, handle, label):
    history = await handle.fetch_history(rpc_timeout=timedelta(seconds=5))
    body = history.to_json()
    assert PRIVATE not in body and 'synthetic-server-user' not in body
    assert 'registered_capability_catalog' not in body and 'Google Workspace Agent' not in body
    assert sum(e.event_type==EventType.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED for e in history.events) == 1
    result = await asyncio.wait_for(Replayer(workflows=[WorkspaceWorkflow],namespace=c.client.namespace).replay_workflow(history),timeout=20)
    assert result.replay_failure is None
    proof = {'schema':'gca.temporal.server-proof.v1','case':label,'workflow_id':history.workflow_id,
        'namespace':c.client.namespace,'events':len(history.events),
        'history_sha256':hashlib.sha256(body.encode()).hexdigest(),'replay_passed':True}
    if label=='lost-start-ack-and-completion':
        incompatible = WorkflowHistory.from_json(history.workflow_id,body)
        scheduled = next(e for e in incompatible.events if e.event_type==EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED)
        scheduled.activity_task_scheduled_event_attributes.activity_type.name='gca.incompatible.replay.probe'
        print('GCA_EXPECTED_INCOMPATIBLE_REPLAY_PROBE',flush=True)
        rejected = await asyncio.wait_for(Replayer(workflows=[WorkspaceWorkflow],namespace=c.client.namespace).replay_workflow(
            incompatible,raise_on_replay_failure=False),timeout=20)
        assert rejected.replay_failure is not None, 'replay gate accepted an incompatible activity command'
        proof['incompatible_history_rejected']=True
    directory = os.getenv('TEMPORAL_PROOF_DIR')
    if directory:
        # Runner provides a newly created, ignored artifact directory. All history
        # above was checked to exclude synthetic task/user/output content.
        Path(directory,label+'.history.json').write_text(body,encoding='utf-8')
        Path(directory,label+'.proof.json').write_text(json.dumps(proof,sort_keys=True),encoding='utf-8')
    print('GCA_TEMPORAL_PROOF '+json.dumps(proof,sort_keys=True))


async def test_real_dispatch_lost_ack_completion_and_history_replay(case):
    c = case
    handle = await started(c,lose_ack=True)
    async with running(c):
        assert await asyncio.wait_for(handle.result(),timeout=25) == 'completed'
    row = await c.pool.fetchrow('SELECT * FROM agent_runs WHERE id=$1',c.identity)
    assert row['functional_completion']==100 and row['lease_owner'] is None
    assert 'Google Workspace Agent' in row['result']['output']
    assert await c.pool.fetchval('SELECT count(*) FROM agent_model_calls WHERE run_id=$1',c.identity)==0
    await replay(c,handle,'lost-start-ack-and-completion')


async def fail_once(c, monkeypatch):
    original = worker._execute_step
    calls = []
    async def execute(*args):
        calls.append('execute')
        if len(calls)==1:
            raise TimeoutError('synthetic transient failure')
        return await original(*args)
    monkeypatch.setattr(worker,'_execute_step',execute)
    return calls


async def test_real_refresh_read_retry_after_worker_restart_and_replay(case, monkeypatch):
    c = case
    calls = await fail_once(c,monkeypatch)
    handle = await started(c)
    async with running(c):
        await until_status(c,handle,'failed')
    # A real worker is stopped. Only the server/history survives this task-poller restart.
    from app.api.routes import runs
    monkeypatch.setattr(runs,'get_settings',lambda: SimpleNamespace(executor_version='unrelated',deployment_version='unrelated'))
    decision = await runs._reconcile_and_resume(c.pool,c.identity,'synthetic-server-user',c.step_id)
    assert decision.state=='safe_to_retry'
    assert await refresh.deliver_one(c.pool,refresh.TemporalRefreshTransport(c.client),run_id=c.identity)
    wake = await c.pool.fetchrow('SELECT * FROM temporal_refresh_outbox WHERE run_id=$1',c.identity)
    assert wake['acknowledged_generation']==wake['desired_generation'] and not wake['blocked']
    async with running(c):
        assert await asyncio.wait_for(handle.result(),timeout=25)=='completed'
    assert calls==['execute','execute']
    rows = await c.pool.fetch('SELECT generation,state FROM temporal_workspace_steps WHERE run_id=$1 ORDER BY generation',c.identity)
    assert [tuple(row.values()) for row in rows]==[(0,'uncertain'),(1,'succeeded')]
    await replay(c,handle,'refresh-retry-worker-restart')


async def configure_chat(c, monkeypatch, *, lose_send_response=False):
    from app.agents import supervisor
    from app.db import oauth_credentials
    from app.runs import verifier
    from app.api.routes import runs
    c.destination,c.text='synthetic@example.invalid',PRIVATE+' CHAT CONTENT'
    c.resolves,c.sends=[],[]
    c.abort_suffix=not lose_send_response
    args={'allowed_tools':['resolve_chat_destination','send_chat_message'],
        'tool_arguments':{'destination':c.destination,'text':c.text}}
    plan=ExecutionPlan(objective=PRIVATE,services=['chat'],steps=[PlanStep(
        id='identity',title='Send exact Chat',service='chat',operation='send',read_only=False,arguments=args)])
    await c.pool.execute('UPDATE agent_runs SET plan=$2::jsonb WHERE id=$1',c.identity,plan.model_dump())
    await c.pool.execute("UPDATE agent_run_steps SET read_only=FALSE,service='chat',operation='send',input_data=$2::jsonb,"
        "max_attempts=1 WHERE id=$1",c.step_id,args)
    @tool
    async def resolve_chat_destination(destination: str) -> dict:
        """Synthetic resolver, not Google."""
        c.resolves.append(destination)
        return {'name':'spaces/synthetic','readbackVerified':True}
    @tool
    async def send_chat_message(space_id: str, text: str) -> dict:
        """Synthetic message counter; response can be deliberately lost."""
        c.sends.append((space_id,text))
        if lose_send_response:
            raise TimeoutError('Synthetic transport lost the response after the effect')
        return {'name':'spaces/synthetic/messages/one'}
    monkeypatch.setattr(worker,'get_toolsets',lambda:{'chat':[resolve_chat_destination,send_chat_message]})
    original=worker.execute_tool_call
    async def stop_before_send(tool,*args,**kwargs):
        if tool.name=='send_chat_message' and c.abort_suffix:
            raise TimeoutError('Synthetic stop before suffix invocation')
        return await original(tool,*args,**kwargs)
    monkeypatch.setattr(worker,'execute_tool_call',stop_before_send)
    async def credentials(*args):
        return SimpleNamespace(synthetic=True)
    async def record(*args,**kwargs):
        return None
    monkeypatch.setattr(oauth_credentials,'load_google_credentials',credentials)
    monkeypatch.setattr(runs,'load_google_credentials',credentials)
    monkeypatch.setattr(supervisor,'_record_tool_call',record)
    monkeypatch.setattr(runs,'get_settings',lambda:SimpleNamespace(executor_version='unrelated',deployment_version='unrelated'))
    class Reply:
        def __init__(self,value):
            self.value=value
        def execute(self):
            return self.value
    monkeypatch.setattr(verifier.google,'chat_service',SimpleNamespace(spaces=lambda:SimpleNamespace(
        get=lambda **kw:Reply({'name':'spaces/synthetic'}),messages=lambda:SimpleNamespace(get=lambda **kw:Reply({
            'name':'spaces/synthetic/messages/one','space':{'name':'spaces/synthetic'},'text':c.text})))))


async def test_real_write_prefix_worker_restart_refresh_and_replay(case,monkeypatch):
    c=case
    await configure_chat(c,monkeypatch)
    handle=await started(c)
    async with running(c):
        await until_status(c,handle,'failed')
    assert c.resolves==[c.destination] and c.sends==[]
    # Actual authenticated resume and real refresh transport while no task poller
    # is running. Neither the signal nor history carries private tool content.
    from app.api.routes.runs import _reconcile_and_resume
    assert (await _reconcile_and_resume(c.pool,c.identity,'synthetic-server-user',c.step_id)).state=='continue_verified_prefix'
    first_permit=await c.pool.fetchval('SELECT id FROM temporal_prefix_generations WHERE run_id=$1',c.identity)
    # Advance only this synthetic proof's permit expiry instead of sleeping for
    # fifteen minutes. Application code cannot edit these immutable rows.
    await c.pool.execute('ALTER TABLE temporal_prefix_generations DISABLE TRIGGER temporal_prefix_generation_binding')
    try:
        await c.pool.execute("UPDATE temporal_prefix_generations SET issued_at=now()-interval '20 minutes',"
            "verification_started_at=now()-interval '20 minutes',expires_at=now()-interval '5 minutes' WHERE run_id=$1",c.identity)
    finally:
        await c.pool.execute('ALTER TABLE temporal_prefix_generations ENABLE TRIGGER temporal_prefix_generation_binding')
    assert await refresh.deliver_one(c.pool,refresh.TemporalRefreshTransport(c.client),run_id=c.identity)
    async with running(c):
        await until_status(c,handle,'failed')
    assert c.resolves==[c.destination] and c.sends==[]
    assert await c.pool.fetchval('SELECT attempt_count FROM agent_run_steps WHERE id=$1',c.step_id)==1
    assert (await _reconcile_and_resume(c.pool,c.identity,'synthetic-server-user',c.step_id)).state=='continue_verified_prefix'
    assert await c.pool.fetchval('SELECT count(*) FROM temporal_prefix_generations WHERE run_id=$1',c.identity)==2
    assert await c.pool.fetchval('SELECT count(*) FROM temporal_prefix_generations WHERE run_id=$1 AND id<>$2',c.identity,first_permit)==1
    assert await refresh.deliver_one(c.pool,refresh.TemporalRefreshTransport(c.client),run_id=c.identity)
    c.abort_suffix=False
    async with running(c):
        assert await asyncio.wait_for(handle.result(),timeout=25)=='completed'
    assert c.resolves==[c.destination] and c.sends==[('spaces/synthetic',c.text)]
    rows=await c.pool.fetch('SELECT generation,state,read_only FROM temporal_workspace_steps WHERE run_id=$1 ORDER BY generation',c.identity)
    assert [tuple(row.values()) for row in rows]==[(0,'uncertain',False),(1,'succeeded',False)]
    assert await c.pool.fetchval('SELECT count(*) FROM agent_model_calls WHERE run_id=$1',c.identity)==0
    assert await c.pool.fetchval('SELECT side_effect_integrity FROM agent_runs WHERE id=$1',c.identity)==100
    await replay(c,handle,'write-prefix-refresh-worker-restart')


async def test_real_unknown_write_never_repeats_and_replays(case,monkeypatch):
    c=case
    await configure_chat(c,monkeypatch,lose_send_response=True)
    handle=await started(c)
    async with running(c):
        await until_status(c,handle,'failed')
        from app.api.routes.runs import _reconcile_and_resume
        decision=await _reconcile_and_resume(c.pool,c.identity,'synthetic-server-user',c.step_id)
        assert decision.state=='manual_required' and decision.reason_code=='tool_receipt_write_outcome_uncertain'
        assert await c.pool.fetchval('SELECT side_effect_integrity FROM agent_runs WHERE id=$1',c.identity)==0
        assert await c.pool.fetchval('SELECT count(*) FROM temporal_prefix_generations WHERE run_id=$1',c.identity)==0
        assert c.resolves==[c.destination] and len(c.sends)==1
        await handle.cancel(rpc_timeout=timedelta(seconds=3))
        with pytest.raises(WorkflowFailureError):
            await asyncio.wait_for(handle.result(),timeout=25)
    assert c.sends==[('spaces/synthetic',c.text)]
    await replay(c,handle,'unknown-write-blocked')


async def test_real_sdk_cancellation_preserves_business_cancellation_and_replays(case, monkeypatch):
    c = case
    await fail_once(c,monkeypatch)
    handle = await started(c)
    async with running(c):
        await until_status(c,handle,'failed')
        await handle.cancel(rpc_timeout=timedelta(seconds=3))
        with pytest.raises(WorkflowFailureError) as error:
            await asyncio.wait_for(handle.result(),timeout=25)
        assert isinstance(error.value.cause,CancelledError)
    assert await c.pool.fetchval('SELECT status FROM agent_runs WHERE id=$1',c.identity)=='cancelled'
    assert await c.pool.fetchval('SELECT state FROM temporal_workspace_steps WHERE run_id=$1',c.identity)=='uncertain'
    await replay(c,handle,'sdk-cancellation')


async def test_real_erasure_refresh_exits_without_recreating_private_ledger(case, monkeypatch):
    c = case
    calls = await fail_once(c,monkeypatch)
    handle = await started(c)
    async with running(c):
        await until_status(c,handle,'failed')
        await c.pool.execute('DELETE FROM agent_runs WHERE id=$1',c.identity)
        assert await refresh.deliver_one(c.pool,refresh.TemporalRefreshTransport(c.client),run_id=c.identity)
        assert await asyncio.wait_for(handle.result(),timeout=25)=='deleted'
    assert not await c.pool.fetchval('SELECT EXISTS(SELECT 1 FROM agent_runs WHERE id=$1)',c.identity)
    assert calls==['execute']
    await replay(c,handle,'erasure-refresh')


async def test_real_sqlite_server_restart_retains_first_execution_and_resumes(database,monkeypatch,tmp_path):
    filename = str(tmp_path/'temporal.sqlite')
    async def start():
        value = await asyncio.wait_for(WorkflowEnvironment.start_local(namespace='gca-local-proof',ip='127.0.0.1',
            ui=False,dev_server_existing_path=os.getenv('GCA_TEMPORAL_CLI'),dev_server_download_version='v1.9.1',
            dev_server_database_filename=filename,dev_server_log_level='error'),timeout=60)
        assert value.client.service_client.config.target_host.startswith('127.0.0.1:')
        return value
    first,second,first_stopped = await start(),None,False
    try:
        async with proof_case(database,first,monkeypatch) as c:
            calls = await fail_once(c,monkeypatch)
            handle = await started(c)
            async with running(c):
                await until_status(c,handle,'failed')
            first_execution = (await handle.describe()).raw_info.first_run_id
            await first.shutdown()
            first_stopped = True
            second = await start()
            c.server,c.client = second,second.client
            handle = c.client.get_workflow_handle(c.workflow_id,first_execution_run_id=first_execution)
            assert (await handle.describe()).raw_info.first_run_id==first_execution
            from app.api.routes import runs
            monkeypatch.setattr(runs,'get_settings',lambda: SimpleNamespace(executor_version='unrelated',deployment_version='unrelated'))
            assert (await runs._reconcile_and_resume(database,c.identity,'synthetic-server-user',c.step_id)).state=='safe_to_retry'
            assert await refresh.deliver_one(database,refresh.TemporalRefreshTransport(c.client),run_id=c.identity)
            async with running(c):
                assert await asyncio.wait_for(handle.result(),timeout=25)=='completed'
            assert calls==['execute','execute']
            assert (await handle.describe()).raw_info.first_run_id==first_execution
            await replay(c,handle,'sqlite-server-restart')
    finally:
        if not first_stopped:
            await first.shutdown()
        if second:
            await second.shutdown()
        print('GCA_TEMPORAL_PERSISTENT_SERVER_STOPPED',flush=True)
