"""Single-write Coolify adapter proofs with a mocked provider and local PostgreSQL."""
import asyncio
import base64
from hashlib import sha256
from uuid import UUID

import httpx
import pytest
import yaml

from app.coding import deployment_admission as deploy
from app.coding import validation_controller as controller
from app.coding import validation_transport as transport
from app.coding.coolify_service import (
    CoolifyServiceError, reconcile_created_service, submit_approved_service,
)
from tests.coding.test_compose_policy import safe_compose
from tests.coding.test_cost_ledger_postgres import DSN
from tests.coding.test_deployment_admission_postgres import prepared as prepared_fixture
from tests.coding.test_host_capacity_postgres import covered as covered_fixture
from tests.coding.test_validation_controller_postgres import (
    context as context_fixture, result as validation_result,
)

pytestmark = pytest.mark.skipif(not DSN, reason="set COST_LEDGER_TEST_DSN to local PostgreSQL")
covered = covered_fixture
prepared = prepared_fixture
context = context_fixture


def test_provider_credential_references_must_be_distinct():
    with pytest.raises(ValueError, match="must be separate"):
        deploy.RegisteredTarget(
            target_id="test-target", base_url="http://127.0.0.1:18000", team_id=0,
            inventory_secret_ref="test/inventory",
            configuration_secret_ref="test/configuration",
            deployment_secret_ref="test/deploy",
            recovery_secret_ref="test/configuration",
            project_id="test-project", server_uuid="server",
            coolify_project_uuid="project", environment_uuid="environment",
            owner_user_id="alice",
        )


async def approved_deployment(covered, context, monkeypatch):
    pool, _, manifest, _, _ = covered
    _, request, host, source = context

    async def validated(_host, envelope, _source):
        return validation_result(envelope)

    monkeypatch.setattr(transport, "dispatch", validated)
    outcome = await controller.execute(
        pool, request, user_id="alice", lease_owner="worker-one",
        idempotency_key="coolify-service-validation", host=host, source=source,
    )
    assert outcome["outcome"] == "passed"
    async with pool.acquire() as conn:
        result_hash = await conn.fetchval(
            "SELECT result_hash FROM coding_validation_claims WHERE id=$1",
            UUID(outcome["claim_id"]),
        )
    payload = safe_compose().encode()
    manifest = manifest.model_copy(update={
        "request_id": request.run_id, "compose_hash": sha256(payload).hexdigest(),
        "validation_claim_id": UUID(outcome["claim_id"]),
        "validated_source_hash": request.source_hash,
        "validation_attestation_hash": result_hash,
    })
    from datetime import datetime, timedelta, timezone

    await deploy.record_user_grant(
        pool, manifest, user_id="alice", authority_reference="test:user-approved-service",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
    )
    row = await deploy.enqueue(pool, manifest, user_id="alice", idempotency_key="service-one")
    return row, payload


@pytest.mark.asyncio
async def test_one_provider_create_then_record_only_submission(covered, context, monkeypatch):
    row, compose = await approved_deployment(covered, context, monkeypatch)
    posts = []

    def provider(request):
        if request.url.path.endswith("/teams/current"):
            return httpx.Response(200, json={"id": 0})
        assert request.url.path == "/api/v1/services"
        assert request.headers["Authorization"] == "Bearer scoped-test-token"
        body = request.read().decode()
        import json

        payload = json.loads(body)
        assert payload["instant_deploy"] is False
        assert payload["force_domain_override"] is False
        assert base64.b64decode(payload["docker_compose_raw"]) == compose
        assert payload["description"] == "gca-compose-sha256:" + sha256(compose).hexdigest()
        posts.append(payload["name"])
        return httpx.Response(201, json={"uuid": "created-service-1"})

    transport = httpx.MockTransport(provider)
    results = await asyncio.gather(*[
        submit_approved_service(
            covered[0], row["id"], user_id="alice", compose=compose,
            resolve_token=lambda _: "scoped-test-token", transport=transport,
        ) for _ in range(4)
    ])
    assert len(posts) == 1
    assert posts[0].startswith("gca-")
    assert sum(result["dispatched"] for result in results) == 1
    assert any(result["status"] == "submitted" for result in results)
    async with covered[0].acquire() as conn:
        assert await conn.fetchval("SELECT status FROM coding_deployments") == "submitted"
        assert await conn.fetchval("SELECT provider_resource_id FROM coding_deployments") == "created-service-1"
        assert await conn.fetchval("SELECT count(*) FROM cloud_cost_reservations") == 1


