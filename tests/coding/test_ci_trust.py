"""Real policy SQL and worker fences; synthetic bounded GitHub Actions reads."""
import base64
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

import asyncpg
import httpx
import pytest

from app.coding import ci_trust, publication_receipts, worker
from app.coding.execution_fence import CodingExecutionFenced
from tests.coding.test_coding_execution_fence import case as shared_case, database  # noqa: F401
from tests.coding.test_coding_publication_receipts import executing, publication

case = shared_case
pytestmark = pytest.mark.asyncio(loop_scope='module')
WORKFLOW = b'name: Reviewed no-secret validation\n'
COMMIT = 'b'*40
BASE = 'a'*40
TREE = 'c'*40
REPOSITORY = 'synthetic/repository'
BRANCH = 'coding/synthetic'


def policy():
    return {'id': uuid4(), 'repository': REPOSITORY, 'workflow_id': 123,
        'workflow_path': '.github/workflows/candidate-validation.yml', 'trusted_commit': BASE,
        'workflow_sha256': hashlib.sha256(WORKFLOW).hexdigest(), 'required_jobs': {'backend': ['Validate no-secret candidate']}}


async def register(pool, value):
    await pool.execute('''INSERT INTO coding_ci_policies(id,repository,workflow_id,workflow_path,trusted_commit,
      workflow_sha256,required_jobs,approved_by,expires_at) VALUES($1,$2,$3,$4,$5,$6,$7::jsonb,'synthetic-human',now()+interval '1 hour')''',
      value['id'], value['repository'], value['workflow_id'], value['workflow_path'], value['trusted_commit'],
      value['workflow_sha256'], json.dumps(value['required_jobs']))


def github(monkeypatch, value, *, problem=None, callback=None):
    run = {'id': 456, 'run_attempt': 2, 'workflow_id': 123, 'path': value['workflow_path'], 'head_sha': COMMIT,
        'head_branch': BRANCH, 'event': 'pull_request', 'repository': {'full_name': REPOSITORY},
        'head_repository': {'full_name': REPOSITORY}, 'pull_requests': [{'number': 42}], 'status': 'completed',
        'conclusion': 'success', 'updated_at': '2026-10-28T00:00:00Z'}
    job = {'id': 789, 'run_id': 456, 'head_sha': COMMIT, 'name': 'backend', 'status': 'completed', 'conclusion': 'success',
        'steps': [{'name': 'Validate no-secret candidate', 'status': 'completed', 'conclusion': 'success'}]}
    if problem in {'skipped', 'neutral', 'failure', 'cancelled'}:
        job['conclusion'] = problem
    if problem == 'step-skipped':
        job['steps'][0]['conclusion'] = 'skipped'
    if problem == 'missing-step':
        job['steps'] = []
    if problem == 'duplicate-step':
        job['steps'].append(deepcopy(job['steps'][0]))
    if problem == 'wrong-job-head':
        job['head_sha'] = 'f'*40
    if problem == 'wrong-job-run':
        job['run_id'] = 999
    if problem == 'wrong-workflow':
        run['workflow_id'] = 999
    if problem == 'wrong-event':
        run['event'] = 'workflow_dispatch'
    if problem == 'fork':
        run['head_repository']['full_name'] = 'other/repository'
    if problem == 'pending':
        run['status'], run['conclusion'] = 'in_progress', None
    calls, counts = [], {}
    async def respond(request):
        calls.append((request.method, request.url.path))
        assert request.method == 'GET'  # No workflow start, rerun, mutation or deployment.
        path = request.url.path
        counts[path] = counts.get(path, 0)+1
        if callback:
            await callback(request)
        if path.endswith('/workflows/123/runs'):
            listed = [deepcopy(run)]
            if problem == 'newer-run' or (problem == 'appearing-run' and counts[path] > 1):
                listed.append({**deepcopy(run), 'id': 999})
            if problem == 'no-runs':
                listed = []
            return httpx.Response(200, json={'workflow_runs': listed,
                'total_count': 101 if problem == 'truncated-runs' else len(listed)})
        if path.endswith('/runs/456') or path.endswith('/runs/999'):
            item = deepcopy(run)
            if problem == 'rerun-race' and counts[path] > 1:
                item['run_attempt'] += 1
            return httpx.Response(200, json=item)
        if path.endswith('/pulls/42'):
            return httpx.Response(200, json={'state': 'open',
                'head': {'sha': 'f'*40 if problem == 'pr-moved' else COMMIT, 'ref': BRANCH, 'repo': {'full_name': REPOSITORY}},
                'base': {'sha': BASE, 'ref': 'main', 'repo': {'full_name': REPOSITORY}}})
        if '/contents/' in path:
            content = WORKFLOW if path.endswith(value['workflow_path']) else b'print(1)\n'
            if problem == 'workflow-changed' and path.endswith(value['workflow_path']):
                content = b'unreviewed workflow'
            return httpx.Response(200, json={'type': 'file', 'encoding': 'base64', 'content': base64.b64encode(content).decode()})
        if '/attempts/2/jobs' in path:
            jobs = [] if problem == 'missing-job' else [deepcopy(job)]
            if problem == 'duplicate-job':
                jobs.append(deepcopy(job))
            return httpx.Response(200, json={'jobs': jobs, 'total_count': 101 if problem == 'truncated-jobs' else len(jobs)})
        if '/git/commits/' in path:
            return httpx.Response(200, json={'sha': COMMIT, 'tree': {'sha': TREE}})
        if path.endswith('/workflows/123'):
            return httpx.Response(200, json={'id': 123, 'path': value['workflow_path'], 'state': 'active'})
        raise AssertionError('Unexpected API path')
    cls = httpx.AsyncClient
    monkeypatch.setattr(ci_trust.httpx, 'AsyncClient', lambda **kwargs: cls(transport=httpx.MockTransport(respond), **kwargs))
    async def headers(*args):
        return {}  # Never read a real App key or make an authentication/network request.
    monkeypatch.setattr(ci_trust, 'github_api_headers', headers)
    return calls


