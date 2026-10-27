"""Guardrails around the local proof runner, separate from real-server evidence."""
import hashlib
import json
import asyncio
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

from scripts import replay_temporal_local_proof as replay
from scripts.verify_temporal_server_local import isolated_environment,stop_owned_group


def test_runner_does_not_forward_developer_or_ci_authority(monkeypatch,tmp_path):
    for name in ('RUNTIME_API_KEY','CODING_GROQ_API_KEY','GITHUB_TOKEN','RAILWAY_TOKEN','GOOGLE_APPLICATION_CREDENTIALS',
                 'OTEL_EXPORTER_OTLP_HEADERS','TEMPORAL_API_KEY','JWT_SECRET_KEY','DATABASE_URL'):
        monkeypatch.setenv(name,'SYNTHETIC PRIVATE AUTHORITY')
    env = isolated_environment(tmp_path,tmp_path/'home',tmp_path/'proof','postgresql://127.0.0.1:5432/postgres')
    assert all('PRIVATE AUTHORITY' not in value for value in env.values())
    assert env['LANGCHAIN_TRACING_V2']=='false'
    assert env['HOME']==str(tmp_path/'home') and 'DATABASE_URL' not in env


@pytest.fixture
def artifacts(tmp_path,monkeypatch):
    monkeypatch.setattr(replay,'__file__',str(tmp_path/'scripts/replay_temporal_local_proof.py'))
    directory = tmp_path/'.gca-local/temporal-proof/synthetic'
    directory.mkdir(parents=True)
    body = json.dumps({'events':[]})
    for label in replay.CASES:
        (directory/(label+'.history.json')).write_text(body,encoding='utf-8')
        proof = {'schema':'gca.temporal.server-proof.v1','case':label,'namespace':'gca-local-proof',
            'workflow_id':'gca.workspace.'+str(uuid4()),'events':0,'history_sha256':hashlib.sha256(body.encode()).hexdigest(),
            'replay_passed':False}  # A claim is not trusted; the replayer must run.
        (directory/(label+'.proof.json')).write_text(json.dumps(proof),encoding='utf-8')
    calls = []
    class FakeReplayer:
        def __init__(self,**kwargs):
            pass
        async def replay_workflow(self,history):
            calls.append(history.workflow_id)
            return SimpleNamespace(replay_failure=None)
    monkeypatch.setattr(replay,'Replayer',FakeReplayer)
    return directory,calls


@pytest.mark.asyncio
async def test_offline_checker_runs_replayer_instead_of_trusting_passed_flag(artifacts):
    directory,calls = artifacts
    assert await replay.replay_directory(directory)==len(replay.CASES)
    assert len(calls)==len(replay.CASES)


@pytest.mark.asyncio
@pytest.mark.parametrize('fault',['checksum','symlink','oversize','identity','event_count'])
async def test_offline_artifact_faults_fail_before_replayer(artifacts,fault,tmp_path):
    directory,calls = artifacts
    label = replay.CASES[0]
    history_file,proof_file = directory/(label+'.history.json'),directory/(label+'.proof.json')
    if fault=='checksum':
        history_file.write_text('{"events":[],"changed":true}',encoding='utf-8')
    elif fault=='symlink':
        target = tmp_path/'unapproved.json'
        target.write_text(history_file.read_text(),encoding='utf-8')
        history_file.unlink()
        history_file.symlink_to(target)
    elif fault=='oversize':
        history_file.write_text('x'*1_000_001,encoding='utf-8')
    else:
        proof = json.loads(proof_file.read_text())
        proof['workflow_id' if fault=='identity' else 'events']='not-approved' if fault=='identity' else 123
        proof_file.write_text(json.dumps(proof),encoding='utf-8')
    with pytest.raises(ValueError):
        await replay.replay_directory(directory)
    assert calls==[]


@pytest.mark.asyncio
async def test_offline_checker_rejects_directories_outside_generated_proofs(tmp_path):
    with pytest.raises(ValueError):
        await replay.replay_directory(tmp_path)


@pytest.mark.asyncio
async def test_cleanup_stops_only_its_newly_created_live_process_group():
    process = await asyncio.create_subprocess_exec(sys.executable,'-c','import time; time.sleep(30)',
        env={},start_new_session=True,stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
    assert process.returncode is None
    try:
        await stop_owned_group(process)
        assert process.returncode is not None
        await stop_owned_group(process)  # Already-gone groups are not errors.
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
