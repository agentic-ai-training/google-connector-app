"""Real PostgreSQL authority/journal, synthetic provider calls only."""
import asyncio
from types import SimpleNamespace
from uuid import uuid4

import asyncpg
from fastapi import HTTPException
import httpx
import pytest

from app.api.routes import admin
from app.coding.ci_trust import CITrustError
from app.improvements import ci_admission, deployment_intents as intents, provider_readback
from app.runs.schemas import ImprovementDecision, CanaryActivationDecision
from app.runs.schemas import CandidateDeploymentAttestation
from tests.coding.test_candidate_ci_admission import source as shared_source, case, database, REAL_ASYNC_CLIENT  # noqa: F401
from tests.coding.test_ci_trust import BASE, COMMIT, REPOSITORY, register

pytestmark = pytest.mark.asyncio(loop_scope='module')
source = shared_source


async def prepared(source):
    snapshot = await ci_admission.prepare(source.pool, source.key, source.digest, REPOSITORY)
    async with source.pool.acquire() as conn, conn.transaction():
        await ci_admission.lock(conn, snapshot)
        approval = await conn.fetchval('''INSERT INTO improvement_approvals
          (proposal_id,stage,proposal_hash,decision,decided_by)
          VALUES($1,'canary',$2,'approved','synthetic-human') RETURNING id''', source.proposal, source.digest)
        await conn.execute("UPDATE improvement_proposals SET status='approved_for_canary' WHERE id=$1", source.proposal)
        row = await intents.prepare(conn, snapshot, approval, project='isolated-project', service='isolated-worker')
        await conn.execute('''INSERT INTO improvement_canaries(proposal_id,cohort,status,control_version,candidate_version)
          VALUES($1,'{}','pending',$2,$3)''', source.proposal, BASE, COMMIT)
    return row


def claim_values(**kwargs):
    return {'run_id': 901, 'attempt': 1, 'candidate_version': COMMIT, 'controller_commit': BASE,
        'project': 'isolated-project', 'service': 'isolated-worker', 'surfaces': ['worker'],
        'provider_target': {'railway_environment_id': 'synthetic-environment', 'railway_service_id': 'synthetic-service-id'}, **kwargs}


async def no_post(value):
    return None


async def allow_observation(*args):
    return {'run_id': 901, 'run_attempt': 1, 'controller_commit': BASE}


async def test_approval_and_intent_are_atomic_and_authority_is_immutable(source):
    row = await prepared(source)
    assert row['state'] == 'prepared'
    with pytest.raises(asyncpg.RaiseError, match='immutable'):
        await source.pool.execute("UPDATE candidate_deployment_intents SET frozen=frozen||'{\"project_id\":\"production\"}' WHERE id=$1", row['id'])
    with pytest.raises(asyncpg.RaiseError, match='reconciliation'):
        await source.pool.execute("UPDATE candidate_deployment_intents SET state='attested' WHERE id=$1", row['id'])
    status = await intents.status(source.pool, row['id'])
    assert [event['state'] for event in status['events']] == ['prepared']
    assert 'nonce_hash' not in status['intent']


async def test_actual_admin_approval_prepares_before_post_and_dispatches_once(source, monkeypatch):
    monkeypatch.setattr(admin, 'get_settings', lambda: SimpleNamespace(github_proposal_repository=REPOSITORY,
        allow_dev_auth=False, candidate_worker_rag_enabled=False,
        railway_candidate_project_id='isolated-project', railway_candidate_worker_service='isolated-worker'))
    calls = []
    async def send(value):
        current = await intents.status(source.pool, value['intent_id'])
        assert current['intent']['state'] == 'dispatch_unknown'
        assert await source.pool.fetchval('SELECT count(*) FROM improvement_approvals WHERE proposal_id=$1', source.proposal) == 1
        calls.append(value)
    monkeypatch.setattr(admin, 'dispatch_candidate_deployment', send)
    result = await admin.decide_canary(source.key, ImprovementDecision(decision='approved', proposal_hash=source.digest),
        SimpleNamespace(state=SimpleNamespace(user_id='synthetic-human')))
    assert result['deployment_dispatch']['status'] == 'dispatched' and len(calls) == 1
    with pytest.raises(CITrustError):
        await intents.dispatch(source.pool, calls[0]['intent_id'], send)
    assert len(calls) == 1


