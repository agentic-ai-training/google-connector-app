"""Authenticated, source-bound permission for a new read-only attempt, never a write."""
from uuid import UUID

from app.runs.reconciliation import ReconciliationDecision
from app.runs.repository import append_event_conn
from app.runs.schemas import ExecutionPlan
from app.runs.temporal_contracts import StepReference
from app.tools.contracts import READ_TOOLS, WRITE_TOOLS


def _denied(reason, decision):
    return ReconciliationDecision('manual_required', reason, None, {**decision.evidence, 'automatic_retry': False})


async def current_journal(conn, run_id, step_id, *, locked=False):
    # Generation zero remains compatible with pre-032 readback schemas.
    return await conn.fetchrow('''SELECT * FROM temporal_workspace_steps j WHERE run_id=$1 AND step_id=$2
      ORDER BY coalesce((to_jsonb(j)->>'generation')::integer,0) DESC LIMIT 1''' + (' FOR UPDATE' if locked else ''),
      run_id, step_id)


def matching_read_plan(run, step):
    try:
        plan = ExecutionPlan.model_validate(run['plan'])
        expected = next(item for item in plan.steps if item.id == step['step_key'])
    except (ValueError, TypeError, KeyError, StopIteration):
        return False
    return (expected.read_only and step['read_only'] and expected.service == step['service']
        and expected.operation == step['operation'] and expected.dependencies == step['dependencies']
        and expected.requires_approval == step['requires_approval'] and expected.risk_level == step['risk_level']
        and expected.arguments == step['input_data'] and expected.preconditions == step['preconditions']
        and expected.postconditions == step['postconditions'])


async def issue_read_retry(conn, run, step, decision, source, actor, journal):
    if not step['read_only']:
        return _denied('temporal_retry_generation_required', decision)
    if (not await conn.fetchval("SELECT to_regclass('temporal_read_retry_permits') IS NOT NULL")
            or journal.get('execution_policy') != 'read-safe-v1'):
        return _denied('temporal_read_retry_provenance_required', decision)
    if source is None or actor != run['user_id'] or decision.reason_code != 'read_only_operation':
        return _denied('temporal_read_retry_authority_required', decision)
    if not matching_read_plan(run, step):
        return _denied('temporal_read_retry_plan_changed', decision)
    allowed = step['input_data'].get('allowed_tools') or []
    if (not isinstance(allowed, list) or any(not isinstance(name, str) or name not in READ_TOOLS for name in allowed)
            or (not allowed and step['operation'] not in {'answer_information','answer_workspace_chat','compose'})):
        return _denied('temporal_read_retry_tool_policy_required', decision)
    if (journal['state'] == 'executing' or journal['completed_at'] is None
            or await conn.fetchval('''SELECT EXISTS(SELECT 1 FROM temporal_workspace_steps
              WHERE run_id=$1 AND step_id=$2 AND (NOT read_only OR state='executing' OR completed_at IS NULL))''',
              run['id'], step['id'])):
        return _denied('temporal_activity_quiescence_required', decision)
    if (await conn.fetchval('''SELECT EXISTS(SELECT 1 FROM agent_tool_receipts
          WHERE run_id=$1 AND step_id=$2 AND NOT read_only)''', run['id'], step['id'])
            or await conn.fetchval('''SELECT EXISTS(SELECT 1 FROM agent_tool_attempts
              WHERE run_id=$1 AND step_id=$2 AND tool_name=ANY($3::text[]))''', run['id'], step['id'], sorted(WRITE_TOOLS))
            or any(item.get('tool') in WRITE_TOOLS for item in (step.get('output_data') or {}).get('tool_executions', []))):
        return _denied('temporal_read_retry_write_evidence', decision)
    if step['attempt_count'] >= min(step['max_attempts'], 4) or journal['generation'] >= 3:
        return _denied('temporal_read_retry_budget_exhausted', decision)
    if step.get('error_category') in {'policy_violation','planning','authentication','permission'}:
        return _denied('temporal_read_retry_external_or_policy_gate', decision)
    from app.runs.temporal_resolution import ResolutionConflict
    observed = await conn.fetchrow('''SELECT temporal_resolution_source($1,$2) AS digest,
      (clock_timestamp()>=$3::timestamptz AND clock_timestamp()-$3::timestamptz<interval '5 minutes') AS fresh''',
      run['id'], step['id'], source.observed_at)
    if observed['digest'] != source.source_hash or not observed['fresh']:
        raise ResolutionConflict('Read retry source changed or observation expired')
    if step['requires_approval'] and not run.get('approval_bypassed'):
        from app.runs.planner import action_hash
        plan = ExecutionPlan.model_validate(run['plan'])
        if not await conn.fetchval('''SELECT EXISTS(SELECT 1 FROM run_approvals WHERE run_id=$1
          AND requested_from=$2 AND status='approved' AND action_hash=$3 AND expires_at>clock_timestamp())''',
          run['id'], actor, action_hash(plan)):
            return _denied('temporal_read_retry_approval_required', decision)
    prior = await conn.fetchrow('''SELECT * FROM temporal_read_retry_permits WHERE run_id=$1 AND step_id=$2
      AND generation=$3 ORDER BY issued_at DESC,id DESC LIMIT 1''', run['id'], step['id'], journal['generation']+1)
    if (prior and prior['source_hash'] == source.source_hash and await conn.fetchval(
            'SELECT $1::timestamptz>clock_timestamp()', prior['expires_at']) and await conn.fetchval(
            'SELECT EXISTS(SELECT 1 FROM workflow_event_tenants WHERE tenant_id=$1 AND user_id=$2)',
            prior['actor_tenant_id'], actor)):
        return decision
    if await conn.fetchval('SELECT count(*) FROM temporal_read_retry_permits WHERE run_id=$1 AND step_id=$2',
            run['id'], step['id']) >= 8:
        return _denied('temporal_read_retry_permit_limit', decision)
    await conn.execute('INSERT INTO workflow_event_tenants(user_id) VALUES($1) ON CONFLICT DO NOTHING', actor)
    tenant = await conn.fetchval('SELECT tenant_id FROM workflow_event_tenants WHERE user_id=$1', actor)
    permit = await conn.fetchval('''INSERT INTO temporal_read_retry_permits(run_id,step_id,run_parent_id,step_parent_id,
      actor_tenant_id,generation,prior_token,source_hash,expires_at)
      VALUES($1,$2,$1,$2,$3,$4,$5,$6,clock_timestamp()+interval '15 minutes'-interval '1 second') RETURNING id''',
      run['id'], step['id'], tenant, journal['generation']+1, journal['execution_token'], source.source_hash)
    await append_event_conn(conn, run['id'], actor, 'read_retry_authorized', step_id=step['id'], phase='recovery',
        payload={'generation': journal['generation']+1, 'permit_id': str(permit), 'write_retry': False})
    return decision


