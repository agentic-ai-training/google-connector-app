"""Write-ahead private tool evidence; a receipt is not verification or approval."""
import asyncio
from dataclasses import dataclass
import hashlib
import json
import re
from uuid import UUID, uuid4

from app.agents.errors import ExecutionFailure
from app.config.settings import get_settings
from app.db.oauth_credentials import decrypt_private_payload, encrypt_private_payload
from app.runs.repository import append_event_conn
from app.runs.tool_admission import execution_authority, fenced_step_commit
from app.tools.contracts import WRITE_TOOLS


class ToolReceiptFailure(ExecutionFailure):
    def __init__(self, *, dispatched):
        super().__init__(
            'Tool invocation evidence requires reconciliation; automatic repetition is blocked.' if dispatched else
            'Tool invocation evidence could not be reserved; no provider call was made.',
            category='worker_reconciliation' if dispatched else 'network',
            component='tool_receipt', boundary='post_dispatch' if dispatched else 'pre_dispatch',
            evidence={'prior_effects': 'requires_reconciliation'} if dispatched else {'provider_called': False})


@dataclass(frozen=True)
class Receipt:
    identity: UUID
    run_id: UUID
    step_id: UUID
    user_id: str
    tool_name: str
    authority_hash: str
    arguments: dict


def _raw(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), default=str).encode()


def _payload(receipt, result=None, *, returned=False):
    value = {'id': str(receipt.identity), 'run_id': str(receipt.run_id), 'step_id': str(receipt.step_id),
             'user_id': receipt.user_id, 'tool': receipt.tool_name, 'arguments': receipt.arguments,
             'authority_hash': receipt.authority_hash}
    if returned:
        value['result'] = result
    raw = _raw(value)
    if len(raw) > min(get_settings().private_tool_result_max_bytes, 2_000_000):
        raise ToolReceiptFailure(dispatched=returned)
    return encrypt_private_payload(raw), hashlib.sha256(raw).hexdigest()


async def begin_receipt(pool, state, tool_name, call):
    if not state.get('run_id'):
        return None  # Existing non-durable rollback path, not new durable authority.
    authority = execution_authority.get()
    if authority is None:
        raise ToolReceiptFailure(dispatched=False)
    try:
        identity = uuid4()
        receipt = Receipt(identity, UUID(str(state['run_id'])), UUID(str(state['step_id'])),
            state['user_id'], tool_name, hashlib.sha256(authority.token.encode()).hexdigest(),
            json.loads(_raw(call.get('args', {}))))
        if (not re.fullmatch(r'[A-Za-z0-9_]{1,100}', tool_name) or not isinstance(receipt.arguments, dict)
                or not isinstance(call.get('id'), str) or not 1 <= len(call['id']) <= 256):
            raise ValueError('invalid invocation identity')
        encrypted, digest = _payload(receipt)
        run = {'id': receipt.run_id, 'user_id': receipt.user_id, 'executor_version': authority.executor_version,
               'lease_owner': authority.token}
        async with asyncio.timeout(5):
            # No RPC inside this lock-holding section. Do not finalize the step.
            async with fenced_step_commit(pool, run, {'id': receipt.step_id}, succeeded=None) as conn:
                step = await conn.fetchrow('SELECT attempt_count,read_only FROM agent_run_steps WHERE id=$1', receipt.step_id)
                sequence = await conn.fetchval('SELECT coalesce(max(sequence_no),0)+1 FROM agent_tool_receipts '
                    'WHERE run_id=$1 AND step_id=$2', receipt.run_id, receipt.step_id)
                await conn.execute('''INSERT INTO agent_tool_receipts(id,run_id,step_id,step_attempt,sequence_no,
                  tool_name,call_hash,authority_hash,read_only,encrypted_payload,payload_hash,expires_at)
                  VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,
                    clock_timestamp()+($12*interval '1 hour')-interval '1 second')''',
                  identity, receipt.run_id, receipt.step_id, step['attempt_count'], sequence, tool_name,
                  hashlib.sha256(call['id'].encode()).hexdigest(), receipt.authority_hash,
                  step['read_only'] and tool_name not in WRITE_TOOLS, encrypted, digest,
                  max(1, min(24, get_settings().private_tool_result_retention_hours)))
                await append_event_conn(conn, receipt.run_id, receipt.user_id, 'tool_receipt_dispatched',
                    step_id=receipt.step_id, phase='tool', payload={'receipt_id': str(identity), 'tool': tool_name})
        return receipt
    except ExecutionFailure:
        raise
    except Exception:
        raise ToolReceiptFailure(dispatched=False) from None


