"""Actual SQL receipts and hosted execution; all GitHub/process effects synthetic."""
import base64
import hashlib
import json

import asyncpg
import httpx
import pytest

from app.coding import publication_receipts as receipts, repository, worker
from app.coding.execution_fence import CodingExecutionFenced
from tests.coding.test_coding_execution_fence import case as shared_case, database  # noqa: F401 -- shared fixture

case = shared_case
pytestmark = pytest.mark.asyncio(loop_scope='module')


async def executing(c):
    plan = {'actions': [{'tool': 'create_file', 'path': 'synthetic.py', 'content': 'print(1)\n'}]}
    data = json.dumps(plan).encode()
    digest = hashlib.sha256(data).hexdigest()
    await c.pool.execute('''UPDATE coding_runs SET status='executing',current_phase='executing',base_commit=$1,
      encrypted_plan=$2,plan_hash=$3,approval_action_hash=$3,approval_status='approved',approved_by=user_id,
      approval_expires_at=now()+interval '1 hour' WHERE id=$4''', 'a'*40, repository.encrypt_json(plan), digest, c.identity)
    # repository.encrypt_json has a different JSON spacing from data; hash actual frozen bytes.
    from app.db.oauth_credentials import decrypt_private_payload
    frozen = await c.pool.fetchval('SELECT encrypted_plan FROM coding_runs WHERE id=$1', c.identity)
    digest = hashlib.sha256(decrypt_private_payload(frozen)).hexdigest()
    await c.pool.execute('UPDATE coding_runs SET plan_hash=$1,approval_action_hash=$1 WHERE id=$2', digest, c.identity)
    c.row = dict(await c.pool.fetchrow('SELECT * FROM coding_runs WHERE id=$1', c.identity))
    return {'version': 1, 'files': {'synthetic.py': base64.b64encode(b'print(1)\n').decode()},
        'changed_paths': ['synthetic.py'], 'linked_build_id': None, 'validation': 'passed'}


def publication(c):
    return {'branch': f"coding/{c.identity}-{c.row['plan_hash'][:16]}", 'commit': 'b'*40,
        'number': 42, 'url': 'https://github.com/synthetic/repository/pull/42'}


async def prepare_dispatch(c):
    manifest = await executing(c)
    await receipts.prepare(c.pool, c.row, manifest)
    await receipts.dispatch(c.pool, c.row)
    return manifest


async def test_receipt_has_encrypted_frozen_files_and_outcome_not_business_success(case):
    c = case
    manifest = await prepare_dispatch(c)
    await receipts.returned(c.pool, c.row, publication(c))
    state, saved, result = await receipts.load(c.pool, c.row)
    assert (state, saved, result) == ('returned', manifest, publication(c))
    assert await c.pool.fetchval('SELECT status FROM coding_runs WHERE id=$1', c.identity) == 'executing'
    assert await c.pool.fetchval('SELECT count(*) FROM coding_artifacts WHERE run_id=$1', c.identity) == 0
    encrypted = await c.pool.fetchval('SELECT encrypted_manifest FROM coding_publication_receipts WHERE run_id=$1', c.identity)
    assert 'synthetic.py' not in encrypted and 'print(1)' not in encrypted


@pytest.mark.parametrize('change', ['cancel', 'replacement'])
async def test_late_ack_is_preserved_but_cannot_complete_cancelled_or_replaced_run(case, change):
    c = case
    await prepare_dispatch(c)
    if change == 'cancel':
        await repository.cancel_coding_run(c.pool, run_id=str(c.identity), user_id=c.row['user_id'], reason='synthetic')
    else:
        await c.pool.execute("UPDATE coding_runs SET lease_owner='new-owner' WHERE id=$1", c.identity)
    await receipts.returned(c.pool, c.row, publication(c))
    assert await c.pool.fetchval('SELECT state FROM coding_publication_receipts WHERE run_id=$1', c.identity) == 'returned'
    with pytest.raises(CodingExecutionFenced):
        await receipts.load(c.pool, c.row)


@pytest.mark.parametrize('change', ['deleted', 'erased', 'source', 'tenant'])
async def test_late_ack_cannot_resurrect_erased_or_rebound_source(case, change):
    c = case
    await prepare_dispatch(c)
    if change == 'deleted':
        await c.pool.execute('UPDATE coding_runs SET deleted_at=now() WHERE id=$1', c.identity)
    elif change == 'erased':
        await c.pool.execute('DELETE FROM coding_runs WHERE id=$1', c.identity)
    elif change == 'source':
        await c.pool.execute("UPDATE coding_runs SET request_hash=repeat('c',64) WHERE id=$1", c.identity)
    else:
        c.row = {**c.row, 'user_id': 'other-user'}
    with pytest.raises(receipts.PublicationReconciliationRequired):
        await receipts.returned(c.pool, c.row, publication(c))


