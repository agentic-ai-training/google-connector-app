"""Core-candidate write-ahead dispatch/claim journal. Not user coding authority.

GitHub reads run outside SQL locks; source/policy/approval are fenced again before
each grant. Unknown dispatch/claim outcomes never authorize automatic repetition.
"""
import asyncio
import hashlib
import json
import re
import secrets
from uuid import UUID

import asyncpg
import httpx

from app.coding.ci_trust import CITrustError, GitHubCI, read_headers
from app.improvements import ci_admission
from app.improvements import provider_readback

WORKFLOW = 'candidate-deploy.yml'
WORKFLOW_PATH = '.github/workflows/' + WORKFLOW
SERVICE_ACTORS = {'trusted-ci', 'trusted-candidate-builder', 'trusted-deployment-controller'}


def _object(value):
    return json.loads(value) if isinstance(value, str) else dict(value)


def _title(identity):
    return 'Deploy governed candidate ' + str(UUID(str(identity)))


async def _limits(conn):
    await conn.execute("SET LOCAL lock_timeout='2s'")
    await conn.execute("SET LOCAL statement_timeout='5s'")


async def _journal_lock(conn, identity):
    try:
        return await conn.fetchrow('''SELECT *,expires_at>clock_timestamp() AS live,
          claim_expires_at>clock_timestamp() AS claim_live
          FROM candidate_deployment_intents WHERE id=$1 FOR UPDATE''', identity)
    except (asyncpg.LockNotAvailableError, asyncpg.QueryCanceledError):
        raise CITrustError('uncontended_deployment_journal_reconcile_before_retry') from None


async def prepare(conn, snapshot, approval_id, *, project, service):
    """Called in the SAME transaction as human approval, after source locking."""
    if not conn.is_in_transaction() or not snapshot or not project or not service:
        raise CITrustError('configured_candidate_deployment_authority')
    proposal = snapshot['proposal']
    controller = snapshot['observed'].get('base_commit', '')
    if not re.fullmatch('[0-9a-f]{40}', controller):
        raise CITrustError('immutable_current_controller_commit')
    approval = await conn.fetchrow('''SELECT * FROM improvement_approvals WHERE id=$1
      AND proposal_id=$2 AND proposal_hash=$3 AND stage='canary' AND decision='approved' ''',
      approval_id, proposal['id'], proposal['content_hash'])
    if not approval or not approval['decided_by'] or approval['decided_by'] in SERVICE_ACTORS:
        raise CITrustError('recorded_human_canary_approval')
    surfaces = sorted(_object(proposal['candidate_manifest']).get('runtime_surfaces') or ['worker'])
    provider_target = provider_readback.load_config().target(surfaces)
    frozen = {'proposal_key': proposal['proposal_key'], 'proposal_hash': proposal['content_hash'],
        'candidate_version': proposal['candidate_version'], 'build_id': str(snapshot['build']['id']),
        'repository': snapshot['policy']['repository'], 'policy_id': str(snapshot['policy']['id']),
        'controller_commit': controller, 'project_id': project, 'service_name': service,
        'runtime_surfaces': surfaces, 'provider_target': provider_target}
    row = await conn.fetchrow('''INSERT INTO candidate_deployment_intents(proposal_id,approval_id,frozen,expires_at)
      VALUES($1,$2,$3::jsonb,clock_timestamp()+interval '1 hour'-interval '1 second') RETURNING *''',
      proposal['id'], approval_id, json.dumps(frozen))
    return dict(row)


async def _snapshot(pool, identity):
    async with pool.acquire() as conn:
        row = await conn.fetchrow('SELECT * FROM candidate_deployment_intents WHERE id=$1', UUID(str(identity)))
    if not row:
        raise CITrustError('retained_deployment_intent')
    frozen = _object(row['frozen'])
    source = await ci_admission.prepare(pool, frozen['proposal_key'], frozen['proposal_hash'],
        frozen['repository'], expected_status='approved_for_canary')
    if (not source or str(source['policy']['id']) != frozen['policy_id']
            or str(source['build']['id']) != frozen['build_id']
            or source['proposal']['candidate_version'] != frozen['candidate_version']
            or source['observed'].get('base_commit') != frozen['controller_commit']
            or sorted(_object(source['proposal']['candidate_manifest']).get('runtime_surfaces') or ['worker'])
                != frozen['runtime_surfaces']):
        raise CITrustError('unchanged_deployment_authority')
    return dict(row), frozen, source


