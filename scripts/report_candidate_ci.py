"""Trusted-default-branch reporter; candidate bytes are bounded inert JSON/data.

No app imports, repository imports, subprocesses, archive extraction, artifact
execution, dynamic URL following or model calls while callback credentials exist.
Server independently verifies all successful/failed Actions identity and gates.
"""
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
from urllib.parse import quote, urlsplit

import httpx


class ReportError(RuntimeError):
    pass


def safe_file(path):
    if (not isinstance(path, str) or not path or PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts
            or PurePosixPath(path).as_posix() != path or '\\' in path or '\x00' in path):
        raise ReportError('Candidate file identity is invalid')
    return path


def github_json(client, repository, path, **kwargs):
    with client.stream('GET', f'https://api.github.com/repos/{repository}{path}', **kwargs) as response:
        if response.status_code != 200:
            raise ReportError('GitHub metadata read failed')
        raw = bytearray()
        for chunk in response.iter_bytes():
            raw.extend(chunk)
            if len(raw) > 2_000_000:
                raise ReportError('GitHub metadata exceeded its bound')
    return json.loads(raw)


def file_bytes(client, repository, path, commit):
    value = github_json(client, repository, '/contents/' + quote(safe_file(path), safe='/'), params={'ref': commit})
    if value.get('type') != 'file' or value.get('encoding') != 'base64':
        raise ReportError('Candidate content is not a bounded regular file')
    raw = base64.b64decode(''.join(value['content'].split()), validate=True)
    if len(raw) > 1_000_000:
        raise ReportError('Candidate content exceeded its bound')
    return raw


def build_report(client, event, repository):
    run = event.get('workflow_run', {})
    if (not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository)
            or event.get('repository', {}).get('full_name') != repository
            or run.get('head_repository', {}).get('full_name') != repository or run.get('event') != 'pull_request'
            or run.get('path') != '.github/workflows/candidate-validation.yml'
            or type(run.get('id')) is not int or type(run.get('run_attempt')) is not int
            or not re.fullmatch('[0-9a-f]{40}', run.get('head_sha', ''))
            or run.get('conclusion') not in {'success', 'failure'}):
        raise ReportError('Trigger identity is not an eligible candidate validation run')
    commit = run['head_sha']
    head = github_json(client, repository, f'/git/commits/{commit}')
    tree_sha = head.get('tree', {}).get('sha', '')
    if head.get('sha') != commit or not re.fullmatch('[0-9a-f]{40}', tree_sha):
        raise ReportError('Candidate commit/tree identity is invalid')
    tree = github_json(client, repository, f'/git/trees/{tree_sha}', params={'recursive': '1'})
    if tree.get('truncated') is not False:
        raise ReportError('Candidate tree is incomplete')
    paths = [item['path'] for item in tree.get('tree', []) if item.get('type') == 'blob'
        and re.fullmatch(r'\.improvement-proposals/[0-9a-f-]{36}\.candidate\.json', item.get('path', ''))]
    if not paths:
        return None  # Ordinary PR, not an automatically governed candidate.
    if len(paths) != 1:
        raise ReportError('Candidate PR needs exactly one machine manifest')
    manifest = json.loads(file_bytes(client, repository, paths[0], commit))
    build_id = manifest.get('build_id')
    from uuid import UUID
    try:
        if str(UUID(build_id)) != build_id or paths[0] != f'.improvement-proposals/{build_id}.candidate.json':
            raise ValueError('mismatch')
    except (ValueError, TypeError, AttributeError):
        raise ReportError('Candidate build identity is invalid') from None
    files = manifest.get('files')
    if (manifest.get('version') != 1 or not isinstance(files, list) or not 1 <= len(files) <= 64
            or any(not isinstance(path, str) for path in files) or len(set(files)) != len(files)):
        raise ReportError('Candidate manifest is invalid')
    hashes, total = {}, 0
    for path in files:
        raw = file_bytes(client, repository, path, commit)
        total += len(raw)
        if total > 8_388_608:
            raise ReportError('Candidate aggregate content exceeded its bound')
        hashes[path] = hashlib.sha256(raw).hexdigest()
    jobs = github_json(client, repository, f"/actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs", params={'per_page': 100})
    if not isinstance(jobs.get('jobs'), list) or jobs.get('total_count') != len(jobs['jobs']):
        raise ReportError('Candidate job metadata is incomplete')
    digest = hashlib.sha256(json.dumps(jobs, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    common = {'repository': repository, 'workflow': run['path'], 'run_id': str(run['id']),
        'commit_sha': commit, 'log_digest': digest}
    if run['conclusion'] == 'success':
        payload = {**common, 'tree_sha': tree_sha, 'suite_version': 'candidate-ci-observed-v3', 'file_hashes': hashes,
            'commands': ['Independently verify required GitHub Actions jobs and steps'],
            'results': {'evidence_kind': 'github_attempt_job_metadata', 'passed': True}, 'passed': True}
        suffix = 'attestation'
    else:
        failed = [re.sub('[^A-Za-z0-9_. -]', '_', str(job.get('name', 'job')))[:100]
            for job in jobs['jobs'] if job.get('conclusion') not in {'success', 'skipped', 'neutral'}][:10]
        if not failed:
            raise ReportError('Failed workflow has no actionable job metadata')
        payload = {**common, 'failed_jobs': failed, 'diagnostic_codes': ['observed_ci_failure'],
            'diagnostics': ['GitHub job failure observed; raw logs were not loaded with callback credentials']}
        suffix = 'validation-failure'
    return build_id, suffix, payload


def main():
    event_file = Path(os.environ['GITHUB_EVENT_PATH'])
    if event_file.stat().st_size > 1_000_000:
        raise ReportError('Trigger payload exceeded its bound')
    event = json.loads(event_file.read_bytes())
    with httpx.Client(timeout=30, follow_redirects=False, headers={
            'Authorization': 'Bearer ' + os.environ['GH_READ_TOKEN'], 'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2026-03-10'}) as client:
        result = build_report(client, event, os.environ['GITHUB_REPOSITORY'])
    if result is None:
        print('No governed candidate manifest; no callback made')
        return
    base = os.environ['CANDIDATE_ATTESTATION_URL'].rstrip('/')
    parsed = urlsplit(base)
    if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ReportError('Configured callback requires HTTPS without credentials/query/fragment')
    build_id, suffix, payload = result
    # Separate client: never send the GitHub token to the application endpoint.
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        response = client.post(f'{base}/admin/candidate-builds/{build_id}/{suffix}', json=payload,
            headers={'X-Candidate-Attestation-Token': os.environ['CANDIDATE_CI_ATTESTATION_TOKEN']})
        if response.status_code != 200:
            raise ReportError('Candidate CI callback was not accepted')
    print('Candidate CI evidence submitted; no candidate code or artifact was executed by reporter')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        raise SystemExit('Candidate CI report stopped: ' + type(exc).__name__) from None
