"""Real Git and isolated Python; inert cloud transports, never real deployment."""
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest
import yaml
import httpx

from scripts import verify_candidate_deployment_source as boundary
from app.coding import owned_process


ROOT = Path(__file__).resolve().parents[2]
REPOSITORY = 'synthetic/repository'


def git(root, *args):
    return subprocess.check_output(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
        '-C', str(root), *args], env={'PATH': os.environ['PATH'], 'HOME': str(root),
        'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1'}, stderr=subprocess.DEVNULL).decode().strip()


@pytest.fixture
def source(tmp_path):
    controller, candidate = tmp_path/'controller', tmp_path/'candidate-source'
    controller.mkdir()
    candidate.mkdir()
    git(candidate, 'init')
    git(candidate, 'config', 'user.name', 'Synthetic Test')
    git(candidate, 'config', 'user.email', 'synthetic@example.invalid')
    git(candidate, 'remote', 'add', 'origin', 'https://github.com/'+REPOSITORY+'.git')
    marker = tmp_path/'must-not-exist'
    (candidate/'json.py').write_text(f"from pathlib import Path; Path({str(marker)!r}).touch()")
    (candidate/'app.py').write_text('raise RuntimeError("candidate must not execute locally")\n')
    git(candidate, 'add', '.')
    git(candidate, 'commit', '-m', 'synthetic candidate data')
    return SimpleNamespace(root=candidate, controller=controller, marker=marker, commit=git(candidate, 'rev-parse', 'HEAD'))


def verify(source, **kwargs):
    values = dict(root=source.root, controller=source.controller, version=source.commit,
                  repository=REPOSITORY, surfaces='api,worker', proposal_key='failure-synthetic')
    return boundary.verify(**{**values, **kwargs})


def test_exact_source_is_data_not_executed_and_git_has_no_provider_authority(source, monkeypatch):
    monkeypatch.setenv('RAILWAY_TOKEN', 'SYNTHETIC_PRIVATE_DEPLOY_TOKEN')
    original = boundary.subprocess.run
    def inspect(*args, **kwargs):
        assert 'RAILWAY_TOKEN' not in kwargs['env'] and 'PYTHONPATH' not in kwargs['env']
        assert kwargs['env']['GIT_CONFIG_GLOBAL'] == '/dev/null'
        return original(*args, **kwargs)
    monkeypatch.setattr(boundary.subprocess, 'run', inspect)
    observed = verify(source)
    assert observed['files'] == 2 and observed['source_commit'] == source.commit
    assert observed['candidate_code_executed'] is False and not source.marker.exists()


@pytest.mark.parametrize('fault', ['head', 'repository', 'surfaces', 'duplicate', 'proposal', 'same-root', 'nested'])
def test_invalid_dispatch_inputs_fail_before_upload(source, fault):
    changes = {'head': {'version': 'f'*40}, 'repository': {'repository': 'other/repository'},
        'surfaces': {'surfaces': 'terminal'}, 'duplicate': {'surfaces': 'worker,worker'},
        'proposal': {'proposal_key': 'x\ncloud-command'}, 'same-root': {'controller': source.root},
        'nested': {'controller': source.root/'nested'}}[fault]
    with pytest.raises(boundary.SourceError):
        verify(source, **changes)
    assert not source.marker.exists()


