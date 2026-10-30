"""Trusted controller only: persist one-use receipt before cloud credentials exist.

Never retry a lost claim response. A failed/existing receipt blocks provider writes.
"""
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import httpx


def main():
    identity = str(UUID(os.environ['CANDIDATE_INTENT_ID']))
    base = os.environ['CANDIDATE_ATTESTATION_URL'].rstrip('/')
    target = urlsplit(base)
    if target.scheme != 'https' or not target.hostname or target.username or target.password or target.query or target.fragment:
        raise RuntimeError('Configured HTTPS authority endpoint required')
    if os.environ.get('GITHUB_REF') != 'refs/heads/main' or os.environ.get('GITHUB_RUN_ATTEMPT') != '1':
        raise RuntimeError('Only the original main-branch workflow may claim deployment')
    receipt = Path('candidate-deployment-claim.json')
    # Reserve exclusive local custody BEFORE claiming; never overwrite a receipt.
    fd = os.open(receipt, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        payload = {'intent_id': identity, 'run_id': int(os.environ['GITHUB_RUN_ID']), 'run_attempt': 1,
            'candidate_version': os.environ['CANDIDATE_VERSION'], 'controller_commit': os.environ['GITHUB_SHA'],
            'project_id': os.environ['RAILWAY_CANDIDATE_PROJECT_ID'],
            'service_name': os.environ['RAILWAY_CANDIDATE_WORKER_SERVICE'],
            'runtime_surfaces': sorted(os.environ['CANDIDATE_RUNTIME_SURFACES'].split(','))}
        target_ids = {'railway_environment_id': os.environ['RAILWAY_CANDIDATE_ENVIRONMENT_ID'],
            'railway_service_id': os.environ['RAILWAY_CANDIDATE_SERVICE_ID']}
        if 'frontend' in payload['runtime_surfaces']:
            target_ids.update({'vercel_team_id': os.environ['VERCEL_ORG_ID'],
                'vercel_project_id': os.environ['VERCEL_PROJECT_ID']})
        payload['provider_target'] = target_ids
        with httpx.Client(timeout=120, follow_redirects=False) as client:
            with client.stream('POST', base + '/admin/improvements/' + os.environ['PROPOSAL_KEY'] + '/deployment-claim',
                    json=payload, headers={'X-Candidate-Deploy-Token': os.environ['CANDIDATE_DEPLOY_ATTESTATION_TOKEN']}) as response:
                if response.status_code != 200:
                    raise RuntimeError('Deployment claim not confirmed; reconcile intent, do not rerun')
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > 16000:
                        raise RuntimeError('Deployment claim not confirmed; reconcile intent, do not rerun')
                result = json.loads(body)
        if (result.get('intent_id') != identity or result.get('run_id') != payload['run_id']
                or result.get('run_attempt') != 1 or result.get('candidate_version') != payload['candidate_version']
                or result.get('project_id') != payload['project_id'] or result.get('service_name') != payload['service_name']
                or result.get('runtime_surfaces') != payload['runtime_surfaces']
                or result.get('provider_target') != target_ids
                or not isinstance(result.get('nonce'), str) or not 40 <= len(result['nonce']) <= 100):
            raise RuntimeError('Deployment claim identity mismatch; reconcile before any provider write')
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            fd = -1
            json.dump(result, handle)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if fd >= 0:
            os.close(fd)
    print('Server-bound single-use deployment claim persisted; nonce is not logged')


if __name__ == '__main__':
    try:
        main()
    except (httpx.HTTPError, ValueError, KeyError, OSError, RuntimeError):
        raise SystemExit('Deployment claim unavailable or uncertain; no provider write authorized. Reconcile the intent.') from None
