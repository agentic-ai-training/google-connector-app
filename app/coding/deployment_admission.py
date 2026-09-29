"""Internal user-service deployment authority, not a model tool or a public API.

Operators register verified targets. The authenticated approval handler records an
exact manifest grant AFTER validating source/test attestations and user authority.
This module neither verifies those attestations nor publishes core-platform candidates.
The provider adapter may send a write only after a successful committed dispatch claim.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.coding import cost_ledger
from app.coding import host_capacity
from app.coding.cloud_budget import CloudCostRequest
from app.coding.coolify import CoolifyTarget


class DeploymentConflict(RuntimeError):
    pass


class RegisteredTarget(CoolifyTarget):
    project_id: str = Field(pattern=r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
    server_uuid: str = Field(pattern=r"^[a-zA-Z0-9-]{1,128}$")
    destination_uuid: str = Field(pattern=r"^[a-zA-Z0-9-]{1,128}$")
    destination_id: int = Field(ge=0, strict=True)
    destination_type: str = Field(pattern=r"^[a-zA-Z0-9_\\.-]{1,128}$")
    coolify_project_uuid: str = Field(pattern=r"^[a-zA-Z0-9-]{1,128}$")
    environment_uuid: str = Field(pattern=r"^[a-zA-Z0-9-]{1,128}$")
    owner_user_id: str = Field(min_length=1, max_length=512)
    # Separate write/deploy credentials will be resolved only inside the adapter.
    configuration_secret_ref: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_/-]{2,127}$")
    deployment_secret_ref: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_/-]{2,127}$")
    recovery_secret_ref: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_/-]{2,127}$")
    capacity_pool_id: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9_-]{2,63}$")

    @model_validator(mode="after")
    def separate_provider_credentials(self):
        references = (
            self.inventory_secret_ref, self.configuration_secret_ref,
            self.deployment_secret_ref, self.recovery_secret_ref,
        )
        if len(set(references)) != len(references):
            raise ValueError("Coolify inventory, configuration, start and recovery credentials must be separate")
        return self


class DeploymentManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    request_id: UUID
    origin: Literal["user_service"] = "user_service"
    target_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,63}$")
    target_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    release_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    compose_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    validation_attestation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    validation_claim_id: UUID | None = None
    validated_source_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    estimated_upper_bound_paise: int = Field(ge=0, strict=True)
    capacity: host_capacity.CapacityRequest | None = None
    planned_end_at: datetime

    @field_validator("planned_end_at")
    @classmethod
    def zoned(cls, value):
        if value.utcoffset() is None:
            raise ValueError("deployment deadline must include a timezone")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def funding_required(self):
        if self.estimated_upper_bound_paise == 0 and self.capacity is None:
            raise ValueError("zero incremental cost requires verified host capacity coverage")
        return self


def fingerprint(model: BaseModel) -> str:
    return hashlib.sha256(json.dumps(model.model_dump(mode="json"), sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


async def _event(conn, deployment_id, event_type):
    await conn.execute(
        "INSERT INTO coding_deployment_events(deployment_id,event_type) VALUES($1,$2)",
        deployment_id, event_type,
    )


async def register_target(pool, target: RegisteredTarget, *, evidence_reference: str):
    """Operator only: a new target ID is required for configuration/ownership changes."""
    cost_ledger._text(evidence_reference)
    async with pool.acquire() as conn, conn.transaction():
        await cost_ledger._account(conn, target.project_id)
        await conn.execute(
            """INSERT INTO coding_deployment_targets
            (target_id,user_id,project_id,config,config_hash,evidence_reference)
            VALUES($1,$2,$3,$4::jsonb,$5,$6) ON CONFLICT DO NOTHING""",
            target.target_id, target.owner_user_id, target.project_id,
            target.model_dump_json(), fingerprint(target), evidence_reference,
        )
        row = await conn.fetchrow(
            "SELECT config_hash,status FROM coding_deployment_targets WHERE target_id=$1",
            target.target_id,
        )
        if row["config_hash"] != fingerprint(target) or row["status"] != "active":
            raise DeploymentConflict("target is immutable or revoked")


async def _target(conn, target_id, user_id, *, allow_revoked=False):
    # All mutations use account -> target -> grant/deployment -> cost lock ordering.
    project = await conn.fetchval(
        "SELECT project_id FROM coding_deployment_targets WHERE target_id=$1 AND user_id=$2",
        target_id, user_id,
    )
    if project is None:
        raise DeploymentConflict("target unavailable for this user")
    await cost_ledger._account(conn, project)
    row = await conn.fetchrow(
        "SELECT * FROM coding_deployment_targets WHERE target_id=$1 AND user_id=$2 FOR UPDATE",
        target_id, user_id,
    )
    if row["status"] != "active" and not allow_revoked:
        raise DeploymentConflict("target revoked")
    return row


async def record_user_grant(pool, manifest: DeploymentManifest, *, user_id: str,
                            authority_reference: str, expires_at: datetime):
    """Authenticated user-service approval handler only; NOT model-supplied evidence."""
    cost_ledger._text(authority_reference)
    now = datetime.now(timezone.utc)
    if expires_at.utcoffset() is None or not now < expires_at <= manifest.planned_end_at:
        raise DeploymentConflict("invalid authorization lifetime")
    async with pool.acquire() as conn, conn.transaction():
        target = await _target(conn, manifest.target_id, user_id)
        if manifest.target_hash != target["config_hash"]:
            raise DeploymentConflict("target identity does not match approved manifest")
        await conn.execute(
            """INSERT INTO coding_deployment_grants
            (id,user_id,target_id,manifest_hash,authority_reference,expires_at)
            VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT DO NOTHING""",
            uuid4(), user_id, manifest.target_id, fingerprint(manifest),
            authority_reference, expires_at,
        )
        # Replays cannot extend or revive authority.
        row = await conn.fetchrow(
            "SELECT * FROM coding_deployment_grants WHERE user_id=$1 AND manifest_hash=$2",
            user_id, fingerprint(manifest),
        )
        if (row["revoked_at"] is not None or row["expires_at"] != expires_at
                or row["authority_reference"] != authority_reference):
            raise DeploymentConflict("existing authorization cannot be overwritten")
        return dict(row)


async def _grant(conn, manifest, user_id):
    row = await conn.fetchrow(
        """SELECT * FROM coding_deployment_grants
        WHERE user_id=$1 AND manifest_hash=$2 AND revoked_at IS NULL AND expires_at>now()
        FOR UPDATE""", user_id, fingerprint(manifest),
    )
    if not row:
        raise DeploymentConflict("matching live authorization required")
    return row


async def _require_passing_validation(conn, manifest, user_id):
    """Bind a provider write to the tenant's real immutable validation result."""
    if manifest.validation_claim_id is None or manifest.validated_source_hash is None:
        raise DeploymentConflict("passing validation claim and source binding required")
    row = await conn.fetchrow(
        "SELECT c.request,c.request_hash,c.result_hash,c.validation_outcome,c.status,"
        "c.result_expires_at,c.run_id,c.target_id,r.deleted_at "
        "FROM coding_validation_claims c JOIN coding_runs r ON r.id=c.run_id "
        "AND r.user_id=c.user_id WHERE c.id=$1 AND c.user_id=$2 "
        "AND c.run_id=$3 AND c.target_id=$4 FOR SHARE OF c,r",
        manifest.validation_claim_id, user_id, manifest.request_id, manifest.target_id,
    )
    if (not row or row["deleted_at"] is not None or row["status"] != "released"
            or row["validation_outcome"] != "passed"
            or row["result_hash"] != manifest.validation_attestation_hash
            or not row["result_expires_at"]
            or row["result_expires_at"] <= datetime.now(timezone.utc)):
        raise DeploymentConflict("passing validation claim is absent, expired or unbound")
    from app.coding.validation_admission import ValidationRequest

    request = ValidationRequest.model_validate(_json(row["request"]))
    if (fingerprint(request) != row["request_hash"]
            or request.run_id != manifest.request_id
            or request.target_id != manifest.target_id
            or request.target_hash != manifest.target_hash
            or request.source_hash != manifest.validated_source_hash):
        raise DeploymentConflict("validation request does not match approved source and target")
    return row


