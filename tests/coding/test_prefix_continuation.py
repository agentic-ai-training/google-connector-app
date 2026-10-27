"""Real SQL admission and actual shared executor; only synthetic provider effects."""
import importlib
import asyncio
from types import SimpleNamespace

import asyncpg
from langchain_core.tools import tool
import pytest

from app.agents import supervisor
from app.agents.errors import ExecutionFailure
from app.api.routes import runs
from app.runs import prefix_continuation as recovery, verifier, worker
from app.runs.schemas import ExecutionPlan, PlanStep
from app.runs.tool_admission import execution_authority
from tests.coding.test_tool_receipts import case as receipt_case, database, failed_sheet

case = receipt_case

pytestmark = pytest.mark.asyncio(loop_scope='module')


async def configure(c, monkeypatch, *, approval=False, dependency=False):
    await failed_sheet(c, monkeypatch, composite=True)
    args = {'allowed_tools': ['create_google_sheet', 'write_google_sheet']}
    step = PlanStep(id='information', title='Populate Sheet', service='sheets', operation='create_and_write',
        read_only=False, requires_approval=approval, arguments=args,
        dependencies=['senders'] if dependency else [])
    plan = ExecutionPlan(objective='Create and populate requested Sheet', services=['sheets'], steps=[step])
    await c.pool.execute('UPDATE agent_runs SET plan=$2::jsonb WHERE id=$1', c.identity, plan.model_dump())
    await c.pool.execute('''UPDATE agent_run_steps SET requires_approval=$2,dependencies=$3 WHERE id=$1''',
        c.step_id, approval, step.dependencies)
    return plan


async def authorize(c):
    return await runs._reconcile_and_resume(c.pool, c.identity, 'synthetic-user', c.step_id)


async def claim(c):
    run = await worker.claim_run(c.pool, 'receipt-worker')
    assert run['id'] == c.identity
    async with c.pool.acquire() as conn:
        step = dict(await worker._claim_step(conn, c.identity))
    return dict(run), step


async def test_real_sheet_helper_rejects_changed_prefix_arguments(case, monkeypatch):
    c = case
    await configure(c, monkeypatch)
    assert (await authorize(c)).state == 'continue_verified_prefix'
    run, step = await claim(c)
    created, populated = [], []
    @tool
    async def create_google_sheet(title: str) -> dict:
        """A second creation must never happen."""
        created.append(title)
        pytest.fail('verified creation was repeated')
    @tool
    async def write_google_sheet(spreadsheet_id: str, range: str, values: list) -> dict:
        """Synthetic population, not Google."""
        assert spreadsheet_id == 'synthetic-sheet'
        populated.append(values)
        return {'spreadsheetId': spreadsheet_id, 'updatedRows': len(values), 'updatedRange': range}
    monkeypatch.setattr(worker, 'get_toolsets', lambda: {'sheets': [create_google_sheet, write_google_sheet]})
    # The real helper deterministically derives its title from the run. The
    # original receipt must match it exactly, not merely identify a similar file.
    title = f"Recent Gmail Senders - {str(c.identity)[:8]}"
    dependencies = [{'output_data': {'tool_executions': [{'tool': 'list_recent_gmail_senders',
        'result': {'senders': [{'sender_name': 'Synthetic', 'sender_email': 'synthetic@example.invalid'}]}}]}}]
    # Use a matching source invocation in a separate test below. Here assert that
    # a mismatched title is rejected *before* either provider operation.
    with pytest.raises(recovery.ContinuationDenied) as error:
        async with recovery.continuation_scope(c.pool, run, step):
            await worker._create_recent_senders_sheet(c.pool, run, step, dependencies)
    assert error.value.evidence['reason_code'] == 'prefix_arguments_changed'
    assert created == [] and populated == [] and c.calls == ['provider']
    assert title != 'SYNTHETIC PRIVATE TITLE'