async def test_receipt_binding_outcome_and_dispatch_are_immutable(case):
    c = case
    await prepare_dispatch(c)
    with pytest.raises(asyncpg.RaiseError):
        await c.pool.execute("UPDATE coding_publication_receipts SET source_hash=repeat('f',64) WHERE run_id=$1", c.identity)
    with pytest.raises(receipts.PublicationReconciliationRequired):
        await receipts.dispatch(c.pool, c.row)
    await receipts.returned(c.pool, c.row, publication(c))
    with pytest.raises(asyncpg.RaiseError):
        await c.pool.execute("UPDATE coding_publication_receipts SET result_hash=repeat('e',64) WHERE run_id=$1", c.identity)
    with pytest.raises(asyncpg.RaiseError):
        await c.pool.execute('UPDATE coding_publication_receipts SET encrypted_manifest=NULL WHERE run_id=$1', c.identity)


async def test_soft_erasure_clears_private_receipt_payloads(case, monkeypatch):
    from app.runs import retention
    from types import SimpleNamespace
    c = case
    await prepare_dispatch(c)
    await receipts.returned(c.pool, c.row, publication(c))
    await c.pool.execute('UPDATE coding_runs SET deleted_at=now() WHERE id=$1', c.identity)
    monkeypatch.setattr(retention, 'get_settings', lambda: SimpleNamespace(raw_telemetry_retention_days=14, workflow_retention_days=90))
    report = await retention.apply_retention(c.pool)
    assert report['expired_coding_publication_payloads'] == 1
    row = await c.pool.fetchrow('SELECT * FROM coding_publication_receipts WHERE run_id=$1', c.identity)
    assert row['encrypted_manifest'] is None and row['encrypted_result'] is None and row['state'] == 'returned'


async def test_recovered_lease_uses_frozen_outcome_without_cli_or_republication(case, monkeypatch):
    c = case
    manifest = await prepare_dispatch(c)
    await receipts.returned(c.pool, c.row, publication(c))
    await c.pool.execute("UPDATE coding_runs SET lease_owner='new-live-owner',attempt_count=attempt_count+1 WHERE id=$1", c.identity)
    c.row = dict(await c.pool.fetchrow('SELECT * FROM coding_runs WHERE id=$1', c.identity))
    calls = []
    async def verify(row, saved, *, guard, publication=None):
        await guard()
        calls.append('read-back')
        assert saved == manifest
        return publication
    monkeypatch.setattr(worker, '_verify_publication', verify)
    await worker._execute(c.pool, c.row)
    assert calls == ['read-back'] and c.calls == []
    assert await c.pool.fetchval('SELECT status FROM coding_runs WHERE id=$1', c.identity) == 'published'
    assert await c.pool.fetchval("SELECT count(*) FROM coding_artifacts WHERE run_id=$1 AND verification_status='verified'", c.identity) == 1


async def test_failed_business_commit_recovers_without_repeating_publication(case, monkeypatch):
    c = case
    await prepare_dispatch(c)
    await receipts.returned(c.pool, c.row, publication(c))
    async def verify(row, saved, *, guard, publication=None):
        await guard()
        return publication
    monkeypatch.setattr(worker, '_verify_publication', verify)
    await c.pool.execute('''CREATE FUNCTION reject_publication_event() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN IF NEW.event_type='coding_draft_published' THEN RAISE EXCEPTION 'synthetic SQL failure'; END IF;
      RETURN NEW; END $$;
      CREATE TRIGGER reject_publication_event BEFORE INSERT ON coding_run_events
      FOR EACH ROW EXECUTE FUNCTION reject_publication_event();''')
    try:
        with pytest.raises(asyncpg.RaiseError):
            await worker._execute(c.pool, c.row)
        assert await c.pool.fetchval('SELECT status FROM coding_runs WHERE id=$1', c.identity) == 'executing'
        assert await c.pool.fetchval('SELECT state FROM coding_publication_receipts WHERE run_id=$1', c.identity) == 'returned'
    finally:
        await c.pool.execute('DROP TRIGGER reject_publication_event ON coding_run_events; DROP FUNCTION reject_publication_event()')
    await worker._execute(c.pool, c.row)
    assert c.calls == []