async def verify(value, **kwargs):
    return await ci_trust.verify_run(value, commit=COMMIT, pull_number=42, base_ref='main', branch=BRANCH,
        file_hashes={'synthetic.py': hashlib.sha256(b'print(1)\n').hexdigest()}, **kwargs)


async def test_exact_attempt_required_jobs_steps_workflow_and_files_pass(monkeypatch):
    value = policy()
    calls = github(monkeypatch, value)
    result = await verify(value, run_id='456')
    assert result['status'] == 'passed' and result['run_attempt'] == 2 and result['tree'] == TREE
    assert result['jobs']['backend']['required_steps'] == {'Validate no-secret candidate': 'success'}
    assert any('/attempts/2/jobs' in path for _, path in calls)


@pytest.mark.parametrize('problem', ['skipped', 'neutral', 'failure', 'cancelled', 'step-skipped', 'missing-step'])
async def test_non_success_job_or_step_is_failed_not_verified(monkeypatch, problem):
    value = policy()
    github(monkeypatch, value, problem=problem)
    assert (await verify(value))['status'] == 'failed'


@pytest.mark.parametrize('problem', ['wrong-workflow', 'wrong-event', 'fork', 'pr-moved', 'workflow-changed',
    'wrong-job-head', 'wrong-job-run', 'missing-job', 'duplicate-job', 'duplicate-step', 'truncated-runs',
    'truncated-jobs', 'appearing-run', 'rerun-race', 'newer-run'])
async def test_untrusted_incomplete_stale_or_racing_evidence_is_rejected(monkeypatch, problem):
    value = policy()
    github(monkeypatch, value, problem=problem)
    with pytest.raises(ci_trust.CITrustError):
        await verify(value, run_id='456')


@pytest.mark.parametrize('problem', ['no-runs', 'pending'])
async def test_no_run_or_current_pending_does_not_complete(monkeypatch, problem):
    value = policy()
    github(monkeypatch, value, problem=problem)
    assert (await verify(value))['status'] == 'pending'


