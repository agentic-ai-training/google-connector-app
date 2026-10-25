"""Deliver refresh hints only; the workflow must reread private authorization."""
import asyncio
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID


class RefreshError(RuntimeError):
    def __init__(self, category):
        self.category = category if category in {'transport', 'binding', 'workflow_closed'} else 'transport'
        super().__init__('Temporal refresh ' + self.category)


@dataclass(frozen=True)
class RefreshIntent:
    run_id: UUID
    namespace: str
    workflow_id: str
    task_queue: str
    request_sha256: str
    first_execution_run_id: UUID
    generation: int
    lease_token: UUID


async def claim(pool, *, run_id=None):
    async with pool.acquire(timeout=5) as conn, conn.transaction():
        await conn.execute("SET LOCAL lock_timeout='2s'; SET LOCAL statement_timeout='5s'")
        if not await conn.fetchval('SELECT notifications_enabled FROM temporal_dispatch_control WHERE singleton FOR UPDATE'):
            return None
        row = await conn.fetchrow('''SELECT o.*,s.namespace,s.workflow_id,s.task_queue,s.request_sha256,s.first_execution_run_id
          FROM temporal_refresh_outbox o JOIN temporal_start_intents s ON s.run_id=o.run_id AND s.workflow_kind='workspace'
          WHERE o.desired_generation>o.acknowledged_generation AND NOT o.blocked AND o.available_at<=now()
            AND (o.lease_token IS NULL OR o.lease_expires_at<=now()) AND s.state='started'
            AND ($1::uuid IS NULL OR o.run_id=$1)
          ORDER BY o.updated_at FOR UPDATE OF o SKIP LOCKED LIMIT 1''', run_id)
        if not row:
            return None
        if row['attempt_count'] >= 10:
            await conn.execute("UPDATE temporal_refresh_outbox SET blocked=TRUE,error_category='retry_exhausted',"
                'lease_token=NULL,lease_expires_at=NULL,inflight_generation=NULL WHERE run_id=$1', row['run_id'])
            return None
        nonce = await conn.fetchval('''UPDATE temporal_refresh_outbox SET lease_token=gen_random_uuid(),
          lease_expires_at=now()+interval '60 seconds',inflight_generation=desired_generation,
          attempt_count=attempt_count+1,updated_at=now() WHERE run_id=$1 RETURNING lease_token''', row['run_id'])
        return RefreshIntent(row['run_id'], row['namespace'], row['workflow_id'], row['task_queue'], row['request_sha256'],
            row['first_execution_run_id'], row['desired_generation'], nonce)


async def acknowledge(pool, item):
    result = await asyncio.wait_for(pool.execute('''UPDATE temporal_refresh_outbox SET acknowledged_generation=$1,
      lease_token=NULL,lease_expires_at=NULL,inflight_generation=NULL,attempt_count=0,error_category=NULL,updated_at=now()
      WHERE run_id=$2 AND lease_token=$3 AND lease_expires_at>now() AND inflight_generation=$1''',
      item.generation, item.run_id, item.lease_token), timeout=5)
    return result == 'UPDATE 1'


async def fail(pool, item, category):
    category = category if category in {'transport', 'binding', 'workflow_closed'} else 'transport'
    await asyncio.wait_for(pool.execute('''UPDATE temporal_refresh_outbox SET blocked=$1,error_category=$2,
      lease_token=NULL,lease_expires_at=NULL,inflight_generation=NULL,
      attempt_count=CASE WHEN desired_generation>$5 THEN 0 ELSE attempt_count END,
      available_at=now()+interval '15 seconds',updated_at=now()
      WHERE run_id=$3 AND lease_token=$4 AND inflight_generation=$5''',
      category != 'transport', category, item.run_id, item.lease_token, item.generation), timeout=5)


async def deliver_one(pool, transport, *, run_id=None):
    """True means a delivery attempt, never approval or business completion."""
    item = await claim(pool, run_id=run_id)
    if item is None:
        return False
    try:
        async with asyncio.timeout(30):
            await transport.send_refresh(item)
        await acknowledge(pool, item)
    except asyncio.CancelledError:
        raise
    except RefreshError as error:
        await fail(pool, item, error.category)
    except Exception:
        await fail(pool, item, 'transport')
    return True


class TemporalRefreshTransport:
    def __init__(self, client):
        self.client = client

    async def send_refresh(self, item):
        from temporalio.client import WorkflowExecutionStatus
        from temporalio.service import RPCError
        if self.client.namespace != item.namespace:
            raise RefreshError('binding')
        handle = self.client.get_workflow_handle(item.workflow_id, first_execution_run_id=str(item.first_execution_run_id))
        try:
            description = await handle.describe(rpc_timeout=timedelta(seconds=10))
            memo = await description.memo()
            if (description.namespace != item.namespace or description.id != item.workflow_id
                    or description.workflow_type != 'gca.workspace.v1' or description.task_queue != item.task_queue
                    or description.raw_info.first_run_id != str(item.first_execution_run_id)
                    or memo.get('gca_dispatch_sha256') != item.request_sha256
                    or memo.get('gca_dispatch_contract') != 'gca.temporal.start.v1'):
                raise RefreshError('binding')
            if description.status != WorkflowExecutionStatus.RUNNING:
                raise RefreshError('workflow_closed')
            # No decision, request body, recipient or user identity is transmitted.
            await handle.signal('refresh', rpc_timeout=timedelta(seconds=10))
        except RPCError:
            raise RefreshError('transport') from None