async def fresh_sheet(c, monkeypatch):
    # Construct the original invocation with precisely the actual helper's args.
    args = {'allowed_tools': ['create_google_sheet','write_google_sheet']}
    plan = ExecutionPlan(objective='Create and populate requested Sheet', services=['sheets'], steps=[PlanStep(
        id='information', title='Populate Sheet', service='sheets', operation='create_and_write', read_only=False,
        arguments=args, dependencies=['senders'])])
    await c.pool.execute('UPDATE agent_runs SET plan=$2::jsonb WHERE id=$1', c.identity, plan.model_dump())
    await c.pool.execute("UPDATE agent_run_steps SET dependencies=ARRAY['senders'],service='sheets',"
        "operation='create_and_write',input_data=$2::jsonb WHERE id=$1", c.step_id, args)
    output = {'tool_executions': [{'tool': 'list_recent_gmail_senders', 'result': {'senders': [
        {'sender_name': 'Synthetic', 'sender_email': 'synthetic@example.invalid'}]}}]}
    await c.pool.execute('''INSERT INTO agent_run_steps(run_id,step_key,sequence_no,title,service,operation,status,output_data)
      VALUES($1,'senders',0,'Synthetic senders','gmail','recent_senders','completed',$2::jsonb)''', c.identity, output)
    @tool
    async def create_google_sheet(title: str) -> dict:
        """Initial synthetic provider effect through the real receipt wrapper."""
        c.calls.append('original-create')
        return {'spreadsheetId': 'synthetic-sheet'}
    await supervisor.execute_tool_call(create_google_sheet, {
        'id': 'original-create', 'args': {'title': f'Recent Gmail Senders - {str(c.identity)[:8]}'}}, c.state, c.pool)
    # Represent a stop after that response, before any suffix invocation. Actual
    # process-death receipt retention has its separate fresh-process probe.
    await failed_sheet(c, monkeypatch, composite=True, skip_invocation=True)


async def test_actual_step_completes_with_same_sheet_and_no_duplicate_creation(case, monkeypatch):
    c = case
    await fresh_sheet(c, monkeypatch)
    from app.config.settings import get_settings
    settings = get_settings().model_copy(update={'executor_version': 'test-v1', 'deployment_version': 'test-v1',
        'worker_lease_seconds': 60, 'executor_role': 'control'})
    monkeypatch.setattr(worker, 'get_settings', lambda: settings)
    assert (await authorize(c)).state == 'continue_verified_prefix'
    assert await c.pool.fetchval("SELECT verification_status FROM agent_artifacts WHERE run_id=$1", c.identity) == 'verified'
    run, step = await claim(c)
    effects = []
    @tool
    async def create_google_sheet(title: str) -> dict:
        """Forbidden duplicate creation."""
        pytest.fail('duplicate creation')
    @tool
    async def write_google_sheet(spreadsheet_id: str, range: str, values: list) -> dict:
        """Synthetic mutation counter, no external I/O."""
        effects.append((spreadsheet_id, range, values))
        return {'spreadsheetId': spreadsheet_id, 'updatedRows': len(values), 'updatedRange': range}
    monkeypatch.setattr(worker, 'get_toolsets', lambda: {'sheets': [create_google_sheet, write_google_sheet]})
    class Reply:
        def __init__(self, value):
            self.value = value
        def execute(self):
            return self.value
    monkeypatch.setattr(verifier.google, 'sheets_service', SimpleNamespace(spreadsheets=lambda: SimpleNamespace(
        get=lambda **kw: Reply({'spreadsheetId': 'synthetic-sheet'}),
        values=lambda: SimpleNamespace(get=lambda **kw: Reply({'values': effects[0][2]})))))
    async with c.pool.acquire() as conn:
        dependencies = await worker._dependency_context(conn, step)
    # Drive the public step wrapper, actual typed helper and actual verifier.
    output = await worker._execute_step(SimpleNamespace(), c.pool, run, step, dependencies)
    assert 'synthetic-sheet' in output and len(effects) == 1
    assert effects[0][0] == 'synthetic-sheet'
    assert c.calls == ['original-create']
    assert await c.pool.fetchval('SELECT status FROM agent_run_steps WHERE id=$1', c.step_id) == 'completed'
    assert await c.pool.fetchval('SELECT count(*) FROM agent_tool_receipts WHERE step_id=$1', c.step_id) == 2
    assert await c.pool.fetchval("SELECT count(*) FROM agent_run_events WHERE run_id=$1 "
        "AND event_type='verified_tool_result_reused'", c.identity) == 1
    assert recovery.active_continuation.get() is None
    # Re-entering even with the same owner cannot consume a single-use grant.
    with pytest.raises(ExecutionFailure):
        async with recovery.continuation_scope(c.pool, run, step):
            pytest.fail('used permit was admitted')