async def test_process_records_bookkeeping_failure_as_reconciliation_not_failed(case, monkeypatch):
    c = case
    await prepare_dispatch(c)
    async def execute(*args):
        raise RuntimeError('Synthetic local SQL failure after external effect')
    async def claim(*args):
        return c.row
    monkeypatch.setattr(worker, '_execute', execute)
    monkeypatch.setattr(worker, '_claim', claim)
    assert await worker.process_one_coding_run(c.pool)
    assert await c.pool.fetchval('SELECT status FROM coding_runs WHERE id=$1', c.identity) == 'reconciling'
    assert await c.pool.fetchval("SELECT count(*) FROM coding_run_events WHERE run_id=$1 AND event_type='coding_failed'", c.identity) == 0


async def test_fresh_execution_freezes_validated_files_before_first_publication(case, monkeypatch):
    c = case
    expected = await executing(c)
    calls = []
    async def local(arguments, *, guard):
        from pathlib import Path
        await guard()
        workspace = Path(arguments[arguments.index('--workspace')+1])
        (workspace/'synthetic.py').write_bytes(b'print(1)\n')
        calls.append('keyless-execute')
        return {'ok': True, 'changes': [{'path': 'synthetic.py', 'changed': True}]}
    async def publish(*args, **kwargs):
        assert await c.pool.fetchval('SELECT state FROM coding_publication_receipts WHERE run_id=$1', c.identity) == 'dispatched'
        stored = await c.pool.fetchrow('SELECT * FROM coding_publication_receipts WHERE run_id=$1', c.identity)
        assert receipts.decode(stored['encrypted_manifest'], stored['manifest_hash']) == expected
        calls.append('publish-once')
        return publication(c)
    async def verify(row, saved, *, guard, publication=None):
        await guard()
        calls.append('read-back')
        return publication
    monkeypatch.setattr(worker, '_run_local', local)
    monkeypatch.setattr(worker, '_publish_draft', publish)
    monkeypatch.setattr(worker, '_verify_publication', verify)
    await worker._execute(c.pool, c.row)
    assert calls == ['keyless-execute', 'publish-once', 'read-back']
    assert c.calls == ['snapshot']
    assert await c.pool.fetchval('SELECT status FROM coding_runs WHERE id=$1', c.identity) == 'published'


async def test_cancellation_after_remote_accept_retains_ack_without_local_success(case, monkeypatch):
    c = case
    manifest = await executing(c)
    await receipts.prepare(c.pool, c.row, manifest)
    async def publish(*args, **kwargs):
        await repository.cancel_coding_run(c.pool, run_id=str(c.identity), user_id=c.row['user_id'], reason='synthetic')
        return publication(c)
    monkeypatch.setattr(worker, '_publish_draft', publish)
    with pytest.raises(CodingExecutionFenced):
        await worker._execute(c.pool, c.row)
    assert await c.pool.fetchval('SELECT state FROM coding_publication_receipts WHERE run_id=$1', c.identity) == 'returned'
    assert await c.pool.fetchval('SELECT status FROM coding_runs WHERE id=$1', c.identity) == 'cancelled'
    assert await c.pool.fetchval('SELECT count(*) FROM coding_artifacts WHERE run_id=$1', c.identity) == 0


async def test_pre_dispatch_sql_failure_can_retry_same_frozen_intent_without_cli(case, monkeypatch):
    c = case
    manifest = await executing(c)
    await receipts.prepare(c.pool, c.row, manifest)
    async def fail_dispatch(*args):
        raise RuntimeError('synthetic failed dispatch transaction')
    monkeypatch.setattr(receipts, 'dispatch', fail_dispatch)
    async def claim(*args):
        return c.row
    monkeypatch.setattr(worker, '_claim', claim)
    assert await worker.process_one_coding_run(c.pool)
    row = await c.pool.fetchrow('SELECT status,error_category FROM coding_runs WHERE id=$1', c.identity)
    assert row['status'] == 'approved' and row['error_category'] == 'publication_pre_dispatch'
    assert await c.pool.fetchval('SELECT state FROM coding_publication_receipts WHERE run_id=$1', c.identity) == 'prepared'
    assert c.calls == []