async def enqueue(pool, manifest: DeploymentManifest, *, user_id: str, idempotency_key: str):
    cost_ledger._text(user_id)
    cost_ledger._text(idempotency_key)
    digest = fingerprint(manifest)
    async with pool.acquire() as conn, conn.transaction():
        # Serialize the user's idempotency namespace even across separately funded projects.
        lock_key = int.from_bytes(hashlib.sha256(json.dumps([user_id, idempotency_key]).encode()).digest()[:8],
                                  byteorder="big", signed=True)
        await conn.execute("SELECT pg_advisory_xact_lock($1::bigint)", lock_key)
        target = await _target(conn, manifest.target_id, user_id)
        if manifest.target_hash != target["config_hash"]:
            raise DeploymentConflict("target identity changed")
        existing = await conn.fetchrow(
            "SELECT * FROM coding_deployments WHERE user_id=$1 AND idempotency_key=$2",
            user_id, idempotency_key,
        )
        if existing:
            if existing["manifest_hash"] != digest:
                raise DeploymentConflict("idempotency key belongs to a different deployment")
            return dict(existing)
        grant = await _grant(conn, manifest, user_id)
        if await conn.fetchval("SELECT id FROM coding_deployments WHERE grant_id=$1", grant["id"]):
            raise DeploymentConflict("authorization already bound to a deployment")
        config = RegisteredTarget.model_validate(_json(target["config"]))
        await _check_capacity(conn, manifest, config)
        cost = None
        if manifest.estimated_upper_bound_paise:
            cost = await cost_ledger.reserve_in_transaction(
                conn, CloudCostRequest(project_id=target["project_id"], category="hosting",
                                       estimated_upper_bound_paise=manifest.estimated_upper_bound_paise,
                                       planned_end_at=manifest.planned_end_at),
                user_id=user_id, idempotency_key="deployment:" + digest,
            )
            if cost["status"] != "reserved":
                raise DeploymentConflict("cost reservation not available")
        cap = manifest.capacity
        row = await conn.fetchrow(
            """INSERT INTO coding_deployments
            (id,user_id,target_id,project_id,grant_id,reservation_id,manifest,manifest_hash,idempotency_key,
             capacity_pool_id,cpu_millis,memory_mib,storage_mib)
            VALUES($1,$2,$3,$4,$5,$6,$7::jsonb,$8,$9,$10,$11,$12,$13) RETURNING *""",
            uuid4(), user_id, manifest.target_id, target["project_id"], grant["id"],
            cost["id"] if cost else None, manifest.model_dump_json(), digest, idempotency_key,
            cap.pool_id if cap else None, cap.cpu_millis if cap else 0,
            cap.memory_mib if cap else 0, cap.storage_mib if cap else 0,
        )
        await _event(conn, row["id"], "reserved")
        return dict(row)