async def _lock(conn, row, source):
    # Keep the existing order: coding -> policy -> build -> proposal -> intent.
    await ci_admission.lock(conn, source)
    current = await _journal_lock(conn, row['id'])
    if not current or not current['live'] or current['approval_id'] is None or current['proposal_id'] is None:
        raise CITrustError('unexpired_retained_deployment_authority')
    if current['frozen'] != row['frozen'] or current['approval_id'] != row['approval_id']:
        raise CITrustError('unchanged_deployment_authority')
    latest = await conn.fetchrow('''SELECT id,decision,proposal_hash FROM improvement_approvals
      WHERE proposal_id=$1 AND stage='canary' ORDER BY decided_at DESC,id DESC LIMIT 1''', current['proposal_id'])
    if (not latest or latest['id'] != current['approval_id'] or latest['decision'] != 'approved'
            or latest['proposal_hash'] != source['proposal']['content_hash']):
        raise CITrustError('current_human_canary_approval')
    pending = await conn.fetchval('''SELECT EXISTS(SELECT 1 FROM improvement_canaries
      WHERE proposal_id=$1 AND candidate_version=$2 AND status='pending')''',
      current['proposal_id'], source['proposal']['candidate_version'])
    if not pending:
        raise CITrustError('pending_isolated_canary_for_deployment')
    return current


async def dispatch(pool, identity, send):
    """Persist uncertain state BEFORE POST; even cancellation cannot release it."""
    row, frozen, source = await _snapshot(pool, identity)
    async with pool.acquire() as conn, conn.transaction():
        current = await _lock(conn, row, source)
        if current['state'] != 'prepared':
            raise CITrustError('single_deployment_dispatch_reconcile_existing_intent')
        await conn.execute('''UPDATE candidate_deployment_intents SET state='dispatch_unknown',
          diagnostic='dispatch_response_unknown' WHERE id=$1''', row['id'])
    try:
        await send({**frozen, 'intent_id': str(row['id'])})
    except (httpx.HTTPError, TimeoutError, RuntimeError):
        # A lost ACK does not prove GitHub rejected the request. No provider text.
        return {'intent_id': str(row['id']), 'status': 'dispatch_unknown', 'requires_reconciliation': True}
    async with pool.acquire() as conn, conn.transaction():
        await _limits(conn)
        # An eager workflow can already have claimed. Never regress that state.
        await conn.execute('''UPDATE candidate_deployment_intents SET state='dispatched',
          diagnostic='dispatch_acknowledged' WHERE id=$1 AND state='dispatch_unknown' ''', row['id'])
        state = await conn.fetchval('SELECT state FROM candidate_deployment_intents WHERE id=$1', row['id'])
    return {'intent_id': str(row['id']), 'status': state}


async def observe_run(frozen, identity, run_id, attempt):
    """Independent Actions readback, never trust callback's workflow assertions."""
    if type(run_id) is not int or run_id <= 0 or attempt != 1:
        raise CITrustError('first_exact_deployment_workflow_attempt')
    async def guard():
        return None
    try:
        async with asyncio.timeout(45), httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            client.headers.update(await read_headers(client, frozen['repository']))
            api = GitHubCI(client, frozen['repository'], guard)
            workflow = await api.get('/actions/workflows/' + WORKFLOW)
            observed = await api.get('/actions/runs/' + str(run_id))
            if (type(workflow.get('id')) is not int or workflow['id'] <= 0
                    or workflow.get('state') != 'active' or workflow.get('path') != WORKFLOW_PATH
                    or observed.get('workflow_id') != workflow.get('id')
                    or observed.get('id') != run_id or observed.get('run_attempt') != 1
                    or observed.get('event') != 'workflow_dispatch' or observed.get('head_branch') != 'main'
                    or observed.get('head_sha') != frozen['controller_commit']
                    or observed.get('path') != WORKFLOW_PATH or observed.get('status') != 'in_progress'
                    or observed.get('display_title') != _title(identity)
                    or observed.get('repository', {}).get('full_name') != frozen['repository']):
                raise CITrustError('exact_running_approved_deployment_controller')
            return {'run_id': run_id, 'run_attempt': 1, 'controller_commit': observed['head_sha']}
    except (TimeoutError, httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError):
        raise CITrustError('available_independent_deployment_run_evidence') from None


