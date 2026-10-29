"""Actual SQL and admin gates; GitHub is a bounded synthetic read-only transport."""
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

from fastapi import HTTPException
import httpx
import pytest
import pytest_asyncio

from app.api.routes import admin
from app.coding.ci_trust import CITrustError
from app.improvements import ci_admission
from app.improvements import provider_readback
from app.runs.schemas import ImprovementDecision, CanaryActivationDecision
from tests.coding.test_coding_execution_fence import case as shared_case, database  # noqa: F401
from tests.coding.test_ci_trust import policy, register, github, REPOSITORY, COMMIT, BASE, TREE, BRANCH

case = shared_case
pytestmark = pytest.mark.asyncio(loop_scope='module')
REAL_ASYNC_CLIENT = httpx.AsyncClient


@pytest_asyncio.fixture(loop_scope='module')
async def source(case, monkeypatch):
    c = case
    monkeypatch.setattr(provider_readback, 'load_config', lambda: provider_readback.ProviderConfig(
        railway_token='SYNTHETIC_PROJECT_TOKEN', railway_environment='synthetic-environment',
        railway_service='synthetic-service-id', vercel_token='SYNTHETIC_VERCEL_TOKEN',
        vercel_team='team_synthetic', vercel_project='prj_synthetic'))
    value = policy()
    await register(c.pool, value)
    calls = github(monkeypatch, value)
    proposal_id, build_id = uuid4(), uuid4()
    path, body = 'app/synthetic.py', 'print(1)\n'
    digest = hashlib.sha256(body.encode()).hexdigest()
    observed = await ci_admission.verify_run(value, commit=COMMIT, pull_number=42, base_ref='main',
        branch=BRANCH, file_hashes={path: digest})
    report = {'passed': True, 'commands': ['synthetic gate'], 'commit_sha': COMMIT, 'tree_sha': TREE,
        'run_id': '456', 'trusted_identity': 'github-actions:'+REPOSITORY+':'+value['workflow_path'],
        'github_observed': observed}
    manifest = {'draft_pr': {'number': 42, 'branch': BRANCH}, 'runtime_surfaces': ['worker'],
        'applicability': {'services': ['gmail'], 'operations': ['search'], 'rag_modes': ['none']}}
    key = 'admission-'+str(proposal_id)
    await c.pool.execute('''INSERT INTO improvement_proposals(id,proposal_key,proposal_type,title,sanitized_summary,
      status,content_hash,candidate_kind,candidate_state,candidate_version,candidate_manifest,validation_report)
      VALUES($1,$2,'code','Synthetic','Synthetic','awaiting_review',$3,'code','validated_implementation',
      $4,$5::jsonb,$6::jsonb)''', proposal_id, key, 'f'*64, COMMIT, json.dumps(manifest), json.dumps(report))
    await c.pool.execute('''INSERT INTO candidate_builds(id,proposal_id,selected_option,mode,status,base_commit,
      candidate_commit,candidate_tree,canonical_digest,model_name,model_policy_version,tool_policy_version,
      token_budget,created_by,checkpoint) VALUES($1,$2,'A','single','validated',$3,$4,$5,$6,
      'synthetic','synthetic','synthetic',12000,'synthetic-human',$7::jsonb)''',
      build_id, proposal_id, BASE, COMMIT, TREE, 'f'*64, json.dumps({'trusted_ci': report}))
    await c.pool.execute('''INSERT INTO candidate_build_files(build_id,path,change_type,content,result_hash)
      VALUES($1,$2,'replace',$3,$4)''', build_id, path, body, digest)
    await c.pool.execute('''INSERT INTO improvement_candidate_files(proposal_id,path,change_type,content,content_hash)
      VALUES($1,$2,'replace',$3,$4)''', proposal_id, path, body, digest)
    await c.pool.execute('''INSERT INTO candidate_validation_runs(build_id,suite_version,commit_sha,status,
      attestation,trusted_identity) VALUES($1,'synthetic',$2,'passed',$3::jsonb,$4)''',
      build_id, COMMIT, json.dumps({'run_id': '456'}), report['trusted_identity'])
    monkeypatch.setattr(admin, 'get_settings', lambda: SimpleNamespace(github_proposal_repository=REPOSITORY,
        allow_dev_auth=True, candidate_worker_rag_enabled=False))
    async def pool():
        return c.pool
    monkeypatch.setattr(admin, 'get_pool', pool)
    calls.clear()
    fixture = SimpleNamespace(pool=c.pool, policy=value, proposal=proposal_id, build=build_id, key=key,
        report=report, calls=calls, digest='f'*64)
    try:
        yield fixture
    finally:
        await c.pool.execute('DELETE FROM improvement_proposals WHERE id=$1', proposal_id)


