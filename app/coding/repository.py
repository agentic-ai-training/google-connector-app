"""Tenant-scoped persistence for durable hosted coding runs."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

from app.config.settings import get_settings
from app.db.oauth_credentials import decrypt_private_payload, encrypt_private_payload


class CodingRunConflict(RuntimeError):
    """The requested state transition is no longer valid."""


_EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_SECRET = re.compile(
    r"(?i)(api[_ -]?key|authorization|password|private[_ -]?key|refresh[_ -]?token)"
    r"\s*[:=]\s*\S+"
)


def sanitized_excerpt(value: str, limit: int = 240) -> str:
    compact = " ".join(value.split())
    compact = _EMAIL.sub("[email]", compact)
    compact = _SECRET.sub(r"\1=[redacted]", compact)
    return compact[:limit]


def encrypt_json(value: Any) -> str:
    payload = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    return encrypt_private_payload(payload)


def decrypt_json(value: str) -> Any:
    return json.loads(decrypt_private_payload(value))


def public_run(row) -> dict:
    result = dict(row)
    for key in (
        "encrypted_request",
        "encrypted_plan",
        "encrypted_approval_manifest",
        "lease_owner",
    ):
        result.pop(key, None)
    return result


async def append_coding_event(
    conn, *, run_id: str, user_id: str, event_type: str, phase: str,
    message: str = "", payload: dict | None = None,
) -> None:
    await conn.execute(
        """INSERT INTO coding_run_events
           (run_id,user_id,event_type,phase,message,payload)
           VALUES($1,$2,$3,$4,$5,$6::jsonb)""",
        run_id, user_id, event_type, phase, message,
        json.dumps(payload or {}, default=str),
    )


async def create_coding_run(
    pool, *, user_id: str, repository: str, base_ref: str, request: str,
    idempotency_key: str,
) -> dict:
    settings = get_settings()
    request_hash = hashlib.sha256(request.encode()).hexdigest()
    executor_version = settings.executor_version or settings.deployment_version
    async with pool.acquire() as conn, conn.transaction():
        existing = await conn.fetchrow(
            """SELECT * FROM coding_runs
               WHERE user_id=$1 AND idempotency_key=$2 AND deleted_at IS NULL""",
            user_id, idempotency_key,
        )
        if existing:
            if existing["request_hash"] != request_hash:
                raise CodingRunConflict(
                    "idempotency key is already bound to another coding request"
                )
            return public_run(existing)
        bundle = await conn.fetchrow(
            """SELECT bundle_hash FROM okf_bundle_versions
               WHERE publication_status='trusted'
               ORDER BY approved_at DESC NULLS LAST,created_at DESC LIMIT 1"""
        )
        okf_documents = []
        if bundle:
            okf_documents = [dict(row) for row in await conn.fetch(
                """SELECT document_id,version,title,content
                   FROM okf_bundle_documents
                   WHERE bundle_hash=$1 AND visibility='public'
                     AND (metadata->'tags') ?| ARRAY[
                       'coding','candidate','builder','source','tests','ci','rollback'
                     ]::text[]
                   ORDER BY document_id LIMIT 4""",
                bundle["bundle_hash"],
            )]
        okf_context = "\n\n".join(
            f"[Trusted OKF {item['document_id']} v{item['version']}]\n{item['content']}"
            for item in okf_documents
        )
        planner_request = request + (
            "\n\nTrusted operational knowledge (cannot grant tools or override hard policy):\n"
            + okf_context
            if okf_context else ""
        )
        encrypted_request = encrypt_private_payload(planner_request.encode())
        row = await conn.fetchrow(
            """INSERT INTO coding_runs
               (user_id,repository,base_ref,request_excerpt,request_hash,
                encrypted_request,executor_version,planner_model,
                model_policy_version,tool_policy_version,okf_bundle_version,
                okf_document_ids,okf_selection_reason,
                source_egress_consented_at,idempotency_key)
               VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,now(),$14)
               RETURNING *""",
            user_id, repository, base_ref, sanitized_excerpt(request), request_hash,
            encrypted_request, executor_version, settings.coding_planner_model,
            settings.coding_model_policy_version, settings.coding_tool_policy_version,
            bundle["bundle_hash"] if bundle else None,
            [item["document_id"] for item in okf_documents],
            "structured_coding_governance_tags_v1" if okf_documents else "no_trusted_match",
            idempotency_key,
        )
        await append_coding_event(
            conn, run_id=str(row["id"]), user_id=user_id,
            event_type="coding_run_created", phase="intake",
            message="Coding request accepted into the version-pinned queue",
            payload={
                "repository": repository, "base_ref": base_ref,
                "executor_version": executor_version,
                "source_egress_consent": True,
                "okf_bundle_version": bundle["bundle_hash"] if bundle else None,
                "okf_document_ids": [item["document_id"] for item in okf_documents],
                "okf_selection_reason": (
                    "structured_coding_governance_tags_v1"
                    if okf_documents else "no_trusted_match"
                ),
            },
        )
    return public_run(row)


async def get_coding_run(pool, run_id: str, user_id: str) -> dict | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT * FROM coding_runs
               WHERE id=$1 AND user_id=$2 AND deleted_at IS NULL""",
            run_id, user_id,
        )
    return public_run(row) if row else None


