"""Bounded Temporal start controller; no prompts, Google calls or run execution."""
import asyncio
from dataclasses import dataclass
from datetime import timedelta
import hashlib
import json
from typing import Protocol
from uuid import UUID

from app.runs.workflow_ownership import workflow_id


class DispatchError(RuntimeError):
    def __init__(self, category: str):
        if category not in {'transport', 'binding', 'not_admissible'}:
            category = 'binding'
        self.category = category
        super().__init__('Temporal start ' + category)


@dataclass(frozen=True)
class StartIntent:
    kind: str
    run_id: UUID
    namespace: str
    workflow_id: str
    task_queue: str
    workflow_version: str
    executor_version: str
    body: bytes
    sha256: str
    lease_token: UUID
    allow_start: bool

    def payload(self) -> dict:
        if not 1 <= len(self.body) <= 2048 or hashlib.sha256(self.body).hexdigest() != self.sha256:
            raise DispatchError('binding')
        expected = {'schema': 'gca.temporal.start.v1', 'kind': self.kind, 'run_id': str(self.run_id),
                    'workflow_version': self.workflow_version, 'executor_version': self.executor_version}
        try:
            value = json.loads(self.body)
        except (ValueError, UnicodeError):
            raise DispatchError('binding') from None
        if value != expected or self.workflow_id != workflow_id(self.kind, self.run_id):
            raise DispatchError('binding')
        return value


@dataclass(frozen=True)
class StartReceipt:
    namespace: str
    workflow_id: str
    first_execution_run_id: UUID
    request_sha256: str


class StartTransport(Protocol):
    async def ensure_started(self, item: StartIntent) -> StartReceipt: ...


async def claim(pool) -> StartIntent | None:
    async with pool.acquire(timeout=5) as conn, conn.transaction():
        await conn.execute("SET LOCAL lock_timeout='2s'; SET LOCAL statement_timeout='5s'")
        enabled = await conn.fetchval('SELECT enabled FROM temporal_dispatch_control WHERE singleton FOR UPDATE')
        row = await conn.fetchrow('''SELECT * FROM temporal_start_intents
          WHERE available_at<=now() AND (state IN ('pending','uncertain') OR
            (state='claimed' AND lease_expires_at<=now())) AND ($1 OR may_have_started)
          ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1''', bool(enabled))
        if not row:
            return None
        if row['attempt_count'] >= 10:
            await conn.execute('''UPDATE temporal_start_intents SET state='blocked',lease_token=NULL,
              lease_expires_at=NULL,last_error_category='retry_exhausted',updated_at=now()
              WHERE workflow_kind=$1 AND run_id=$2''', row['workflow_kind'], row['run_id'])
            return None
        parent_queries = {
            'workspace': 'SELECT * FROM agent_runs WHERE id=$1 FOR SHARE',
            'coding': 'SELECT * FROM coding_runs WHERE id=$1 FOR SHARE',
        }
        parent = await conn.fetchrow(parent_queries[row['workflow_kind']], row['run_id'])
        live = bool(parent and parent['orchestration_owner'] == 'temporal' and parent['deleted_at'] is None
                    and parent['status'] == 'queued' and parent['executor_version'] == row['executor_version']
                    and parent['temporal_namespace'] == row['namespace']
                    and parent['temporal_workflow_id'] == row['workflow_id']
                    and parent['temporal_task_queue'] == row['task_queue']
                    and parent['temporal_workflow_version'] == row['workflow_version'])
        # Write ahead of every possible RPC. Lease loss never permits new identity.
        claimed = await conn.fetchrow('''UPDATE temporal_start_intents SET state='claimed',
          may_have_started=TRUE,attempt_count=attempt_count+1,lease_token=gen_random_uuid(),
          lease_expires_at=now()+interval '90 seconds',updated_at=now()
          WHERE workflow_kind=$1 AND run_id=$2 RETURNING *,start_deadline>now() AS within_deadline''',
          row['workflow_kind'], row['run_id'])
        return StartIntent(row['workflow_kind'], row['run_id'], row['namespace'], row['workflow_id'],
            row['task_queue'], row['workflow_version'], row['executor_version'], bytes(row['request_bytes']),
            row['request_sha256'], claimed['lease_token'], bool(enabled and live and claimed['within_deadline']))


