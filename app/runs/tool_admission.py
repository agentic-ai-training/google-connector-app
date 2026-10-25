"""Live per-tool fencing, using trusted execution context rather than model state."""
import asyncio
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


async def require_tool_admission(pool, state) -> None:
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
                  r.lease_owner,r.lease_expires_at>now() AS lease_current,s.status AS step_status
                  FROM agent_runs r JOIN agent_run_steps s ON s.run_id=r.id
                  WHERE r.id=$1 AND s.id=$2 AND r.user_id=$3''', run_id, step_id, authority.user_id, timeout=2)
                if row is None or row['deleted_at'] is not None:
                    raise ToolAdmissionDenied('run_or_step_not_visible')
                if row['run_status'] == 'cancelled':
                    raise ToolAdmissionDenied('run_cancelled')
                if (row['run_status'] != 'running' or row['step_status'] != 'running'
                        or row['owner'] != authority.owner or row['executor_version'] != authority.executor_version):
                    raise ToolAdmissionDenied('live_execution_binding_changed')
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
    except ToolAdmissionDenied:
        raise
    except Exception:
        raise ExecutionFailure('Live tool admission could not be verified; no provider call was made.',
            category='network', component='tool_admission', boundary='pre_tool_live_ledger',
            evidence={'reason_code': 'admission_database_unavailable', 'provider_called': False}) from None