async def test_exact_frozen_file_hash_is_required(monkeypatch):
    value = policy()
    github(monkeypatch, value)
    with pytest.raises(ci_trust.CITrustError, match='frozen candidate file hashes'):
        await ci_trust.verify_run(value, commit=COMMIT, pull_number=42, base_ref='main', branch=BRANCH,
            file_hashes={'synthetic.py': 'f'*64})


async def test_human_policy_is_immutable_not_inferred_and_rechecked(case):
    c = case
    value = policy()
    value['repository'] = 'synthetic/' + uuid4().hex
    async with c.pool.acquire() as conn:
        with pytest.raises(ci_trust.CITrustError):
            await ci_trust.latest_policy(conn, value['repository'])
    await register(c.pool, value)
    with pytest.raises(asyncpg.RaiseError):
        await c.pool.execute('UPDATE coding_ci_policies SET workflow_id=999 WHERE id=$1', value['id'])
    replacement = {**value, 'id': uuid4()}
    await register(c.pool, replacement)
    async with c.pool.acquire() as conn, conn.transaction():
        with pytest.raises(ci_trust.CITrustError, match='unchanged trust policy'):
            await ci_trust.require_same_policy(conn, value)


async def ci_case(c):
    manifest = await executing(c)
    await publication_receipts.prepare(c.pool, c.row, manifest)
    await publication_receipts.dispatch(c.pool, c.row)
    result = publication(c)
    await publication_receipts.returned(c.pool, c.row, result)
    await c.pool.execute('''INSERT INTO coding_artifacts(run_id,user_id,artifact_type,content_hash,metadata)
      VALUES($1,$2,'pull_request',$3,'{}')''', c.identity, c.row['user_id'], 'f'*64)
    await c.pool.execute('''UPDATE coding_runs SET status='ci_running',candidate_commit=$1,
      branch_name=$2,pull_request_number=42,pull_request_url=$3 WHERE id=$4''',
      COMMIT, BRANCH, result['url'], c.identity)
    c.row = dict(await c.pool.fetchrow('SELECT * FROM coding_runs WHERE id=$1', c.identity))
    settings = worker.get_settings()
    settings.coding_ci_timeout_minutes = 30
    value = policy()
    await register(c.pool, value)
    return value


@pytest.mark.parametrize('result', ['passed', 'pending', 'failed'])
async def test_actual_ci_worker_commits_only_required_observed_success(case, monkeypatch, result):
    c = case
    value = await ci_case(c)
    async def observed(*args, **kwargs):
        await kwargs['guard']()
        return {'status': result, 'commit': COMMIT, 'policy_id': str(value['id']), 'jobs': {'backend': {}},
            'url': 'https://github.com/synthetic/repository/actions/runs/456'}
    monkeypatch.setattr(worker, 'verify_run', observed)
    if result == 'failed':
        with pytest.raises(worker.CodingWorkerError):
            await worker._monitor_ci(c.pool, c.row)
    else:
        await worker._monitor_ci(c.pool, c.row)
        status = await c.pool.fetchval('SELECT status FROM coding_runs WHERE id=$1', c.identity)
        assert status == ('completed' if result == 'passed' else 'ci_running')
    artifacts = await c.pool.fetchval("SELECT count(*) FROM coding_artifacts WHERE run_id=$1 AND artifact_type='ci_attestation'", c.identity)
    assert artifacts == (1 if result == 'passed' else 0)


@pytest.mark.parametrize('change', ['cancel', 'head', 'policy'])
async def test_stale_ci_success_cannot_overwrite_cancellation_new_head_or_policy(case, monkeypatch, change):
    c = case
    value = await ci_case(c)
    async def observed(*args, **kwargs):
        if change == 'cancel':
            await c.pool.execute("UPDATE coding_runs SET status='cancelled',lease_owner=NULL WHERE id=$1", c.identity)
        elif change == 'head':
            await c.pool.execute("UPDATE coding_runs SET candidate_commit=repeat('f',40) WHERE id=$1", c.identity)
        else:
            await register(c.pool, {**value, 'id': uuid4()})
        return {'status': 'passed', 'commit': COMMIT, 'policy_id': str(value['id']), 'jobs': {'backend': {}}, 'url': 'synthetic'}
    monkeypatch.setattr(worker, 'verify_run', observed)
    with pytest.raises(ci_trust.CITrustError if change == 'policy' else CodingExecutionFenced):
        await worker._monitor_ci(c.pool, c.row)
    assert await c.pool.fetchval("SELECT count(*) FROM coding_artifacts WHERE run_id=$1 AND artifact_type='ci_attestation'", c.identity) == 0


