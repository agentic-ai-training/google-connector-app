"""One registered, SQL-only consumer. No model-selected handlers or external writes."""
import hashlib

from app.runs.event_outbox import EventDeliveryError, consume, decode


CONSUMER_NAME = 'workflow_projection_v1'
CONSUMER_GROUP = 'gca-workflow-projection-v1'
FAILURE_CATEGORIES = {'record_invalid', 'key_mismatch', 'source_unavailable',
                      'projection_failed', 'commit_unconfirmed', 'assignment_lost'}


class ProjectionDeliveryError(EventDeliveryError):
    def __init__(self, category):
        if category not in FAILURE_CATEGORIES:
            raise ValueError('closed consumer failure category required')
        self.category = category
        super().__init__(category)


async def project(conn, event):
    """Derived event visibility only; never set run completion or execute a tool."""
    if not conn.is_in_transaction():
        raise ValueError('projection requires the inbox transaction')
    await conn.execute('''INSERT INTO workflow_run_event_projection(
        run_id,tenant_id,last_event_id,last_sequence,last_event_type,last_phase)
        VALUES($1,$2,$3,$4,$5,$6)
        ON CONFLICT(run_id) DO UPDATE SET
          observed_event_count=workflow_run_event_projection.observed_event_count+1,
          last_event_id=CASE WHEN EXCLUDED.last_sequence>workflow_run_event_projection.last_sequence
            THEN EXCLUDED.last_event_id ELSE workflow_run_event_projection.last_event_id END,
          last_event_type=CASE WHEN EXCLUDED.last_sequence>workflow_run_event_projection.last_sequence
            THEN EXCLUDED.last_event_type ELSE workflow_run_event_projection.last_event_type END,
          last_phase=CASE WHEN EXCLUDED.last_sequence>workflow_run_event_projection.last_sequence
            THEN EXCLUDED.last_phase ELSE workflow_run_event_projection.last_phase END,
          last_sequence=GREATEST(EXCLUDED.last_sequence,workflow_run_event_projection.last_sequence),
          observed_at=now()
        WHERE workflow_run_event_projection.tenant_id=EXCLUDED.tenant_id''',
        event.run_id, event.tenant_id, event.event_id, event.sequence, event.event_type, event.phase)


async def apply(pool, *, key: bytes, body: bytes) -> str:
    """Return only after the SQL transaction commits; erased bytes never reappear."""
    try:
        event = decode(body)
    except EventDeliveryError:
        raise ProjectionDeliveryError('record_invalid') from None
    expected = (str(event.tenant_id) + ':' + str(event.run_id)).encode()
    if key != expected:
        raise ProjectionDeliveryError('key_mismatch')
    try:
        inserted = await consume(pool, consumer_name=CONSUMER_NAME, body=body, handler=project)
        return 'applied' if inserted else 'duplicate'
    except EventDeliveryError:
        # Exact digest evidence is written atomically BEFORE authoritative erasure.
        # Unknown or modified messages cannot masquerade as an erased event.
        async with pool.acquire() as conn:
            erased = await conn.fetchval('''SELECT EXISTS(SELECT 1 FROM workflow_event_revocations
                WHERE content_sha256=$1 AND expires_at>now())''', hashlib.sha256(body).hexdigest())
        if erased:
            return 'erased'
        raise ProjectionDeliveryError('source_unavailable') from None


async def record_failure(pool, *, partition: int, offset: int, body, category: str):
    """Bounded sanitized evidence, never a license to advance an unsafe offset."""
    if (type(partition) is not int or not 0 <= partition <= 2 or type(offset) is not int
            or offset < 0 or category not in FAILURE_CATEGORIES):
        raise ValueError('valid fixed-topic consumer failure identity required')
    digest = hashlib.sha256(body).hexdigest() if isinstance(body, bytes) and len(body) <= 24576 else None
    async with pool.acquire() as conn, conn.transaction():
        await conn.fetchval('SELECT singleton FROM workflow_consumer_control WHERE singleton FOR UPDATE')
        count = await conn.fetchval('SELECT count(*) FROM workflow_consumer_failures')
        existing = await conn.fetchval('''SELECT 1 FROM workflow_consumer_failures
            WHERE topic='gca.workflow.events.v1' AND partition_id=$1 AND broker_offset=$2''', partition, offset)
        if count >= 10000 and not existing:
            raise EventDeliveryError('consumer diagnostic capacity exhausted')
        await conn.execute('''INSERT INTO workflow_consumer_failures(topic,partition_id,broker_offset,content_sha256,category)
            VALUES('gca.workflow.events.v1',$1,$2,$3,$4)
            ON CONFLICT(topic,partition_id,broker_offset) DO UPDATE SET
              attempts=LEAST(3,workflow_consumer_failures.attempts+1),
              category=EXCLUDED.category,last_seen_at=now()''', partition, offset, digest, category)


async def prune_diagnostics(conn) -> dict:
    if not conn.is_in_transaction():
        raise ValueError('diagnostic retention requires a transaction')
    counts = {}
    for name, sql in (
        ('workflow_revocations', '''WITH expired AS (SELECT content_sha256 FROM workflow_event_revocations
            WHERE expires_at<now() ORDER BY expires_at FOR UPDATE SKIP LOCKED LIMIT 500)
            DELETE FROM workflow_event_revocations r USING expired e WHERE r.content_sha256=e.content_sha256
            RETURNING r.content_sha256'''),
        ('workflow_consumer_failures', '''WITH expired AS (SELECT topic,partition_id,broker_offset FROM workflow_consumer_failures
            WHERE last_seen_at<now()-interval '7 days' ORDER BY last_seen_at FOR UPDATE SKIP LOCKED LIMIT 500)
            DELETE FROM workflow_consumer_failures f USING expired e WHERE
              (f.topic,f.partition_id,f.broker_offset)=(e.topic,e.partition_id,e.broker_offset) RETURNING f.partition_id'''),
    ):
        counts[name] = len(await conn.fetch(sql))
    return counts