@pytest.mark.asyncio
async def test_ambiguous_provider_error_retains_capacity_and_cannot_repost(
        covered, context, monkeypatch):
    row, compose = await approved_deployment(covered, context, monkeypatch)
    posts = []

    def provider(request):
        if request.url.path.endswith("/teams/current"):
            return httpx.Response(200, json={"id": 0})
        posts.append(request.url.path)
        return httpx.Response(503, text="SECRET provider detail")

    transport = httpx.MockTransport(provider)
    first = await submit_approved_service(
        covered[0], row["id"], user_id="alice", compose=compose,
        resolve_token=lambda _: "scoped-test-token", transport=transport,
    )
    second = await submit_approved_service(
        covered[0], row["id"], user_id="alice", compose=compose,
        resolve_token=lambda _: "scoped-test-token", transport=transport,
    )
    assert first["status"] == second["status"] == "uncertain"
    assert first["error"] == "provider_rejected"
    assert "SECRET" not in str(first)
    assert posts == ["/api/v1/services"]
    async with covered[0].acquire() as conn:
        assert await conn.fetchval("SELECT capacity_released_at FROM coding_deployments") is None


@pytest.mark.asyncio
async def test_lost_create_response_is_uncertain_not_a_second_post(covered, context, monkeypatch):
    row, compose = await approved_deployment(covered, context, monkeypatch)
    posts = []

    def provider(request):
        if request.url.path.endswith("/teams/current"):
            return httpx.Response(200, json={"id": 0})
        posts.append(request.url.path)
        raise httpx.ReadTimeout("private provider data", request=request)

    transport = httpx.MockTransport(provider)
    first = await submit_approved_service(
        covered[0], row["id"], user_id="alice", compose=compose,
        resolve_token=lambda _: "scoped-test-token", transport=transport,
    )
    replay = await submit_approved_service(
        covered[0], row["id"], user_id="alice", compose=compose,
        resolve_token=lambda _: "scoped-test-token", transport=transport,
    )
    assert first["status"] == replay["status"] == "uncertain"
    assert first["error"] == "timeout" and "private" not in str(first)
    assert posts == ["/api/v1/services"]


@pytest.mark.asyncio
async def test_bad_artifact_or_team_cannot_consume_claim(covered, context, monkeypatch):
    row, compose = await approved_deployment(covered, context, monkeypatch)
    calls = []

    def wrong_team(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"id": 999})

    transport = httpx.MockTransport(wrong_team)
    with pytest.raises(ValueError, match="does not match"):
        await submit_approved_service(
            covered[0], row["id"], user_id="alice", compose=compose + b"\n",
            resolve_token=lambda _: "scoped-test-token", transport=transport,
        )
    assert calls == []
    with pytest.raises(Exception, match="team_mismatch"):
        await submit_approved_service(
            covered[0], row["id"], user_id="alice", compose=compose,
            resolve_token=lambda _: "scoped-test-token", transport=transport,
        )
    assert calls == ["/api/v1/teams/current"]
    async with covered[0].acquire() as conn:
        assert await conn.fetchval("SELECT status FROM coding_deployments") == "reserved"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["failed", "expired", "changed_source"])
