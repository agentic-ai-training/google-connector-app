"""Private frozen publication intent and late outcomes; never approval or success.

An unacknowledged dispatch is not permission to repeat a GitHub mutation. A later
owner may read back external state but must still hold current run authority.
"""
import asyncio
import hashlib
import json

from app.coding.execution_fence import SOURCE_FIELDS, coding_commit
from app.coding.repository import append_coding_event
from app.db.oauth_credentials import decrypt_private_payload, encrypt_private_payload


class PublicationReconciliationRequired(RuntimeError):
    def __init__(self):
        super().__init__('GitHub publication requires read-only reconciliation; automatic repetition is blocked.')


def raw(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), default=str).encode()


def source_hash(row):
    # A recovered lease has a new attempt/owner, not a different approved source.
    fields = {key: row.get(key) for key in SOURCE_FIELDS if key != 'attempt_count'}
    return hashlib.sha256(raw(fields)).hexdigest()


def decode(value, digest):
    if not value:
        raise PublicationReconciliationRequired()
    data = decrypt_private_payload(value)
    if hashlib.sha256(data).hexdigest() != digest:
        raise PublicationReconciliationRequired()
    return json.loads(data)


async def load(pool, row):
    async with coding_commit(pool, row) as conn:
        receipt = await conn.fetchrow('''SELECT *,expires_at>clock_timestamp() AS live
          FROM coding_publication_receipts WHERE run_id=$1''', row['id'])
    if receipt is None:
        return None
    if receipt['user_id'] != row['user_id'] or receipt['source_hash'] != source_hash(row) or not receipt['live']:
        raise PublicationReconciliationRequired()
    manifest = decode(receipt['encrypted_manifest'], receipt['manifest_hash'])
    result = decode(receipt['encrypted_result'], receipt['result_hash']) if receipt['state'] == 'returned' else None
    return receipt['state'], manifest, result


async def prepare(pool, row, manifest):
    data = raw(manifest)
    if len(data) > 8_388_608:
        raise PublicationReconciliationRequired()
    async with coding_commit(pool, row) as conn:
        await conn.execute('''INSERT INTO coding_publication_receipts
          (run_id,user_id,source_hash,manifest_hash,encrypted_manifest,authority_hash)
          VALUES($1,$2,$3,$4,$5,$6)''', row['id'], row['user_id'], source_hash(row),
          hashlib.sha256(data).hexdigest(), encrypt_private_payload(data),
          hashlib.sha256(row['lease_owner'].encode()).hexdigest())
        await append_coding_event(conn, run_id=str(row['id']), user_id=row['user_id'],
            event_type='coding_publication_prepared', phase='publication',
            message='Validated files are frozen in a private publication intent; no GitHub write is proven',
            payload={'manifest_hash': hashlib.sha256(data).hexdigest(), 'files': len(manifest['files'])})


async def dispatch(pool, row):
    async with coding_commit(pool, row) as conn:
        receipt = await conn.fetchval('''UPDATE coding_publication_receipts SET state='dispatched',dispatched_at=clock_timestamp()
          WHERE run_id=$1 AND user_id=$2 AND source_hash=$3 AND state='prepared'
            AND expires_at>clock_timestamp() AND encrypted_manifest IS NOT NULL RETURNING run_id''',
          row['id'], row['user_id'], source_hash(row))
        if receipt is None:
            raise PublicationReconciliationRequired()
        await append_coding_event(conn, run_id=str(row['id']), user_id=row['user_id'],
            event_type='coding_publication_dispatched', phase='publication',
            message='Write-ahead publication dispatch recorded; external outcome is not yet verified')


async def returned(pool, row, result):
    """Preserve a late acknowledgement without overwriting cancellation/replacement.

    Run lock follows erasure/admission order. Hard deletion, soft erasure, changed
    source or expired evidence prohibit resurrection. No run outcome is changed.
    """
    data = raw(result)
    if len(data) > 8000:
        raise PublicationReconciliationRequired()
    async with asyncio.timeout(5), pool.acquire(timeout=2) as conn, conn.transaction():
        await conn.execute("SET LOCAL lock_timeout='2s'; SET LOCAL statement_timeout='3s'")
        current = await conn.fetchrow('SELECT * FROM coding_runs WHERE id=$1 AND user_id=$2 FOR UPDATE',
            row['id'], row['user_id'])
        if current is None or current['deleted_at'] is not None or source_hash(current) != source_hash(row):
            raise PublicationReconciliationRequired()
        receipt = await conn.fetchval('''UPDATE coding_publication_receipts SET state='returned',
          encrypted_result=$1,result_hash=$2,returned_at=clock_timestamp()
          WHERE run_id=$3 AND user_id=$4 AND source_hash=$5 AND state='dispatched'
            AND expires_at>clock_timestamp() AND encrypted_manifest IS NOT NULL RETURNING run_id''',
          encrypt_private_payload(data), hashlib.sha256(data).hexdigest(), row['id'], row['user_id'], source_hash(row))
        if receipt is None:
            raise PublicationReconciliationRequired()
        await append_coding_event(conn, run_id=str(row['id']), user_id=row['user_id'],
            event_type='coding_publication_acknowledged', phase='publication',
            message='External publication response retained privately; this acknowledgement is not run completion')
