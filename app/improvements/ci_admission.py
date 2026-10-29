"""Fresh code-candidate CI admission; no deployment, merge or approval authority."""
import hashlib
import json
import re
import time

import asyncpg

from app.coding.ci_trust import CITrustError, latest_policy, require_same_policy, verify_run


def _object(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise CITrustError('valid_frozen_candidate_evidence') from None
    if not isinstance(value, dict):
        raise CITrustError('valid_frozen_candidate_evidence')
    return value


def _files(rows, field):
    if not 1 <= len(rows) <= 64:
        raise CITrustError('bounded_frozen_candidate_files')
    result, total = {}, 0
    for row in rows:
        path, digest = row['path'], row[field]
        body = (row['content'] or '').encode()
        total += len(body)
        if len(body) > 1_000_000 or total > 8_388_608:
            raise CITrustError('bounded_frozen_candidate_files')
        if (path in result or not re.fullmatch('[0-9a-f]{64}', digest)
                or hashlib.sha256(body).hexdigest() != digest):
            raise CITrustError('unchanged_frozen_candidate_files')
        result[path] = (row['change_type'], digest)
    return result


async def _source(conn, proposal, repository):
    commit = proposal['candidate_version'] or ''
    if not re.fullmatch('[0-9a-f]{40}', commit):
        raise CITrustError('immutable_candidate_commit')
    builds = await conn.fetch('''SELECT * FROM candidate_builds WHERE proposal_id=$1
      AND candidate_commit=$2 AND status='validated' ORDER BY created_at DESC,id DESC LIMIT 2''',
      proposal['id'], commit)
    if len(builds) != 1:
        raise CITrustError('one_server_verified_candidate_build')
    build = dict(builds[0])
    report = _object(proposal['validation_report'])
    observed = _object(report.get('github_observed'))
    if (report.get('passed') is not True
            or report != _object(_object(build['checkpoint']).get('trusted_ci'))
            or build['canonical_digest'] != proposal['content_hash']
            or report.get('commit_sha') != commit or observed.get('commit') != commit
            or report.get('tree_sha') != build['candidate_tree']
            or observed.get('repository') != repository or observed.get('status') != 'passed'):
        raise CITrustError('server_verified_source_bound_ci_receipt')
    files = _files(await conn.fetch('''SELECT path,change_type,content,result_hash FROM candidate_build_files
      WHERE build_id=$1 ORDER BY path''', build['id']), 'result_hash')
    proposal_files = _files(await conn.fetch('''SELECT path,change_type,content,content_hash FROM improvement_candidate_files
      WHERE proposal_id=$1 ORDER BY path''', proposal['id']), 'content_hash')
    if files != proposal_files:
        raise CITrustError('unchanged_frozen_candidate_files')
    receipt = await conn.fetchval('''SELECT EXISTS(SELECT 1 FROM candidate_validation_runs
      WHERE build_id=$1 AND status='passed' AND commit_sha=$2 AND trusted_identity=$3
        AND attestation->>'run_id'=$4)''', build['id'], commit, report.get('trusted_identity'), report.get('run_id'))
    if not receipt:
        raise CITrustError('retained_server_ci_attestation')
    manifest = _object(proposal['candidate_manifest'])
    draft = _object(manifest.get('draft_pr') or {})
    coding = None
    if build['coding_run_id']:
        row = await conn.fetchrow('SELECT * FROM coding_runs WHERE id=$1', build['coding_run_id'])
        if (not row or row['deleted_at'] is not None or row['status'] not in {'completed','published','ci_running'}
                or row['candidate_commit'] != commit or row['repository'] != repository
                or row['base_commit'] != build['base_commit']):
            raise CITrustError('retained_coding_publication_source')
        coding = dict(row)
    number = coding['pull_request_number'] if coding else draft.get('number')
    branch = coding['branch_name'] if coding else draft.get('branch')
    base_ref = coding['base_ref'] if coding else 'main'
    if type(number) is not int or number <= 0 or not isinstance(branch, str) or not branch:
        raise CITrustError('published_candidate_pr_identity')
    return {'proposal': dict(proposal), 'build': build, 'coding': coding, 'files': files,
            'report': report, 'number': number, 'branch': branch, 'base_ref': base_ref}


async def prepare(pool, proposal_key, proposal_hash, repository, *, expected_status='awaiting_review'):
    """Read actual current CI outside all business-row locks. Never call a model."""
    async with pool.acquire() as conn:
        proposal = await conn.fetchrow('SELECT * FROM improvement_proposals WHERE proposal_key=$1', proposal_key)
        if not proposal or proposal['content_hash'] != proposal_hash:
            raise CITrustError('unchanged_candidate_review')
        if proposal['status'] != expected_status:
            raise CITrustError('matching_candidate_review_stage')
        if proposal['candidate_kind'] != 'code':
            return None  # Existing separately governed registry/OKF path, not a code-CI bypass.
        source = await _source(conn, proposal, repository)
        policy = await latest_policy(conn, repository)
    observed = await verify_run(policy, commit=proposal['candidate_version'], pull_number=source['number'],
        base_ref=source['base_ref'], branch=source['branch'],
        file_hashes={path: item[1] for path, item in source['files'].items()})
    if observed.get('status') != 'passed' or observed.get('tree') != source['build']['candidate_tree']:
        raise CITrustError('successful_current_candidate_ci')
    return {**source, 'policy': policy, 'observed': observed, 'observed_monotonic': time.monotonic()}


async def _lock(conn, snapshot):
    """Fence fresh evidence against revocation, erasure and source changes.

    Lock order matches CI acceptance: coding run → policy → build/files → proposal.
    This is a short SQL-only transaction; no GitHub requests under these locks.
    """
    if not conn.is_in_transaction():
        raise CITrustError('sql_candidate_admission_transaction')
    await conn.execute("SET LOCAL lock_timeout='2s'")
    await conn.execute("SET LOCAL statement_timeout='5s'")
    if not 0 <= time.monotonic() - snapshot['observed_monotonic'] <= 60:
        raise CITrustError('fresh_candidate_admission')
    expected = snapshot['coding']
    if expected:
        row = await conn.fetchrow('SELECT * FROM coding_runs WHERE id=$1 FOR UPDATE', expected['id'])
        if not row or dict(row) != expected:
            raise CITrustError('unchanged_coding_publication_source')
    await require_same_policy(conn, snapshot['policy'])
    build = await conn.fetchrow('SELECT * FROM candidate_builds WHERE id=$1 FOR UPDATE', snapshot['build']['id'])
    if not build or dict(build) != snapshot['build']:
        raise CITrustError('unchanged_verified_candidate_build')
    await conn.fetch('SELECT path FROM candidate_build_files WHERE build_id=$1 FOR UPDATE', build['id'])
    proposal = await conn.fetchrow('SELECT * FROM improvement_proposals WHERE id=$1 FOR UPDATE', snapshot['proposal']['id'])
    if not proposal or dict(proposal) != snapshot['proposal']:
        raise CITrustError('unchanged_candidate_review')
    await conn.fetch('SELECT path FROM improvement_candidate_files WHERE proposal_id=$1 FOR UPDATE', proposal['id'])
    current = await _source(conn, proposal, snapshot['policy']['repository'])
    if current['files'] != snapshot['files'] or current['report'] != snapshot['report']:
        raise CITrustError('unchanged_frozen_candidate_files')
    if not 0 <= time.monotonic() - snapshot['observed_monotonic'] <= 60:
        raise CITrustError('fresh_candidate_admission')
    return proposal


async def lock(conn, snapshot):
    try:
        return await _lock(conn, snapshot)
    except (asyncpg.LockNotAvailableError, asyncpg.QueryCanceledError):
        raise CITrustError('uncontended_candidate_review_retry') from None