async def test_validation_proof_is_rechecked_before_provider_access(
        covered, context, monkeypatch, change):
    row, compose = await approved_deployment(covered, context, monkeypatch)
    updates = {
        "failed": "UPDATE coding_validation_claims SET validation_outcome='failed'",
        "expired": "UPDATE coding_validation_claims SET result_recorded_at=now()-interval '2 days',"
                   "result_expires_at=now()-interval '1 day'",
        "changed_source": "UPDATE coding_validation_claims SET request="
                          "jsonb_set(request,'{source_hash}','\"" + "f" * 64 + "\"')",
    }
    async with covered[0].acquire() as conn:
        await conn.execute(updates[change])
    calls = []
    transport = httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(200))
    with pytest.raises(deploy.DeploymentConflict, match="validation"):
        await submit_approved_service(
            covered[0], row["id"], user_id="alice", compose=compose,
            resolve_token=lambda _: pytest.fail("credential was consulted"), transport=transport,
        )
    assert calls == []
    async with covered[0].acquire() as conn:
        assert await conn.fetchval("SELECT status FROM coding_deployments") == "reserved"


@pytest.mark.asyncio
async def test_other_tenant_cannot_preflight_or_create(covered, context, monkeypatch):
    row, compose = await approved_deployment(covered, context, monkeypatch)
    with pytest.raises(deploy.DeploymentConflict, match="unavailable"):
        await submit_approved_service(
            covered[0], row["id"], user_id="mallory", compose=compose,
            resolve_token=lambda _: pytest.fail("credential was consulted"),
        )


@pytest.mark.asyncio
async def test_lost_response_reconciles_exact_service_without_repost(covered, context, monkeypatch):
    row, compose = await approved_deployment(covered, context, monkeypatch)
    manifest = deploy.DeploymentManifest.model_validate(deploy._json(row["manifest"]))
    names = []
    expose_sensitive = [False]

    def provider(request):
        if request.url.path.endswith("/teams/current"):
            return httpx.Response(200, json={"id": 0})
        if request.url.path == "/api/v1/servers/server":
            return httpx.Response(200, json={"id": 1, "uuid": "server"})
        if request.url.path == "/api/v1/projects/project":
            return httpx.Response(200, json={"id": 3, "uuid": "project"})
        if request.url.path == "/api/v1/projects/project/environment":
            return httpx.Response(200, json={"id": 2, "project_id": 3})
        if request.method == "POST":
            names.append(request.url.path)
            raise httpx.ReadTimeout("response lost", request=request)
        service = {
            "name": "gca-" + manifest.request_id.hex, "uuid": "created-service-1",
            "description": "gca-compose-sha256:" + sha256(compose).hexdigest(),
            "server_id": 1, "environment_id": 2,
            "deleted_at": None, "secrets": "never-return",
        }
        if expose_sensitive[0]:
            # Installed Coolify re-dumps YAML; byte equality is not valid.
            service["docker_compose_raw"] = yaml.safe_dump(
                yaml.safe_load(compose), sort_keys=True,
            )
        return httpx.Response(200, json=[service])

    transport = httpx.MockTransport(provider)
    observed_refs = []

    def recovery_token(reference):
        observed_refs.append(reference)
        assert reference == "test/recovery"
        return "scoped-test-token"

    await submit_approved_service(
        covered[0], row["id"], user_id="alice", compose=compose,
        resolve_token=lambda _: "scoped-test-token", transport=transport,
    )
    with pytest.raises(CoolifyServiceError, match="sensitive_read_required"):
        await reconcile_created_service(
            covered[0], row["id"], user_id="alice", compose=compose,
            resolve_token=recovery_token, transport=transport,
        )
    expose_sensitive[0] = True
    recovered = await reconcile_created_service(
        covered[0], row["id"], user_id="alice", compose=compose,
        resolve_token=recovery_token, transport=transport,
    )
    assert observed_refs == ["test/recovery", "test/recovery"]
    assert recovered == {"deployment_id": row["id"], "status": "submitted",
                         "resource_id": "created-service-1", "reconciled": True}
    assert "secrets" not in str(recovered)
    assert names == ["/api/v1/services"]


