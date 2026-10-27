"""Authenticated, single-use recovery of an exactly verified ordered write prefix.

This never retries an attempted suffix or authorizes a different plan. Private
provider results remain in the encrypted receipt store, not in the grant/events.
"""
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
import hashlib
import json
from uuid import uuid4

from app.agents.errors import ExecutionFailure
from app.runs.repository import append_event_conn
from app.runs.tool_admission import execution_authority, fenced_step_commit
from app.runs.tool_receipts import receipt_evidence
from app.tools.contracts import bind_ordered_output_lineage, write_contract_for


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


def ordered_prefix(step, executions):
    contract = write_contract_for(step.get('service'), step.get('operation'),
        (step.get('input_data') or {}).get('allowed_tools', []))
    if (not contract or contract.completion_mode != 'ordered'
            or not 0 < len(executions) < len(contract.required_tools)):
        return None
    for name, item in zip(contract.required_tools, executions):
        result = item.get('result')
        if (item.get('tool') != name or not isinstance(item.get('arguments'), dict)
                or not isinstance(result, dict) or result.get('error') or result.get('success') is False):
            return None
    return contract


async def source_hash(conn, run, step):
    dependencies = await conn.fetch('''SELECT id,step_key,status,output_data FROM agent_run_steps
      WHERE run_id=$1 AND step_key=ANY($2::text[]) ORDER BY sequence_no,id''', run['id'], step['dependencies'])
    if len(dependencies) != len(set(step['dependencies'])) or any(row['status'] != 'completed' for row in dependencies):
        return None
    return digest({'run': {key: run.get(key) for key in ('id','user_id','request','plan','executor_version',
        'orchestration_owner','canary_id','cohort_assignment','requires_approval','approval_bypassed')}, 'step': {key: step.get(key) for key in (
        'id','step_key','service','operation','read_only','input_data','dependencies','requires_approval',
        'risk_level','preconditions','postconditions','max_attempts')}, 'dependencies': [dict(row) for row in dependencies]})


async def current_authority(conn, run, step, actor):
    from app.runs.schemas import ExecutionPlan
    from app.runs.planner import action_hash
    try:
        plan = ExecutionPlan.model_validate(run['plan'])
        expected = next(item for item in plan.steps if item.id == step['step_key'])
    except (KeyError, TypeError, ValueError, StopIteration):
        return False
    if (actor != run['user_id'] or expected.read_only or step['read_only']
            or expected.service != step['service'] or expected.operation != step['operation']
            or expected.arguments != step['input_data'] or expected.dependencies != step['dependencies']
            or expected.requires_approval != step['requires_approval'] or expected.risk_level != step['risk_level']
            or ((step.get('input_data') or {}).get('semantic_authorization') or {}).get('authorized') is False):
        return False
    if (expected.preconditions != (step.get('preconditions') or [])
            or expected.postconditions != (step.get('postconditions') or [])):
        return False
    if step['requires_approval'] and not run.get('approval_bypassed'):
        return await conn.fetchval('''SELECT EXISTS(SELECT 1 FROM run_approvals WHERE run_id=$1
          AND requested_from=$2 AND status='approved' AND action_hash=$3 AND expires_at>clock_timestamp())''',
          run['id'], actor, action_hash(plan))
    return True