async def acknowledge(pool, item: StartIntent, receipt: StartReceipt) -> bool:
    if (receipt.namespace != item.namespace or receipt.workflow_id != item.workflow_id
            or receipt.request_sha256 != item.sha256 or not isinstance(receipt.first_execution_run_id, UUID)):
        raise DispatchError('binding')
    result = await pool.execute('''UPDATE temporal_start_intents SET state='started',first_execution_run_id=$1,
      lease_token=NULL,lease_expires_at=NULL,last_error_category=NULL,updated_at=now()
      WHERE workflow_kind=$2 AND run_id=$3 AND state='claimed' AND lease_token=$4 AND lease_expires_at>now()''',
      receipt.first_execution_run_id, item.kind, item.run_id, item.lease_token)
    return result == 'UPDATE 1'


async def fail(pool, item: StartIntent, category: str) -> None:
    if category not in {'transport', 'binding', 'not_admissible'}:
        category = 'transport'
    await pool.execute('''UPDATE temporal_start_intents SET state=$1,lease_token=NULL,lease_expires_at=NULL,
      last_error_category=$2,available_at=now()+interval '30 seconds',updated_at=now()
      WHERE workflow_kind=$3 AND run_id=$4 AND state='claimed' AND lease_token=$5''',
      'uncertain' if category == 'transport' else 'blocked', category, item.kind, item.run_id, item.lease_token)


async def dispatch_one(pool, transport: StartTransport) -> bool:
    """Return whether an intent was attempted, NOT whether business work completed."""
    item = await claim(pool)
    if item is None:
        return False
    try:
        item.payload()
        async with asyncio.timeout(45):
            receipt = await transport.ensure_started(item)
        await acknowledge(pool, item, receipt)
    except asyncio.CancelledError:
        # Do not convert cancellation to retry-success; write-ahead uncertainty and
        # lease expiry allow the next controller to reconcile this SAME identity.
        raise
    except DispatchError as error:
        await fail(pool, item, error.category)
    except Exception:
        await fail(pool, item, 'transport')
    return True


class TemporalStartTransport:
    """Real optional SDK adapter; caller supplies an explicitly configured client."""
    def __init__(self, client):
        self.client = client

    async def ensure_started(self, item: StartIntent) -> StartReceipt:
        from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
        from temporalio.exceptions import WorkflowAlreadyStartedError
        from temporalio.service import RPCError, RPCStatusCode
        if self.client.namespace != item.namespace:
            raise DispatchError('binding')
        payload = item.payload()
        kind_names = {'workspace': 'gca.workspace.v1', 'coding': 'gca.coding.v1'}
        if item.kind not in kind_names:
            raise DispatchError('binding')
        handle = self.client.get_workflow_handle(item.workflow_id)
        try:
            description = await handle.describe(rpc_timeout=timedelta(seconds=10))
        except RPCError as error:
            if error.status != RPCStatusCode.NOT_FOUND:
                raise DispatchError('transport') from None
            if not item.allow_start:
                raise DispatchError('not_admissible') from None
            try:
                await self.client.start_workflow(kind_names[item.kind], payload,
                    id=item.workflow_id, task_queue=item.task_queue,
                    id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                    id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
                    execution_timeout=timedelta(hours=24), task_timeout=timedelta(seconds=10),
                    memo={'gca_dispatch_sha256': item.sha256, 'gca_dispatch_contract': 'gca.temporal.start.v1'},
                    rpc_timeout=timedelta(seconds=10))
            except WorkflowAlreadyStartedError:
                # Concurrent start / lost ACK: verify real identity instead of reuse.
                pass
            except Exception:
                raise DispatchError('transport') from None
            description = await handle.describe(rpc_timeout=timedelta(seconds=10))
        memo = await description.memo()
        if (description.namespace != item.namespace or description.id != item.workflow_id
                or description.task_queue != item.task_queue or description.workflow_type != kind_names[item.kind]
                or memo.get('gca_dispatch_sha256') != item.sha256
                or memo.get('gca_dispatch_contract') != 'gca.temporal.start.v1'):
            raise DispatchError('binding')
        try:
            first = UUID(description.raw_info.first_run_id)
        except (ValueError, AttributeError, TypeError):
            raise DispatchError('binding') from None
        return StartReceipt(item.namespace, item.workflow_id, first, item.sha256)
