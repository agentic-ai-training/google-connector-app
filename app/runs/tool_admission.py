"""Live per-tool fencing, using trusted execution context rather than model state."""
import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from app.agents.errors import ExecutionFailure


@dataclass(frozen=True)
class ExecutionAuthority:
    owner: str
    run_id: UUID
    user_id: str
    executor_version: str
    token: str
    step_id: UUID | None = None
    workflow_execution_id: UUID | None = None
    deadline: datetime | None = None


execution_authority: ContextVar[ExecutionAuthority | None] = ContextVar('tool_execution_authority', default=None)


class ToolAdmissionDenied(ExecutionFailure):
    def __init__(self, reason: str):
        super().__init__('Tool execution is no longer admitted; no provider call was made.',
            category='cancelled' if reason == 'run_cancelled' else 'execution_fenced',
            component='tool_admission', boundary='pre_tool_live_ledger',
            evidence={'reason_code': reason, 'provider_called': False})


class CompletionFenced(ToolAdmissionDenied):
    def __init__(self, reason: str):
        ExecutionFailure.__init__(self, 'Completion is no longer admitted; earlier provider effects may require reconciliation.',
            category='cancelled' if reason == 'run_cancelled' else 'execution_fenced',
            component='completion_fence', boundary='completion_live_ledger',
            evidence={'reason_code': reason, 'prior_effects': 'requires_reconciliation'})


async def require_tool_admission(pool, state, *, tool_name=None) -> None:
    # The rollback /chat path has no durable identity. Do not reinterpret a missing
    # durable database as legacy authorization when a run ID IS present.
    if not state.get('run_id'):
        if execution_authority.get() is not None:
            raise ToolAdmissionDenied('durable_identity_missing')
        return
    authority = execution_authority.get()
    try:
        run_id, step_id = UUID(str(state['run_id'])), UUID(str(state['step_id']))
    except (ValueError, TypeError, KeyError):
        raise ToolAdmissionDenied('durable_identity_invalid') from None
    if (pool is None or authority is None or authority.run_id != run_id
            or authority.user_id != state.get('user_id')
            or (authority.step_id is not None and authority.step_id != step_id)
            or authority.owner not in {'postgres', 'temporal'}):
        raise ToolAdmissionDenied('execution_context_missing_or_mismatched')
    nonce = None
    if authority.owner == 'temporal':
        try:
            nonce = UUID(authority.token)
        except (ValueError, TypeError, AttributeError):
            raise ToolAdmissionDenied('temporal_authority_invalid') from None
        if (authority.step_id is None or not isinstance(authority.workflow_execution_id, UUID)
                or not isinstance(authority.deadline, datetime) or authority.deadline.utcoffset() is None):
            raise ToolAdmissionDenied('temporal_authority_invalid')
    if authority.deadline is not None:
        if not isinstance(authority.deadline, datetime) or authority.deadline.utcoffset() is None:
            raise ToolAdmissionDenied('activity_deadline_invalid')
        if authority.deadline <= datetime.now(timezone.utc):
            raise ToolAdmissionDenied('activity_deadline_expired')
    try:
        async with asyncio.timeout(5):
            async with pool.acquire(timeout=2) as conn:
                row = await conn.fetchrow('''SELECT r.status AS run_status,r.deleted_at,r.executor_version,
                  COALESCE(to_jsonb(r)->>'orchestration_owner','postgres') AS owner,
                  r.lease_owner,r.lease_expires_at>now() AS lease_current,s.status AS step_status,
                  (to_jsonb(s)->>'read_only')::boolean AS step_read_only
                  FROM agent_runs r JOIN agent_run_steps s ON s.run_id=r.id
                  WHERE r.id=$1 AND s.id=$2 AND r.user_id=$3''', run_id, step_id, authority.user_id, timeout=2)
                if row is None or row['deleted_at'] is not None:
                    raise ToolAdmissionDenied('run_or_step_not_visible')
                if row['run_status'] == 'cancelled':
                    raise ToolAdmissionDenied('run_cancelled')
                if (row['run_status'] != 'running' or row['step_status'] != 'running'
                        or row['owner'] != authority.owner or row['executor_version'] != authority.executor_version):
                    raise ToolAdmissionDenied('live_execution_binding_changed')
                if tool_name is not None and row['step_read_only']:
                    from app.tools.contracts import READ_TOOLS
                    if tool_name not in READ_TOOLS:
                        raise ExecutionFailure('A read-only step cannot invoke an unclassified or write tool.',
                            category='policy_violation', component='tool_admission', boundary='read_only_contract',
                            evidence={'provider_called': False, 'reason_code': 'read_only_tool_policy'})
                if authority.owner == 'postgres':
                    if not row['lease_current'] or row['lease_owner'] != authority.token:
                        raise ToolAdmissionDenied('postgres_lease_lost')
                else:
                    admitted = await conn.fetchval('''SELECT EXISTS(SELECT 1 FROM temporal_workspace_steps j
                      JOIN temporal_start_intents t ON t.run_id=j.run_id AND t.workflow_kind='workspace'
                      WHERE j.run_id=$1 AND j.step_id=$2 AND j.execution_token=$3
                        AND j.workflow_execution_id=$4 AND j.state='executing'
                        AND t.state='started' AND t.first_execution_run_id=$4)''',
                      run_id, step_id, nonce, authority.workflow_execution_id, timeout=2)
                    if not admitted:
                        raise ToolAdmissionDenied('temporal_activity_fenced')
    except ExecutionFailure:
        raise
    except Exception:
        raise ExecutionFailure('Live tool admission could not be verified; no provider call was made.',
            category='network', component='tool_admission', boundary='pre_tool_live_ledger',
            evidence={'reason_code': 'admission_database_unavailable', 'provider_called': False}) from None