async def bind_prefix_decision(conn, run, step, decision, receipts, actor, executor_version, observed_at=None,
        observed_source=None, temporal_source=None):
    from app.runs.reconciliation import ReconciliationDecision
    if decision.state != 'continue_verified_prefix':
        return decision
    def blocked(reason):
        return ReconciliationDecision('manual_required', reason, None, decision.evidence)
    owner = run.get('orchestration_owner', 'postgres')
    if owner not in {'postgres','temporal'}:
        return blocked('prefix_continuation_owner_required')
    journal = None
    if owner == 'temporal':
        from app.runs.temporal_prefix_retry import preflight
        journal, reason = await preflight(conn, run, step, decision, temporal_source)
        if reason:
            return blocked(reason)
    if not await conn.fetchval("SELECT to_regclass('tool_prefix_continuations') IS NOT NULL"):
        return blocked('prefix_continuation_schema_required')
    if observed_at is None or not await conn.fetchval('''SELECT clock_timestamp()>=$1::timestamptz
      AND clock_timestamp()-$1::timestamptz<interval '5 minutes' ''', observed_at):
        return blocked('prefix_readback_expired')
    if (executor_version != run['executor_version'] or not receipts.digest or receipts.blocked
            or not ordered_prefix(step, receipts.executions) or not await current_authority(conn, run, step, actor)):
        return blocked('prefix_continuation_authority_required')
    if step.get('error_category') in {'policy_violation','planning','authentication','permission'}:
        return blocked('prefix_external_or_policy_gate')
    source = await source_hash(conn, run, step)
    if source is None:
        return blocked('prefix_dependencies_not_verified')
    if not observed_source or source != observed_source:
        return blocked('prefix_source_changed_during_readback')
    prior = await conn.fetchrow('''SELECT *,expires_at>clock_timestamp() AS fresh FROM tool_prefix_continuations
      WHERE run_id=$1 AND step_id=$2 AND next_attempt=$3 ORDER BY created_at DESC,id DESC LIMIT 1 FOR UPDATE''',
      run['id'], step['id'], step['attempt_count']+1)
    if prior:
        fresh = prior['fresh']
        if owner == 'temporal':
            fresh = fresh and await conn.fetchval('''SELECT EXISTS(SELECT 1 FROM temporal_prefix_generations
              WHERE id=$1 AND prefix_grant_id=$1 AND expires_at>clock_timestamp()
                AND source_hash=temporal_resolution_source($2,$3))''', prior['id'], run['id'], step['id'])
        if prior['consumed_at'] is not None:
            return blocked('prefix_continuation_already_consumed')
        if fresh:
            if prior['source_hash'] == source and prior['receipt_digest'] == receipts.digest:
                return decision
            return blocked('prefix_continuation_source_changed')
        if not await conn.fetchval("SELECT to_regclass('temporal_prefix_generations') IS NOT NULL"):
            return blocked('prefix_grant_renewal_schema_required')
    if await conn.fetchval('''SELECT count(*) FROM tool_prefix_continuations WHERE run_id=$1 AND step_id=$2''',
            run['id'], step['id']) >= 3:
        return blocked('prefix_continuation_budget_exhausted')
    from app.runs.worker import _store_artifacts
    from app.runs.verifier import extract_artifacts
    # Save only readback-proven artifacts before the Temporal source hash is
    # frozen; the original observation was checked before this intentional edit.
    await _store_artifacts(conn, run, step, extract_artifacts(receipts.executions), verified=True)
    identity = uuid4()
    await conn.execute('''INSERT INTO tool_prefix_continuations(id,run_id,step_id,prior_attempt,next_attempt,
      source_hash,receipt_digest,prefix_length,executor_version,expires_at)
      VALUES($1,$2,$3,$4,$4+1,$5,$6,$7,$8,clock_timestamp()+interval '15 minutes'-interval '1 second')''',
      identity, run['id'], step['id'], step['attempt_count'], source, receipts.digest,
      len(receipts.executions), executor_version)
    if journal is not None:
        from app.runs.temporal_prefix_retry import register
        await register(conn, run, step, identity, journal, decision, temporal_source)
    await append_event_conn(conn, run['id'], actor, 'write_prefix_continuation_issued', step_id=step['id'],
        phase='reconciliation', payload={'permit_id': str(identity), 'prefix_length': len(receipts.executions),
        'next_attempt': step['attempt_count']+1, 'automatic_retry': False})
    return decision


class ContinuationDenied(ExecutionFailure):
    def __init__(self, reason):
        super().__init__('Verified-prefix recovery is no longer admitted; no new provider call was made.',
            category='worker_reconciliation', component='prefix_continuation', boundary='prefix_recovery_gate',
            evidence={'reason_code': reason, 'provider_called': False})


@dataclass
class Continuation:
    permit: dict
    prefix: list[dict]
    tools: tuple[str, ...]
    run: dict
    step: dict
    cursor: int = 0
    executions: list[dict] = field(default_factory=list)
    busy: bool = False


active_continuation: ContextVar[Continuation | None] = ContextVar('verified_write_prefix', default=None)


async def _temporal_grant_current(conn, run, step, owner, permit):
    if owner.owner != 'temporal':
        return True
    return await conn.fetchval('''SELECT EXISTS(SELECT 1 FROM temporal_workspace_steps j
      JOIN temporal_prefix_generations p ON p.id=j.prefix_permit_id
      WHERE j.run_id=$1 AND j.step_id=$2 AND j.execution_token::text=$3 AND j.state='executing'
        AND p.id=$4 AND p.prefix_grant_id=$4 AND p.execution_token=j.execution_token
        AND p.workflow_execution_id=j.workflow_execution_id AND p.expires_at>clock_timestamp())''',
      run['id'], step['id'], owner.token, permit['id'])


