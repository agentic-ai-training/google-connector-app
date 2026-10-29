"""Trusted controller receipt custody; all HTTP is an inert synthetic transport."""
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import yaml

from scripts import claim_candidate_deployment as controller

REAL_CLIENT = httpx.Client
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    values = {'CANDIDATE_INTENT_ID': str(uuid4()), 'CANDIDATE_ATTESTATION_URL': 'https://authority.example',
        'GITHUB_REF': 'refs/heads/main', 'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_RUN_ID': '901',
        'CANDIDATE_VERSION': 'b'*40, 'GITHUB_SHA': 'a'*40, 'RAILWAY_CANDIDATE_PROJECT_ID': 'isolated-project',
        'RAILWAY_CANDIDATE_WORKER_SERVICE': 'isolated-worker', 'CANDIDATE_RUNTIME_SURFACES': 'worker',
        'PROPOSAL_KEY': 'synthetic-proposal', 'CANDIDATE_DEPLOY_ATTESTATION_TOKEN': 'SYNTHETIC_CALLBACK_ONLY'}
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return values


def transport(monkeypatch, *, failure=None):
    calls = []
    def respond(request):
        calls.append(request)
        assert request.method == 'POST' and request.url.host == 'authority.example'
        payload = json.loads(request.content)
        if failure == 'timeout':
            raise httpx.ReadTimeout('SYNTHETIC_PRIVATE_RESPONSE')
        response = {key: payload[key] for key in ('intent_id', 'run_id', 'run_attempt', 'candidate_version',
            'project_id', 'service_name', 'runtime_surfaces')}
        response['nonce'] = 'SYNTHETIC_CLAIM_NONCE_'+'x'*32
        if failure == 'identity':
            response['project_id'] = 'production'
        return httpx.Response(409 if failure == 'denied' else 200, json=response)
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: REAL_CLIENT(transport=httpx.MockTransport(respond), **kwargs))
    return calls


def test_private_exclusive_receipt_no_nonce_printed(configured, monkeypatch, capsys):
    calls = transport(monkeypatch)
    controller.main()
    receipt = Path('candidate-deployment-claim.json')
    assert receipt.stat().st_mode & 0o777 == 0o600
    payload = json.loads(receipt.read_text())
    assert payload['nonce'] not in capsys.readouterr().out
    with pytest.raises(FileExistsError):
        controller.main()
    assert len(calls) == 1


@pytest.mark.parametrize('failure', ['timeout', 'identity', 'denied'])
def test_uncertain_claim_never_overwrites_or_automatically_reposts(configured, monkeypatch, failure):
    calls = transport(monkeypatch, failure=failure)
    with pytest.raises((httpx.ReadTimeout, RuntimeError)):
        controller.main()
    assert Path('candidate-deployment-claim.json').read_bytes() == b''
    with pytest.raises(FileExistsError):
        controller.main()
    assert len(calls) == 1


@pytest.mark.parametrize('key,value', [('GITHUB_REF', 'refs/heads/candidate'), ('GITHUB_RUN_ATTEMPT', '2'),
    ('CANDIDATE_ATTESTATION_URL', 'https://authority.example/?token=bad')])
def test_invalid_workflow_context_never_requests_authority(configured, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    calls = transport(monkeypatch)
    with pytest.raises(RuntimeError):
        controller.main()
    assert calls == [] and not Path('candidate-deployment-claim.json').exists()


def test_workflow_claim_precedes_all_provider_credentials_and_uses_trusted_controller():
    workflow = yaml.safe_load((ROOT/'.github/workflows/candidate-deploy.yml').read_text())
    steps = workflow['jobs']['deploy']['steps']
    index = next(i for i, step in enumerate(steps) if step.get('run') == 'python -I scripts/claim_candidate_deployment.py')
    assert 'intent_id' in workflow.get('on', workflow.get(True))['workflow_dispatch']['inputs']
    assert '${{ inputs.intent_id }}' in workflow['run-name']
    assert all(not any(name in (step.get('env') or {}) for name in ('RAILWAY_TOKEN', 'VERCEL_TOKEN'))
        for step in steps[:index+1])
    assert all(i > index for i, step in enumerate(steps)
        if any(name in (step.get('env') or {}) for name in ('RAILWAY_TOKEN', 'VERCEL_TOKEN')))
    assert workflow['jobs']['deploy']['defaults']['run']['working-directory'] == 'controller'