async def test_unconfigured_target_rolls_back_human_approval_and_pending_canary(source, monkeypatch):
    monkeypatch.setattr(admin, 'get_settings', lambda: SimpleNamespace(github_proposal_repository=REPOSITORY,
        allow_dev_auth=False, candidate_worker_rag_enabled=False,
        railway_candidate_project_id='', railway_candidate_worker_service='isolated-worker'))
    with pytest.raises(HTTPException) as error:
        await admin.decide_canary(source.key, ImprovementDecision(decision='approved', proposal_hash=source.digest),
            SimpleNamespace(state=SimpleNamespace(user_id='synthetic-human')))
    assert error.value.status_code == 409
    assert await source.pool.fetchval('SELECT count(*) FROM improvement_approvals WHERE proposal_id=$1', source.proposal) == 0
    assert await source.pool.fetchval('SELECT count(*) FROM candidate_deployment_intents WHERE proposal_id=$1', source.proposal) == 0
    assert await source.pool.fetchval('SELECT status FROM improvement_proposals WHERE id=$1', source.proposal) == 'awaiting_review'


@pytest.mark.parametrize('failure', ['timeout', 'cancel'])
async def test_lost_ack_or_cancellation_retains_unknown_and_never_reposts(source, failure):
    row = await prepared(source)
    calls = []
    async def lost(value):
        calls.append(value)
        assert (await intents.status(source.pool, row['id']))['intent']['state'] == 'dispatch_unknown'
        if failure == 'cancel':
            raise asyncio.CancelledError()
        raise httpx.ReadTimeout('SYNTHETIC_PRIVATE_ERROR_NOT_RETAINED')
    if failure == 'cancel':
        with pytest.raises(asyncio.CancelledError):
            await intents.dispatch(source.pool, row['id'], lost)
    else:
        result = await intents.dispatch(source.pool, row['id'], lost)
        assert result['status'] == 'dispatch_unknown' and result['requires_reconciliation'] is True
        assert 'SYNTHETIC_PRIVATE' not in str(result)
    with pytest.raises(CITrustError):
        await intents.dispatch(source.pool, row['id'], lost)
    assert len(calls) == 1


@pytest.mark.parametrize('change', ['source', 'policy', 'rejected', 'approval'])
async def test_changed_authority_prevents_dispatch_before_post(source, change):
    row = await prepared(source)
    if change == 'source':
        await source.pool.execute("UPDATE improvement_candidate_files SET content='changed' WHERE proposal_id=$1", source.proposal)
    elif change == 'policy':
        await register(source.pool, {**source.policy, 'id': uuid4()})
    elif change == 'rejected':
        await source.pool.execute("UPDATE improvement_proposals SET status='rejected' WHERE id=$1", source.proposal)
    else:
        await source.pool.execute('''INSERT INTO improvement_approvals(proposal_id,stage,proposal_hash,decision,decided_by)
          VALUES($1,'canary',$2,'rejected','synthetic-human')''', source.proposal, source.digest)
    calls = []
    async def send(value):
        calls.append(value)
    with pytest.raises(CITrustError):
        await intents.dispatch(source.pool, row['id'], send)
    assert calls == []


@pytest.mark.parametrize('wrong', ['project', 'service', 'candidate_version', 'controller_commit', 'surfaces'])
async def test_target_or_source_mismatch_prevents_claim(source, monkeypatch, wrong):
    row = await prepared(source)
    await intents.dispatch(source.pool, row['id'], no_post)
    async def forbidden(*args):
        pytest.fail('identity mismatch should not reach Actions readback')
    monkeypatch.setattr(intents, 'observe_run', forbidden)
    with pytest.raises(CITrustError):
        await intents.claim(source.pool, row['id'], **claim_values(**{wrong: ['api'] if wrong == 'surfaces' else 'wrong'}))
    assert (await intents.status(source.pool, row['id']))['intent']['state'] == 'dispatched'