@pytest.mark.parametrize('change', ['request', 'plan', 'executor', 'approval', 'erase', 'receipt'])
async def test_changed_source_or_evidence_never_calls_provider(case, monkeypatch, change):
    c = case
    await configure(c, monkeypatch)
    assert (await authorize(c)).state == 'continue_verified_prefix'
    run, step = await claim(c)
    if change == 'request':
        await c.pool.execute("UPDATE agent_runs SET request='Different task' WHERE id=$1", c.identity)
    elif change == 'plan':
        await c.pool.execute("UPDATE agent_runs SET plan='{}' WHERE id=$1", c.identity)
    elif change == 'executor':
        await c.pool.execute("UPDATE agent_runs SET executor_version='new-version' WHERE id=$1", c.identity)
    elif change == 'approval':
        await c.pool.execute('UPDATE agent_run_steps SET requires_approval=TRUE WHERE id=$1', c.step_id)
    elif change == 'erase':
        await c.pool.execute('UPDATE agent_runs SET deleted_at=now() WHERE id=$1', c.identity)
    else:
        # New evidence is a digest mismatch even when its result is well formed.
        from app.runs.tool_receipts import begin_receipt, finish_receipt
        receipt = await begin_receipt(c.pool, c.state, 'write_google_sheet', {'id': 'new-evidence', 'args': {}})
        await finish_receipt(c.pool, receipt, returned=True, result={'spreadsheetId': 'synthetic-sheet'})
    with pytest.raises(ExecutionFailure):
        async with recovery.continuation_scope(c.pool, run, step):
            pytest.fail('changed authority admitted')
    assert c.calls == ['provider']
    assert recovery.active_continuation.get() is None


@pytest.mark.parametrize('mutation', ['expire', 'consumed', 'wrong_tool', 'wrong_lineage', 'cancel'])
async def test_each_tool_rechecks_permit_and_never_repeats_prefix(case, monkeypatch, mutation):
    c = case
    await configure(c, monkeypatch)
    assert (await authorize(c)).state == 'continue_verified_prefix'
    run, step = await claim(c)
    async with recovery.continuation_scope(c.pool, run, step):
        call = {'id': 'reused', 'args': {'title': 'SYNTHETIC PRIVATE TITLE'}}
        if mutation == 'expire':
            # Clock expiry without waiting or weakening the immutable trigger.
            await c.pool.execute('ALTER TABLE tool_prefix_continuations DISABLE TRIGGER protect_tool_prefix_continuation')
            try:
                await c.pool.execute("UPDATE tool_prefix_continuations SET created_at=now()-interval '20 minutes',"
                    "expires_at=now()-interval '5 minutes' WHERE run_id=$1", c.identity)
            finally:
                await c.pool.execute('ALTER TABLE tool_prefix_continuations ENABLE TRIGGER protect_tool_prefix_continuation')
        elif mutation == 'consumed':
            with pytest.raises(asyncpg.RaiseError, match='immutable'):
                await c.pool.execute('UPDATE tool_prefix_continuations SET consumed_at=now() WHERE run_id=$1', c.identity)
            return
        elif mutation == 'cancel':
            await c.pool.execute("UPDATE agent_runs SET status='cancelled' WHERE id=$1", c.identity)
        if mutation == 'wrong_lineage':
            reused, raw = await recovery.before_tool(c.pool, c.state, 'create_google_sheet', call)
            assert reused and raw['spreadsheetId'] == 'synthetic-sheet'
            call = {'id': 'wrong-target', 'args': {'spreadsheet_id': 'another-sheet'}}
        name = 'write_google_sheet' if mutation in {'wrong_tool','wrong_lineage'} else 'create_google_sheet'
        with pytest.raises(ExecutionFailure):
            await recovery.before_tool(c.pool, c.state, name, call)
    assert c.calls == ['provider']