@pytest.mark.asyncio
async def test_reconciliation_never_adopts_wrong_or_duplicate_service(
        covered, context, monkeypatch):
    row, compose = await approved_deployment(covered, context, monkeypatch)
    manifest = deploy.DeploymentManifest.model_validate(deploy._json(row["manifest"]))

    def creation(request):
        if request.url.path.endswith("/teams/current"):
            return httpx.Response(200, json={"id": 0})
        return httpx.Response(503)

    await submit_approved_service(
        covered[0], row["id"], user_id="alice", compose=compose,
        resolve_token=lambda _: "scoped-test-token", transport=httpx.MockTransport(creation),
    )
    name = "gca-" + manifest.request_id.hex
    description = "gca-compose-sha256:" + sha256(compose).hexdigest()
    for services in (
        [{"name": name, "uuid": "wrong-content", "description": description,
          "docker_compose_raw": "secret",
          "server_id": 1, "environment_id": 2}],
        [{"name": name, "uuid": value, "description": description,
          "docker_compose_raw": compose.decode(),
          "server_id": 1, "environment_id": 2}
         for value in ("duplicate-1", "duplicate-2")],
    ):
        def listing(request):
            if request.url.path.endswith("/teams/current"):
                return httpx.Response(200, json={"id": 0})
            if request.url.path == "/api/v1/servers/server":
                return httpx.Response(200, json={"id": 1, "uuid": "server"})
            if request.url.path == "/api/v1/projects/project":
                return httpx.Response(200, json={"id": 3, "uuid": "project"})
            if request.url.path == "/api/v1/projects/project/environment":
                return httpx.Response(200, json={"id": 2, "project_id": 3})
            return httpx.Response(200, json=services)

        with pytest.raises(CoolifyServiceError, match="identity_ambiguous"):
            await reconcile_created_service(
                covered[0], row["id"], user_id="alice", compose=compose,
                resolve_token=lambda _: "scoped-test-token",
                transport=httpx.MockTransport(listing),
            )
    async with covered[0].acquire() as conn:
        assert await conn.fetchval("SELECT status FROM coding_deployments") == "uncertain"


@pytest.mark.asyncio
async def test_reconciliation_requires_same_server_and_retains_absent_outcome(
        covered, context, monkeypatch):
    row, compose = await approved_deployment(covered, context, monkeypatch)
    manifest = deploy.DeploymentManifest.model_validate(deploy._json(row["manifest"]))

    def provider(request):
        if request.url.path.endswith("/teams/current"):
            return httpx.Response(200, json={"id": 0})
        if request.url.path == "/api/v1/servers/server":
            return httpx.Response(200, json={"id": 1, "uuid": "server"})
        if request.url.path == "/api/v1/projects/project":
            return httpx.Response(200, json={"id": 3, "uuid": "project"})
        if request.url.path == "/api/v1/projects/project/environment":
            return httpx.Response(200, json={"id": 2, "project_id": 3})
        if request.method == "POST":
            return httpx.Response(503)
        return httpx.Response(200, json=[])

    transport = httpx.MockTransport(provider)
    await submit_approved_service(
        covered[0], row["id"], user_id="alice", compose=compose,
        resolve_token=lambda _: "scoped-test-token", transport=transport,
    )
    missing = await reconcile_created_service(
        covered[0], row["id"], user_id="alice", compose=compose,
        resolve_token=lambda _: "scoped-test-token", transport=transport,
    )
    assert missing["status"] == "uncertain" and missing["reconciled"] is False

    def wrong_server(request):
        if request.url.path == "/api/v1/services":
            return httpx.Response(200, json=[{
                "name": "gca-" + manifest.request_id.hex, "uuid": "wrong-server-service",
                "description": "gca-compose-sha256:" + sha256(compose).hexdigest(),
                "docker_compose_raw": compose.decode(), "server_id": 99,
                "environment_id": 2, "deleted_at": None,
            }])
        return provider(request)

    with pytest.raises(CoolifyServiceError, match="identity_ambiguous"):
        await reconcile_created_service(
            covered[0], row["id"], user_id="alice", compose=compose,
            resolve_token=lambda _: "scoped-test-token",
            transport=httpx.MockTransport(wrong_server),
        )
    async with covered[0].acquire() as conn:
        assert await conn.fetchval("SELECT status FROM coding_deployments") == "uncertain"