async def _check_capacity(conn, manifest, config, *, exclude=None):
    if config.capacity_pool_id != (manifest.capacity.pool_id if manifest.capacity else None):
        raise DeploymentConflict("manifest capacity does not match target coverage")
    if manifest.capacity:
        await host_capacity.admit(conn, manifest.capacity, project_id=config.project_id,
                                  server_uuid=config.server_uuid, planned_end_at=manifest.planned_end_at,
                                  exclude=exclude)


async def claim_dispatch(pool, deployment_id: UUID, *, user_id: str,
                         require_validation: bool = False):
    """Return one committed write claim, or None for replay; network work occurs afterwards."""
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "SELECT * FROM coding_deployments WHERE id=$1 AND user_id=$2", deployment_id, user_id,
        )
        if not row:
            raise DeploymentConflict("deployment unavailable for this user")
        target = await _target(conn, row["target_id"], user_id)
        row = await conn.fetchrow("SELECT * FROM coding_deployments WHERE id=$1 FOR UPDATE", deployment_id)
        if row["status"] != "reserved":
            return None
        manifest = DeploymentManifest.model_validate(_json(row["manifest"]))
        if fingerprint(manifest) != row["manifest_hash"] or manifest.target_hash != target["config_hash"]:
            raise DeploymentConflict("immutable deployment evidence changed")
        await _grant(conn, manifest, user_id)
        if require_validation:
            await _require_passing_validation(conn, manifest, user_id)
        if row["capacity_released_at"] is not None:
            raise DeploymentConflict("deployment capacity has already been released")
        await _check_capacity(conn, manifest, RegisteredTarget.model_validate(_json(target["config"])),
                              exclude=deployment_id)
        if row["reservation_id"] and not await cost_ledger.dispatch_in_transaction(
            conn, project_id=row["project_id"], reservation_id=row["reservation_id"], user_id=user_id,
        ):
            raise DeploymentConflict("cost dispatch already consumed; reconcile before retry")
        await conn.execute(
            "UPDATE coding_deployments SET status='dispatching',updated_at=now() WHERE id=$1", deployment_id,
        )
        await _event(conn, deployment_id, "dispatch_intent")
        return {"deployment_id": deployment_id, "manifest": manifest,
                "target": RegisteredTarget.model_validate(_json(target["config"])),
                "reservation_id": row["reservation_id"]}


async def revoke_target(pool, target_id: str, *, user_id: str):
    """Block new claims. Already dispatched operations still require provider reconciliation."""
    async with pool.acquire() as conn, conn.transaction():
        await _target(conn, target_id, user_id, allow_revoked=True)
        await conn.execute(
            "UPDATE coding_deployment_targets SET status='revoked',revoked_at=COALESCE(revoked_at,now()) "
            "WHERE target_id=$1", target_id,
        )


