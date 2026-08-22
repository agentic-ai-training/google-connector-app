"""Semantic, tenant-scoped cache policy for coding-agent observations.

Cacheability follows the entity's correctness boundary. A cached observation can reduce
work, but it can never prove a live write, service health, approval, CI result, or current
deployment state.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.db.oauth_credentials import decrypt_private_payload, encrypt_private_payload


@dataclass(frozen=True)
class CacheDecision:
    cacheable: bool
    ttl_seconds: int
    invalidation: str
    reason: str
    may_satisfy_live_postcondition: bool = False


POLICIES = {
    "immutable_source": CacheDecision(
        True, 90 * 86400, "content_hash_changed", "immutable source is content-addressed",
    ),
    "repository_inventory": CacheDecision(
        True, 86400, "commit_or_worktree_identity_changed", "inventory is commit-scoped",
    ),
    "source_excerpt": CacheDecision(
        True, 7 * 86400, "file_hash_changed", "excerpt is keyed by the complete file hash",
    ),
    "database_schema": CacheDecision(
        True, 300, "migration_revision_changed", "schema is revision-scoped and short-lived",
    ),
    "validation_evidence": CacheDecision(
        True, 90 * 86400, "tree_or_toolchain_changed", "validation is immutable for one tree and toolchain",
    ),
    "okf_bundle": CacheDecision(
        True, 30 * 86400, "trusted_bundle_hash_changed", "trusted OKF is version-addressed",
    ),
    "process_health": CacheDecision(
        True, 5, "time_or_process_identity_changed", "health is momentary and never completion proof",
    ),
    "log_tail": CacheDecision(
        True, 5, "file_size_or_mtime_changed", "log tails are momentary observations",
    ),
    "deployment_status": CacheDecision(
        True, 5, "deployment_id_or_time_changed", "deployment status is live external state",
    ),
    "secret": CacheDecision(False, 0, "never", "credentials and tokens are never cached"),
    "raw_private_content": CacheDecision(False, 0, "never", "raw private content is never cached here"),
    "write_result": CacheDecision(False, 0, "never", "write completion requires authoritative verification"),
    "approval": CacheDecision(False, 0, "never", "human decisions are durable records, not cache entries"),
}


def select_cache_policy(entity_type: str) -> CacheDecision:
    return POLICIES.get(
        entity_type,
        CacheDecision(False, 0, "unclassified", "unclassified entities fail closed"),
    )


def _cache_key(entity_type: str, scope_key: str, source_version: str) -> str:
    value = f"{entity_type}\0{scope_key}\0{source_version}".encode()
    return hashlib.sha256(value).hexdigest()


async def put_cache_entry(
    pool, *, user_id: str, entity_type: str, scope_key: str,
    producer_version: str, source_version: str, payload: Any,
) -> dict | None:
    decision = select_cache_policy(entity_type)
    if not decision.cacheable:
        return None
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    content_hash = hashlib.sha256(raw).hexdigest()
    key = _cache_key(entity_type, scope_key, source_version)
    expires = datetime.now(timezone.utc) + timedelta(seconds=decision.ttl_seconds)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO coding_cache_entries
               (user_id,entity_type,scope_key,cache_key,producer_version,source_version,
                content_hash,encrypted_payload,policy_reason,expires_at)
               VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
               ON CONFLICT(user_id,entity_type,scope_key,cache_key) DO UPDATE SET
                 producer_version=excluded.producer_version,
                 content_hash=excluded.content_hash,
                 encrypted_payload=excluded.encrypted_payload,
                 policy_reason=excluded.policy_reason,expires_at=excluded.expires_at,
                 invalidated_at=NULL,last_accessed_at=now(),access_count=0
               RETURNING id,entity_type,scope_key,cache_key,producer_version,source_version,
                         content_hash,policy_reason,expires_at""",
            user_id, entity_type, scope_key, key, producer_version, source_version,
            content_hash, encrypt_private_payload(raw), decision.reason, expires,
        )
    return dict(row)


async def get_cache_entry(
    pool, *, user_id: str, entity_type: str, scope_key: str, source_version: str,
) -> Any | None:
    decision = select_cache_policy(entity_type)
    if not decision.cacheable:
        return None
    key = _cache_key(entity_type, scope_key, source_version)
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            """SELECT id,encrypted_payload FROM coding_cache_entries
               WHERE user_id=$1 AND entity_type=$2 AND scope_key=$3 AND cache_key=$4
                 AND invalidated_at IS NULL AND expires_at>now() FOR UPDATE""",
            user_id, entity_type, scope_key, key,
        )
        if not row:
            return None
        await conn.execute(
            "UPDATE coding_cache_entries SET last_accessed_at=now(),access_count=access_count+1 WHERE id=$1",
            row["id"],
        )
    return json.loads(decrypt_private_payload(row["encrypted_payload"]))


async def invalidate_cache_scope(
    pool, *, user_id: str, entity_type: str, scope_key: str,
) -> int:
    async with pool.acquire() as conn:
        status = await conn.execute(
            """UPDATE coding_cache_entries SET invalidated_at=now()
               WHERE user_id=$1 AND entity_type=$2 AND scope_key=$3
                 AND invalidated_at IS NULL""",
            user_id, entity_type, scope_key,
        )
    return int(status.rsplit(" ", 1)[-1])
