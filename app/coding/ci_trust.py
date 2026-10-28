"""Read-only GitHub Actions identity, exact attempt and required gate verification.

Trust is registered by a human, not inferred from arbitrary check names, a
callback bearer token, model prose, a cache entry or a client-supplied passed flag.
"""
import asyncio
import base64
import hashlib
import json
import re
from pathlib import PurePosixPath
from urllib.parse import quote

import httpx
import jwt

from app.improvements.publisher import github_ci_headers as github_api_headers


class CITrustError(RuntimeError):
    def __init__(self, category):
        self.category = category
        super().__init__('CI verification requires ' + category.replace('_', ' ') + '; completion is not proven.')


def validate_jobs(jobs):
    if not isinstance(jobs, dict) or not 1 <= len(jobs) <= 20 or len(json.dumps(jobs).encode()) > 32000:
        raise CITrustError('valid_required_jobs')
    for name, steps in jobs.items():
        if (not isinstance(name, str) or not 1 <= len(name) <= 200 or not isinstance(steps, list)
                or not 1 <= len(steps) <= 50
                or any(not isinstance(step, str) or not 1 <= len(step) <= 200 for step in steps)
                or len(set(steps)) != len(steps)):
            raise CITrustError('valid_required_steps')


async def latest_policy(conn, repository):
    row = await conn.fetchrow('''SELECT *,expires_at>clock_timestamp() AS live FROM coding_ci_policies
      WHERE repository=$1 ORDER BY created_at DESC,id DESC LIMIT 1''', repository)
    if not row or not row['live'] or not row['enabled']:
        raise CITrustError('current_human_reviewed_policy')
    policy = dict(row)
    if isinstance(policy['required_jobs'], str):
        policy['required_jobs'] = json.loads(policy['required_jobs'])
    validate_jobs(policy['required_jobs'])
    return policy


async def require_same_policy(conn, policy):
    if not conn.is_in_transaction():
        raise CITrustError('sql_policy_commit_transaction')
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('coding-ci-policy:'||$1,0))", policy['repository'])
    current = await latest_policy(conn, policy['repository'])
    if current['id'] != policy['id']:
        raise CITrustError('unchanged_trust_policy')


class GitHubCI:
    def __init__(self, client, repository, guard):
        self.client = client
        self.base = f'https://api.github.com/repos/{repository}'
        self.guard = guard

    async def get(self, path, **kwargs):
        await self.guard()
        async with self.client.stream('GET', self.base + path, **kwargs) as response:
            if response.status_code in {401, 403}:
                raise CITrustError('github_actions_and_contents_read_permission')
            if response.status_code != 200:
                raise CITrustError('available_github_evidence')
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > 2_000_000:
                    raise CITrustError('bounded_github_evidence')
        try:
            return json.loads(body)
        except (ValueError, UnicodeError):
            raise CITrustError('valid_github_evidence') from None

    async def content(self, path, commit, limit=1_000_000):
        if (not isinstance(path, str) or not path or PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts
                or PurePosixPath(path).as_posix() != path or '\\' in path or '\x00' in path):
            raise CITrustError('canonical_file_identity')
        if not re.fullmatch('[0-9a-f]{40}', commit):
            raise CITrustError('exact_commit_identity')
        value = await self.get('/contents/' + quote(path, safe='/'), params={'ref': commit})
        try:
            content = base64.b64decode(''.join(value['content'].split()), validate=True)
        except (KeyError, TypeError, ValueError):
            raise CITrustError('file_content_evidence') from None
        if value.get('type') != 'file' or value.get('encoding') != 'base64' or len(content) > limit:
            raise CITrustError('bounded_file_evidence')
        return content


async def _noop():
    return None


async def read_headers(client, repository):
    try:
        return await github_api_headers(client, repository)
    except (RuntimeError, ValueError, jwt.PyJWTError):
        raise CITrustError('scoped_read_only_github_app_access') from None
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in {401, 403}:
            raise CITrustError('scoped_read_only_github_app_access') from None
        raise


