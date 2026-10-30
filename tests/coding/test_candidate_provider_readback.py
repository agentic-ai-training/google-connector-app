"""Actual independent verifier with synthetic bounded fixed-origin provider HTTP."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import runpy
from pathlib import Path

import httpx
import pytest

from app.coding.ci_trust import CITrustError
from app.improvements import provider_readback as readback

pytestmark = pytest.mark.asyncio
REAL_CLIENT = httpx.AsyncClient
CONFIG = readback.ProviderConfig(railway_token='SYNTHETIC_PROJECT_TOKEN', railway_environment='environment-id',
    railway_service='service-id', vercel_token='SYNTHETIC_VERCEL_TOKEN', vercel_team='team-id', vercel_project='project-id')
VERSION = 'a'*40
DIGEST = 'sha256:'+'b'*64


def evidence(surfaces):
    frozen = {'proposal_key': 'synthetic-proposal', 'proposal_hash': 'c'*64, 'candidate_version': VERSION,
        'project_id': 'railway-project', 'service_name': 'isolated-worker', 'runtime_surfaces': sorted(surfaces),
        'provider_target': CONFIG.target(surfaces)}
    body = {'candidate_version': VERSION, 'project_id': frozen['project_id'], 'service_name': frozen['service_name'],
        'runtime_surfaces': sorted(surfaces), 'deployment_id': 'deployment-id', 'image_digest': DIGEST}
    if 'api' in surfaces:
        body['deployment_url'] = 'https://synthetic.up.railway.app'
    if 'frontend' in surfaces:
        body.update({'frontend_deployment_id': 'dpl_synthetic', 'frontend_url': 'https://synthetic.vercel.app'})
    return frozen, body


def provider_data(surfaces):
    stamp = datetime.now(timezone.utc)
    return {'data': {'projectToken': {'projectId': 'railway-project', 'environmentId': 'environment-id'},
        'deployment': {'id': 'deployment-id', 'status': 'SUCCESS', 'projectId': 'railway-project',
            'serviceId': 'service-id', 'environmentId': 'environment-id', 'service': {'id': 'service-id', 'name': 'isolated-worker'},
            'createdAt': stamp.isoformat(), 'meta': {'imageDigest': DIGEST, 'private': 'SYNTHETIC_PRIVATE_META'},
            'instances': [{'id': 'instance-id', 'status': 'RUNNING'}]},
        'serviceInstance': {'serviceId': 'service-id', 'environmentId': 'environment-id',
            'activeDeployments': [{'id': 'deployment-id', 'status': 'SUCCESS'}],
            'domains': {'serviceDomains': [{'domain': 'synthetic.up.railway.app'}] if 'api' in surfaces else [], 'customDomains': []}},
        'deploymentLogs': [{'message': 'SYNTHETIC_PRIVATE_LOG'},
            {'message': 'candidate_runtime_ready role=candidate executor_version='+VERSION}]}}


def front_data():
    return {'id': 'dpl_synthetic', 'projectId': 'project-id', 'readyState': 'READY', 'target': None,
        'url': 'synthetic.vercel.app', 'createdAt': int(datetime.now(timezone.utc).timestamp()*1000),
        'meta': {'candidateVersion': VERSION, 'proposalKey': 'synthetic-proposal'}}


def configure(monkeypatch, surfaces, *, railway=None, front=None, bad_health=None, huge=False):
    railway = railway if railway is not None else provider_data(surfaces)
    front = front if front is not None else front_data()
    calls = []
    async def respond(request):
        calls.append((request.method, str(request.url)))
        if request.url.host == 'backboard.railway.com':
            assert request.method == 'POST' and request.headers['Project-Access-Token'] == CONFIG.railway_token
            assert 'authorization' not in request.headers
            payload = json.loads(request.content)
            assert payload['query'] == readback.RAILWAY_QUERY and payload['query'].startswith('query ')
            assert payload['variables'] == {'deployment': 'deployment-id', 'service': 'service-id', 'environment': 'environment-id'}
            return httpx.Response(200, content=b'x'*1_000_001) if huge else httpx.Response(200, json=railway)
        if request.url.host == 'api.vercel.com':
            assert request.method == 'GET' and request.headers['authorization'] == 'Bearer '+CONFIG.vercel_token
            assert 'project-access-token' not in request.headers and request.url.params['teamId'] == 'team-id'
            assert request.url.path == '/v13/deployments/dpl_synthetic'
            return httpx.Response(200, json=front)
        assert request.method == 'GET' and request.url.host in {'synthetic.up.railway.app', 'synthetic.vercel.app'}
        assert 'authorization' not in request.headers and 'project-access-token' not in request.headers
        if bad_health == 'redirect':
            return httpx.Response(302, headers={'location': 'http://127.0.0.1/private'})
        return httpx.Response(200, json={'status': 'ok', 'executor_role': 'candidate',
            'executor_version': 'wrong' if bad_health else VERSION, 'deployment_version': 'wrong' if bad_health else VERSION})
    def client(**kwargs):
        assert kwargs['follow_redirects'] is False and kwargs['trust_env'] is False
        return REAL_CLIENT(transport=httpx.MockTransport(respond), **kwargs)
    monkeypatch.setattr(httpx, 'AsyncClient', client)
    return calls


@pytest.mark.parametrize('surfaces', [['worker'], ['api'], ['worker', 'frontend'], ['api', 'frontend']])
async def test_independent_provider_identity_running_image_and_health_without_secret_leak(monkeypatch, surfaces):
    frozen, body = evidence(surfaces)
    calls = configure(monkeypatch, surfaces)
    observed = await readback.verify(CONFIG, frozen, body, claimed_at=datetime.now(timezone.utc)-timedelta(seconds=5))
    result = observed.require_fresh()
    assert result['status'] == 'passed' and result['railway']['image_digest'] == DIGEST
    assert 'SYNTHETIC_PRIVATE' not in str(result) and CONFIG.railway_token not in str(result)
    assert result['source_provenance_is_separate'] is True
    assert len(calls) == 1+('api' in surfaces)+2*('frontend' in surfaces)


@pytest.mark.parametrize('fault', ['token-project', 'token-environment', 'project', 'service', 'environment', 'name',
    'status', 'image', 'empty-instances', 'stopped-instance', 'other-active', 'no-readiness', 'public-worker',
    'old-deployment', 'graphql', 'malformed'])
async def test_railway_false_claims_fail_closed(monkeypatch, fault):
    frozen, body = evidence(['worker'])
    raw = provider_data(['worker'])
    data = raw['data']
    if fault.startswith('token-'):
        data['projectToken']['projectId' if fault == 'token-project' else 'environmentId'] = 'wrong'
    elif fault in {'project', 'service', 'environment', 'status', 'image', 'name'}:
        if fault == 'image':
            data['deployment']['meta']['imageDigest'] = 'sha256:'+'f'*64
        elif fault == 'name':
            data['deployment']['service']['name'] = 'control-worker'
        else:
            data['deployment'][{'project':'projectId','service':'serviceId','environment':'environmentId','status':'status'}[fault]] = 'wrong'
    elif fault == 'empty-instances':
        data['deployment']['instances'] = []
    elif fault == 'stopped-instance':
        data['deployment']['instances'][0]['status'] = 'STOPPED'
    elif fault == 'other-active':
        data['serviceInstance']['activeDeployments'].append({'id':'old-deployment','status':'SUCCESS'})
    elif fault == 'no-readiness':
        data['deploymentLogs'] = []
    elif fault == 'public-worker':
        data['serviceInstance']['domains']['customDomains'] = [{'domain':'private.example'}]
    elif fault == 'old-deployment':
        data['deployment']['createdAt'] = (datetime.now(timezone.utc)-timedelta(days=1)).isoformat()
    elif fault == 'graphql':
        raw['errors'] = [{'message':'SYNTHETIC_PRIVATE_PROVIDER_ERROR'}]
    else:
        data['deployment'] = 'not an object'
    configure(monkeypatch, ['worker'], railway=raw)
    with pytest.raises(CITrustError) as error:
        await readback.verify(CONFIG, frozen, body, claimed_at=datetime.now(timezone.utc)-timedelta(seconds=5))
    assert 'SYNTHETIC_PRIVATE' not in str(error.value)


@pytest.mark.parametrize('fault', ['project', 'production', 'version', 'proposal', 'host', 'not-ready', 'old'])
async def test_vercel_must_be_exact_ready_preview(monkeypatch, fault):
    frozen, body = evidence(['frontend'])
    front = front_data()
    if fault in {'version', 'proposal'}:
        front['meta']['candidateVersion' if fault == 'version' else 'proposalKey'] = 'wrong'
    else:
        front[{'project':'projectId','production':'target','host':'url','not-ready':'readyState','old':'createdAt'}[fault]] = (
            'production' if fault == 'production' else 1 if fault == 'old' else 'wrong')
    configure(monkeypatch, ['frontend'], front=front)
    with pytest.raises(CITrustError):
        await readback.verify(CONFIG, frozen, body, claimed_at=datetime.now(timezone.utc)-timedelta(seconds=5))


@pytest.mark.parametrize('surface', ['api', 'frontend'])
@pytest.mark.parametrize('fault', ['version', 'redirect'])
async def test_credential_free_health_rejects_mismatch_or_redirect(monkeypatch, surface, fault):
    frozen, body = evidence([surface])
    configure(monkeypatch, [surface], bad_health=fault)
    with pytest.raises(CITrustError):
        await readback.verify(CONFIG, frozen, body, claimed_at=datetime.now(timezone.utc)-timedelta(seconds=5))


@pytest.mark.parametrize('fault', ['missing-token', 'changed-target', 'bad-id', 'huge'])
async def test_config_and_response_bounds(monkeypatch, fault):
    frozen, body = evidence(['worker'])
    config = replace(CONFIG, railway_token='') if fault == 'missing-token' else CONFIG
    if fault == 'changed-target':
        frozen['provider_target']['railway_service_id'] = 'other'
    if fault == 'bad-id':
        body['deployment_id'] = '../another-route'
    calls = configure(monkeypatch, ['worker'], huge=fault == 'huge')
    with pytest.raises(CITrustError):
        await readback.verify(config, frozen, body, claimed_at=datetime.now(timezone.utc)-timedelta(seconds=5))
    assert len(calls) == (1 if fault == 'huge' else 0)


async def test_expired_observation_cannot_commit_as_current():
    with pytest.raises(CITrustError):
        readback.Observation({'status':'passed'}, observed_monotonic=0).require_fresh()


@pytest.mark.parametrize('same_target', [True, False])
async def test_pinned_cli_readiness_selects_service_and_environment_ids(tmp_path, monkeypatch, same_target):
    monkeypatch.chdir(tmp_path)
    for key, value in {'RAILWAY_CANDIDATE_SERVICE_ID':'service-id', 'RAILWAY_CANDIDATE_ENVIRONMENT_ID':'environment-id',
        'CANDIDATE_VERSION':VERSION, 'CANDIDATE_RUNTIME_SURFACES':'worker'}.items():
        monkeypatch.setenv(key, value)
    (tmp_path/'candidate-domain.json').write_text(json.dumps({'deployment_url':None}))
    calls = []
    def output(argv, **kwargs):
        calls.append(argv)
        if argv[1] == 'logs':
            assert argv[2] == 'deployment-id' and argv[argv.index('--service')+1] == 'service-id'
            return 'candidate_runtime_ready role=candidate executor_version='+VERSION
        assert argv == ['railway','status','--json']
        deployment = {'id':'deployment-id','status':'SUCCESS','createdAt':'2026-10-29T00:00:00Z',
            'meta':{'imageDigest':DIGEST},'instances':[{'status':'RUNNING'}]}
        return json.dumps({'environments':{'edges':[{'node':{'serviceInstances':{'edges':[{'node':{
            'serviceName':'same-mutable-name', 'serviceId':'service-id',
            'environmentId':'environment-id' if same_target else 'wrong-environment',
            'activeDeployments':[deployment]}}]}}}]}})
    monkeypatch.setattr('subprocess.check_output', output)
    monkeypatch.setattr('time.sleep', lambda *args: None)
    entry = Path(__file__).resolve().parents[2]/'scripts/wait_candidate_deployment.py'
    if same_target:
        runpy.run_path(str(entry), run_name='__main__')
        assert json.loads((tmp_path/'candidate-deployment.json').read_text())['latestDeployment']['id'] == 'deployment-id'
        assert len(calls) == 2
    else:
        with pytest.raises(SystemExit, match='did not become healthy'):
            runpy.run_path(str(entry), run_name='__main__')
        assert not (tmp_path/'candidate-deployment.json').exists() and len(calls) == 60