@pytest.mark.parametrize('block', ['approval', 'dependency', 'version', 'unknown', 'suffix_attempted', 'temporal'])
async def test_no_grant_for_unproven_or_unauthorized_work(case, monkeypatch, block):
    c = case
    await configure(c, monkeypatch, approval=block == 'approval', dependency=block == 'dependency')
    if block == 'version':
        monkeypatch.setattr(runs, 'get_settings', lambda: SimpleNamespace(executor_version='new', deployment_version='new'))
    elif block in {'unknown', 'suffix_attempted'}:
        from app.runs.tool_receipts import begin_receipt, finish_receipt
        await c.pool.execute("UPDATE agent_runs SET status='running' WHERE id=$1", c.identity)
        await c.pool.execute("UPDATE agent_run_steps SET status='running' WHERE id=$1", c.step_id)
        receipt = await begin_receipt(c.pool, c.state, 'write_google_sheet', {'id': 'suffix', 'args': {}})
        if block == 'suffix_attempted':
            await finish_receipt(c.pool, receipt, result={'error': 'Synthetic response'}, returned=True)
        await c.pool.execute("UPDATE agent_runs SET status='failed' WHERE id=$1", c.identity)
        await c.pool.execute("UPDATE agent_run_steps SET status='failed' WHERE id=$1", c.step_id)
    elif block == 'temporal':
        # Owner binding itself is immutable. Exercise binder policy directly,
        # without corrupting that stronger independent SQL invariant.
        async with c.pool.acquire() as conn, conn.transaction():
            run = dict(await conn.fetchrow('SELECT * FROM agent_runs WHERE id=$1 FOR UPDATE', c.identity))
            step = dict(await conn.fetchrow('SELECT * FROM agent_run_steps WHERE id=$1 FOR UPDATE', c.step_id))
            receipts = await recovery.receipt_evidence(conn, c.identity, c.step_id, 'synthetic-user')
            from app.runs.reconciliation import ReconciliationDecision
            decision = await recovery.bind_prefix_decision(conn, {**run, 'orchestration_owner': 'temporal'}, step,
                ReconciliationDecision('continue_verified_prefix','verified_ordered_prefix',str(c.step_id)),
                receipts, 'synthetic-user', 'test-v1')
        assert decision.reason_code == 'temporal_prefix_provenance_required'
        return
    decision = await authorize(c)
    assert decision.state == 'manual_required'
    assert await c.pool.fetchval('SELECT count(*) FROM tool_prefix_continuations WHERE run_id=$1', c.identity) == 0


async def test_grant_binding_downgrade_erasure_and_no_private_payload(case, monkeypatch):
    c = case
    await configure(c, monkeypatch)
    assert (await authorize(c)).state == 'continue_verified_prefix'
    row = dict(await c.pool.fetchrow('SELECT * FROM tool_prefix_continuations WHERE run_id=$1', c.identity))
    assert 'PRIVATE' not in str(row) and 'synthetic-sheet' not in str(row)
    with pytest.raises(asyncpg.RaiseError, match='immutable'):
        await c.pool.execute("UPDATE tool_prefix_continuations SET source_hash=repeat('0',64) WHERE run_id=$1", c.identity)
    migration = importlib.import_module('migrations.versions.033_verified_prefix_continuation')
    with pytest.raises(asyncpg.RaiseError, match='retained'):
        await c.pool.execute(migration.DOWNGRADE_SQL)
    await c.pool.execute('DELETE FROM agent_runs WHERE id=$1', c.identity)
    assert await c.pool.fetchval('SELECT count(*) FROM tool_prefix_continuations WHERE run_id=$1', c.identity) == 0