class _ConnectionView:
    """Borrow a connection only for admission reads inside its owning transaction."""
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self, **kwargs):
        yield self.conn


def _run_scope(run):
    authority = execution_authority.get()
    if (authority is None or authority.run_id != run['id'] or authority.user_id != run['user_id']
            or authority.executor_version != run['executor_version']):
        raise CompletionFenced('completion_context_mismatched')
    return authority


@asynccontextmanager
async def fenced_postgres_commit(pool, run):
    """SQL-only critical section; never hold this context over provider I/O."""
    from app.runs.workflow_ownership import WorkflowOwnershipError, require_postgres_lease
    authority = _run_scope(run)
    if authority.owner != 'postgres' or authority.token != run['lease_owner']:
        raise CompletionFenced('postgres_completion_context_mismatched')
    async with pool.acquire(timeout=5) as conn, conn.transaction():
        await conn.execute("SET LOCAL lock_timeout='2s'; SET LOCAL statement_timeout='5s'")
        row = await conn.fetchrow('SELECT id,status FROM agent_runs WHERE id=$1 AND user_id=$2 FOR UPDATE',
            authority.run_id, authority.user_id)
        if row is not None and row['status'] == 'cancelled':
            raise CompletionFenced('run_cancelled')
        try:
            await require_postgres_lease(_ConnectionView(conn), 'workspace', run)
        except WorkflowOwnershipError:
            raise CompletionFenced('postgres_completion_fenced') from None
        yield conn


@asynccontextmanager
async def fenced_step_commit(pool, run, step, *, succeeded):
    """Lock run→step→journal; None admits metadata without finalizing the journal."""
    authority = _run_scope(run)
    state = {'run_id': str(run['id']), 'step_id': str(step['id']), 'user_id': run['user_id']}
    async with pool.acquire(timeout=5) as conn, conn.transaction():
        await conn.execute("SET LOCAL lock_timeout='2s'; SET LOCAL statement_timeout='5s'")
        if not await conn.fetchval('SELECT id FROM agent_runs WHERE id=$1 AND user_id=$2 FOR UPDATE',
                run['id'], run['user_id']):
            raise CompletionFenced('completion_run_not_visible')
        if not await conn.fetchval('SELECT id FROM agent_run_steps WHERE id=$1 AND run_id=$2 FOR UPDATE',
                step['id'], run['id']):
            raise CompletionFenced('completion_step_not_visible')
        if authority.owner == 'temporal':
            journal = await conn.fetchrow('''SELECT execution_token,state FROM temporal_workspace_steps
              WHERE run_id=$1 AND step_id=$2 AND execution_token::text=$3 FOR UPDATE''',
              run['id'], step['id'], authority.token)
            if journal is None or str(journal['execution_token']) != authority.token or journal['state'] != 'executing':
                raise CompletionFenced('temporal_completion_fenced')
        try:
            await require_tool_admission(_ConnectionView(conn), state)
        except ToolAdmissionDenied as error:
            raise CompletionFenced(error.evidence['reason_code']) from None
        except ExecutionFailure:
            raise ExecutionFailure('Completion admission could not be verified; earlier effects require reconciliation.',
                category='network', component='completion_fence', boundary='completion_live_ledger',
                evidence={'prior_effects': 'requires_reconciliation'}) from None
        yield conn
        if authority.owner == 'temporal' and succeeded is not None:
            result = await conn.execute('''UPDATE temporal_workspace_steps SET state=$1,completed_at=clock_timestamp(),
              error_category=CASE WHEN $1='failed' THEN 'execution' ELSE NULL END
              WHERE run_id=$2 AND step_id=$3 AND execution_token=$4 AND state='executing' ''',
              'succeeded' if succeeded else 'failed', run['id'], step['id'], UUID(authority.token))
            if result != 'UPDATE 1':
                raise CompletionFenced('temporal_completion_fenced')
