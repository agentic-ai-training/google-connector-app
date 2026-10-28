"""Actual PostgreSQL and actual coding phase functions; synthetic external tools."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app.coding import repository, worker
from app.coding.execution_fence import CodingExecutionFenced, coding_commit, require_coding_authority
from tests.coding.test_temporal_workspace import database as workspace_database

database=workspace_database
pytestmark=pytest.mark.asyncio(loop_scope='module')


@pytest_asyncio.fixture(loop_scope='module')
async def case(database,monkeypatch):
    identity=uuid4()
    settings=SimpleNamespace(executor_version='test-v1',deployment_version='test-v1',coding_worker_lease_seconds=60,
        coding_groq_api_key='SYNTHETIC_CODING_KEY',coding_approval_ttl_hours=1)
    monkeypatch.setattr(worker,'get_settings',lambda:settings)
    await database.execute('''INSERT INTO coding_runs(id,user_id,repository,request_excerpt,request_hash,encrypted_request,
      executor_version,planner_model,model_policy_version,tool_policy_version,source_egress_consented_at,idempotency_key)
      VALUES($1,'synthetic-user','synthetic/repository','Synthetic coding request',$2,$3,'test-v1',
        'synthetic-model','synthetic-model-policy','synthetic-tool-policy',now(),$4)''',identity,
      hashlib.sha256(b'synthetic request').hexdigest(),repository.encrypt_json({'request':'synthetic request'}),str(identity))
    row=dict(await worker._claim(database))
    c=SimpleNamespace(pool=database,identity=identity,row=row,calls=[],cancel=False,replace=False)
    async def snapshot(repository,ref,destination,*,guard):
        await guard()
        c.calls.append('snapshot')
        return 'a'*40
    async def local(arguments,*,coding_key=None,guard=None):
        await guard()
        c.calls.append('planner')
        state=Path(arguments[arguments.index('--state-dir')+1])
        plan={'actions':[{'tool':'create_file','path':'tests/synthetic.py','content':'synthetic'}]}
        raw=json.dumps(plan).encode()
        (state/'plan.json').write_bytes(raw)
        (state/'manifest.json').write_text(json.dumps({'changes':[{'path':'tests/synthetic.py'}]}))
        if c.cancel:
            await repository_module_cancel(c)
        if c.replace:
            await c.pool.execute("UPDATE coding_runs SET lease_owner='replacement' WHERE id=$1",c.identity)
        return {'plan_path':str(state/'plan.json'),'preview':{'approval_manifest':str(state/'manifest.json'),
            'approval_token':hashlib.sha256(raw).hexdigest()},'input_tokens':10,'output_tokens':20}
    monkeypatch.setattr(worker,'_repository_snapshot',snapshot)
    monkeypatch.setattr(worker,'_run_local',local)
    try:
        yield c
    finally:
        await database.execute('DELETE FROM coding_runs WHERE id=$1',identity)
        await database.execute("DELETE FROM coding_cache_entries WHERE user_id='synthetic-user' AND scope_key=$1",str(identity))


async def repository_module_cancel(c):
    await repository.cancel_coding_run(c.pool,run_id=str(c.identity),user_id='synthetic-user',reason='Synthetic cancellation')


async def test_actual_plan_commits_source_bound_approval_and_artifacts(case):
    c=case
    await worker._plan(c.pool,c.row)
    row=await c.pool.fetchrow('SELECT * FROM coding_runs WHERE id=$1',c.identity)
    assert row['status']=='awaiting_approval' and row['lease_owner'] is None and row['plan_hash']==row['approval_action_hash']
    assert c.calls==['snapshot','planner']
    assert await c.pool.fetchval('SELECT count(*) FROM coding_artifacts WHERE run_id=$1',c.identity)==2
    assert await c.pool.fetchval('SELECT count(*) FROM coding_run_steps WHERE run_id=$1',c.identity)==1


@pytest.mark.parametrize('change',['cancel','replacement','expired','deleted','source','tenant','executor'])
async def test_each_live_change_denies_admission_and_atomic_commit(case,change):
    c=case
    if change=='cancel':
        await repository_module_cancel(c)
    elif change=='replacement':
        await c.pool.execute("UPDATE coding_runs SET lease_owner='replacement' WHERE id=$1",c.identity)
    elif change=='expired':
        await c.pool.execute("UPDATE coding_runs SET lease_expires_at=now()-interval '1 second' WHERE id=$1",c.identity)
    elif change=='deleted':
        await c.pool.execute('UPDATE coding_runs SET deleted_at=now() WHERE id=$1',c.identity)
    elif change=='source':
        await c.pool.execute("UPDATE coding_runs SET request_hash=repeat('b',64) WHERE id=$1",c.identity)
    elif change=='tenant':
        c.row={**c.row,'user_id':'another-user'}
    else:
        c.row={**c.row,'executor_version':'another-executor'}
    with pytest.raises(CodingExecutionFenced):
        await require_coding_authority(c.pool,c.row)
    with pytest.raises(CodingExecutionFenced):
        async with coding_commit(c.pool,c.row):
            pytest.fail('fenced phase reached commit body')
    with pytest.raises(CodingExecutionFenced):
        await worker._plan(c.pool,c.row)
    assert c.calls==[]


@pytest.mark.parametrize('kind',['cancel','replacement'])
async def test_post_planner_race_cannot_write_success_artifacts_or_events(case,kind):
    c=case
    c.cancel=kind=='cancel'
    c.replace=kind=='replacement'
    with pytest.raises(CodingExecutionFenced):
        await worker._plan(c.pool,c.row)
    assert await c.pool.fetchval('SELECT count(*) FROM coding_artifacts WHERE run_id=$1',c.identity)==0
    assert await c.pool.fetchval('SELECT count(*) FROM coding_run_steps WHERE run_id=$1',c.identity)==0
    assert await c.pool.fetchval("SELECT count(*) FROM coding_run_events WHERE run_id=$1 "
        "AND event_type='coding_approval_required'",c.identity)==0
    row=await c.pool.fetchrow('SELECT * FROM coding_runs WHERE id=$1',c.identity)
    assert row['status']=='cancelled' if c.cancel else row['lease_owner']=='replacement'


async def test_plan_metadata_transaction_rolls_back_on_event_failure(case):
    c=case
    await c.pool.execute('''CREATE FUNCTION reject_coding_approval_event() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN IF NEW.event_type='coding_approval_required' THEN RAISE EXCEPTION 'synthetic event failure'; END IF;
      RETURN NEW; END $$;
      CREATE TRIGGER reject_coding_approval_event BEFORE INSERT ON coding_run_events
      FOR EACH ROW EXECUTE FUNCTION reject_coding_approval_event();''')
    try:
        with pytest.raises(asyncpg.RaiseError,match='event failure'):
            await worker._plan(c.pool,c.row)
        assert await c.pool.fetchval('SELECT status FROM coding_runs WHERE id=$1',c.identity)=='planning'
        assert await c.pool.fetchval('SELECT count(*) FROM coding_artifacts WHERE run_id=$1',c.identity)==0
        assert await c.pool.fetchval('SELECT count(*) FROM coding_run_steps WHERE run_id=$1',c.identity)==0
    finally:
        await c.pool.execute('DROP TRIGGER reject_coding_approval_event ON coding_run_events; DROP FUNCTION reject_coding_approval_event()')


async def test_process_entrypoint_preserves_cancel_after_planner(case,monkeypatch):
    c=case
    async def claim(*args):
        return c.row
    monkeypatch.setattr(worker,'_claim',claim)
    c.cancel=True
    assert await worker.process_one_coding_run(c.pool)
    assert await c.pool.fetchval('SELECT status FROM coding_runs WHERE id=$1',c.identity)=='cancelled'
    assert await c.pool.fetchval("SELECT count(*) FROM coding_run_events WHERE run_id=$1 AND event_type='coding_failed'",c.identity)==0


async def test_expired_execution_approval_blocks_without_decrypting_or_publishing(case,monkeypatch):
    c=case
    await c.pool.execute("UPDATE coding_runs SET status='executing',plan_hash=repeat('a',64),approval_action_hash=repeat('a',64),"
        "approval_status='approved',approved_by='synthetic-user',approval_expires_at=now()-interval '1 second' WHERE id=$1",c.identity)
    c.row=dict(await c.pool.fetchrow('SELECT * FROM coding_runs WHERE id=$1',c.identity))
    async def claim(*args):
        return c.row
    monkeypatch.setattr(worker,'_claim',claim)
    assert await worker.process_one_coding_run(c.pool)
    row=await c.pool.fetchrow('SELECT * FROM coding_runs WHERE id=$1',c.identity)
    assert row['status']=='blocked' and row['approval_status']=='expired' and row['lease_owner'] is None
    assert c.calls==[]


async def test_publication_checks_owner_between_github_mutations(case,monkeypatch,tmp_path):
    c=case
    await c.pool.execute("UPDATE coding_runs SET status='executing',plan_hash=repeat('a',64),approval_action_hash=repeat('a',64),"
        "approval_status='approved',approved_by='synthetic-user',approval_expires_at=now()+interval '1 hour' WHERE id=$1",c.identity)
    c.row=dict(await c.pool.fetchrow('SELECT * FROM coding_runs WHERE id=$1',c.identity))
    (tmp_path/'synthetic.py').write_text('SYNTHETIC SOURCE')
    requests=[]
    async def respond(request):
        requests.append((request.method,request.url.path))
        if request.method=='POST':
            # A replacement wins after the first blob RPC returns. No tree,
            # commit, branch or pull request may follow it.
            await c.pool.execute("UPDATE coding_runs SET lease_owner='replacement' WHERE id=$1",c.identity)
            return httpx.Response(201,json={'sha':'synthetic-blob'})
        return httpx.Response(200,json={'tree':{'sha':'synthetic-tree'}} if '/commits/' in request.url.path else {'tree':[]})
    client=httpx.AsyncClient
    monkeypatch.setattr(worker.httpx,'AsyncClient',lambda **kwargs:client(transport=httpx.MockTransport(respond),**kwargs))
    async def headers(*args):
        return {}  # No real GitHub authentication or network access.
    monkeypatch.setattr(worker,'github_api_headers',headers)
    async def guard():
        await require_coding_authority(c.pool,c.row)
    with pytest.raises(CodingExecutionFenced):
        await worker._publish_draft('synthetic/repository','main','a'*40,str(c.identity),'a'*64,
            tmp_path,['synthetic.py'],guard=guard)
    assert [path for method,path in requests if method=='POST']==['/repos/synthetic/repository/git/blobs']
    assert await c.pool.fetchval('SELECT count(*) FROM coding_artifacts WHERE run_id=$1',c.identity)==0


async def test_observation_cache_cannot_escape_a_rolled_back_business_transaction(case):
    c=case
    with pytest.raises(RuntimeError,match='synthetic outer failure'):
        async with coding_commit(c.pool,c.row) as conn:
            await worker._cache_observation(conn,c.row,scope_key=str(c.identity),source_version='synthetic-source',
                payload={'result':'synthetic-evidence'})
            raise RuntimeError('synthetic outer failure')
    assert await c.pool.fetchval('SELECT count(*) FROM coding_cache_entries WHERE scope_key=$1',str(c.identity))==0


async def test_cache_failure_rolls_back_only_its_savepoint(case):
    c=case
    await c.pool.execute('''CREATE FUNCTION reject_coding_cache() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN RAISE EXCEPTION 'synthetic cache failure'; END $$;
      CREATE TRIGGER reject_coding_cache BEFORE INSERT ON coding_cache_entries
      FOR EACH ROW EXECUTE FUNCTION reject_coding_cache();''')
    try:
        async with coding_commit(c.pool,c.row) as conn:
            await worker._cache_observation(conn,c.row,scope_key=str(c.identity),source_version='synthetic-source',payload={})
            # A failed INSERT without a savepoint would leave this tx aborted.
            await conn.execute("UPDATE coding_runs SET current_phase='cache_failure_recovered' WHERE id=$1",c.identity)
        assert await c.pool.fetchval('SELECT current_phase FROM coding_runs WHERE id=$1',c.identity)=='cache_failure_recovered'
    finally:
        await c.pool.execute('DROP TRIGGER reject_coding_cache ON coding_cache_entries; DROP FUNCTION reject_coding_cache()')


async def test_actual_tenant_erasure_cannot_race_cache_after_business_commit(case):
    import asyncio
    from app.runs.retention import delete_user_data
    c=case
    async with coding_commit(c.pool,c.row) as conn:
        await worker._cache_observation(conn,c.row,scope_key=str(c.identity),source_version='synthetic-source',payload={})
        erasure=asyncio.create_task(delete_user_data(c.pool,'synthetic-user'))
        async with asyncio.timeout(5):
            # Observe the actual erasure query waiting on the run lock, rather
            # than assuming that an arbitrary sleep scheduled it far enough.
            while not await c.pool.fetchval('''SELECT EXISTS(SELECT 1 FROM pg_stat_activity
              WHERE datname=current_database() AND wait_event_type='Lock' AND query LIKE 'DELETE FROM coding_runs%')'''):
                await asyncio.sleep(.01)
        assert not erasure.done()
    await asyncio.wait_for(erasure,timeout=5)
    assert await c.pool.fetchval('SELECT count(*) FROM coding_cache_entries WHERE scope_key=$1',str(c.identity))==0
    with pytest.raises(CodingExecutionFenced):
        async with coding_commit(c.pool,c.row):
            pytest.fail('erased run reacquired a cache-writing transaction')