async def test_chat_uses_same_resolved_space_and_sends_exact_content_once(case, monkeypatch):
    c = case
    from app.runs.tool_receipts import begin_receipt, finish_receipt
    destination, text = 'recipient@example.invalid', 'SYNTHETIC PRIVATE MESSAGE'
    receipt = await begin_receipt(c.pool, c.state, 'resolve_chat_destination', {
        'id': 'original-resolve', 'args': {'destination': destination}})
    await finish_receipt(c.pool, receipt, returned=True, result={'name': 'spaces/synthetic', 'readbackVerified': True})
    arguments = {'allowed_tools': ['resolve_chat_destination','send_chat_message'],
        'tool_arguments': {'destination': destination, 'text': text}}
    plan = ExecutionPlan(objective='Send requested Chat', services=['chat'], steps=[PlanStep(id='information',
        title='Send Chat', service='chat', operation='send', read_only=False, arguments=arguments)])
    await c.pool.execute("UPDATE agent_runs SET status='failed',plan=$2::jsonb WHERE id=$1", c.identity, plan.model_dump())
    await c.pool.execute("UPDATE agent_run_steps SET status='failed',service='chat',operation='send',input_data=$2::jsonb "
        "WHERE id=$1", c.step_id, arguments)
    from app.config.settings import get_settings
    settings = get_settings().model_copy(update={'executor_version': 'test-v1', 'deployment_version': 'test-v1',
        'worker_lease_seconds': 60, 'executor_role': 'control'})
    monkeypatch.setattr(worker, 'get_settings', lambda: settings)
    monkeypatch.setattr(runs, 'get_settings', lambda: settings)
    async def credentials(*args):
        return None
    monkeypatch.setattr(runs, 'load_google_credentials', credentials)
    sent = []
    class Reply:
        def __init__(self, value):
            self.value = value
        def execute(self):
            return self.value
    monkeypatch.setattr(verifier.google, 'chat_service', SimpleNamespace(spaces=lambda: SimpleNamespace(
        get=lambda **kw: Reply({'name': 'spaces/synthetic'}), messages=lambda: SimpleNamespace(
            get=lambda **kw: Reply({'name': 'spaces/synthetic/messages/one', 'space': {'name': 'spaces/synthetic'},
                'text': sent[0]})))))
    @tool
    async def resolve_chat_destination(destination: str) -> dict:
        """Must reuse the prior verified resolver result."""
        pytest.fail('resolver was repeated')
    @tool
    async def send_chat_message(space_id: str, text: str) -> dict:
        """Synthetic message counter, never Google."""
        assert space_id == 'spaces/synthetic'
        sent.append(text)
        return {'name': 'spaces/synthetic/messages/one'}
    monkeypatch.setattr(worker, 'get_toolsets', lambda: {'chat': [resolve_chat_destination, send_chat_message]})
    assert (await authorize(c)).state == 'continue_verified_prefix'
    run, step = await claim(c)
    output = await worker._execute_step(SimpleNamespace(), c.pool, run, step, [])
    assert text in output and sent == [text]
    assert await c.pool.fetchval('SELECT status FROM agent_run_steps WHERE id=$1', c.step_id) == 'completed'
    assert await c.pool.fetchval('SELECT count(*) FROM agent_tool_receipts WHERE step_id=$1', c.step_id) == 2


async def test_resume_event_failure_rolls_back_grant_and_queue_atomically(case, monkeypatch):
    c = case
    await configure(c, monkeypatch)
    await c.pool.execute('''CREATE FUNCTION reject_prefix_resume_event() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN IF NEW.event_type='run_reconciled' THEN RAISE EXCEPTION 'synthetic event failure'; END IF;
      RETURN NEW; END $$;
      CREATE TRIGGER reject_prefix_resume_event BEFORE INSERT ON agent_run_events
      FOR EACH ROW EXECUTE FUNCTION reject_prefix_resume_event();''')
    try:
        with pytest.raises(asyncpg.RaiseError, match='event failure'):
            await authorize(c)
        assert await c.pool.fetchval('SELECT count(*) FROM tool_prefix_continuations WHERE run_id=$1', c.identity) == 0
        assert await c.pool.fetchval('SELECT status FROM agent_runs WHERE id=$1', c.identity) == 'failed'
        assert await c.pool.fetchval('SELECT status FROM agent_run_steps WHERE id=$1', c.step_id) == 'failed'
        assert await c.pool.fetchval('SELECT count(*) FROM agent_artifacts WHERE run_id=$1', c.identity) == 0
    finally:
        await c.pool.execute('DROP TRIGGER reject_prefix_resume_event ON agent_run_events; DROP FUNCTION reject_prefix_resume_event()')
    assert (await authorize(c)).state == 'continue_verified_prefix'


async def test_explicit_continuations_are_bounded_and_never_reset_attempts(case, monkeypatch):
    c = case
    await configure(c, monkeypatch)
    for attempt in range(2, 5):
        assert (await authorize(c)).state == 'continue_verified_prefix'
        run, step = await claim(c)
        assert step['attempt_count'] == attempt
        async with recovery.continuation_scope(c.pool, run, step):
            reused, _ = await recovery.before_tool(c.pool, c.state, 'create_google_sheet', {
                'id': f'reuse-{attempt}', 'args': {'title': 'SYNTHETIC PRIVATE TITLE'}})
            assert reused
            with pytest.raises(recovery.ContinuationDenied, match='no new provider'):
                recovery.require_complete_prefix()
        # Simulate stopping before the suffix was invoked; no new effect or
        # receipt. Another authenticated manual resume can continue, not retry it.
        await c.pool.execute("UPDATE agent_runs SET status='failed' WHERE id=$1", c.identity)
        await c.pool.execute("UPDATE agent_run_steps SET status='failed' WHERE id=$1", c.step_id)
    blocked = await authorize(c)
    assert blocked.reason_code == 'prefix_continuation_budget_exhausted'
    assert await c.pool.fetchval('SELECT attempt_count FROM agent_run_steps WHERE id=$1', c.step_id) == 4
    assert c.calls == ['provider']


