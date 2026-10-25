"""Internal readback-proof bridge, not a client-supplied success flag."""
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re
from urllib.parse import urlsplit

from app.runs.reconciliation import ReconciliationDecision


class ResolutionConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class ResolutionSnapshot:
    source_hash: str
    observed_at: datetime


def verified_receipt(step):
    from app.runs.worker import _verified_delivery_receipt
    from app.runs.verifier import extract_artifacts
    executions = (step.get('output_data') or {}).get('tool_executions') or []
    receipt = _verified_delivery_receipt(step, {'tool_executions': executions})
    if receipt:
        return receipt
    lines = [f"Reconciled and verified the requested {step['service']} {step['operation']} operation."]
    for artifact in extract_artifacts(executions)[:10]:
        identity = str(artifact.get('external_id') or '')
        link = str(artifact.get('url') or '')
        if step['service'] == 'sheets' and re.fullmatch(r'[A-Za-z0-9_-]{1,256}', identity):
            link = f'https://docs.google.com/spreadsheets/d/{identity}/edit'
        try:
            parsed = urlsplit(link)
            allowed = (parsed.scheme == 'https' and parsed.hostname in {
                'docs.google.com', 'drive.google.com', 'calendar.google.com', 'meet.google.com', 'chat.google.com'}
                and not parsed.username and not parsed.password)
        except ValueError:
            allowed = False
        lines.append('Verified resource: ' + (link if allowed else identity)[:500])
    return '\n'.join(lines)[:8000]


async def snapshot(conn, run, step):
    if run.get('orchestration_owner') != 'temporal':
        return None
    if not await conn.fetchval("SELECT to_regclass('temporal_step_resolutions') IS NOT NULL"):
        return None
    row = await conn.fetchrow('''SELECT temporal_resolution_source($1,$2) AS digest,
      clock_timestamp() AS observed_at''', run['id'], step['id'])
    return ResolutionSnapshot(row['digest'], row['observed_at']) if row['digest'] else None


async def bind_decision(conn, run, step, decision, source, actor):
    """Call only with the deterministic reconciler's result inside the resume tx."""
    if run.get('orchestration_owner') != 'temporal':
        return decision
    if not await conn.fetchval("SELECT to_regclass('temporal_workspace_steps') IS NOT NULL"):
        return ReconciliationDecision('manual_required', 'temporal_recovery_schema_required', None)
    journal = await conn.fetchrow('SELECT * FROM temporal_workspace_steps WHERE run_id=$1 AND step_id=$2 FOR UPDATE',
        run['id'], step['id'])
    if journal is None:
        return decision  # No activity ever crossed this step's write-ahead boundary.
    if decision.state == 'safe_to_retry':
        return ReconciliationDecision('manual_required', 'temporal_retry_generation_required', None,
            {**decision.evidence, 'journal_state': journal['state'], 'automatic_retry': False})
    if decision.state != 'already_completed':
        return decision
    if journal['state'] == 'executing' or journal['completed_at'] is None:
        return ReconciliationDecision('manual_required', 'temporal_activity_quiescence_required', None,
            {**decision.evidence, 'automatic_retry': False})
    if (source is None or actor != run['user_id'] or decision.reason_code != 'postconditions_already_satisfied'
            or not isinstance(decision.evidence.get('verification'), dict) or not decision.evidence['verification']):
        raise ResolutionConflict('Fresh deterministic readback evidence is required')
    from app.runs.schemas import ExecutionPlan
    try:
        plan = ExecutionPlan.model_validate(run['plan'])
        expected = next(item for item in plan.steps if item.id == step['step_key'])
    except (ValueError, TypeError, KeyError, StopIteration):
        raise ResolutionConflict('Current typed plan is required') from None
    if (expected.service != step['service'] or expected.operation != step['operation']
            or expected.read_only != step['read_only'] or expected.dependencies != step['dependencies']
            or expected.requires_approval != step['requires_approval'] or expected.risk_level != step['risk_level']
            or expected.arguments != step['input_data']):
        raise ResolutionConflict('Step no longer matches the current typed plan')
    semantic = (step.get('input_data') or {}).get('semantic_authorization') or {}
    if not isinstance(semantic, dict):
        raise ResolutionConflict('Current semantic authority is invalid')
    if semantic.get('authorized') is False:
        return ReconciliationDecision('manual_required', 'unintended_write_requires_review', None, decision.evidence)
    if step['requires_approval']:
        from app.runs.planner import action_hash
        authorized = await conn.fetchval('''SELECT EXISTS(SELECT 1 FROM run_approvals
          WHERE run_id=$1 AND requested_from=$2 AND status='approved' AND action_hash=$3
            AND decided_at<=$4)''', run['id'], actor, action_hash(plan), journal['started_at'])
        if not authorized:
            return ReconciliationDecision('manual_required', 'temporal_approval_evidence_required', None, decision.evidence)
    current = await conn.fetchrow('''SELECT temporal_resolution_source($1,$2) AS digest,
      (clock_timestamp()>=$3::timestamptz AND clock_timestamp()-$3::timestamptz<interval '5 minutes') AS fresh''',
      run['id'], step['id'], source.observed_at)
    if current['digest'] != source.source_hash or not current['fresh']:
        raise ResolutionConflict('Reconciliation source changed or observation expired')
    proof = hashlib.sha256(json.dumps(decision.evidence['verification'], sort_keys=True,
        separators=(',', ':'), default=str).encode()).hexdigest()
    output = dict(step.get('output_data') or {})
    output.update(output=verified_receipt(step), verification='Deterministic readback reconciled required write postconditions')
    await conn.execute('UPDATE agent_run_steps SET output_data=$2::jsonb WHERE id=$1', step['id'], json.dumps(output, default=str))
    resolved_hash = await conn.fetchval('SELECT temporal_resolution_source($1,$2)', run['id'], step['id'])
    await conn.execute('INSERT INTO workflow_event_tenants(user_id) VALUES($1) ON CONFLICT DO NOTHING', actor)
    tenant = await conn.fetchval('SELECT tenant_id FROM workflow_event_tenants WHERE user_id=$1', actor)
    await conn.execute('''INSERT INTO temporal_step_resolutions(run_id,step_id,execution_token,source_hash,
      verification_hash,actor_tenant_id,verification_started_at,expires_at)
      VALUES($1,$2,$3,$4,$5,$6,$7::timestamptz,$7::timestamptz+interval '15 minutes') ON CONFLICT DO NOTHING''',
      run['id'], step['id'], journal['execution_token'], resolved_hash, proof, tenant, source.observed_at)
    return decision