async def revoke_grant(pool, grant_id: UUID, *, user_id: str):
    async with pool.acquire() as conn, conn.transaction():
        target_id = await conn.fetchval(
            "SELECT target_id FROM coding_deployment_grants WHERE id=$1 AND user_id=$2", grant_id, user_id,
        )
        if target_id is None:
            raise DeploymentConflict("authorization unavailable for this user")
        await _target(conn, target_id, user_id, allow_revoked=True)
        await conn.execute(
            "UPDATE coding_deployment_grants SET revoked_at=COALESCE(revoked_at,now()) WHERE id=$1", grant_id,
        )


async def record_provider_observation(pool, deployment_id: UUID, *, user_id: str,
                                      evidence_hash: str, resource_id: str | None = None):
    """Trusted adapter reconciliation, including after revocation. Never grants a retry.

    A missing resource ID records ambiguity. An ID records submission, NOT health,
    behavior verification or completion. Evidence must come from authenticated provider
    responses/inventory, never a model's claim. No raw logs or credentials are stored here.
    """
    if not re.fullmatch(r"[0-9a-f]{64}", evidence_hash):
        raise ValueError("provider evidence must be a SHA-256 digest")
    if resource_id is not None and not re.fullmatch(r"[a-zA-Z0-9-]{1,128}", resource_id):
        raise ValueError("invalid provider resource identity")
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "SELECT * FROM coding_deployments WHERE id=$1 AND user_id=$2", deployment_id, user_id,
        )
        if not row:
            raise DeploymentConflict("deployment unavailable for this user")
        await _target(conn, row["target_id"], user_id, allow_revoked=True)
        row = await conn.fetchrow("SELECT * FROM coding_deployments WHERE id=$1 FOR UPDATE", deployment_id)
        if row["status"] == "submitted":
            if row["provider_resource_id"] != resource_id:
                raise DeploymentConflict("submitted provider identity cannot be replaced")
            return dict(row)
        if row["status"] not in {"dispatching", "uncertain"}:
            raise DeploymentConflict("no dispatch to reconcile")
        state = "submitted" if resource_id else "uncertain"
        if resource_id is None and row["reservation_id"]:
            cost = await cost_ledger._owned(conn, row["project_id"], row["reservation_id"], user_id)
            if cost["status"] not in {"dispatched", "uncertain"}:
                raise DeploymentConflict("cost reconciliation state inconsistent")
            await conn.execute(
                "UPDATE cloud_cost_reservations SET status='uncertain',updated_at=now() WHERE id=$1",
                row["reservation_id"],
            )
            await cost_ledger._event(conn, row["project_id"], "uncertain", row["reservation_id"], evidence_hash)
        updated = await conn.fetchrow(
            """UPDATE coding_deployments SET status=$2,provider_resource_id=$3,
            provider_evidence_hash=$4,updated_at=now() WHERE id=$1 RETURNING *""",
            deployment_id, state, resource_id, evidence_hash,
        )
        await _event(conn, deployment_id, state)
        return dict(updated)


async def release_capacity(pool, deployment_id: UUID, *, user_id: str, evidence_hash: str):
    """Trusted cancellation/decommission adapter only, after proving resources are gone.

    Never call on a timeout alone. Disk/volume retention must be reconciled first.
    Financial settlement is independent and the parent host obligation remains held.
    """
    if not re.fullmatch(r"[0-9a-f]{64}", evidence_hash):
        raise ValueError("release evidence must be a SHA-256 digest")
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "SELECT * FROM coding_deployments WHERE id=$1 AND user_id=$2", deployment_id, user_id,
        )
        if not row:
            raise DeploymentConflict("deployment unavailable for this user")
        await _target(conn, row["target_id"], user_id, allow_revoked=True)
        row = await conn.fetchrow("SELECT * FROM coding_deployments WHERE id=$1 FOR UPDATE", deployment_id)
        if not row["capacity_pool_id"]:
            raise DeploymentConflict("deployment has no host capacity allocation")
        if row["capacity_released_at"]:
            return dict(row)
        state = "cancelled" if row["status"] == "reserved" else "retired"
        updated = await conn.fetchrow(
            """UPDATE coding_deployments SET capacity_released_at=now(),status=$2,
            provider_evidence_hash=$3,updated_at=now() WHERE id=$1 RETURNING *""",
            deployment_id, state, evidence_hash,
        )
        await _event(conn, deployment_id, "capacity_released")
        return dict(updated)