@pytest.mark.parametrize('fault', ['modified', 'untracked', 'ignored', 'symlink', 'hardlink', 'large'])
def test_changed_or_escaping_upload_tree_is_rejected(source, fault, tmp_path):
    if fault == 'modified':
        (source.root/'app.py').write_text('changed')
    elif fault == 'untracked':
        (source.root/'extra.py').write_text('untracked')
    elif fault == 'ignored':
        (source.root/'.gitignore').write_text('.env.private\n')
        git(source.root, 'add', '.gitignore')
        git(source.root, 'commit', '-m', 'synthetic ignore')
        source.commit = git(source.root, 'rev-parse', 'HEAD')
        (source.root/'.env.private').write_text('SYNTHETIC_PRIVATE_CONTENT')
    elif fault == 'symlink':
        (source.root/'linked').symlink_to(source.controller, target_is_directory=True)
        git(source.root, 'add', 'linked')
        git(source.root, 'commit', '-m', 'synthetic symlink')
        source.commit = git(source.root, 'rev-parse', 'HEAD')
    elif fault == 'hardlink':
        os.link(source.root/'app.py', tmp_path/'outside-link')
    else:
        with (source.root/'large').open('wb') as handle:
            handle.truncate(16_777_217)
        git(source.root, 'add', 'large')
        git(source.root, 'commit', '-m', 'synthetic size')
        source.commit = git(source.root, 'rev-parse', 'HEAD')
    with pytest.raises(boundary.SourceError):
        verify(source)