async def test_actual_source_reverified_then_committed_under_policy_lock(source):
    snapshot = await ci_admission.prepare(source.pool, source.key, source.digest, REPOSITORY)
    assert source.calls and all(method == 'GET' for method, _ in source.calls)
    assert snapshot['observed']['run_attempt'] == 2
    async with source.pool.acquire() as conn, conn.transaction():
        proposal = await ci_admission.lock(conn, snapshot)
        assert proposal['candidate_version'] == COMMIT


@pytest.mark.parametrize('tamper', ['report', 'content', 'hash', 'head', 'receipt', 'build'])
async def test_forged_or_changed_evidence_fails_before_any_github_read(source, tamper):
    if tamper == 'report':
        value = deepcopy(source.report)
        value['commands'] = ['admin asserted passing tests']
        await source.pool.execute('UPDATE improvement_proposals SET validation_report=$2::jsonb WHERE id=$1',
            source.proposal, json.dumps(value))
    elif tamper == 'content':
        await source.pool.execute("UPDATE improvement_candidate_files SET content='changed' WHERE proposal_id=$1", source.proposal)
    elif tamper == 'hash':
        await source.pool.execute("UPDATE improvement_candidate_files SET content_hash=repeat('e',64) WHERE proposal_id=$1", source.proposal)
    elif tamper == 'head':
        await source.pool.execute("UPDATE improvement_proposals SET candidate_version=repeat('d',40) WHERE id=$1", source.proposal)
    elif tamper == 'receipt':
        await source.pool.execute('DELETE FROM candidate_validation_runs WHERE build_id=$1', source.build)
    else:
        await source.pool.execute("UPDATE candidate_builds SET status='failed' WHERE id=$1", source.build)
    with pytest.raises(CITrustError):
        await ci_admission.prepare(source.pool, source.key, source.digest, REPOSITORY)
    assert source.calls == []


@pytest.mark.parametrize('change', ['policy', 'proposal', 'files', 'build', 'age'])
async def test_inflight_changes_cannot_commit_admission(source, change):
    snapshot = await ci_admission.prepare(source.pool, source.key, source.digest, REPOSITORY)
    if change == 'policy':
        await register(source.pool, {**source.policy, 'id': uuid4()})
    elif change == 'proposal':
        await source.pool.execute("UPDATE improvement_proposals SET status='rejected' WHERE id=$1", source.proposal)
    elif change == 'files':
        await source.pool.execute("UPDATE candidate_build_files SET result_hash=repeat('d',64) WHERE build_id=$1", source.build)
    elif change == 'build':
        await source.pool.execute("UPDATE candidate_builds SET status='failed' WHERE id=$1", source.build)
    else:
        snapshot['observed_monotonic'] -= 61
    with pytest.raises(CITrustError):
        async with source.pool.acquire() as conn, conn.transaction():
            await ci_admission.lock(conn, snapshot)


async def test_latest_failed_required_gate_blocks_even_with_old_passing_receipt(source, monkeypatch):
    monkeypatch.setattr(httpx, 'AsyncClient', REAL_ASYNC_CLIENT)
    github(monkeypatch, source.policy, problem='failure')
    with pytest.raises(CITrustError) as error:
        await ci_admission.prepare(source.pool, source.key, source.digest, REPOSITORY)
    assert error.value.category == 'successful_current_candidate_ci'