async def _verify_workflow_definition(repository, workflow_id, workflow_path, trusted_commit, workflow_sha256, *, guard=_noop):
    async with asyncio.timeout(120), httpx.AsyncClient(timeout=30) as client:
        await guard()
        client.headers.update(await read_headers(client, repository))
        api = GitHubCI(client, repository, guard)
        definition = await api.get(f'/actions/workflows/{workflow_id}')
        if definition.get('id') != workflow_id or definition.get('path') != workflow_path or definition.get('state') != 'active':
            raise CITrustError('matching_workflow_identity')
        content = await api.content(workflow_path, trusted_commit)
        if hashlib.sha256(content).hexdigest() != workflow_sha256:
            raise CITrustError('reviewed_workflow_digest')


async def verify_workflow_definition(repository, workflow_id, workflow_path, trusted_commit, workflow_sha256, *, guard=_noop):
    try:
        await _verify_workflow_definition(repository, workflow_id, workflow_path, trusted_commit, workflow_sha256, guard=guard)
    except (TimeoutError, httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError):
        raise CITrustError('available_complete_github_evidence') from None


async def _verify_run(policy, *, commit, pull_number, base_ref, branch, run_id=None, file_hashes=None, guard=_noop):
    """Return pending/failed/passed from actual Actions facts, not posted claims.

    Exact required step names and a reviewed workflow digest form a versioned
    contract. This is not independent semantic proof that every test is sufficient.
    """
    validate_jobs(policy['required_jobs'])
    if not re.fullmatch('[0-9a-f]{40}', commit) or type(pull_number) is not int or pull_number <= 0:
        raise CITrustError('exact_publication_identity')
    repository = policy['repository']
    async with asyncio.timeout(120), httpx.AsyncClient(timeout=30) as client:
        await guard()
        client.headers.update(await read_headers(client, repository))
        api = GitHubCI(client, repository, guard)
        runs = await api.get(f"/actions/workflows/{policy['workflow_id']}/runs",
            params={'head_sha': commit, 'event': 'pull_request', 'per_page': 100})
        listed = runs.get('workflow_runs')
        if not isinstance(listed, list) or type(runs.get('total_count')) is not int or runs['total_count'] != len(listed):
            raise CITrustError('complete_workflow_inventory')
        if not listed:
            return {'status': 'pending', 'policy_id': str(policy['id']), 'commit': commit}
        if any(type(item.get('id')) is not int for item in listed):
            raise CITrustError('valid_workflow_run_identity')
        newest = max(listed, key=lambda item: item['id'])
        if run_id is not None and str(newest['id']) != str(run_id):
            raise CITrustError('latest_workflow_run')
        run = await api.get(f"/actions/runs/{newest['id']}")
        identity = ('id', 'run_attempt', 'head_sha', 'workflow_id', 'event', 'status', 'conclusion', 'updated_at')
        if (run.get('id') != newest['id'] or run.get('workflow_id') != policy['workflow_id']
                or run.get('path') != policy['workflow_path'] or run.get('head_sha') != commit
                or run.get('head_branch') != branch or run.get('event') != 'pull_request'
                or run.get('repository', {}).get('full_name') != repository
                or run.get('head_repository', {}).get('full_name') != repository
                or type(run.get('run_attempt')) is not int or run['run_attempt'] < 1
                or not any(pull.get('number') == pull_number for pull in run.get('pull_requests', []))):
            raise CITrustError('matching_workflow_run')
        pull = await api.get(f'/pulls/{pull_number}')
        if (pull.get('state') != 'open' or pull.get('head', {}).get('sha') != commit
                or pull.get('head', {}).get('ref') != branch
                or pull.get('head', {}).get('repo', {}).get('full_name') != repository
                or pull.get('base', {}).get('ref') != base_ref
                or pull.get('base', {}).get('repo', {}).get('full_name') != repository):
            raise CITrustError('current_pull_request_head')
        definition = await api.content(policy['workflow_path'], commit)
        base_definition = await api.content(policy['workflow_path'], pull['base']['sha'])
        if any(hashlib.sha256(value).hexdigest() != policy['workflow_sha256'] for value in [definition, base_definition]):
            raise CITrustError('reviewed_workflow_digest')
        if run.get('status') != 'completed':
            return {'status': 'pending', 'policy_id': str(policy['id']), 'commit': commit}
        jobs = await api.get(f"/actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs", params={'per_page': 100})
        items = jobs.get('jobs')
        if not isinstance(items, list) or type(jobs.get('total_count')) is not int or jobs['total_count'] != len(items):
            raise CITrustError('complete_attempt_jobs')
        observed = {}
        for item in items:
            name = item.get('name')
            if name not in policy['required_jobs']:
                continue
            if name in observed or item.get('run_id') != run['id'] or item.get('head_sha') != commit or type(item.get('id')) is not int:
                raise CITrustError('unambiguous_required_jobs')
            steps = item.get('steps')
            if not isinstance(steps, list):
                raise CITrustError('required_step_evidence')
            required_steps = {}
            for step in steps:
                if step.get('name') in policy['required_jobs'][name]:
                    if step['name'] in required_steps:
                        raise CITrustError('unambiguous_required_steps')
                    required_steps[step['name']] = step
            passed = (item.get('status') == 'completed' and item.get('conclusion') == 'success'
                and set(required_steps) == set(policy['required_jobs'][name])
                and all(step.get('status') == 'completed' and step.get('conclusion') == 'success'
                    for step in required_steps.values()))
            observed[name] = {'job_id': item['id'], 'conclusion': item.get('conclusion'), 'passed': passed,
                'required_steps': {key: value.get('conclusion') for key, value in required_steps.items()}}
        if set(observed) != set(policy['required_jobs']):
            raise CITrustError('all_required_jobs')
        state = 'passed' if run.get('conclusion') == 'success' and all(job['passed'] for job in observed.values()) else 'failed'
        head = await api.get(f'/git/commits/{commit}')
        if head.get('sha') != commit or not re.fullmatch('[0-9a-f]{40}', head.get('tree', {}).get('sha', '')):
            raise CITrustError('candidate_tree_identity')
        if file_hashes is not None:
            if not 1 <= len(file_hashes) <= 64:
                raise CITrustError('bounded_candidate_files')
            total = 0
            for path, expected in sorted(file_hashes.items()):
                content = await api.content(path, commit)
                total += len(content)
                if total > 8_388_608 or hashlib.sha256(content).hexdigest() != expected:
                    raise CITrustError('frozen_candidate_file_hashes')
        latest = await api.get(f"/actions/runs/{run['id']}")
        again = await api.get(f"/actions/workflows/{policy['workflow_id']}/runs",
            params={'head_sha': commit, 'event': 'pull_request', 'per_page': 100})
        if (any(run.get(key) != latest.get(key) for key in identity)
                or again.get('total_count') != runs['total_count'] or again.get('workflow_runs') != listed):
            raise CITrustError('stable_latest_attempt')
        return {'status': state, 'policy_id': str(policy['id']), 'repository': repository,
            'commit': commit, 'tree': head['tree']['sha'], 'workflow_id': policy['workflow_id'],
            'workflow_path': policy['workflow_path'], 'workflow_sha256': policy['workflow_sha256'],
            'run_id': str(run['id']), 'run_attempt': run['run_attempt'], 'jobs': observed,
            'url': f"https://github.com/{repository}/actions/runs/{run['id']}"}


async def verify_run(policy, *, commit, pull_number, base_ref, branch, run_id=None, file_hashes=None, guard=_noop):
    try:
        return await _verify_run(policy, commit=commit, pull_number=pull_number, base_ref=base_ref,
            branch=branch, run_id=run_id, file_hashes=file_hashes, guard=guard)
    except (TimeoutError, httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError):
        raise CITrustError('available_complete_github_evidence') from None