async def test_policy_publication_is_human_scoped_and_definition_bound(case, monkeypatch):
    from app.api.routes import admin
    from starlette.requests import Request
    from fastapi import HTTPException
    c = case
    value = policy()
    github(monkeypatch, value)
    monkeypatch.setattr(admin, 'get_settings', lambda: SimpleNamespace(coding_allowed_repositories=REPOSITORY,
        github_proposal_repository=REPOSITORY))
    async def pool():
        return c.pool
    monkeypatch.setattr(admin, 'get_pool', pool)
    request = Request({'type': 'http'})
    request.state.is_admin, request.state.user_id = True, 'trusted-ci'
    body = admin.CodingCITrustPolicyIn(**{key: val for key, val in value.items() if key not in {'id'}},
        confirmation='APPROVE_CI_TRUST_POLICY')
    with pytest.raises(HTTPException) as exc:
        await admin.publish_coding_ci_policy(body, request)
    assert exc.value.status_code == 403
    request.state.user_id = 'synthetic-human'
    result = await admin.publish_coding_ci_policy(body, request)
    assert result['policy']['approved_by'] == 'synthetic-human' and result['policy']['workflow_sha256'] == value['workflow_sha256']


async def test_policy_change_serializes_against_the_final_commit(case):
    import asyncio
    c = case
    value = policy()
    value['repository'] = 'synthetic/' + uuid4().hex
    await register(c.pool, value)
    async with c.pool.acquire() as conn:
        async with conn.transaction():
            await ci_trust.require_same_policy(conn, value)
            replacement = {**value, 'id': uuid4()}
            task = asyncio.create_task(register(c.pool, replacement))
            try:
                # Observe a real advisory-lock wait, not a sleep-based assumption.
                for _ in range(100):
                    blocked = await conn.fetchval("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=current_database() "
                        "AND wait_event='advisory' AND query LIKE 'INSERT INTO coding_ci_policies%')")
                    if blocked:
                        break
                    await asyncio.sleep(0.01)
                assert blocked and not task.done()
                assert (await ci_trust.latest_policy(conn, value['repository']))['id'] == value['id']
            except BaseException:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise
        await task
        assert (await ci_trust.latest_policy(conn, value['repository']))['id'] == replacement['id']


async def test_human_revocation_blocks_current_and_old_policies_without_deletion(case, monkeypatch):
    from app.api.routes import admin
    from starlette.requests import Request
    c = case
    value = policy()
    value['repository'] = 'synthetic/' + uuid4().hex
    await register(c.pool, value)
    async def pool():
        return c.pool
    monkeypatch.setattr(admin, 'get_pool', pool)
    request = Request({'type': 'http'})
    request.state.is_admin, request.state.user_id = True, 'synthetic-human'
    result = await admin.revoke_coding_ci_policy(admin.CodingCITrustRevocationIn(repository=value['repository'],
        policy_id=value['id'], confirmation='REVOKE_CI_TRUST_POLICY'), request)
    assert result['enabled'] is False
    async with c.pool.acquire() as conn, conn.transaction():
        with pytest.raises(ci_trust.CITrustError):
            await ci_trust.require_same_policy(conn, value)
    assert await c.pool.fetchval('SELECT count(*) FROM coding_ci_policies WHERE repository=$1', value['repository']) == 2


