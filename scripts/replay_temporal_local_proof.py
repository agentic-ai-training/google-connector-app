"""Replay bounded saved local-proof histories offline, with no server or database."""
import asyncio
import hashlib
import json
from pathlib import Path
import re
import sys
from uuid import UUID

from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer

from app.runs.temporal_workflow import WorkspaceWorkflow

CASES = ('lost-start-ack-and-completion','refresh-retry-worker-restart','sdk-cancellation','erasure-refresh','sqlite-server-restart')


async def replay_directory(directory):
    root = Path(__file__).resolve().parents[1]/'.gca-local'/'temporal-proof'
    target = Path(directory).resolve()
    if not target.is_relative_to(root.resolve()) or not target.is_dir():
        raise ValueError('Only a generated local Temporal proof directory is accepted')
    for label in CASES:
        history_file,proof_file = target/(label+'.history.json'),target/(label+'.proof.json')
        if (history_file.is_symlink() or proof_file.is_symlink() or history_file.stat().st_size>1_000_000
                or proof_file.stat().st_size>16_000):
            raise ValueError('Local proof artifact type or size rejected')
        body,proof = history_file.read_text(encoding='utf-8'),json.loads(proof_file.read_text(encoding='utf-8'))
        workflow_id = proof.get('workflow_id','')
        if (proof.get('schema')!='gca.temporal.server-proof.v1' or proof.get('namespace')!='gca-local-proof'
                or proof.get('case')!=label or not workflow_id.startswith('gca.workspace.')
                or str(UUID(workflow_id.removeprefix('gca.workspace.')))!=workflow_id.removeprefix('gca.workspace.')
                or not re.fullmatch(r'[0-9a-f]{64}',proof.get('history_sha256',''))
                or hashlib.sha256(body.encode()).hexdigest()!=proof['history_sha256']):
            raise ValueError('Local proof identity or checksum rejected')
        history = WorkflowHistory.from_json(workflow_id,body)
        if len(history.events)!=proof.get('events'):
            raise ValueError('Local proof event count rejected')
        result = await asyncio.wait_for(Replayer(workflows=[WorkspaceWorkflow],namespace='gca-local-proof').replay_workflow(history),timeout=20)
        if result.replay_failure is not None:
            raise RuntimeError('Saved local proof failed replay')
        print('OFFLINE_REPLAY_PASSED '+label,flush=True)
    return len(CASES)


if __name__=='__main__':
    if len(sys.argv)!=2:
        raise SystemExit('Provide one generated local proof directory')
    count = asyncio.run(replay_directory(sys.argv[1]))
    print(f'Offline replay: {count}/{count} passed; no Temporal server or PostgreSQL used')
