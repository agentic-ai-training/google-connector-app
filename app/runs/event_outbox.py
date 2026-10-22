"""Opt-in reference-only workflow delivery, never an execution authority.

PostgreSQL commits business event + immutable envelope + delivery intent together.
Delivery is at least once. Consumer SQL effects and deduplication share one local
transaction; this is NOT exactly-once execution of Google or deployment writes.
"""
import asyncio
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


MAX_WIRE_BYTES = 16384


class EventDeliveryError(RuntimeError):
    pass


class WorkflowEvent(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    schema_name: str = Field(alias='schema', pattern=r'^gca\.workflow\.event\.v1$')
    event_id: UUID
    tenant_id: UUID
    run_id: UUID
    sequence: int = Field(gt=0, strict=True)
    source_event_id: int = Field(gt=0, strict=True)
    event_type: str = Field(pattern=r'^[a-zA-Z][a-zA-Z0-9_.-]{0,95}$')
    phase: str | None = Field(max_length=96)
    created_at: datetime

    @field_validator('created_at')
    @classmethod
    def aware(cls, value):
        if value.utcoffset() is None:
            raise ValueError('workflow timestamps must be timezone-aware')
        return value


def decode(body: bytes) -> WorkflowEvent:
    if not isinstance(body, bytes) or not 1 <= len(body) <= MAX_WIRE_BYTES:
        raise EventDeliveryError('workflow event exceeds byte bound')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise EventDeliveryError('duplicate workflow event field')
            result[key] = value
        return result
    try:
        value = json.loads(body, object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        return WorkflowEvent.model_validate(value)
    except (ValueError, TypeError, RecursionError):
        raise EventDeliveryError('workflow event schema is invalid') from None


@dataclass(frozen=True)
class Publication:
    event_id: UUID
    lease_token: UUID
    body: bytes
    key: bytes


@dataclass(frozen=True)
class BrokerAcknowledgement:
    event_id: UUID
    topic: str
    partition: int
    offset: int


async def set_capture(pool, *, enabled: bool, evidence_reference: str):
    """Trusted operator only; no API route/model parameter grants this authority."""
    if type(enabled) is not bool or not isinstance(evidence_reference, str) or not 1 <= len(evidence_reference.encode()) <= 512:
        raise ValueError('explicit bounded capture evidence required')
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute('UPDATE workflow_delivery_control SET capture_enabled=$1 WHERE singleton', enabled)
        await conn.execute('INSERT INTO workflow_delivery_audit(enabled,evidence_reference) VALUES($1,$2)',
                           enabled, evidence_reference)


async def claim(pool, *, owner: str, batch_size: int = 8, lease_seconds: int = 60) -> list[Publication]:
    if (not isinstance(owner, str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,128}', owner)
            or type(batch_size) is not int or not 1 <= batch_size <= 16
            or type(lease_seconds) is not int or not 1 <= lease_seconds <= 120):
        raise ValueError('bounded publisher identity/batch/lease required')
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("""UPDATE workflow_event_outbox SET state='dead_letter',failure_category='lease_expired',
            lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL
            WHERE state='leased' AND lease_expires_at<=now() AND attempts>=12""")
        rows = await conn.fetch("""WITH ready AS (
            SELECT d.event_id FROM workflow_event_outbox d JOIN workflow_event_envelopes e USING(event_id)
            WHERE ((d.state='pending' AND d.available_at<=now()) OR
                   (d.state='leased' AND d.lease_expires_at<=now())) AND d.attempts<12
              AND NOT EXISTS(SELECT 1 FROM workflow_event_envelopes earlier
                JOIN workflow_event_outbox p ON p.event_id=earlier.event_id
                WHERE earlier.run_id=e.run_id AND earlier.sequence_no<e.sequence_no AND p.state<>'published')
            ORDER BY e.created_at,e.source_event_id FOR UPDATE OF d SKIP LOCKED LIMIT $1
          ), leased AS (
            UPDATE workflow_event_outbox d SET state='leased',attempts=attempts+1,
              lease_owner=$2,lease_token=gen_random_uuid(),lease_expires_at=now()+($3 * interval '1 second')
            FROM ready WHERE d.event_id=ready.event_id RETURNING d.event_id,d.lease_token)
          SELECT e.*,l.lease_token FROM leased l JOIN workflow_event_envelopes e USING(event_id)""",
                                batch_size, owner, lease_seconds)
    result = []
    for row in rows:
        body = bytes(row['wire_bytes'])
        event = decode(body)
        if hashlib.sha256(body).hexdigest() != row['content_sha256'] or event.event_id != row['event_id']:
            raise EventDeliveryError('retained event identity/hash differs')
        result.append(Publication(event.event_id, row['lease_token'], body,
                                  (str(event.tenant_id) + ':' + str(event.run_id)).encode()))
    return result


async def acknowledge(pool, publication: Publication, *, receipt: BrokerAcknowledgement) -> bool:
    if (not isinstance(receipt, BrokerAcknowledgement) or receipt.event_id != publication.event_id
            or receipt.topic != 'gca.workflow.events.v1' or type(receipt.partition) is not int
            or not 0 <= receipt.partition <= 2 or type(receipt.offset) is not int or receipt.offset < -1):
        raise EventDeliveryError('matching broker acknowledgement required')
    async with pool.acquire() as conn, conn.transaction():
        tenant = await conn.fetchval("""UPDATE workflow_event_outbox d SET state='published',published_at=now(),
            lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,failure_category=NULL,
            broker_partition=$3,broker_offset=$4
            FROM workflow_event_envelopes e WHERE d.event_id=e.event_id AND d.event_id=$1
              AND d.state='leased' AND d.lease_token=$2 AND d.lease_expires_at>now()
            RETURNING e.tenant_id""", publication.event_id, publication.lease_token, receipt.partition, receipt.offset)
        if tenant is None:
            return False
        await conn.execute('UPDATE workflow_delivery_control SET pending_count=pending_count-1 WHERE singleton')
        await conn.execute('UPDATE workflow_event_tenants SET pending_count=pending_count-1 WHERE tenant_id=$1', tenant)
        return True


async def failed(pool, publication: Publication, *, category: str = 'delivery_unconfirmed') -> bool:
    if category not in {'network', 'broker_unavailable', 'broker_rejected', 'serialization', 'lease_expired', 'delivery_unconfirmed'}:
        raise ValueError('sanitized delivery failure category required')
    async with pool.acquire() as conn:
        value = await conn.fetchval("""UPDATE workflow_event_outbox SET
            state=CASE WHEN attempts>=12 THEN 'dead_letter' ELSE 'pending' END,
            available_at=now()+(LEAST(3600,power(2,attempts)::integer) * interval '1 second'),
            lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,failure_category=$3
            WHERE event_id=$1 AND state='leased' AND lease_token=$2 AND lease_expires_at>now() RETURNING event_id""",
                                    publication.event_id, publication.lease_token, category)
    return value is not None


async def consume(pool, *, consumer_name: str, body: bytes, handler) -> bool:
    """Registered trusted SQL projection only; external side effects are forbidden.

    The original envelope must still exist and match exactly. Privacy deletion
    therefore invalidates old broker messages instead of recreating user content.
    """
    if not isinstance(consumer_name, str) or not re.fullmatch(r'[a-z][a-z0-9_.-]{0,63}', consumer_name):
        raise ValueError('registered consumer identity required')
    event = decode(body)
    async with pool.acquire() as conn, conn.transaction():
        expected = await conn.fetchval('SELECT e.wire_bytes FROM workflow_event_envelopes e JOIN agent_runs r ON r.id=e.run_id '
                                       'WHERE e.event_id=$1 AND e.tenant_id=$2 AND r.deleted_at IS NULL FOR SHARE OF e',
                                       event.event_id, event.tenant_id)
        if expected is None or bytes(expected) != body:
            raise EventDeliveryError('event is unknown, changed or deleted')
        inserted = await conn.fetchval("""INSERT INTO workflow_event_inbox(consumer_name,event_id,tenant_id)
            VALUES($1,$2,$3) ON CONFLICT DO NOTHING RETURNING event_id""", consumer_name, event.event_id, event.tenant_id)
        if inserted is None:
            return False
        await handler(conn, event)
        return True


async def publish_once(pool, producer, *, owner: str) -> dict:
    """Bounded delivery; producer.publish must wait for a real broker ACK."""
    publications = await claim(pool, owner=owner)
    gate = asyncio.Semaphore(4)
    async def one(publication):
        async with gate:
            try:
                async with asyncio.timeout(10):
                    receipt = await producer.publish(key=publication.key, body=publication.body)
                return 'published' if await acknowledge(pool, publication, receipt=receipt) else 'lease_lost'
            except asyncio.CancelledError:
                # Unknown in-flight ACK: retained lease expires; consumer dedup is mandatory.
                raise
            except Exception:
                await failed(pool, publication)
                return 'unconfirmed'
    outcomes = await asyncio.gather(*(one(item) for item in publications))
    return {key: outcomes.count(key) for key in ('published', 'lease_lost', 'unconfirmed')}


async def prune_published_in_transaction(conn, *, retention_days: int = 90) -> int:
    """Bounded metadata retention; pending/uncertain/dead-letter intent survives.

    Inbox cascades with the authoritative envelope. A later old broker message
    becomes unknown/rejected, never a new execution. User erasure is separate.
    """
    if not conn.is_in_transaction() or type(retention_days) is not int or not 1 <= retention_days <= 90:
        raise ValueError('transaction and approved workflow retention required')
    rows = await conn.fetch("""WITH expired AS (
        SELECT e.event_id FROM workflow_event_envelopes e JOIN workflow_event_outbox d USING(event_id)
        WHERE d.state='published' AND e.created_at<now()-($1 * interval '1 day')
        ORDER BY e.created_at FOR UPDATE OF e SKIP LOCKED LIMIT 500)
      DELETE FROM workflow_event_envelopes e USING expired
      WHERE e.event_id=expired.event_id RETURNING e.event_id""", retention_days)
    return len(rows)