async def finish_receipt(pool, receipt, *, result=None, returned):
    if receipt is None:
        return
    try:
        encrypted, digest = _payload(receipt, result, returned=returned)
        async with asyncio.timeout(5):
            async with pool.acquire(timeout=2) as conn, conn.transaction():
                await conn.execute("SET LOCAL lock_timeout='2s'; SET LOCAL statement_timeout='3s'")
                # Preserve the run→receipt lock order used by admission/erasure.
                # Ownership may have changed; recording the old response is still
                # allowed, but erasure or tenant mismatch is not.
                if await conn.fetchval('SELECT id FROM agent_runs WHERE id=$1 AND user_id=$2 '
                        'AND deleted_at IS NULL FOR UPDATE', receipt.run_id, receipt.user_id) is None:
                    raise ToolReceiptFailure(dispatched=True)
                # Capture late evidence, not business success: no run/step state or
                # ownership is changed, and a completed receipt cannot be rewritten.
                row = await conn.fetchrow('''UPDATE agent_tool_receipts t SET state=$1,encrypted_payload=$2,
                  payload_hash=$3,completed_at=clock_timestamp() FROM agent_runs r
                  WHERE t.id=$4 AND t.run_id=$5 AND t.step_id=$6 AND t.authority_hash=$7
                    AND t.state='dispatched' AND t.expires_at>clock_timestamp()
                    AND r.id=t.run_id AND r.user_id=$8 AND r.deleted_at IS NULL RETURNING t.id''',
                  'returned' if returned else 'unknown', encrypted, digest, receipt.identity, receipt.run_id,
                  receipt.step_id, receipt.authority_hash, receipt.user_id)
                if row is None:
                    raise ToolReceiptFailure(dispatched=True)
                await append_event_conn(conn, receipt.run_id, receipt.user_id,
                    'tool_receipt_returned' if returned else 'tool_receipt_unknown', step_id=receipt.step_id,
                    phase='tool', payload={'receipt_id': str(receipt.identity), 'tool': receipt.tool_name})
    except Exception:
        raise ToolReceiptFailure(dispatched=True) from None


@dataclass(frozen=True)
class ReceiptEvidence:
    digest: str
    executions: list[dict]
    blocked: bool


async def receipt_evidence(conn, run_id, step_id, user_id):
    # A pre-031 deployment has no receipts, never retroactively invent them.
    if not await conn.fetchval("SELECT to_regclass('agent_tool_receipts') IS NOT NULL"):
        return ReceiptEvidence('', [], False)
    rows = await conn.fetch('''SELECT t.*,t.expires_at>clock_timestamp() AS fresh FROM agent_tool_receipts t
      JOIN agent_runs r ON r.id=t.run_id WHERE t.run_id=$1 AND t.step_id=$2
        AND r.user_id=$3 AND r.deleted_at IS NULL ORDER BY t.sequence_no LIMIT 65''', run_id, step_id, user_id)
    digest = hashlib.sha256(_raw([{key: row[key] for key in ('id','state','payload_hash','step_attempt','expires_at')}
        for row in rows])).hexdigest()
    executions, blocked = [], len(rows) > 64
    for row in rows:
        try:
            if row['state'] != 'returned' or not row['fresh'] or row['encrypted_payload'] is None:
                raise ValueError('unavailable result')
            raw = decrypt_private_payload(row['encrypted_payload'])
            value = json.loads(raw)
            expected = {'id': str(row['id']), 'run_id': str(run_id), 'step_id': str(step_id),
                        'user_id': user_id, 'tool': row['tool_name'], 'authority_hash': row['authority_hash']}
            if hashlib.sha256(raw).hexdigest() != row['payload_hash'] or any(value.get(k) != v for k,v in expected.items()):
                raise ValueError('binding changed')
            executions.append({'tool': value['tool'], 'arguments': value['arguments'], 'result': value['result']})
        except Exception:
            if not row['read_only']:
                blocked = True
    return ReceiptEvidence(digest, executions, blocked)