async def claim(pool, identity, *, run_id, attempt, candidate_version, controller_commit,
                project, service, surfaces, provider_target):
    row, frozen, source = await _snapshot(pool, identity)
    if (candidate_version != frozen['candidate_version'] or controller_commit != frozen['controller_commit']
            or project != frozen['project_id'] or service != frozen['service_name']
            or sorted(surfaces) != frozen['runtime_surfaces']
            or provider_target != frozen.get('provider_target')
            or provider_readback.load_config().target(surfaces) != provider_target):
        raise CITrustError('exact_approved_deployment_target_and_source')
    await observe_run(frozen, identity, run_id, attempt)
    nonce = secrets.token_urlsafe(32)
    async with pool.acquire() as conn, conn.transaction():
        current = await _lock(conn, row, source)
        if (current['state'] not in {'dispatch_unknown', 'dispatched'}
                or current['workflow_run_id'] not in {None, run_id}):
            raise CITrustError('single_use_deployment_claim_reconcile_do_not_retry')
        await conn.execute('''UPDATE candidate_deployment_intents SET state='claimed',workflow_run_id=$2,
          workflow_attempt=1,nonce_hash=$3,claimed_at=clock_timestamp(),
          claim_expires_at=clock_timestamp()+interval '30 minutes' WHERE id=$1''',
          row['id'], run_id, hashlib.sha256(nonce.encode()).hexdigest())
    # Only this response contains the nonce. Lost response is uncertain, NOT renewable.
    return {'intent_id': str(row['id']), 'run_id': run_id, 'run_attempt': 1,
        'nonce': nonce, 'candidate_version': frozen['candidate_version'],
        'project_id': project, 'service_name': service, 'runtime_surfaces': frozen['runtime_surfaces'],
        'provider_target': frozen['provider_target']}


async def finish(conn, identity, *, proposal, source, run_id, nonce, project, service, surfaces):
    """Bind attestation to one earlier claim; NOT independent provider proof."""
    row = await _journal_lock(conn, UUID(str(identity)))
    if (not row or row['state'] != 'claimed' or not row['claim_live'] or row['proposal_id'] != proposal['id']
            or str(row['workflow_run_id']) != str(run_id) or not nonce
            or not secrets.compare_digest(row['nonce_hash'], hashlib.sha256(nonce.encode()).hexdigest())):
        raise CITrustError('live_single_use_deployment_attestation_claim')
    frozen = _object(row['frozen'])
    if (frozen['proposal_hash'] != proposal['content_hash'] or frozen['candidate_version'] != proposal['candidate_version']
            or frozen['project_id'] != project or frozen['service_name'] != service
            or frozen['runtime_surfaces'] != sorted(surfaces)
            or frozen['policy_id'] != str(source['policy']['id'])
            or frozen['build_id'] != str(source['build']['id'])
            or frozen['controller_commit'] != source['observed'].get('base_commit')):
        raise CITrustError('unchanged_attested_deployment_source')
    latest = await conn.fetchrow('''SELECT id,decision,proposal_hash FROM improvement_approvals
      WHERE proposal_id=$1 AND stage='canary' ORDER BY decided_at DESC,id DESC LIMIT 1''', proposal['id'])
    if (not latest or latest['id'] != row['approval_id'] or latest['decision'] != 'approved'
            or latest['proposal_hash'] != frozen['proposal_hash']):
        raise CITrustError('current_human_canary_approval')
    if not await conn.fetchval('''SELECT EXISTS(SELECT 1 FROM improvement_canaries
      WHERE proposal_id=$1 AND candidate_version=$2 AND status='pending')''', proposal['id'], frozen['candidate_version']):
        raise CITrustError('pending_isolated_canary_for_deployment')
    await conn.execute("UPDATE candidate_deployment_intents SET state='attested' WHERE id=$1", row['id'])