@pytest.mark.parametrize('grant', ['read', 'write', 'missing'])
async def test_ci_installation_token_is_explicitly_read_only(monkeypatch, grant):
    from app.improvements import publisher
    monkeypatch.setattr(publisher, 'get_settings', lambda: SimpleNamespace(github_coding_app_id='123',
        github_coding_app_installation_id='456', github_coding_app_private_key='SYNTHETIC_NOT_A_KEY'))
    monkeypatch.setattr(publisher.jwt, 'encode', lambda *args, **kwargs: 'SYNTHETIC_APP_JWT')
    async def respond(request):
        assert request.method == 'POST' and request.url.path == '/app/installations/456/access_tokens'
        body = json.loads(request.content)
        assert body == {'repositories': ['repository'], 'permissions': {'actions': 'read', 'contents': 'read', 'pull_requests': 'read'}}
        permissions = {'actions': 'read', 'contents': grant, 'pull_requests': 'read', 'metadata': 'read'}
        if grant == 'missing':
            permissions = {}
        return httpx.Response(201, json={'token': 'SYNTHETIC_INSTALLATION', 'permissions': permissions})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        if grant == 'read':
            result = await publisher.github_ci_headers(client, REPOSITORY)
            assert result['Authorization'] == 'Bearer SYNTHETIC_INSTALLATION'
        else:
            with pytest.raises(RuntimeError):
                await publisher.github_ci_headers(client, REPOSITORY)


async def test_ci_never_falls_back_to_write_capable_legacy_token(monkeypatch):
    from app.improvements import publisher
    monkeypatch.setattr(publisher, 'get_settings', lambda: SimpleNamespace(github_coding_app_id='',
        github_coding_app_installation_id='', github_coding_app_private_key='', github_proposal_token='SYNTHETIC_LEGACY'))
    async with httpx.AsyncClient() as client:
        with pytest.raises(RuntimeError, match='requires scoped GitHub App'):
            await publisher.github_ci_headers(client, REPOSITORY)


@pytest.mark.parametrize('category,expected', [('available_complete_github_evidence', 'ci_running'),
    ('github_actions_and_contents_read_permission', 'blocked')])
async def test_transient_read_retries_but_missing_permission_requires_action(case, monkeypatch, category, expected):
    c = case
    await ci_case(c)
    async def verify(*args, **kwargs):
        raise ci_trust.CITrustError(category)
    async def claim(*args):
        return c.row
    monkeypatch.setattr(worker, 'verify_run', verify)
    monkeypatch.setattr(worker, '_claim', claim)
    assert await worker.process_one_coding_run(c.pool)
    assert await c.pool.fetchval('SELECT status FROM coding_runs WHERE id=$1', c.identity) == expected
    assert await c.pool.fetchval("SELECT count(*) FROM coding_artifacts WHERE run_id=$1 AND artifact_type='ci_attestation'", c.identity) == 0


async def test_ci_clock_starts_at_publication_not_old_request(case, monkeypatch):
    c = case
    await ci_case(c)
    await c.pool.execute("UPDATE coding_runs SET started_at=now()-interval '2 days' WHERE id=$1", c.identity)
    c.row = dict(await c.pool.fetchrow('SELECT * FROM coding_runs WHERE id=$1', c.identity))
    async def verify(*args, **kwargs):
        return {'status': 'pending'}
    monkeypatch.setattr(worker, 'verify_run', verify)
    await worker._monitor_ci(c.pool, c.row)
    assert await c.pool.fetchval('SELECT status FROM coding_runs WHERE id=$1', c.identity) == 'ci_running'


async def test_owner_can_recheck_ci_without_reopening_execution(case, monkeypatch):
    from app.coding import repository
    c = case
    await ci_case(c)
    await c.pool.execute("UPDATE coding_runs SET status='blocked',current_phase='ci_evidence_required',lease_owner=NULL,lease_expires_at=NULL WHERE id=$1", c.identity)
    before = await c.pool.fetchval('SELECT approval_expires_at FROM coding_runs WHERE id=$1', c.identity)
    result = await repository.request_ci_readback(c.pool, run_id=str(c.identity), user_id=c.row['user_id'], action_hash=c.row['plan_hash'])
    assert result['status'] == 'published'
    claimed = await worker._claim(c.pool)
    assert claimed['status'] == 'ci_running' and claimed['candidate_commit'] == COMMIT
    assert await c.pool.fetchval('SELECT approval_expires_at FROM coding_runs WHERE id=$1', c.identity) == before