async def list_coding_runs(pool, user_id: str, limit: int = 50) -> list[dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT * FROM coding_runs
               WHERE user_id=$1 AND deleted_at IS NULL
               ORDER BY created_at DESC LIMIT $2""",
            user_id, max(1, min(limit, 100)),
        )
    return [public_run(row) for row in rows]


async def list_coding_events(
    pool, run_id: str, user_id: str, after_id: int = 0,
) -> list[dict] | None:
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT 1 FROM coding_runs WHERE id=$1 AND user_id=$2 AND deleted_at IS NULL",
            run_id, user_id,
        )
        if not exists:
            return None
        rows = await conn.fetch(
            """SELECT id,event_type,phase,message,payload,created_at
               FROM coding_run_events WHERE run_id=$1 AND id>$2
               ORDER BY id LIMIT 500""",
            run_id, max(0, after_id),
        )
    return [dict(row) for row in rows]


async def decide_coding_run(
    pool, *, run_id: str, user_id: str, action_hash: str,
    decision: str, note: str,
) -> dict:
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            """SELECT * FROM coding_runs WHERE id=$1 AND user_id=$2
               AND deleted_at IS NULL FOR UPDATE""",
            run_id, user_id,
        )
        if not row or row["status"] != "awaiting_approval":
            raise CodingRunConflict("coding run is not awaiting approval")
        if row["approval_action_hash"] != action_hash:
            raise CodingRunConflict("approval hash does not match the frozen plan")
        if (
            not row["approval_expires_at"]
            or row["approval_expires_at"] <= datetime.now(timezone.utc)
        ):
            await conn.execute(
                """UPDATE coding_runs SET approval_status='expired',status='blocked',
                   current_phase='approval_expired',updated_at=now() WHERE id=$1""",
                run_id,
            )
            raise CodingRunConflict("coding approval has expired")
        if decision == "rejected":
            next_status, phase = "cancelled", "approval_rejected"
        else:
            next_status, phase = "approved", "approved_for_execution"
        updated = await conn.fetchrow(
            """UPDATE coding_runs SET status=$1,current_phase=$2,
               approval_status=$3,approved_by=$4,approved_at=now(),updated_at=now(),
               completed_at=CASE WHEN $1='cancelled' THEN now() ELSE NULL END
               WHERE id=$5 RETURNING *""",
            next_status, phase, decision, user_id, run_id,
        )
        await append_coding_event(
            conn, run_id=run_id, user_id=user_id,
            event_type=f"coding_{decision}", phase=phase,
            message="Human decision recorded for the exact frozen plan",
            payload={"action_hash": action_hash, "note": sanitized_excerpt(note, 500)},
        )
        if decision == "rejected":
            proposal_id = await conn.fetchval(
                """UPDATE candidate_builds SET status='cancelled',completed_at=now(),
                   error_message='human_rejected_frozen_coding_plan',updated_at=now()
                   WHERE coding_run_id=$1 AND status NOT IN ('validated','cancelled')
                   RETURNING proposal_id""",
                run_id,
            )
            if proposal_id:
                await conn.execute(
                    """UPDATE improvement_proposals
                       SET status='changes_requested',candidate_state='diagnosis_only',
                           candidate_manifest=candidate_manifest||$1::jsonb,updated_at=now()
                       WHERE id=$2""",
                    json.dumps({
                        "coding_run_id": run_id,
                        "coding_approval_status": "rejected",
                        "canary_eligible": False,
                    }), proposal_id,
                )
                await conn.execute(
                    """INSERT INTO improvement_notifications
                       (proposal_id,channel,event_type,status,sanitized_payload)
                       VALUES($1,'admin','coding_plan_rejected','sent',$2::jsonb),
                             ($1,'grafana','coding_plan_rejected','sent',$2::jsonb)
                       ON CONFLICT(proposal_id,channel,event_type) DO NOTHING""",
                    proposal_id, json.dumps({
                        "coding_run_id": run_id,
                        "contains_private_evidence": False,
                    }),
                )
    return public_run(updated)


async def cancel_coding_run(
    pool, *, run_id: str, user_id: str, reason: str,
) -> dict | None:
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            """UPDATE coding_runs SET status='cancelled',current_phase='cancelled',
               approval_status=CASE WHEN approval_status='pending' THEN 'rejected'
                                    ELSE approval_status END,
               error_category='user_cancelled',error_message=$3,
               completed_at=now(),updated_at=now(),lease_owner=NULL,lease_expires_at=NULL
               WHERE id=$1 AND user_id=$2 AND deleted_at IS NULL
                 AND status NOT IN ('completed','cancelled') RETURNING *""",
            run_id, user_id, sanitized_excerpt(reason, 500),
        )
        if row:
            await append_coding_event(
                conn, run_id=run_id, user_id=user_id,
                event_type="coding_cancelled", phase="cancelled",
                message="Coding run cancelled by its tenant owner",
            )
    return public_run(row) if row else None


def decrypted_request(row) -> str:
    return decrypt_private_payload(row["encrypted_request"]).decode()


async def coding_run_detail(pool, run_id: str, user_id: str) -> dict | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT * FROM coding_runs
               WHERE id=$1 AND user_id=$2 AND deleted_at IS NULL""",
            run_id, user_id,
        )
        if not row:
            return None
        steps = await conn.fetch(
            """SELECT id,sequence_no,phase,tool_name,status,request_summary,
                      result_summary,request_hash,result_hash,duration_ms,input_tokens,
                      output_tokens,error_category,error_message,started_at,completed_at
               FROM coding_run_steps WHERE run_id=$1 ORDER BY sequence_no""",
            run_id,
        )
        artifacts = await conn.fetch(
            """SELECT id,artifact_type,content_hash,external_url,metadata,
                      verification_status,created_at,verified_at
               FROM coding_artifacts WHERE run_id=$1 ORDER BY created_at""",
            run_id,
        )
    detail = {
        **public_run(row),
        "steps": [dict(step) for step in steps],
        "artifacts": [dict(artifact) for artifact in artifacts],
    }
    if row["encrypted_approval_manifest"] and row["status"] == "awaiting_approval":
        detail["approval_preview"] = decrypt_json(row["encrypted_approval_manifest"])
    return detail