async def require_attested(conn, identity, *, proposal, source):
    """Recheck retained human/source authority under activation's existing locks.

    Dispatch expiry is not reused as an activation TTL. This explicit new human
    traffic decision still requires current CI/policy and fresh provider evidence.
    """
    row = await _journal_lock(conn, UUID(str(identity)))
    if not row or row['state'] != 'attested' or row['proposal_id'] != proposal['id']:
        raise CITrustError('retained_attested_intent_before_traffic_activation')
    frozen = _object(row['frozen'])
    if (frozen['proposal_hash'] != proposal['content_hash'] or frozen['candidate_version'] != proposal['candidate_version']
            or frozen['build_id'] != str(source['build']['id']) or frozen['policy_id'] != str(source['policy']['id'])
            or frozen['controller_commit'] != source['observed'].get('base_commit')
            or frozen.get('provider_target') != provider_readback.load_config().target(frozen['runtime_surfaces'])):
        raise CITrustError('unchanged_attested_authority_before_traffic_activation')
    latest = await conn.fetchrow('''SELECT id,decision,proposal_hash FROM improvement_approvals
      WHERE proposal_id=$1 AND stage='canary' ORDER BY decided_at DESC,id DESC LIMIT 1''', proposal['id'])
    if (not latest or latest['id'] != row['approval_id'] or latest['decision'] != 'approved'
            or latest['proposal_hash'] != frozen['proposal_hash']):
        raise CITrustError('current_human_canary_approval')