@pytest.mark.parametrize('stage', ['canary', 'activate', 'promotion'])
async def test_real_admin_stages_require_fresh_ci_before_effects(source, monkeypatch, stage):
    request = SimpleNamespace(state=SimpleNamespace(user_id='synthetic-human'))
    if stage != 'canary':
        state = 'approved_for_canary' if stage == 'activate' else 'awaiting_promotion'
        await source.pool.execute('UPDATE improvement_proposals SET status=$2 WHERE id=$1', source.proposal, state)
        await source.pool.execute('''INSERT INTO improvement_canaries(proposal_id,cohort,status,control_version,candidate_version)
          VALUES($1,'{}',$2,$3,$4)''', source.proposal, 'pending' if stage == 'activate' else 'passed', BASE, COMMIT)
        await source.pool.execute('UPDATE improvement_proposals SET deployment_evidence=$2::jsonb WHERE id=$1',
            source.proposal, json.dumps({'verified': True, 'candidate_version': COMMIT, 'trusted_identity': 'synthetic-controller'}))
    merges = []
    async def merge(proposal):
        merges.append(proposal['id'])
        return {'number': 42, 'merged': True, 'commit_sha': 'd'*40}
    monkeypatch.setattr(admin, 'promote_candidate_pr', merge)
    async def provider(*args, **kwargs):
        return provider_readback.Observation({'status': 'passed', 'synthetic_provider': True})
    monkeypatch.setattr(provider_readback, 'verify_registered', provider)
    async def attested(*args, **kwargs):
        return None  # This test isolates current CI gates; journal/provider tests are separate.
    monkeypatch.setattr(admin.deployment_intents, 'require_attested', attested)
    body = ImprovementDecision(decision='approved', proposal_hash=source.digest)
    if stage == 'canary':
        result = await admin.decide_canary(source.key, body, request)
        assert result['status'] == 'approved_for_canary'
    elif stage == 'activate':
        result = await admin.activate_canary(source.key,
            CanaryActivationDecision(decision='approved', proposal_hash=source.digest, traffic_percent=5), request)
        assert result['status'] == 'canary_active'
    else:
        result = await admin.decide_promotion(source.key, body, request)
        assert len(merges) == 1 and result['status'] == 'production_pending'
    assert source.calls
    evidence = await source.pool.fetchval("SELECT sanitized_payload FROM improvement_evidence WHERE proposal_id=$1 "
        "AND evidence_type='current_ci_admission'", source.proposal)
    if isinstance(evidence, str):
        evidence = json.loads(evidence)
    assert evidence['github_observed']['run_attempt'] == 2
    assert evidence['candidate_digest'] == source.digest and evidence['ci_does_not_grant_approval'] is True


async def test_rejection_needs_no_current_policy_or_remote_read(source):
    request = SimpleNamespace(state=SimpleNamespace(user_id='synthetic-human'))
    await source.pool.execute('DELETE FROM candidate_validation_runs WHERE build_id=$1', source.build)
    result = await admin.decide_canary(source.key,
        ImprovementDecision(decision='rejected', proposal_hash=source.digest), request)
    assert result['status'] == 'rejected' and source.calls == []


async def test_busy_build_is_bounded_and_returns_retry_not_success(source):
    snapshot = await ci_admission.prepare(source.pool, source.key, source.digest, REPOSITORY)
    async with source.pool.acquire() as held, held.transaction():
        await held.fetchrow('SELECT id FROM candidate_builds WHERE id=$1 FOR UPDATE', source.build)
        with pytest.raises(CITrustError) as error:
            async with source.pool.acquire() as conn, conn.transaction():
                await ci_admission.lock(conn, snapshot)
        assert error.value.category == 'uncontended_candidate_review_retry'


@pytest.mark.parametrize('stage', ['canary', 'activate', 'promotion'])
async def test_bad_ci_cannot_record_approval_activate_or_call_merge(source, monkeypatch, stage):
    state = {'canary': 'awaiting_review', 'activate': 'approved_for_canary', 'promotion': 'awaiting_promotion'}[stage]
    await source.pool.execute('UPDATE improvement_proposals SET status=$2 WHERE id=$1', source.proposal, state)
    await source.pool.execute('DELETE FROM candidate_validation_runs WHERE build_id=$1', source.build)
    async def forbidden(*args):
        pytest.fail('bad CI must not call deployment or merge')
    monkeypatch.setattr(admin, 'promote_candidate_pr', forbidden)
    monkeypatch.setattr(admin, 'dispatch_candidate_deployment', forbidden)
    request = SimpleNamespace(state=SimpleNamespace(user_id='synthetic-human'))
    body = ImprovementDecision(decision='approved', proposal_hash=source.digest)
    with pytest.raises(HTTPException) as error:
        if stage == 'canary':
            await admin.decide_canary(source.key, body, request)
        elif stage == 'activate':
            await admin.activate_canary(source.key,
                CanaryActivationDecision(decision='approved', proposal_hash=source.digest, traffic_percent=5), request)
        else:
            await admin.decide_promotion(source.key, body, request)
    assert error.value.status_code == 409
    assert await source.pool.fetchval('SELECT count(*) FROM improvement_approvals WHERE proposal_id=$1', source.proposal) == 0
    assert await source.pool.fetchval('SELECT status FROM improvement_proposals WHERE id=$1', source.proposal) == state
    assert await source.pool.fetchval('SELECT count(*) FROM improvement_evidence WHERE proposal_id=$1', source.proposal) == 0
    assert source.calls == []