async def test_uncertain_dispatch_never_repeats_writes_and_enters_reconciliation(case, monkeypatch):
    c = case
    await prepare_dispatch(c)
    async def verify(*args, **kwargs):
        raise receipts.PublicationReconciliationRequired()
    monkeypatch.setattr(worker, '_verify_publication', verify)
    async def claim(*args):
        return c.row
    monkeypatch.setattr(worker, '_claim', claim)
    assert await worker.process_one_coding_run(c.pool)
    assert c.calls == []
    assert await c.pool.fetchval('SELECT status FROM coding_runs WHERE id=$1', c.identity) == 'reconciling'
    assert await c.pool.fetchval('SELECT count(*) FROM coding_artifacts WHERE run_id=$1', c.identity) == 0


async def test_owner_reconciliation_requeues_readback_without_renewing_approval(case, monkeypatch):
    c = case
    await prepare_dispatch(c)
    await c.pool.execute("UPDATE coding_runs SET status='reconciling',lease_owner=NULL,lease_expires_at=NULL WHERE id=$1", c.identity)
    before = await c.pool.fetchval('SELECT approval_expires_at FROM coding_runs WHERE id=$1', c.identity)
    result = await repository.request_publication_reconciliation(c.pool, run_id=str(c.identity),
        user_id=c.row['user_id'], action_hash=c.row['plan_hash'])
    assert result['status'] == 'approved' and 'encrypted_plan' not in result
    assert await c.pool.fetchval('SELECT approval_expires_at FROM coding_runs WHERE id=$1', c.identity) == before
    c.row = dict(await worker._claim(c.pool))
    assert c.row['status'] == 'executing'
    expected = publication(c)
    async def verify(row, manifest, *, guard, publication=None):
        await guard()
        return expected
    monkeypatch.setattr(worker, '_verify_publication', verify)
    await worker._execute(c.pool, c.row)
    assert c.calls == []
    assert await c.pool.fetchval('SELECT status FROM coding_runs WHERE id=$1', c.identity) == 'published'


@pytest.mark.parametrize('problem', ['tenant', 'hash', 'cancelled', 'approval', 'expired', 'source', 'prepared', 'active-lease'])
async def test_reconciliation_control_plane_cannot_grant_new_authority(case, problem):
    c = case
    manifest = await executing(c)
    await receipts.prepare(c.pool, c.row, manifest)
    if problem != 'prepared':
        await receipts.dispatch(c.pool, c.row)
    await c.pool.execute("UPDATE coding_runs SET status='reconciling',lease_owner=NULL,lease_expires_at=NULL WHERE id=$1", c.identity)
    user, digest = c.row['user_id'], c.row['plan_hash']
    if problem == 'tenant':
        user = 'other-user'
    elif problem == 'hash':
        digest = 'f'*64
    elif problem == 'cancelled':
        await c.pool.execute("UPDATE coding_runs SET status='cancelled' WHERE id=$1", c.identity)
    elif problem == 'approval':
        await c.pool.execute("UPDATE coding_runs SET approval_status='rejected' WHERE id=$1", c.identity)
    elif problem == 'expired':
        await c.pool.execute("UPDATE coding_runs SET approval_expires_at=now()-interval '1 second' WHERE id=$1", c.identity)
    elif problem == 'source':
        await c.pool.execute("UPDATE coding_runs SET base_commit=repeat('c',40) WHERE id=$1", c.identity)
    elif problem == 'active-lease':
        await c.pool.execute("UPDATE coding_runs SET lease_owner='another-owner',lease_expires_at=now()+interval '1 hour' WHERE id=$1", c.identity)
    with pytest.raises(repository.CodingRunConflict):
        await repository.request_publication_reconciliation(c.pool, run_id=str(c.identity), user_id=user, action_hash=digest)


async def test_missing_ack_recovers_using_only_exact_github_readback(case, monkeypatch):
    c = case
    manifest = await prepare_dispatch(c)
    expected = publication(c)
    async def verify(row, saved, *, guard, publication=None):
        await guard()
        assert publication is None and saved == manifest
        return expected
    monkeypatch.setattr(worker, '_verify_publication', verify)
    await worker._execute(c.pool, c.row)
    assert c.calls == []
    stored = await c.pool.fetchrow('SELECT state,encrypted_result,result_hash FROM coding_publication_receipts WHERE run_id=$1', c.identity)
    assert stored['state'] == 'returned' and receipts.decode(stored['encrypted_result'], stored['result_hash']) == expected


