"""New Temporal activity authority for a proven, never-repeated write prefix."""
from uuid import UUID

from app.runs.prefix_continuation import current_authority, digest, ordered_prefix, source_hash
from app.runs.tool_receipts import receipt_evidence


async def preflight(conn, run, step, decision, source):
    if not await conn.fetchval("SELECT to_regclass('temporal_prefix_generations') IS NOT NULL"):
        return None, 'temporal_prefix_generation_required'
    from app.runs.temporal_read_retry import current_journal
    journal = await current_journal(conn, run['id'], step['id'], locked=True)
    if (journal is None or journal.get('execution_policy') != 'prefix-safe-v1' or journal['read_only']
            or not isinstance(decision.evidence.get('verification'), dict) or not decision.evidence['verification']):
        return None, 'temporal_prefix_provenance_required'
    if (journal['state'] == 'executing' or journal['completed_at'] is None or await conn.fetchval('''SELECT EXISTS(
      SELECT 1 FROM temporal_workspace_steps WHERE run_id=$1 AND step_id=$2
      AND (read_only OR execution_policy<>'prefix-safe-v1' OR state='executing' OR completed_at IS NULL))''',
      run['id'], step['id'])):
        return None, 'temporal_activity_quiescence_required'
    if journal['generation'] >= 3 or step['attempt_count'] != journal['generation']+1:
        return None, 'temporal_prefix_generation_budget_exhausted'
    if source is None:
        return None, 'temporal_prefix_readback_source_required'
    observed = await conn.fetchrow('''SELECT temporal_resolution_source($1,$2) AS digest,
      clock_timestamp()>=$3::timestamptz AND clock_timestamp()-$3::timestamptz<interval '5 minutes' AS fresh''',
      run['id'], step['id'], source.observed_at)
    if not observed['fresh'] or observed['digest'] != source.source_hash:
        return None, 'temporal_prefix_source_changed'
    return journal, None


async def register(conn, run, step, identity, journal, decision, source):
    await conn.execute('INSERT INTO workflow_event_tenants(user_id) VALUES($1) ON CONFLICT DO NOTHING', run['user_id'])
    tenant = await conn.fetchval('SELECT tenant_id FROM workflow_event_tenants WHERE user_id=$1', run['user_id'])
    await conn.execute('''INSERT INTO temporal_prefix_generations(id,run_id,step_id,run_parent_id,step_parent_id,
      actor_tenant_id,prefix_grant_id,generation,prior_token,workflow_execution_id,source_hash,verification_hash,
      verification_started_at,expires_at)
      SELECT $1,$2,$3,$2,$3,$4,$1,$5,$6,$7,temporal_resolution_source($2,$3),$8,$9::timestamptz,
        least(g.expires_at,$9::timestamptz+interval '15 minutes'-interval '1 second')
      FROM tool_prefix_continuations g WHERE g.id=$1''', identity, run['id'], step['id'], tenant,
      journal['generation']+1, journal['execution_token'], journal['workflow_execution_id'],
      digest(decision.evidence['verification']), source.observed_at)


async def next_prefix_reference(conn, row, journal):
    from app.runs.temporal_contracts import StepReference
    if not await conn.fetchval("SELECT to_regclass('temporal_prefix_generations') IS NOT NULL"):
        return StepReference(str(row['id']), False, journal['generation'], None)
    permit = await conn.fetchrow('''SELECT id,generation FROM temporal_prefix_generations WHERE run_id=$1
      AND step_id=$2 AND generation=$3 ORDER BY issued_at DESC,id DESC LIMIT 1''',
      row['run_id'], row['id'], journal['generation']+1)
    if permit:
        return StepReference(str(row['id']), False, permit['generation'], str(permit['id']))
    return StepReference(str(row['id']), False, journal['generation'],
        str(journal['prefix_permit_id']) if journal.get('prefix_permit_id') else None)


async def prefix_permit(conn, run, step, request, prior):
    from app.runs.temporal_read_retry import next_reference
    current = await next_reference(conn, step)
    if current.generation != request.generation or current.permit_id != request.permit_id:
        return None, 'superseded'
    if not await conn.fetchval("SELECT to_regclass('temporal_prefix_generations') IS NOT NULL"):
        return None, 'not_admitted'
    permit = await conn.fetchrow('''SELECT p.*,p.expires_at>clock_timestamp() AS fresh FROM temporal_prefix_generations p
      WHERE p.id=$1''', UUID(request.permit_id))
    grant = await conn.fetchrow('''SELECT *,expires_at>clock_timestamp() AS fresh FROM tool_prefix_continuations
      WHERE id=$1''', UUID(request.permit_id))
    receipts = await receipt_evidence(conn, run['id'], step['id'], run['user_id'])
    if (not permit or not grant or not permit['fresh'] or not grant['fresh'] or grant['consumed_at'] is not None
            or permit['prefix_grant_id'] != grant['id'] or permit['run_id'] != run['id'] or permit['step_id'] != step['id']
            or permit['source_hash'] != await conn.fetchval('SELECT temporal_resolution_source($1,$2)', run['id'], step['id'])
            or grant['source_hash'] != await source_hash(conn, run, step)
            or receipts.blocked or receipts.digest != grant['receipt_digest'] or not ordered_prefix(step, receipts.executions)
            or len(receipts.executions) != grant['prefix_length'] or grant['next_attempt'] != step['attempt_count']+1
            or step['read_only'] or prior['read_only'] or prior['execution_policy'] != 'prefix-safe-v1'
            or permit['prior_token'] != prior['execution_token'] or permit['generation'] != prior['generation']+1
            or prior['state'] == 'executing' or prior['completed_at'] is None
            or not await current_authority(conn, run, step, run['user_id'])):
        return None, 'not_admitted'
    return permit, None
