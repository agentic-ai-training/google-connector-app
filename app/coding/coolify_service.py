"""Trusted, single-dispatch Coolify Service creation for user applications.

Creation intentionally uses instant_deploy=false. Environment secrets, start,
health, behavior and public URL are later durable steps, not inferred from 201.
This module is internal controller code; it must never be a model-callable tool.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from uuid import UUID

import httpx

from app.coding import deployment_admission as admission
from app.coding.compose_policy import validate_compose


class CoolifyServiceError(RuntimeError):
    def __init__(self, category):
        self.category = category
        super().__init__(f"coolify_service:{category}")


RESOURCE_ID = re.compile(r"^[a-zA-Z0-9-]{1,128}$")


class CoolifyServiceClient:
    """One approved target, one in-memory operator credential, fixed API routes."""

    def __init__(self, target: admission.RegisteredTarget, token: str, *, transport=None):
        if not isinstance(token, str) or not token or any(char.isspace() for char in token):
            raise CoolifyServiceError("credential_unavailable")
        self.target = target
        self._client = httpx.AsyncClient(
            base_url=target.base_url,
            headers={"Authorization": f"Bearer {token}", "Accept-Encoding": "identity"},
            timeout=httpx.Timeout(20, connect=5), follow_redirects=False,
            trust_env=False, transport=transport,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self._client.aclose()

    async def _json(self, method: str, path: str, *, payload=None, expected: int):
        try:
            async with self._client.stream(method, path, json=payload) as response:
                if response.status_code != expected:
                    category = {401: "authentication", 403: "permission", 404: "not_found",
                                409: "conflict", 429: "rate_limit"}.get(
                                    response.status_code, "provider_rejected",
                                )
                    raise CoolifyServiceError(category)
                if response.headers.get("content-encoding", "identity") != "identity":
                    raise CoolifyServiceError("unexpected_compression")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > 65_536:
                        raise CoolifyServiceError("response_too_large")
                    body.extend(chunk)
                try:
                    result = json.loads(body)
                except (ValueError, UnicodeError, RecursionError):
                    raise CoolifyServiceError("invalid_response") from None
                if not isinstance(result, dict):
                    raise CoolifyServiceError("invalid_response")
                return result
        except httpx.TimeoutException:
            raise CoolifyServiceError("timeout") from None
        except httpx.HTTPError:
            raise CoolifyServiceError("transport") from None

    async def verify_team(self):
        result = await self._json("GET", "/api/v1/teams/current", expected=200)
        if type(result.get("id")) is not int or result["id"] != self.target.team_id:
            raise CoolifyServiceError("team_mismatch")

    async def create(self, *, request_id: UUID, compose: bytes):
        """Exactly one network mutation; no automatic retry, no instant deployment."""
        result = await self._json(
            "POST", "/api/v1/services", expected=201,
            payload={
                "name": "gca-" + request_id.hex,
                "project_uuid": self.target.coolify_project_uuid,
                "environment_uuid": self.target.environment_uuid,
                "server_uuid": self.target.server_uuid,
                "instant_deploy": False,
                "force_domain_override": False,
                "docker_compose_raw": base64.b64encode(compose).decode("ascii"),
            },
        )
        resource_id = result.get("uuid")
        if not isinstance(resource_id, str) or not RESOURCE_ID.fullmatch(resource_id):
            raise CoolifyServiceError("resource_identity_missing")
        return resource_id


async def submit_approved_service(pool, deployment_id: UUID, *, user_id: str,
                                  compose: bytes, resolve_token, transport=None):
    """Preflight, claim once, create once, persist submitted/uncertain evidence.

    A crashed worker must reconcile by provider inventory and deterministic name;
    invoking this function again never issues another POST for a consumed claim.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT d.manifest,d.manifest_hash,d.status,t.config,t.config_hash "
            "FROM coding_deployments d JOIN coding_deployment_targets t "
            "ON t.target_id=d.target_id AND t.user_id=d.user_id AND t.project_id=d.project_id "
            "WHERE d.id=$1 AND d.user_id=$2", deployment_id, user_id,
        )
    if not row:
        raise admission.DeploymentConflict("deployment unavailable for this user")
    if row["status"] != "reserved":
        # Existing dispatch intent is authoritative. Do not query the provider or
        # return a new write token just because the caller retried.
        return {"deployment_id": deployment_id, "status": row["status"], "dispatched": False}
    manifest = admission.DeploymentManifest.model_validate(admission._json(row["manifest"]))
    target = admission.RegisteredTarget.model_validate(admission._json(row["config"]))
    if (admission.fingerprint(manifest) != row["manifest_hash"]
            or admission.fingerprint(target) != row["config_hash"]
            or manifest.target_hash != row["config_hash"]):
        raise admission.DeploymentConflict("immutable deployment identity changed")
    validate_compose(compose, manifest)
    async with pool.acquire() as conn, conn.transaction():
        await admission._require_passing_validation(conn, manifest, user_id)
    token = resolve_token(target.configuration_secret_ref)
    async with CoolifyServiceClient(target, token, transport=transport) as client:
        # A bad token/team cannot consume the durable write claim.
        await client.verify_team()
        issued = await admission.claim_dispatch(
            pool, deployment_id, user_id=user_id, require_validation=True,
        )
        if issued is None:
            return {"deployment_id": deployment_id, "status": "already_dispatched",
                    "dispatched": False}
        if (admission.fingerprint(issued["manifest"]) != admission.fingerprint(manifest)
                or admission.fingerprint(issued["target"]) != admission.fingerprint(target)):
            raise admission.DeploymentConflict("dispatch identity changed; reconcile before retry")
        try:
            resource_id = await client.create(request_id=manifest.request_id, compose=compose)
        except CoolifyServiceError as error:
            evidence = hashlib.sha256(
                f"{deployment_id}:create:{error.category}".encode(),
            ).hexdigest()
            row = await admission.record_provider_observation(
                pool, deployment_id, user_id=user_id, evidence_hash=evidence,
            )
            return {"deployment_id": deployment_id, "status": row["status"],
                    "dispatched": True, "error": error.category}
        evidence = hashlib.sha256(
            f"{deployment_id}:created:{resource_id}".encode(),
        ).hexdigest()
        row = await admission.record_provider_observation(
            pool, deployment_id, user_id=user_id,
            evidence_hash=evidence, resource_id=resource_id,
        )
        return {"deployment_id": deployment_id, "status": row["status"],
                "dispatched": True, "resource_id": resource_id}