@pytest.mark.parametrize('change', ['cancel', 'head', 'files', 'policy', 'none'])
async def test_candidate_callback_uses_actual_source_and_cannot_revive_or_retarget(case, monkeypatch, change):
    from app.api.routes import admin
    from app.runs.schemas import CandidateValidationAttestation
    from fastapi import HTTPException
    c = case
    value = await ci_case(c)
    proposal, build = uuid4(), uuid4()
    digest = hashlib.sha256(b'print(1)\n').hexdigest()
    await c.pool.execute('''INSERT INTO improvement_proposals(id,proposal_key,proposal_type,title,sanitized_summary,
      status,content_hash,candidate_kind,candidate_version,exact_diff) VALUES($1,$2,'code','Synthetic','Synthetic',
      'awaiting_review',$3,'code',$4,'synthetic exact diff')''', proposal, str(proposal), 'f'*64, COMMIT)
    await c.pool.execute('''INSERT INTO candidate_builds(id,proposal_id,selected_option,mode,status,base_commit,candidate_commit,
      model_name,model_policy_version,tool_policy_version,token_budget,created_by,coding_run_id)
      VALUES($1,$2,'A','single','drafted',$3,$4,'synthetic','synthetic','synthetic',12000,'synthetic-human',$5)''',
      build, proposal, BASE, COMMIT, c.identity)
    await c.pool.execute('''INSERT INTO candidate_build_files(build_id,path,change_type,result_hash,content)
      VALUES($1,'synthetic.py','create',$2,'print(1)\n')''', build, digest)
    monkeypatch.setattr(admin, 'get_settings', lambda: SimpleNamespace(github_proposal_repository=REPOSITORY))
    async def pool():
        return c.pool
    monkeypatch.setattr(admin, 'get_pool', pool)
    async def verify(*args, **kwargs):
        if change == 'cancel':
            await c.pool.execute("UPDATE coding_runs SET status='cancelled',lease_owner=NULL WHERE id=$1", c.identity)
        elif change == 'head':
            await c.pool.execute("UPDATE coding_runs SET candidate_commit=repeat('f',40) WHERE id=$1", c.identity)
        elif change == 'files':
            await c.pool.execute("UPDATE candidate_build_files SET result_hash=repeat('e',64) WHERE build_id=$1", build)
        elif change == 'policy':
            await register(c.pool, {**value, 'id': uuid4()})
        return {'status': 'passed', 'tree': TREE, 'workflow_path': value['workflow_path'], 'run_id': '456',
            'jobs': {'backend': {'passed': True}}, 'policy_id': str(value['id'])}
    monkeypatch.setattr(admin, 'verify_run', verify)
    body = CandidateValidationAttestation(commit_sha=COMMIT, tree_sha=TREE, repository=REPOSITORY,
        workflow='UNTRUSTED_REPORTED_LABEL', run_id='456', suite_version='synthetic-suite',
        commands=['synthetic verified gate'], results={'passed': True}, file_hashes={'synthetic.py': digest}, log_digest='d'*64, passed=True)
    try:
        if change != 'none':
            with pytest.raises(HTTPException) as exc:
                await admin.attest_candidate_build(str(build), body)
            assert exc.value.status_code == 409
            assert await c.pool.fetchval('SELECT status FROM candidate_builds WHERE id=$1', build) == 'drafted'
        else:
            result = await admin.attest_candidate_build(str(build), body)
            assert result['status'] == 'validated'
            assert await c.pool.fetchval('SELECT status FROM coding_runs WHERE id=$1', c.identity) == 'completed'
            evidence = await c.pool.fetchval('SELECT validation_report FROM improvement_proposals WHERE id=$1', proposal)
            if isinstance(evidence, str):
                evidence = json.loads(evidence)
            assert evidence['trusted_identity'].endswith(value['workflow_path'])
            assert evidence['github_observed']['policy_id'] == str(value['id'])
    finally:
        await c.pool.execute('DELETE FROM improvement_proposals WHERE id=$1', proposal)
