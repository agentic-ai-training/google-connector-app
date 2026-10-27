"""Trusted Workspace activity bridge to the existing executor and private ledger."""
import asyncio
from contextlib import suppress
import json
from datetime import timedelta
from uuid import UUID

from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.runs.temporal_contracts import FinalizeRequest, RunSnapshot, StepReference, StepRequest, WorkflowReference
from app.runs.tool_admission import ExecutionAuthority, execution_authority
from app.runs.temporal_read_retry import current_journal, next_reference, retry_permit


def _json_row(row):
    value = dict(row)
    for key in ('plan', 'input_data', 'output_data', 'result', 'incident_summary'):
        if isinstance(value.get(key), str):
            value[key] = json.loads(value[key])
    return value


class WorkspaceActivities:
    def __init__(self, pool, app, *, namespace: str, task_queue: str, workflow_version: str, executor_version: str):
        self.pool, self.app = pool, app
        self.namespace, self.task_queue = namespace, task_queue
        self.workflow_version, self.executor_version = workflow_version, executor_version

    async def _bound(self, conn, reference: WorkflowReference, *, locked=False):
        reference.validate()
        info = activity.info()
        identity = UUID(reference.run_id)
        if (info.workflow_namespace != self.namespace or info.task_queue != self.task_queue
                or info.workflow_type != 'gca.workspace.v1' or info.workflow_id != 'gca.workspace.' + reference.run_id
                or reference.workflow_version != self.workflow_version or reference.executor_version != self.executor_version):
            raise ApplicationError('Workflow activity binding rejected', type='WorkflowBinding', non_retryable=True)
        query = 'SELECT * FROM agent_runs WHERE id=$1 FOR UPDATE' if locked else 'SELECT * FROM agent_runs WHERE id=$1'
        row = await conn.fetchrow(query, identity)
        if row is None or row['deleted_at'] is not None:
            return None, False
        if (row['orchestration_owner'] != 'temporal' or row['temporal_namespace'] != self.namespace
                or row['temporal_task_queue'] != self.task_queue or row['executor_version'] != self.executor_version
                or row['temporal_workflow_version'] != self.workflow_version or row['temporal_workflow_id'] != info.workflow_id):
            raise ApplicationError('Workflow ledger binding rejected', type='WorkflowBinding', non_retryable=True)
        first = await conn.fetchval('''SELECT first_execution_run_id FROM temporal_start_intents
          WHERE workflow_kind='workspace' AND run_id=$1 AND state='started' ''', identity)
        ready = first is not None and str(first) == info.workflow_run_id
        if first is not None and not ready:
            raise ApplicationError('Workflow execution lineage rejected', type='WorkflowBinding', non_retryable=True)
        return _json_row(row), ready

    @activity.defn(name='gca.workspace.inspect')
    async def inspect(self, reference: WorkflowReference) -> RunSnapshot:
        async with self.pool.acquire() as conn:
            run, receipt = await self._bound(conn, reference)
            if run is None:
                return RunSnapshot('deleted')
            if run['status'] in {'completed', 'partial', 'failed', 'cancelled', 'awaiting_approval', 'awaiting_clarification'}:
                if run['status'] in {'completed', 'partial', 'failed', 'cancelled'}:
                    await self._terminal_facts(run)
                return RunSnapshot(run['status'])
            if not receipt:
                return RunSnapshot('awaiting_start_receipt')
            rows = await conn.fetch('''SELECT candidate.id,candidate.run_id,candidate.read_only FROM agent_run_steps candidate
              WHERE candidate.run_id=$1 AND candidate.status='pending'
              AND NOT EXISTS(SELECT 1 FROM unnest(candidate.dependencies) dependency
                LEFT JOIN agent_run_steps required ON required.run_id=candidate.run_id AND required.step_key=dependency
                WHERE required.id IS NULL OR required.status<>'completed')
              ORDER BY sequence_no LIMIT 4''', UUID(reference.run_id))
            return RunSnapshot('running', [await next_reference(conn, row) for row in rows])

    async def _heartbeat(self, reference, token, step_id, stop):
        while not stop.is_set():
            activity.heartbeat({'step_id': step_id})
            await self.pool.execute('''UPDATE agent_runs SET heartbeat_at=now() WHERE id=$1 AND deleted_at IS NULL
              AND EXISTS(SELECT 1 FROM temporal_workspace_steps WHERE run_id=$1 AND step_id=$2
                AND execution_token=$3 AND state='executing')''', UUID(reference.run_id), UUID(step_id), token)
            try:
                await asyncio.wait_for(stop.wait(), timeout=10)
            except asyncio.TimeoutError:
                pass

    async def _terminal_facts(self, run):
        from app.evaluation.collector import record_run_evaluation
        await record_run_evaluation(self.pool, run['id'])
        if run['status'] in {'failed', 'partial'}:
            from app.improvements.failure_intelligence import record_failure_incident
            await record_failure_incident(self.pool, occurrence_key=f"run:{run['id']}:terminal",
                run_id=run['id'], session_id=run['session_id'], user_id=run['user_id'], message=run['request'],
                intent_kind=run.get('intent_kind') or 'workspace_action', stage='execution',
                category=run.get('error_category') or 'execution', component='temporal_workspace_activity',
                error=run.get('error_message') or 'Required steps did not all complete',
                completion={key: float(run[key]) for key in
                    ('technical_completion', 'functional_completion', 'user_visible_completion', 'side_effect_integrity')},
                evidence={'boundary': 'temporal_step_journal', 'workflow_version': self.workflow_version},
                policy=run.get('plan') or {})

    @activity.defn(name='gca.workspace.step')
    async def execute_step(self, request: StepRequest) -> str:
        from app.runs import worker
        from app.db.oauth_credentials import load_google_credentials
        from app.db.google_clients import request_google_credentials
        try:
            request.validate()
        except (ValueError, TypeError, AttributeError):
            raise ApplicationError('Invalid step generation reference', type='WorkflowInput', non_retryable=True) from None
        identity, step_id = UUID(request.run.run_id), UUID(request.step_id)
        info = activity.info()
        if info.activity_id != request.activity_id:
            raise ApplicationError('Step activity identity mismatch', type='WorkflowBinding', non_retryable=True)
        async with self.pool.acquire() as conn, conn.transaction():
            run, receipt = await self._bound(conn, request.run, locked=True)
            if run is None or run['status'] == 'cancelled':
                return 'cancelled'
            if not receipt or run['status'] not in {'queued', 'running'}:
                return 'not_admitted'
            step = await conn.fetchrow('SELECT * FROM agent_run_steps WHERE id=$1 AND run_id=$2 FOR UPDATE', step_id, identity)
            if step is None:
                return 'deleted'
            prior = await current_journal(conn, identity, step_id, locked=True)
            permit = None
            if prior:
                generation = prior.get('generation', 0)
                if request.generation < generation:
                    return 'superseded'
                if request.generation == generation and generation > 0:
                    admitted_permit = prior.get('prefix_permit_id') or prior.get('retry_permit_id')
                    if request.permit_id != str(admitted_permit):
                        # Renewal can replace a permit before its generation is
                        # admitted. The old same-number activity must not mark
                        # its replacement executing journal uncertain.
                        return 'superseded'
                if request.generation > generation:
                    if request.generation != generation + 1 or step['status'] != 'pending':
                        return 'not_admitted'
                    if step['read_only']:
                        permit, rejected = await retry_permit(conn, run, _json_row(step), request, prior)
                    else:
                        from app.runs.temporal_prefix_retry import prefix_permit
                        permit, rejected = await prefix_permit(conn, run, _json_row(step), request, prior)
                    if rejected:
                        if rejected != 'superseded':
                            await conn.execute("UPDATE agent_run_steps SET status='failed',error_category='worker_reconciliation',"
                                "error_message='Recovery permit expired or source changed; explicit review required' WHERE id=$1", step_id)
                        return rejected
                elif step['status'] == 'pending' and (await next_reference(conn, step)).generation > generation:
                    return 'superseded'
            elif request.generation != 0:
                return 'not_admitted'
            if prior and permit is None:
                if (step['status'] == 'completed'
                        and await conn.fetchval("SELECT to_regclass('temporal_step_resolutions') IS NOT NULL")
                        and await conn.fetchval('SELECT temporal_step_resolved($1,$2)', identity, step_id)):
                    return 'succeeded'
                if prior['state'] == 'executing':
                    await conn.execute("UPDATE temporal_workspace_steps SET state='uncertain',error_category='worker_reconciliation' "
                        'WHERE run_id=$1 AND step_id=$2 AND execution_token=$3', identity, step_id, prior['execution_token'])
                    return 'uncertain'
                if prior['state'] != 'succeeded' or step['status'] != 'completed':
                    await conn.execute("UPDATE agent_run_steps SET status='failed',error_category='worker_reconciliation',"
                        "error_message='Prior activity requires explicit reconciliation before another execution' "
                        'WHERE id=$1 AND status<>\'completed\'', step_id)
                return prior['state']
            if step['status'] == 'completed':
                return 'succeeded'
            if step['status'] != 'pending':
                return 'not_admitted'
            blocked = await conn.fetchval('''SELECT EXISTS(SELECT 1 FROM unnest($2::text[]) d
              LEFT JOIN agent_run_steps s ON s.run_id=$1 AND s.step_key=d
              WHERE s.id IS NULL OR s.status<>'completed')''', identity, step['dependencies'])
            if blocked:
                return 'not_admitted'
            if step['requires_approval'] and not run.get('approval_bypassed'):
                from app.runs.planner import action_hash
                from app.runs.schemas import ExecutionPlan
                try:
                    digest = action_hash(ExecutionPlan.model_validate(run['plan']))
                except (ValueError, TypeError):
                    raise ApplicationError('Current typed plan is invalid', type='WorkflowPlan', non_retryable=True) from None
                approved = await conn.fetchval('''SELECT EXISTS(SELECT 1 FROM run_approvals
                  WHERE run_id=$1 AND requested_from=$2 AND status='approved' AND action_hash=$3
                    AND expires_at>clock_timestamp())''',
                  identity, run['user_id'], digest)
                if not approved:
                    await conn.execute("UPDATE agent_run_steps SET status='failed',error_category='policy_violation',"
                        "error_message='Current plan approval evidence is required before this step' WHERE id=$1", step_id)
                    return 'not_admitted'
            token = await conn.fetchval('''INSERT INTO temporal_workspace_steps
              (run_id,step_id,run_parent_id,step_parent_id,workflow_execution_id,activity_id,read_only,
               execution_token,generation,execution_policy,retry_permit_id,prefix_permit_id)
              VALUES($1,$2,$1,$2,$3,$4,$5,coalesce($6,gen_random_uuid()),$7,$8,$9,$10) RETURNING execution_token''',
              identity, step_id, UUID(info.workflow_run_id), info.activity_id, step['read_only'],
              permit['execution_token'] if permit else None, request.generation,
              'read-safe-v1' if step['read_only'] else 'prefix-safe-v1',
              UUID(request.permit_id) if permit and step['read_only'] else None,
              UUID(request.permit_id) if permit and not step['read_only'] else None)
            claimed = await conn.fetchrow('''UPDATE agent_run_steps SET status='running',attempt_count=attempt_count+1,
              started_at=now() WHERE id=$1 RETURNING *''', step_id)
            await conn.execute("UPDATE agent_runs SET status='running',current_phase='execution',started_at=COALESCE(started_at,now()),"
                'heartbeat_at=now(),current_step_id=$2 WHERE id=$1', identity, step_id)
            step = _json_row(claimed)
            dependencies = await worker._dependency_context(conn, step)
        stop = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat(request.run, token, request.step_id, stop))
        credential_token = None
        authority_token = None
        outcome = 'uncertain'
        try:
            # Repeat the live binding check immediately before credentials, not
            # merely when the workflow was first started.
            async with self.pool.acquire() as conn:
                current, receipt = await self._bound(conn, request.run)
            if current is None or not receipt or current['status'] == 'cancelled':
                return 'cancelled'
            deadline = info.started_time + min(info.start_to_close_timeout or timedelta(minutes=5), timedelta(minutes=5))
            authority_token = execution_authority.set(ExecutionAuthority('temporal', identity, run['user_id'],
                self.executor_version, str(token), step_id, UUID(info.workflow_run_id), deadline))
            if step.get('operation') not in {'answer_information', 'answer_workspace_chat'}:
                credentials = await load_google_credentials(self.pool, run['user_id'])
                if credentials is None:
                    raise RuntimeError('Google connection required')
                credential_token = request_google_credentials.set(credentials)
            await worker._execute_step(self.app, self.pool, run, step, dependencies)
            outcome = 'succeeded'
        except asyncio.CancelledError:
            raise
        except Exception:
            # Existing verified failures retain their safe details in the private
            # business step. Unknown outcomes never become a blind write retry.
            state = await self.pool.fetchval('SELECT status FROM agent_run_steps WHERE id=$1', step_id)
            outcome = 'failed' if state == 'failed' else 'uncertain'
        finally:
            if authority_token is not None:
                execution_authority.reset(authority_token)
            if credential_token is not None:
                request_google_credentials.reset(credential_token)
            stop.set()
            heartbeat.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await heartbeat
            async def finish():
                async with self.pool.acquire() as conn, conn.transaction():
                    await conn.execute('''UPDATE temporal_workspace_steps SET state=$1,completed_at=now(),
                      error_category=CASE WHEN $1='uncertain' THEN 'worker_reconciliation'
                        WHEN $1='failed' THEN 'execution' ELSE NULL END
                      WHERE run_id=$2 AND step_id=$3 AND execution_token=$4 AND state='executing' ''',
                      outcome, identity, step_id, token)
                    await conn.execute('''UPDATE temporal_workspace_steps SET completed_at=clock_timestamp()
                      WHERE run_id=$1 AND step_id=$2 AND execution_token=$3 AND state='uncertain' AND completed_at IS NULL''',
                      identity, step_id, token)
            await asyncio.shield(asyncio.wait_for(finish(), timeout=5))
        recorded = await self.pool.fetchval('SELECT state FROM temporal_workspace_steps WHERE run_id=$1 AND step_id=$2 '
            'AND execution_token=$3', identity, step_id, token)
        return recorded or 'uncertain'

    @activity.defn(name='gca.workspace.finalize')
    async def finalize(self, request: FinalizeRequest) -> str:
        from app.runs.incident import completion_from_steps, build_incident
        identity = UUID(request.run.run_id)
        async with self.pool.acquire() as conn, conn.transaction():
            run, _ = await self._bound(conn, request.run, locked=True)
            if run is None:
                return 'deleted'
            if run['status'] == 'completed':
                return 'completed'
            rows = await conn.fetch('SELECT * FROM agent_run_steps WHERE run_id=$1 ORDER BY sequence_no FOR UPDATE', identity)
            steps = [_json_row(row) for row in rows]
            resolution_schema = await conn.fetchval("SELECT to_regclass('temporal_step_resolutions') IS NOT NULL")
            unresolved = ("AND NOT temporal_step_resolved(run_id,step_id)" if resolution_schema else '')
            if await conn.fetchval("SELECT to_regclass('temporal_read_retry_permits') IS NOT NULL"):
                unresolved += ' AND NOT temporal_read_superseded(run_id,step_id,execution_token)'
            if await conn.fetchval("SELECT to_regclass('temporal_prefix_generations') IS NOT NULL"):
                unresolved += ' AND NOT temporal_prefix_superseded(run_id,step_id,execution_token)'
            uncertain = await conn.fetchval(("SELECT EXISTS(SELECT 1 FROM temporal_workspace_steps WHERE run_id=$1 "
                "AND state IN ('executing','uncertain') __RESOLUTION__) ").replace('__RESOLUTION__', unresolved), identity)
            if await conn.fetchval("SELECT to_regclass('agent_tool_receipts') IS NOT NULL"):
                uncertain = uncertain or await conn.fetchval('''SELECT EXISTS(SELECT 1 FROM agent_tool_receipts
                  WHERE run_id=$1 AND NOT read_only AND state IN ('dispatched','unknown'))''', identity)
            if uncertain:
                await conn.execute("UPDATE temporal_workspace_steps SET state='uncertain',error_category='worker_reconciliation' "
                    "WHERE run_id=$1 AND state='executing'", identity)
            effective = []
            for step in steps:
                if step['status'] == 'running':
                    step = {**step, 'status': 'failed', 'error_category': 'worker_reconciliation',
                            'error_message': 'Activity outcome needs reconciliation; automatic repetition blocked'}
                    await conn.execute("UPDATE agent_run_steps SET status='failed',error_category='worker_reconciliation',"
                        'error_message=$2,completed_at=now() WHERE id=$1', step['id'], step['error_message'])
                effective.append(step)
            completion = completion_from_steps(effective)
            complete = bool(effective) and all(s['status'] == 'completed' for s in effective) and not uncertain
            status = ('cancelled' if request.reason == 'cancelled' or run['status'] == 'cancelled' else
                      'completed' if complete else 'partial' if any(s['status'] == 'completed' for s in effective) else 'failed')
            if uncertain:
                completion['side_effect_integrity'] = 0
                completion['functional_completion'] = min(99, completion['functional_completion'])
                completion['user_visible_completion'] = min(99, completion['user_visible_completion'])
            output = '\n\n'.join(str(s.get('output_data', {}).get('output') or '')
                for s in effective if s['status'] == 'completed')
            incident = {} if complete else build_incident(effective, 'worker_reconciliation' if uncertain else 'execution',
                'Activity outcome needs reconciliation' if uncertain else 'Required steps did not all complete')
            usage = await conn.fetchrow('''SELECT COALESCE(array_agg(DISTINCT model) FILTER(WHERE model IS NOT NULL),'{}') AS models,
              COALESCE(sum(input_tokens),0) AS inputs,COALESCE(sum(output_tokens),0) AS outputs
              FROM agent_model_calls WHERE run_id=$1''', identity)
            await conn.execute('''UPDATE agent_runs SET status=$1,current_phase=$1,result=$2::jsonb,incident_summary=$3::jsonb,
              technical_completion=$4,functional_completion=$5,user_visible_completion=$6,side_effect_integrity=$7,
              completed_at=now(),heartbeat_at=now(),current_step_id=NULL,models_used=$8,input_tokens=$9,output_tokens=$10,
              error_category=$11,error_message=$12 WHERE id=$13''', status, json.dumps({'output': output}), json.dumps(incident),
              completion['technical_completion'], completion['functional_completion'], completion['user_visible_completion'],
              completion['side_effect_integrity'], usage['models'], usage['inputs'], usage['outputs'],
              None if complete else 'worker_reconciliation' if uncertain else 'execution',
              None if complete else 'Required steps did not all complete; inspect the private run ledger', identity)
            await conn.execute('''INSERT INTO agent_run_events(run_id,user_id,event_type,phase,message,payload)
              VALUES($1,$2,$3,$4,'Temporal business ledger finalization',$5::jsonb)''',
              identity, run['user_id'], 'run_' + status, status, json.dumps({'task_complete': status == 'completed'}))
        current = await self.pool.fetchrow('SELECT * FROM agent_runs WHERE id=$1', identity)
        if current is not None and current['deleted_at'] is None:
            await self._terminal_facts(_json_row(current))
        return status