@asynccontextmanager
async def continuation_scope(pool, run, step):
    context = None
    if not step.get('read_only', True):
        async with fenced_step_commit(pool, run, step, succeeded=None) as conn:
            exists = await conn.fetchval("SELECT to_regclass('tool_prefix_continuations') IS NOT NULL")
            permit = await conn.fetchrow('''SELECT *,expires_at>clock_timestamp() AS fresh FROM tool_prefix_continuations
              WHERE run_id=$1 AND step_id=$2 AND next_attempt=$3 ORDER BY created_at DESC,id DESC LIMIT 1 FOR UPDATE''',
              run['id'], step['id'], step['attempt_count']) if exists else None
            if run.get('orchestration_owner') == 'temporal':
                owner = execution_authority.get()
                journal = await conn.fetchrow('''SELECT to_jsonb(j)->>'prefix_permit_id' AS prefix_permit
                  FROM temporal_workspace_steps j WHERE run_id=$1 AND step_id=$2 AND execution_token::text=$3''',
                  run['id'], step['id'], owner.token)
                expected = journal['prefix_permit'] if journal else None
                if (permit is not None and str(permit['id']) != expected) or (expected and permit is None):
                    raise ContinuationDenied('temporal_prefix_grant_mismatched')
                if permit and not await _temporal_grant_current(conn, run, step, owner, permit):
                    raise ContinuationDenied('temporal_prefix_grant_expired')
            if permit:
                live_run = dict(await conn.fetchrow('SELECT * FROM agent_runs WHERE id=$1', run['id']))
                live_step = dict(await conn.fetchrow('SELECT * FROM agent_run_steps WHERE id=$1', step['id']))
                receipts = await receipt_evidence(conn, run['id'], step['id'], run['user_id'])
                contract = ordered_prefix(live_step, receipts.executions)
                if (not permit['fresh'] or permit['consumed_at'] is not None or receipts.blocked
                        or receipts.digest != permit['receipt_digest'] or not contract
                        or len(receipts.executions) != permit['prefix_length']
                        or permit['executor_version'] != live_run['executor_version']
                        or await source_hash(conn, live_run, live_step) != permit['source_hash']
                        or not await current_authority(conn, live_run, live_step, live_run['user_id'])):
                    raise ContinuationDenied('permit_or_source_changed')
                owner = execution_authority.get()
                await conn.execute('''UPDATE tool_prefix_continuations SET consumed_at=clock_timestamp(),authority_hash=$2
                  WHERE id=$1''', permit['id'], digest(owner.token))
                context = Continuation(dict(permit), receipts.executions, contract.required_tools, live_run, live_step)
                await append_event_conn(conn, run['id'], run['user_id'], 'write_prefix_continuation_started',
                    step_id=step['id'], phase='reconciliation', payload={'permit_id': str(permit['id']),
                    'prefix_length': len(receipts.executions), 'attempt': step['attempt_count']})
    token = active_continuation.set(context)
    try:
        yield
    finally:
        active_continuation.reset(token)


def _single_admission(function):
    @wraps(function)
    async def guarded(*args, **kwargs):
        context = active_continuation.get()
        if context is None:
            return await function(*args, **kwargs)
        if context.busy:
            raise ContinuationDenied('concurrent_prefix_admission')
        context.busy = True
        try:
            return await function(*args, **kwargs)
        finally:
            context.busy = False
    return guarded


@_single_admission
async def before_tool(pool, state, name, call):
    """Return (reused, raw result). Never manufacture an invocation receipt."""
    context = active_continuation.get()
    if context is None:
        return False, None
    if (str(context.run['id']) != str(state.get('run_id')) or str(context.step['id']) != str(state.get('step_id'))
            or context.run['user_id'] != state.get('user_id') or context.cursor >= len(context.tools)
            or name != context.tools[context.cursor]):
        raise ContinuationDenied('ordered_contract_changed')
    reused = context.cursor < len(context.prefix)
    expected = context.prefix[context.cursor] if reused else None
    if reused and digest(call.get('args', {})) != digest(expected['arguments']):
        raise ContinuationDenied('prefix_arguments_changed')
    contract = write_contract_for(context.step['service'], context.step['operation'])
    bound, _ = bind_ordered_output_lineage(contract, {'name': name, 'args': call.get('args', {})}, context.executions)
    if bound['args'] != call.get('args', {}):
        raise ContinuationDenied('output_lineage_changed')
    async with fenced_step_commit(pool, context.run, context.step, succeeded=None) as conn:
        live = await conn.fetchrow('''SELECT *,expires_at>clock_timestamp() AS fresh FROM tool_prefix_continuations
          WHERE id=$1''', context.permit['id'])
        run = dict(await conn.fetchrow('SELECT * FROM agent_runs WHERE id=$1', context.run['id']))
        step = dict(await conn.fetchrow('SELECT * FROM agent_run_steps WHERE id=$1', context.step['id']))
        owner = execution_authority.get()
        if (not live or not live['fresh'] or live['authority_hash'] != digest(owner.token)
                or await source_hash(conn, run, step) != live['source_hash']
                or not await current_authority(conn, run, step, run['user_id'])
                or not await _temporal_grant_current(conn, run, step, owner, live)):
            raise ContinuationDenied('continuation_authority_changed')
        if reused:
            receipts = await receipt_evidence(conn, run['id'], step['id'], run['user_id'])
            if receipts.blocked or receipts.digest != live['receipt_digest']:
                raise ContinuationDenied('prefix_evidence_changed')
            await append_event_conn(conn, run['id'], run['user_id'], 'verified_tool_result_reused',
                step_id=step['id'], phase='reconciliation', payload={'permit_id': str(live['id']),
                'tool': name, 'prefix_index': context.cursor, 'provider_called': False})
    # Advance before a fresh RPC: a failing call cannot be tried again in this
    # scope. A new manual recovery must reconcile its actual receipt instead.
    context.cursor += 1
    if reused:
        context.executions.append(expected)
    return reused, expected['result'] if reused else None


def after_tool(name, call, result):
    context = active_continuation.get()
    if context:
        context.executions.append({'tool': name, 'arguments': call.get('args', {}), 'result': result})


def require_complete_prefix():
    context = active_continuation.get()
    if context is not None and context.cursor != len(context.tools):
        raise ContinuationDenied('ordered_contract_not_completed')