async def next_reference(conn, row):
    identity, step_id = row['run_id'], row['id']
    if not await conn.fetchval("SELECT to_regclass('temporal_read_retry_permits') IS NOT NULL"):
        return StepReference(str(step_id), row['read_only'])
    journal = await current_journal(conn, identity, step_id)
    if not journal:
        return StepReference(str(step_id), row['read_only'])
    if not row['read_only']:
        from app.runs.temporal_prefix_retry import next_prefix_reference
        return await next_prefix_reference(conn, row, journal)
    permit = await conn.fetchrow('''SELECT id,generation FROM temporal_read_retry_permits WHERE run_id=$1
      AND step_id=$2 AND generation=$3 ORDER BY issued_at DESC,id DESC LIMIT 1''', identity, step_id, journal['generation']+1)
    if permit:
        return StepReference(str(step_id), row['read_only'], permit['generation'], str(permit['id']))
    return StepReference(str(step_id), row['read_only'], journal['generation'],
        str(journal['retry_permit_id']) if journal['retry_permit_id'] else None)


async def retry_permit(conn, run, step, request, prior):
    """Read under run/step locks, before the journal INSERT's independent SQL guard."""
    current = await next_reference(conn, step)
    if current.generation != request.generation or current.permit_id != request.permit_id:
        return None, 'superseded'
    permit = await conn.fetchrow('SELECT *,expires_at>clock_timestamp() AS fresh FROM temporal_read_retry_permits WHERE id=$1',
        UUID(request.permit_id))
    if (not permit or not permit['fresh'] or not matching_read_plan(run, step)
            or permit['source_hash'] != await conn.fetchval('SELECT temporal_resolution_source($1,$2)', run['id'], step['id'])
            or step['attempt_count'] >= min(step['max_attempts'],4) or not step['read_only']
            or prior['state'] == 'executing' or prior['completed_at'] is None):
        return None, 'not_admitted'
    return permit, None