async def test_parallel_admissions_cannot_reuse_a_prefix_twice(case, monkeypatch):
    c = case
    await configure(c, monkeypatch)
    assert (await authorize(c)).state == 'continue_verified_prefix'
    run, step = await claim(c)
    async with recovery.continuation_scope(c.pool, run, step):
        outcomes = await asyncio.gather(*(recovery.before_tool(c.pool, c.state, 'create_google_sheet', {
            'id': f'concurrent-{index}', 'args': {'title': 'SYNTHETIC PRIVATE TITLE'}}) for index in range(2)),
            return_exceptions=True)
        assert sum(isinstance(value, recovery.ContinuationDenied) for value in outcomes) == 1
        assert sum(isinstance(value, tuple) and value[0] for value in outcomes) == 1
    assert c.calls == ['provider']


async def test_request_changed_during_readback_cannot_receive_a_grant(case, monkeypatch):
    c = case
    await configure(c, monkeypatch)
    original = runs.reconcile_failed_step
    async def changed(*args, **kwargs):
        decision = await original(*args, **kwargs)
        await c.pool.execute("UPDATE agent_runs SET request='Different intent during readback' WHERE id=$1", c.identity)
        return decision
    monkeypatch.setattr(runs, 'reconcile_failed_step', changed)
    decision = await authorize(c)
    assert decision.state == 'manual_required'
    assert decision.reason_code == 'prefix_source_changed_during_readback'
    assert await c.pool.fetchval('SELECT count(*) FROM tool_prefix_continuations WHERE run_id=$1', c.identity) == 0


async def test_approval_must_remain_current_before_prefix_reuse(case, monkeypatch):
    c = case
    plan = await configure(c, monkeypatch, approval=True)
    from app.runs.planner import action_hash
    await c.pool.execute('''INSERT INTO run_approvals(run_id,requested_from,action_hash,action_summary,status,expires_at,decided_at)
      VALUES($1,'synthetic-user',$2,'{}'::jsonb,'approved',now()+interval '1 hour',now())''',
      c.identity, action_hash(plan))
    assert (await authorize(c)).state == 'continue_verified_prefix'
    run, step = await claim(c)
    async with recovery.continuation_scope(c.pool, run, step):
        await c.pool.execute("UPDATE run_approvals SET expires_at=now()-interval '1 second' WHERE run_id=$1", c.identity)
        with pytest.raises(recovery.ContinuationDenied) as error:
            await recovery.before_tool(c.pool, c.state, 'create_google_sheet', {
                'id': 'reused', 'args': {'title': 'SYNTHETIC PRIVATE TITLE'}})
        assert error.value.evidence['reason_code'] == 'continuation_authority_changed'
    assert c.calls == ['provider']


async def test_artifact_presence_is_not_verification_or_safe_delete_authority(case):
    c = case
    run = dict(await c.pool.fetchrow('SELECT * FROM agent_runs WHERE id=$1', c.identity))
    step = dict(await c.pool.fetchrow('SELECT * FROM agent_run_steps WHERE id=$1', c.step_id))
    artifacts = [{'external_id': 'synthetic-resource', 'safe_to_delete': True}]
    async with c.pool.acquire() as conn:
        await worker._store_artifacts(conn, run, step, artifacts)
    row = await c.pool.fetchrow('SELECT * FROM agent_artifacts WHERE run_id=$1', c.identity)
    assert row['verification_status'] == 'unverified' and row['verified_at'] is None and not row['safe_to_delete']
    async with c.pool.acquire() as conn:
        await worker._store_artifacts(conn, run, step, artifacts, verified=True)
    row = await c.pool.fetchrow('SELECT * FROM agent_artifacts WHERE run_id=$1', c.identity)
    assert row['verification_status'] == 'verified' and row['verified_at'] and row['safe_to_delete']
    async with c.pool.acquire() as conn:
        await worker._store_artifacts(conn, run, step, artifacts, verified=False)
    row = await c.pool.fetchrow('SELECT * FROM agent_artifacts WHERE run_id=$1', c.identity)
    assert row['verification_status'] == 'unverified' and row['verified_at'] is None and not row['safe_to_delete']
