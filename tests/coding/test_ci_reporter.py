"""Reporter consumes inert API data; never executes candidate sources/artifacts."""
import ast
import base64
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import yaml

from scripts import report_candidate_ci as reporter

ROOT = Path(__file__).resolve().parents[2]
REPOSITORY = 'synthetic/repository'
COMMIT = 'b'*40
TREE = 'c'*40


def event():
    return {'repository': {'full_name': REPOSITORY}, 'workflow_run': {'id': 456, 'run_attempt': 2, 'event': 'pull_request',
        'path': '.github/workflows/candidate-validation.yml', 'head_sha': COMMIT, 'conclusion': 'success',
        'head_repository': {'full_name': REPOSITORY}}}


def transport(build_id, *, problem=None, source=b'print(1)\n', observed=None):
    def respond(request):
        if observed is not None:
            observed.append(request)
        if request.url.host != 'api.github.com':
            assert request.url.host == 'synthetic.example'
            assert 'authorization' not in request.headers
            assert request.headers['x-candidate-attestation-token'] == 'SYNTHETIC_CALLBACK'
            return httpx.Response(200, json={'status': 'accepted'})
        assert 'x-candidate-attestation-token' not in request.headers
        assert request.method == 'GET'
        path = request.url.path
        if '/git/commits/' in path:
            return httpx.Response(200, json={'sha': COMMIT, 'tree': {'sha': TREE}})
        if '/git/trees/' in path:
            assert path.endswith(TREE)
            files = [] if problem == 'no-manifest' else [{'type': 'blob', 'path': f'.improvement-proposals/{build_id}.candidate.json'}]
            return httpx.Response(200, json={'tree': files, 'truncated': problem == 'truncated'})
        if '/contents/' in path:
            if path.endswith('candidate.json'):
                content = json.dumps({'version': 1, 'build_id': build_id, 'files': ['../secret' if problem == 'unsafe-path' else 'synthetic.py']}).encode()
            else:
                content = source
            return httpx.Response(200, json={'type': 'file', 'encoding': 'base64', 'content': base64.b64encode(content).decode()})
        if path.endswith('/attempts/2/jobs'):
            return httpx.Response(200, json={'total_count': 1, 'jobs': [{'name': 'backend', 'conclusion': 'failure' if problem == 'failed' else 'success'}]})
        raise AssertionError('Unexpected reporter API path')
    return httpx.MockTransport(respond)


def test_inert_candidate_content_is_hashed_without_execution(tmp_path):
    build_id = str(uuid4())
    marker = tmp_path/'must-not-exist'
    source = f"__import__('pathlib').Path({str(marker)!r}).write_text('executed')".encode()
    with httpx.Client(transport=transport(build_id, source=source)) as client:
        build, suffix, payload = reporter.build_report(client, event(), REPOSITORY)
    assert build == build_id and suffix == 'attestation'
    assert payload['file_hashes'] == {'synthetic.py': hashlib.sha256(source).hexdigest()}
    assert payload['results']['evidence_kind'] == 'github_attempt_job_metadata'
    assert not marker.exists()


@pytest.mark.parametrize('problem', ['truncated', 'unsafe-path', 'fork', 'wrong-workflow', 'wrong-event'])
def test_untrusted_trigger_or_incomplete_tree_is_rejected(problem):
    value = event()
    if problem == 'fork':
        value['workflow_run']['head_repository']['full_name'] = 'other/repository'
    elif problem == 'wrong-workflow':
        value['workflow_run']['path'] = '.github/workflows/unreviewed.yml'
    elif problem == 'wrong-event':
        value['workflow_run']['event'] = 'workflow_dispatch'
    with httpx.Client(transport=transport(str(uuid4()), problem=problem)) as client:
        with pytest.raises(reporter.ReportError):
            reporter.build_report(client, value, REPOSITORY)


def test_non_candidate_pr_does_not_send_callback():
    with httpx.Client(transport=transport(str(uuid4()), problem='no-manifest')) as client:
        assert reporter.build_report(client, event(), REPOSITORY) is None


def test_failure_report_uses_metadata_not_downloaded_logs():
    value = event()
    value['workflow_run']['conclusion'] = 'failure'
    observed = []
    with httpx.Client(transport=transport(str(uuid4()), problem='failed', observed=observed)) as client:
        _, suffix, payload = reporter.build_report(client, value, REPOSITORY)
    assert suffix == 'validation-failure' and payload['failed_jobs'] == ['backend']
    assert all('/logs' not in request.url.path and '/artifacts' not in request.url.path for request in observed)


def test_actual_report_main_keeps_github_and_callback_credentials_separate(tmp_path, monkeypatch, capsys):
    path = tmp_path/'event.json'
    path.write_text(json.dumps(event()))
    for key, value in {'GITHUB_EVENT_PATH': str(path), 'GITHUB_REPOSITORY': REPOSITORY,
            'GH_READ_TOKEN': 'SYNTHETIC_GH_READ', 'CANDIDATE_ATTESTATION_URL': 'https://synthetic.example',
            'CANDIDATE_CI_ATTESTATION_TOKEN': 'SYNTHETIC_CALLBACK'}.items():
        monkeypatch.setenv(key, value)
    observed = []
    cls = httpx.Client
    # One stable build identity must be shared by both clients.
    build_id = str(uuid4())
    monkeypatch.setattr(reporter.httpx, 'Client', lambda **kwargs: cls(transport=transport(build_id, observed=observed), **kwargs))
    reporter.main()
    assert len([request for request in observed if request.method == 'POST']) == 1
    output = capsys.readouterr().out
    assert 'SYNTHETIC_' not in output


def test_workflow_jobs_are_keyless_and_reporter_stays_on_trusted_default_branch():
    candidate_path = ROOT/'.github/workflows/candidate-validation.yml'
    raw = candidate_path.read_text()
    candidate = yaml.safe_load(raw)
    assert 'secrets.' not in raw and '015 (head)' not in raw
    for job in candidate['jobs'].values():
        for step in job['steps']:
            if step.get('uses', '').startswith('actions/checkout@'):
                assert step['with']['ref'] == '${{ github.event.pull_request.head.sha }}'
                assert step['with']['persist-credentials'] is False
    for name, gate in [('backend', 'Validate candidate with no production credentials'), ('web', 'Validate candidate web'),
            ('flutter', 'Validate candidate mobile'), ('coding-runtime', 'Validate candidate Rust broker')]:
        assert any(step.get('name') == gate for step in candidate['jobs'][name]['steps'])
    report = yaml.safe_load((ROOT/'.github/workflows/candidate-ci-report.yml').read_text())
    steps = report['jobs']['report']['steps']
    assert steps[0]['with']['ref'] == '${{ github.sha }}'
    assert report['permissions'] == {'contents': 'read', 'actions': 'read'}
    assert not any('download-artifact' in step.get('uses', '') for step in steps)
    source = ast.parse((ROOT/'scripts/report_candidate_ci.py').read_text())
    imports = {alias.name for node in ast.walk(source) if isinstance(node, ast.Import) for alias in node.names}
    assert not imports & {'subprocess', 'pickle', 'importlib', 'zipfile', 'tarfile'}
    assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {'eval', 'exec', 'compile', '__import__'}
        for node in ast.walk(source))
