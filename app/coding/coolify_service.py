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
from app.coding.compose_policy import (
    ComposePolicyError, parse_compose_document, validate_compose,
)


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

    async def _json(self, method: str, path: str, *, payload=None, expected: int,
                    result_type=dict, max_bytes=65_536):
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
                    if len(body) + len(chunk) > max_bytes:
                        raise CoolifyServiceError("response_too_large")
                    body.extend(chunk)
                try:
                    result = json.loads(body)
                except (ValueError, UnicodeError, RecursionError):
                    raise CoolifyServiceError("invalid_response") from None
                if not isinstance(result, result_type):
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

    async def _verified_target_ids(self):
        server = await self._json(
            "GET", "/api/v1/servers/" + self.target.server_uuid, expected=200,
        )
        project = await self._json(
            "GET", "/api/v1/projects/" + self.target.coolify_project_uuid, expected=200,
        )
        environment = await self._json(
            "GET", "/api/v1/projects/" + self.target.coolify_project_uuid + "/"
            + self.target.environment_uuid, expected=200,
        )
        if (server.get("uuid") != self.target.server_uuid
                or project.get("uuid") != self.target.coolify_project_uuid
                or environment.get("uuid") != self.target.environment_uuid
                or type(server.get("id")) is not int
                or type(project.get("id")) is not int
                or type(environment.get("id")) is not int
                or type(environment.get("project_id")) is not int
                or environment.get("project_id") != project["id"]):
            raise CoolifyServiceError("target_identity_ambiguous")
        return server["id"], environment["id"]

    async def verify_create_target(self):
        """Read-only target preflight before consuming the one-time write claim."""
        await self._verified_target_ids()
        destination = await self._json(
            "GET", "/api/v1/destinations/" + self.target.destination_uuid,
            expected=200,
        )
        expected_type = {
            "App\\Models\\StandaloneDocker": "standalone",
            "App\\Models\\SwarmDocker": "swarm",
        }.get(self.target.destination_type)
        if (expected_type is None or destination.get("uuid") != self.target.destination_uuid
                or destination.get("server_uuid") != self.target.server_uuid
                or destination.get("type") != expected_type):
            raise CoolifyServiceError("destination_identity_ambiguous")
        # Coolify's public destination API omits the numeric ID. Its registered
        # UUID-to-ID/type mapping must have separate trusted operator evidence.

    async def create(self, *, request_id: UUID, compose: bytes):
        """Exactly one network mutation; no automatic retry, no instant deployment."""
        compose_hash = hashlib.sha256(compose).hexdigest()
        payload = {
                "name": "gca-" + request_id.hex,
                "description": "gca-compose-sha256:" + compose_hash,
                "project_uuid": self.target.coolify_project_uuid,
                "environment_uuid": self.target.environment_uuid,
                "server_uuid": self.target.server_uuid,
                "destination_uuid": self.target.destination_uuid,
                "instant_deploy": False,
                "force_domain_override": False,
                "docker_compose_raw": base64.b64encode(compose).decode("ascii"),
        }
        result = await self._json(
            "POST", "/api/v1/services", expected=201, payload=payload,
        )
        resource_id = result.get("uuid")
        if not isinstance(resource_id, str) or not RESOURCE_ID.fullmatch(resource_id):
            raise CoolifyServiceError("resource_identity_missing")
        return resource_id

    def _require_resource_identity(self, item, *, request_id: UUID, compose_hash: str,
                                   server_id: int, environment_id: int):
        resource_id = item.get("uuid") if isinstance(item, dict) else None
        if (not isinstance(resource_id, str) or not RESOURCE_ID.fullmatch(resource_id)
                or item.get("name") != "gca-" + request_id.hex
                or item.get("description") != "gca-compose-sha256:" + compose_hash
                or item.get("deleted_at") is not None
                or type(item.get("server_id")) is not int
                or type(item.get("environment_id")) is not int
                or item.get("server_id") != server_id
                or item.get("environment_id") != environment_id
                or type(item.get("destination_id")) is not int
                or item.get("destination_id") != self.target.destination_id
                or item.get("destination_type") != self.target.destination_type):
            raise CoolifyServiceError("identity_ambiguous")
        return resource_id

    async def find_service_candidate(self, *, request_id: UUID, compose_hash: str):
        """List metadata using an ordinary team-read token, not sensitive-read."""
        server_id, environment_id = await self._verified_target_ids()
        values = await self._json(
            "GET", "/api/v1/services", expected=200, result_type=list,
            max_bytes=1_048_576,
        )
        if len(values) > 1000:
            raise CoolifyServiceError("inventory_too_large")
        if any(isinstance(item, dict) and "docker_compose_raw" in item for item in values):
            raise CoolifyServiceError("overscoped_inventory_token")
        name = "gca-" + request_id.hex
        matches = []
        for item in values:
            if not isinstance(item, dict) or item.get("name") != name:
                continue
            matches.append(self._require_resource_identity(
                item, request_id=request_id, compose_hash=compose_hash,
                server_id=server_id, environment_id=environment_id,
            ))
        if len(matches) > 1:
            raise CoolifyServiceError("identity_ambiguous")
        return (matches[0], server_id, environment_id) if matches else None

    async def verify_service_content(self, *, resource_id: str, request_id: UUID,
                                     compose: bytes, server_id: int, environment_id: int):
        """Use a separate sensitive-read token for only one candidate resource."""
        if not RESOURCE_ID.fullmatch(resource_id):
            raise CoolifyServiceError("identity_ambiguous")
        item = await self._json(
            "GET", "/api/v1/services/" + resource_id, expected=200,
            max_bytes=524_288,
        )
        compose_hash = hashlib.sha256(compose).hexdigest()
        if self._require_resource_identity(
            item, request_id=request_id, compose_hash=compose_hash,
            server_id=server_id, environment_id=environment_id,
        ) != resource_id:
            raise CoolifyServiceError("identity_ambiguous")
        raw = item.get("docker_compose_raw")
        if not isinstance(raw, str):
            raise CoolifyServiceError("sensitive_read_required")
        # Installed Coolify normalizes Compose YAML on create. Compare parsed
        # structure after checking the byte-bound approved input, not text bytes.
        possible = [raw.encode("utf-8")]
        try:
            possible.append(base64.b64decode(raw, validate=True))
        except (ValueError, UnicodeError):
            pass
        approved_document = parse_compose_document(compose)
        for candidate in possible:
            try:
                if parse_compose_document(candidate) == approved_document:
                    return
            except ComposePolicyError:
                continue
        raise CoolifyServiceError("identity_ambiguous")


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
        await client.verify_create_target()
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