async def test_claim_is_single_use_and_nonce_only_once_then_bound_finish(source, monkeypatch):
    row = await prepared(source)
    await intents.dispatch(source.pool, row['id'], no_post)
    monkeypatch.setattr(intents, 'observe_run', allow_observation)
    permit = await intents.claim(source.pool, row['id'], **claim_values())
    with pytest.raises(CITrustError):
        await intents.claim(source.pool, row['id'], **claim_values())
    snapshot = await ci_admission.prepare(source.pool, source.key, source.digest, REPOSITORY, expected_status='approved_for_canary')
    async with source.pool.acquire() as conn, conn.transaction():
        proposal = await ci_admission.lock(conn, snapshot)
        await intents.finish(conn, row['id'], proposal=proposal, source=snapshot, run_id=901,
            nonce=permit['nonce'], project='isolated-project', service='isolated-worker', surfaces=['worker'])
    result = await intents.status(source.pool, row['id'])
    assert permit['nonce'] not in str(result) and 'nonce_hash' not in str(result)
    assert [event['state'] for event in result['events']] == ['prepared', 'dispatch_unknown', 'dispatched', 'claimed', 'attested']


@pytest.mark.parametrize('bad', ['nonce', 'run', 'target', 'approval'])
async def test_unbound_or_revoked_attestation_cannot_finish(source, monkeypatch, bad):
    row = await prepared(source)
    await intents.dispatch(source.pool, row['id'], no_post)
    monkeypatch.setattr(intents, 'observe_run', allow_observation)
    permit = await intents.claim(source.pool, row['id'], **claim_values())
    snapshot = await ci_admission.prepare(source.pool, source.key, source.digest, REPOSITORY, expected_status='approved_for_canary')
    if bad == 'approval':
        await source.pool.execute('''INSERT INTO improvement_approvals(proposal_id,stage,proposal_hash,decision,decided_by)
          VALUES($1,'canary',$2,'rejected','synthetic-human')''', source.proposal, source.digest)
    with pytest.raises(CITrustError):
        async with source.pool.acquire() as conn, conn.transaction():
            proposal = await ci_admission.lock(conn, snapshot)
            await intents.finish(conn, row['id'], proposal=proposal, source=snapshot,
                run_id=999 if bad == 'run' else 901, nonce='bad' if bad == 'nonce' else permit['nonce'],
                project='production' if bad == 'target' else 'isolated-project', service='isolated-worker', surfaces=['worker'])
    assert (await intents.status(source.pool, row['id']))['intent']['state'] == 'claimed'


def workflow_run(identity):
    return {'id': 901, 'run_attempt': 1, 'workflow_id': 888, 'path': intents.WORKFLOW_PATH,
        'head_sha': BASE, 'head_branch': 'main', 'event': 'workflow_dispatch', 'status': 'in_progress',
        'display_title': intents._title(identity), 'repository': {'full_name': REPOSITORY}}


@pytest.mark.parametrize('fault', ['none', 'attempt', 'ref', 'head', 'workflow', 'path', 'title', 'event', 'repository', 'status'])
async def test_actual_actions_readback_rejects_wrong_run_identity(monkeypatch, fault):
    identity = uuid4()
    run = workflow_run(identity)
    if fault != 'none':
        key = {'attempt': 'run_attempt', 'ref': 'head_branch', 'head': 'head_sha', 'workflow': 'workflow_id',
            'path': 'path', 'title': 'display_title', 'event': 'event', 'repository': 'repository', 'status': 'status'}[fault]
        run[key] = 2 if fault == 'attempt' else 'wrong'
    calls = []
    async def respond(request):
        assert request.method == 'GET'
        calls.append(request.url.path)
        return httpx.Response(200, json={'id': 888, 'state': 'active', 'path': intents.WORKFLOW_PATH}
            if '/workflows/' in request.url.path else run)
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: REAL_ASYNC_CLIENT(transport=httpx.MockTransport(respond), **kwargs))
    async def headers(*args):
        return {}
    monkeypatch.setattr(intents, 'read_headers', headers)
    frozen = {'repository': REPOSITORY, 'controller_commit': BASE}
    if fault == 'none':
        assert (await intents.observe_run(frozen, identity, 901, 1))['run_id'] == 901
    else:
        with pytest.raises(CITrustError):
            await intents.observe_run(frozen, identity, 901, 1)
    assert len(calls) == 2