def test_actual_isolated_python_ignores_poisoned_candidate_import_path(source):
    scripts = source.controller/'scripts'
    scripts.mkdir()
    entry = scripts/'verify_candidate_deployment_source.py'
    shutil.copyfile(ROOT/'scripts'/entry.name, entry)
    env = {'PATH': os.environ['PATH'], 'HOME': str(source.controller), 'PYTHONPATH': str(source.root),
        'CANDIDATE_SOURCE_ROOT': str(source.root), 'CANDIDATE_VERSION': source.commit,
        'GITHUB_REPOSITORY': REPOSITORY, 'GITHUB_REF': 'refs/heads/main',
        'CANDIDATE_RUNTIME_SURFACES': 'worker', 'PROPOSAL_KEY': 'failure-synthetic'}
    result = subprocess.run([sys.executable, '-I', str(entry)], cwd=source.root, env=env,
                            capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr.decode()
    assert json.loads(result.stdout)['candidate_code_executed'] is False and not source.marker.exists()


def test_workflow_separates_controller_candidate_and_credential_steps():
    raw = (ROOT/'.github/workflows/candidate-deploy.yml').read_text()
    job = yaml.safe_load(raw)['jobs']['deploy']
    assert job['if'] == "github.ref == 'refs/heads/main'"
    assert job['defaults']['run']['working-directory'] == 'controller'
    checkouts = [step for step in job['steps'] if step.get('uses', '').startswith('actions/checkout@')]
    assert checkouts[0]['with']['ref'] == '${{ github.sha }}'
    assert checkouts[0]['with']['path'] == 'controller'
    assert checkouts[1]['with']['path'] == 'candidate-source'
    assert all(step['with']['persist-credentials'] is False for step in checkouts)
    runs = [step for step in job['steps'] if 'run' in step]
    assert all('${{ inputs.' not in step['run'] for step in runs)
    install = next(step for step in runs if step['name'].startswith('Install pinned'))
    assert 'secrets.' not in json.dumps(install) and '@latest' not in raw
    assert '@railway/cli@5.71.0' in install['run'] and 'vercel@65.7.0' in install['run']
    upload = next(step for step in runs if step['name'].startswith('Pin candidate'))['run']
    assert '--path-as-root' in upload and 'railway up "$CANDIDATE_SOURCE_ROOT"' in upload
    for step in runs:
        if 'python ' in step['run'] and 'secrets.' in json.dumps(step.get('env', {})):
            assert step['run'].startswith('python -I scripts/')
    for filename in ['configure_candidate_domain.py', 'wait_candidate_deployment.py', 'deploy_candidate_frontend.py']:
        assert '@latest' not in (ROOT/'scripts'/filename).read_text()


def test_failed_frontend_cli_never_echoes_credential_argv_or_output(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    for key, value in {'CANDIDATE_RUNTIME_SURFACES': 'frontend', 'CANDIDATE_VERSION': 'b'*40,
        'VERCEL_TOKEN': 'SYNTHETIC_PRIVATE_TOKEN', 'CANDIDATE_SOURCE_ROOT': str(tmp_path/'candidate'),
        'VERCEL_ORG_ID': 'team_synthetic', 'VERCEL_PROJECT_ID': 'prj_synthetic',
        'CONTROL_FRONTEND_URL': 'https://control.example', 'PROPOSAL_KEY': 'synthetic'}.items():
        monkeypatch.setenv(key, value)
    calls = []
    monkeypatch.setattr(httpx, 'get', lambda *args, **kwargs:
        SimpleNamespace(is_success=True, json=lambda: {'id': 'prj_synthetic'}))
    async def failed(command, **kwargs):
        calls.append(command)
        assert command[0] == 'vercel' and command[command.index('--cwd')+1] == str(tmp_path/'candidate')
        assert 'RAILWAY_TOKEN' not in kwargs['env'] and 'RUNTIME_API_KEY' not in kwargs['env']
        assert kwargs['output_limit'] == 2_097_152 and kwargs['timeout'] == 600
        return 1, b'SYNTHETIC_PRIVATE_TOKEN'
    monkeypatch.setattr(owned_process, 'run_owned', failed)
    with pytest.raises(SystemExit) as error:
        runpy.run_path(str(ROOT/'scripts/deploy_candidate_frontend.py'), run_name='__main__')
    captured = capsys.readouterr()
    assert len(calls) == 1
    assert 'SYNTHETIC_PRIVATE' not in str(error.value)+captured.out+captured.err


@pytest.mark.parametrize('case', ['valid', 'project-before-write', 'project-after-write', 'production', 'version'])
def test_frontend_readback_and_exact_preview_evidence(tmp_path, monkeypatch, case):
    monkeypatch.chdir(tmp_path)
    for key, value in {'CANDIDATE_RUNTIME_SURFACES': 'frontend', 'CANDIDATE_VERSION': 'b'*40,
        'VERCEL_TOKEN': 'SYNTHETIC_PRIVATE_TOKEN', 'CANDIDATE_SOURCE_ROOT': str(tmp_path/'candidate'),
        'VERCEL_ORG_ID': 'team_synthetic', 'VERCEL_PROJECT_ID': 'prj_synthetic',
        'CONTROL_FRONTEND_URL': 'https://control.example', 'PROPOSAL_KEY': 'synthetic'}.items():
        monkeypatch.setenv(key, value)
    calls = []
    async def deployed(command, **kwargs):
        calls.append('cli')
        assert kwargs['cwd'] == ROOT
        return 0, b'https://synthetic-preview.vercel.app\n'
    def read(url, **kwargs):
        assert url.startswith('https://api.vercel.com/')
        assert kwargs['params'] == {'teamId': 'team_synthetic'}
        calls.append(url)
        if '/v9/projects/' in url:
            result = {'id': 'other' if case == 'project-before-write' else 'prj_synthetic'}
        else:
            result = {'id': 'dpl_synthetic', 'projectId': 'other' if case == 'project-after-write' else 'prj_synthetic',
                'target': 'production' if case == 'production' else 'preview',
                'meta': {'candidateVersion': 'c'*40 if case == 'version' else 'b'*40}}
        return SimpleNamespace(is_success=True, json=lambda: result, raise_for_status=lambda: None)
    monkeypatch.setattr(owned_process, 'run_owned', deployed)
    monkeypatch.setattr(httpx, 'get', read)
    entry = str(ROOT/'scripts/deploy_candidate_frontend.py')
    if case == 'valid':
        runpy.run_path(entry, run_name='__main__')
        report = json.loads((tmp_path/'candidate-frontend.json').read_text())
        assert report['frontend_deployment_id'] == 'dpl_synthetic' and report['frontend_source_commit'] == 'b'*40
    else:
        with pytest.raises(SystemExit):
            runpy.run_path(entry, run_name='__main__')
        assert not (tmp_path/'candidate-frontend.json').exists()
    assert ('cli' in calls) is (case != 'project-before-write')