async def reconcile_created_service(pool, deployment_id: UUID, *, user_id: str,
                                    compose: bytes, resolve_token, transport=None):
    """Resolve a lost create response by name + equivalent Compose; never repost."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT d.manifest,d.manifest_hash,d.status,d.provider_resource_id,"
            "t.config,t.config_hash FROM coding_deployments d "
            "JOIN coding_deployment_targets t ON t.target_id=d.target_id "
            "AND t.user_id=d.user_id AND t.project_id=d.project_id "
            "WHERE d.id=$1 AND d.user_id=$2", deployment_id, user_id,
        )
    if not row:
        raise admission.DeploymentConflict("deployment unavailable for this user")
    if row["status"] not in {"dispatching", "uncertain"}:
        return {"deployment_id": deployment_id, "status": row["status"],
                "resource_id": row["provider_resource_id"], "reconciled": False}
    manifest = admission.DeploymentManifest.model_validate(admission._json(row["manifest"]))
    target = admission.RegisteredTarget.model_validate(admission._json(row["config"]))
    if (admission.fingerprint(manifest) != row["manifest_hash"]
            or admission.fingerprint(target) != row["config_hash"]
            or manifest.target_hash != row["config_hash"]):
        raise admission.DeploymentConflict("immutable deployment identity changed")
    validate_compose(compose, manifest)
    inventory_token = resolve_token(target.inventory_secret_ref)
    async with CoolifyServiceClient(target, inventory_token, transport=transport) as inventory:
        await inventory.verify_team()
        candidate = await inventory.find_service_candidate(
            request_id=manifest.request_id, compose_hash=manifest.compose_hash,
        )
    if candidate is None:
        return {"deployment_id": deployment_id, "status": row["status"],
                "resource_id": None, "reconciled": False}
    resource_id, server_id, environment_id = candidate
    recovery_token = resolve_token(target.recovery_secret_ref)
    async with CoolifyServiceClient(target, recovery_token, transport=transport) as recovery:
        await recovery.verify_team()
        await recovery.verify_service_content(
            resource_id=resource_id, request_id=manifest.request_id, compose=compose,
            server_id=server_id, environment_id=environment_id,
        )
    evidence = hashlib.sha256(
        f"{deployment_id}:reconciled:{resource_id}:{manifest.compose_hash}".encode(),
    ).hexdigest()
    updated = await admission.record_provider_observation(
        pool, deployment_id, user_id=user_id,
        evidence_hash=evidence, resource_id=resource_id,
    )
    return {"deployment_id": deployment_id, "status": updated["status"],
            "resource_id": resource_id, "reconciled": True}