@pytest.mark.parametrize('found', [0, 1, 2, 101])
async def test_read_only_reconciliation_never_retries_or_renews(source, monkeypatch, found):
    row = await prepared(source)
    async def lost(value):
        raise httpx.ReadTimeout('synthetic')
    await intents.dispatch(source.pool, row['id'], lost)
    calls = []
    async def respond(request):
        assert request.method == 'GET'
        calls.append(request.url.path)
        runs = [workflow_run(row['id']) for _ in range(min(found, 2))]
        return httpx.Response(200, json={'workflow_runs': runs, 'total_count': found})
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: REAL_ASYNC_CLIENT(transport=httpx.MockTransport(respond), **kwargs))
    async def headers(*args):
        return {}
    monkeypatch.setattr(intents, 'read_headers', headers)
    if found > 1:
        with pytest.raises(CITrustError):
            await intents.reconcile(source.pool, row['id'])
    else:
        result = await intents.reconcile(source.pool, row['id'])
        assert result['automatic_retry_allowed'] is False and result['intent']['state'] == 'dispatch_unknown'
        assert result['intent']['workflow_run_id'] == (901 if found else None)
        observation = result['intent']['last_observation']
        assert observation['matched_runs'] == found
    assert len(calls) == 1


async def test_cancelled_intent_cannot_be_rearmed_in_sql(source):
    row = await prepared(source)
    await source.pool.execute("UPDATE candidate_deployment_intents SET state='cancelled',diagnostic='human_cancelled' WHERE id=$1", row['id'])
    with pytest.raises(asyncpg.RaiseError, match='reconciliation'):
        await source.pool.execute("UPDATE candidate_deployment_intents SET state='prepared' WHERE id=$1", row['id'])


async def test_concurrent_dispatches_post_at_most_once(source):
    row = await prepared(source)
    calls = []
    async def send(value):
        calls.append(value)
        await asyncio.sleep(0)
    results = await asyncio.gather(intents.dispatch(source.pool, row['id'], send),
        intents.dispatch(source.pool, row['id'], send), return_exceptions=True)
    assert len(calls) == 1 and sum(isinstance(result, CITrustError) for result in results) == 1


async def test_human_cancellation_fences_late_workflow_and_requests_review(source, monkeypatch):
    row = await prepared(source)
    await intents.dispatch(source.pool, row['id'], no_post)
    result = await intents.cancel_unclaimed(source.pool, row['id'], actor='synthetic-human', note='Re-review target')
    assert result['intent']['state'] == 'cancelled'
    assert await source.pool.fetchval('SELECT status FROM improvement_proposals WHERE id=$1', source.proposal) == 'changes_requested'
    monkeypatch.setattr(intents, 'observe_run', allow_observation)
    with pytest.raises(CITrustError):
        await intents.claim(source.pool, row['id'], **claim_values())


async def test_claimed_deployment_requires_reconciliation_not_false_cancellation(source, monkeypatch):
    row = await prepared(source)
    await intents.dispatch(source.pool, row['id'], no_post)
    monkeypatch.setattr(intents, 'observe_run', allow_observation)
    await intents.claim(source.pool, row['id'], **claim_values())
    with pytest.raises(CITrustError):
        await intents.cancel_unclaimed(source.pool, row['id'], actor='synthetic-human', note='Cancel')
    assert (await intents.status(source.pool, row['id']))['intent']['state'] == 'claimed'


async def test_prepare_failure_rolls_back_approval_and_no_intent_is_published(source):
    snapshot = await ci_admission.prepare(source.pool, source.key, source.digest, REPOSITORY)
    with pytest.raises(CITrustError):
        async with source.pool.acquire() as conn, conn.transaction():
            await ci_admission.lock(conn, snapshot)
            approval = await conn.fetchval('''INSERT INTO improvement_approvals(proposal_id,stage,proposal_hash,decision,decided_by)
              VALUES($1,'canary',$2,'approved','synthetic-human') RETURNING id''', source.proposal, source.digest)
            await intents.prepare(conn, snapshot, approval, project='', service='worker')
    assert await source.pool.fetchval('SELECT count(*) FROM improvement_approvals WHERE proposal_id=$1', source.proposal) == 0


async def test_attempt_rerun_cannot_claim_even_with_valid_source(source):
    row = await prepared(source)
    await intents.dispatch(source.pool, row['id'], no_post)
    with pytest.raises(CITrustError, match='first exact'):
        await intents.claim(source.pool, row['id'], **claim_values(attempt=2))