async def status(pool, identity):
    """No remote RPC, no nonce, no new execution authority."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow('''SELECT id,proposal_id,frozen,state,workflow_run_id,workflow_attempt,
          created_at,updated_at,expires_at,claimed_at,claim_expires_at,diagnostic
          ,last_observation
          FROM candidate_deployment_intents WHERE id=$1''', UUID(str(identity)))
        events = await conn.fetch('''SELECT sequence,state,workflow_run_id,created_at
          FROM candidate_deployment_intent_events WHERE intent_id=$1 ORDER BY sequence''', UUID(str(identity)))
    if not row:
        raise CITrustError('retained_deployment_intent')
    return {'intent': dict(row), 'events': [dict(event) for event in events],
            'automatic_retry_allowed': False, 'provider_attestation_is_separate': True}


async def reconcile(pool, identity):
    """Read Actions only. Absence/terminal failure NEVER grants another dispatch.

    No cloud changes, claim renewal or automatic retry. A truncated/ambiguous list
    is insufficient evidence. Audit survives proposal erasure through the journal.
    """
    existing = await status(pool, identity)
    row = existing['intent']
    frozen = _object(row['frozen'])
    async def guard():
        return None
    try:
        async with asyncio.timeout(45), httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            client.headers.update(await read_headers(client, frozen['repository']))
            api = GitHubCI(client, frozen['repository'], guard)
            if row['workflow_run_id']:
                runs = [await api.get('/actions/runs/' + str(row['workflow_run_id']))]
            else:
                listing = await api.get('/actions/workflows/' + WORKFLOW + '/runs', params={
                    'event': 'workflow_dispatch', 'branch': 'main', 'per_page': 100,
                    'created': '>=' + row['created_at'].isoformat()})
                runs = listing.get('workflow_runs')
                if not isinstance(runs, list) or listing.get('total_count') != len(runs) or len(runs) > 100:
                    raise CITrustError('complete_bounded_deployment_run_listing')
            matching = [run for run in runs if run.get('display_title') == _title(identity)
                and run.get('path') == WORKFLOW_PATH and run.get('event') == 'workflow_dispatch'
                and run.get('head_branch') == 'main' and run.get('head_sha') == frozen['controller_commit']
                and run.get('repository', {}).get('full_name') == frozen['repository']]
            if len(matching) > 1:
                raise CITrustError('one_unambiguous_deployment_run')
            observation = {'matched_runs': len(matching), 'absence_does_not_prove_rejection': not matching}
            if matching:
                run = matching[0]
                if (type(run.get('id')) is not int or run['id'] <= 0
                        or type(run.get('run_attempt')) is not int or run['run_attempt'] < 1
                        or run.get('status') not in {'queued', 'in_progress', 'completed', 'waiting', 'pending', 'requested'}
                        or run.get('conclusion') not in {None, 'success', 'failure', 'cancelled', 'skipped', 'timed_out',
                            'action_required', 'neutral', 'stale', 'startup_failure'}):
                    raise CITrustError('valid_deployment_run_observation')
                observation.update({key: run.get(key) for key in ('id', 'run_attempt', 'status', 'conclusion')})
    except (TimeoutError, httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError):
        raise CITrustError('available_deployment_reconciliation_evidence') from None
    async with pool.acquire() as conn, conn.transaction():
        await _limits(conn)
        current = await _journal_lock(conn, row['id'])
        if not current or current['frozen'] != row['frozen']:
            raise CITrustError('unchanged_reconciliation_identity')
        if current['workflow_run_id'] and matching and current['workflow_run_id'] != observation['id']:
            raise CITrustError('unchanged_deployment_workflow_identity')
        await conn.execute('UPDATE candidate_deployment_intents SET last_observation=$2::jsonb WHERE id=$1',
            row['id'], json.dumps(observation))
        if matching and current['state'] in {'dispatch_unknown', 'dispatched'} and observation['run_attempt'] == 1:
            await conn.execute('''UPDATE candidate_deployment_intents SET workflow_run_id=$2,workflow_attempt=1
              WHERE id=$1 AND workflow_run_id IS NULL''', row['id'], observation['id'])
    return await status(pool, identity)


async def cancel_unclaimed(pool, identity, *, actor, note):
    """Human cancellation fences late claims. Never claims remote rollback.

    After a claim, independent remote-effect reconciliation is mandatory instead.
    Lock order is proposal -> intent, compatible with the admission lock chain.
    """
    if not actor or actor in SERVICE_ACTORS or not note.strip():
        raise CITrustError('explicit_human_deployment_cancellation')
    async with pool.acquire() as conn, conn.transaction():
        await _limits(conn)
        before = await conn.fetchrow('SELECT * FROM candidate_deployment_intents WHERE id=$1', UUID(str(identity)))
        if not before:
            raise CITrustError('retained_deployment_intent')
        proposal = await conn.fetchrow('SELECT * FROM improvement_proposals WHERE id=$1 FOR UPDATE', before['proposal_id'])
        current = await _journal_lock(conn, before['id'])
        if current['state'] not in {'prepared', 'dispatch_unknown', 'dispatched'}:
            raise CITrustError('unclaimed_deployment_only_reconcile_existing_claim')
        await conn.execute("UPDATE candidate_deployment_intents SET state='cancelled',diagnostic='human_cancelled' WHERE id=$1", before['id'])
        frozen = _object(current['frozen'])
        if (proposal and proposal['status'] == 'approved_for_canary'
                and proposal['content_hash'] == frozen['proposal_hash']):
            await conn.execute('''INSERT INTO improvement_approvals(proposal_id,stage,proposal_hash,decision,decided_by,decision_note)
              VALUES($1,'canary',$2,'changes_requested',$3,$4)''', proposal['id'], frozen['proposal_hash'], actor, note[:2000])
            await conn.execute("UPDATE improvement_proposals SET status='changes_requested',updated_at=now() WHERE id=$1", proposal['id'])
            await conn.execute("UPDATE improvement_canaries SET status='rolled_back',ended_at=now(),rollback_reason='Unclaimed deployment cancelled' "
                "WHERE proposal_id=$1 AND status='pending'", proposal['id'])
    return await status(pool, identity)