@pytest.mark.parametrize('problem,code', [(None, None), ('disabled', 503), ('not-admin', 403),
    ('foreign-tenant', 404), ('repository', 403), ('approval', 409)])
async def test_authenticated_route_preserves_feature_tenant_repo_and_approval_gates(case, monkeypatch, problem, code):
    from fastapi import HTTPException
    from starlette.requests import Request
    from types import SimpleNamespace
    from app.api.routes import coding
    from app.coding.schemas import CodingPublicationReconcile
    c = case
    await prepare_dispatch(c)
    await c.pool.execute("UPDATE coding_runs SET status='reconciling',lease_owner=NULL,lease_expires_at=NULL WHERE id=$1", c.identity)
    settings = SimpleNamespace(coding_agent_enabled=problem != 'disabled', coding_agent_admin_only=True,
        coding_allowed_repositories='other/repository' if problem == 'repository' else c.row['repository'])
    monkeypatch.setattr(coding, 'get_settings', lambda: settings)
    async def pool():
        return c.pool
    monkeypatch.setattr(coding, 'get_pool', pool)
    request = Request({'type': 'http'})
    request.state.user_id = 'other-user' if problem == 'foreign-tenant' else c.row['user_id']
    request.state.is_admin = problem != 'not-admin'
    body = CodingPublicationReconcile(action_hash='f'*64 if problem == 'approval' else c.row['plan_hash'])
    if code is not None:
        with pytest.raises(HTTPException) as exc:
            await coding.reconcile_publication(c.identity, body, request)
        assert exc.value.status_code == code
        assert await c.pool.fetchval('SELECT status FROM coding_runs WHERE id=$1', c.identity) == 'reconciling'
    else:
        result = await coding.reconcile_publication(c.identity, body, request)
        assert result['run']['status'] == 'approved' and 'encrypted_plan' not in result['run']


@pytest.mark.parametrize('problem', [None, 'missing', 'extra-file', 'wrong-content', 'wrong-base', 'not-draft', 'wrong-repo', 'truncated'])
async def test_real_readback_requires_exact_tree_and_draft_identity(case, monkeypatch, problem):
    c = case
    manifest = await executing(c)
    expected = publication(c)
    content = b'print(1)\n'
    blob = hashlib.sha1(b'blob ' + str(len(content)).encode() + b'\0' + content, usedforsecurity=False).hexdigest()
    requested = []
    async def respond(request):
        requested.append(request.method)
        assert request.method == 'GET'
        path = request.url.path
        if '/git/ref/' in path:
            return httpx.Response(404 if problem == 'missing' else 200, json={'object': {'sha': expected['commit']}})
        if '/git/commits/' in path:
            is_base = path.endswith('a'*40)
            return httpx.Response(200, json={'tree': {'sha': 'old' if is_base else 'new'},
                'parents': [{'sha': 'c'*40 if problem == 'wrong-base' else 'a'*40}]})
        if '/git/trees/' in path:
            tree = [] if path.endswith('/old') else [{'path': 'synthetic.py', 'mode': '100644', 'type': 'blob',
                'sha': 'f'*40 if problem == 'wrong-content' else blob}]
            if problem == 'extra-file' and path.endswith('/new'):
                tree.append({'path': 'unapproved.py', 'mode': '100644', 'type': 'blob', 'sha': 'c'*40})
            return httpx.Response(200, json={'truncated': problem == 'truncated', 'tree': tree})
        if path.endswith('/pulls'):
            return httpx.Response(200, json=[{'number': 42}])
        return httpx.Response(200, json={'number': 42, 'html_url': expected['url'], 'state': 'open', 'draft': problem != 'not-draft',
            'head': {'sha': expected['commit'], 'ref': expected['branch'], 'repo': {'full_name': c.row['repository']}},
            'base': {'ref': 'main', 'repo': {'full_name': 'other/repository' if problem == 'wrong-repo' else c.row['repository']}}})
    client = httpx.AsyncClient
    monkeypatch.setattr(worker.httpx, 'AsyncClient', lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs))
    async def headers(*args):
        return {}
    monkeypatch.setattr(worker, 'github_api_headers', headers)
    async def guard():
        await worker.require_coding_authority(c.pool, c.row)
    if problem:
        with pytest.raises(receipts.PublicationReconciliationRequired):
            await worker._verify_publication(c.row, manifest, guard=guard, publication=expected)
    else:
        assert await worker._verify_publication(c.row, manifest, guard=guard, publication=expected) == expected
    assert set(requested) == {'GET'}