async def test_actual_attestation_route_consumes_claim_without_storing_nonce(source, monkeypatch):
    row = await prepared(source)
    await intents.dispatch(source.pool, row['id'], no_post)
    monkeypatch.setattr(intents, 'observe_run', allow_observation)
    permit = await intents.claim(source.pool, row['id'], **claim_values())
    monkeypatch.setattr(admin, 'get_settings', lambda: SimpleNamespace(github_proposal_repository=REPOSITORY,
        railway_candidate_project_id='isolated-project', railway_candidate_worker_service='isolated-worker'))
    async def provider(*args, **kwargs):
        return provider_readback.Observation({'status': 'passed', 'synthetic_provider': True})
    monkeypatch.setattr(provider_readback, 'verify_for_intent', provider)
    body = CandidateDeploymentAttestation(intent_id=row['id'], intent_nonce=permit['nonce'],
        candidate_version=COMMIT, deployment_id='synthetic-deployment', service_name='isolated-worker',
        project_id='isolated-project', workflow='arbitrary-callback-name-is-not-trust', run_id='901',
        image_digest='sha256:'+'c'*64, source_commit=COMMIT, runtime_surfaces=['worker'],
        smoke_tests={'passed': True}, verified=True)
    result = await admin.attest_candidate_deployment(source.key, body)
    assert result['deployment_verified'] is True
    evidence = await source.pool.fetchval('SELECT deployment_evidence FROM improvement_proposals WHERE id=$1', source.proposal)
    assert permit['nonce'] not in str(evidence) and 'intent_nonce' not in evidence
    assert evidence['trusted_identity'].endswith(intents.WORKFLOW_PATH)
    with pytest.raises(HTTPException) as error:
        await admin.attest_candidate_deployment(source.key, body)
    assert error.value.status_code == 409
    async def registered(*args, **kwargs):
        return provider_readback.Observation({'status': 'passed', 'synthetic_provider': True})
    monkeypatch.setattr(provider_readback, 'verify_registered', registered)
    activated = await admin.activate_canary(source.key, CanaryActivationDecision(decision='approved',
        proposal_hash=source.digest, traffic_percent=5), SimpleNamespace(state=SimpleNamespace(user_id='synthetic-human')))
    assert activated['status'] == 'canary_active'


async def test_no_pending_canary_blocks_dispatch_even_with_human_approval(source):
    row = await prepared(source)
    await source.pool.execute('DELETE FROM improvement_canaries WHERE proposal_id=$1', source.proposal)
    with pytest.raises(CITrustError, match='pending isolated canary'):
        await intents.dispatch(source.pool, row['id'], no_post)


async def test_busy_journal_is_bounded_and_never_posts(source):
    row = await prepared(source)
    calls = []
    async def send(value):
        calls.append(value)
    async with source.pool.acquire() as held, held.transaction():
        await held.fetchrow('SELECT id FROM candidate_deployment_intents WHERE id=$1 FOR UPDATE', row['id'])
        with pytest.raises(CITrustError, match='uncontended deployment journal'):
            await intents.dispatch(source.pool, row['id'], send)
    assert calls == []
    assert (await intents.status(source.pool, row['id']))['intent']['state'] == 'prepared'


@pytest.mark.parametrize('fault', ['failure', 'stale'])
async def test_provider_failure_or_staleness_cannot_activate_traffic(source, monkeypatch, fault):
    row = await prepared(source)
    async def provider(*args, **kwargs):
        if fault == 'failure':
            raise CITrustError('synthetic_provider_readback_mismatch')
        return provider_readback.Observation({'status': 'passed'}, observed_monotonic=0)
    monkeypatch.setattr(provider_readback, 'verify_registered', provider)
    with pytest.raises(HTTPException) as error:
        await admin.activate_canary(source.key, CanaryActivationDecision(decision='approved',
            proposal_hash=source.digest, traffic_percent=5), SimpleNamespace(state=SimpleNamespace(user_id='synthetic-human')))
    assert error.value.status_code == 409
    assert (await intents.status(source.pool, row['id']))['intent']['state'] == 'prepared'
    assert await source.pool.fetchval('SELECT status FROM improvement_proposals WHERE id=$1', source.proposal) == 'approved_for_canary'
    assert not await source.pool.fetchval('SELECT routing_enabled FROM improvement_canaries WHERE proposal_id=$1', source.proposal)
    assert await source.pool.fetchval("SELECT count(*) FROM improvement_evidence WHERE proposal_id=$1 AND evidence_type='current_provider_readback'", source.proposal) == 0
