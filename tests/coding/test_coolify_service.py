"""Single-write Coolify adapter proofs with a mocked provider and local PostgreSQL."""
import asyncio
import base64
from hashlib import sha256
import time
from uuid import UUID, uuid4

import httpx
import pytest
import yaml

from app.coding import deployment_admission as deploy
from app.coding import isolated_validation as isolation
from app.coding import validation_controller as controller
from app.coding import validation_transport as transport
from app.coding.build_provenance import (
    BuildProvenanceError, BuildStatement, SignedBuildAttestation, canonical_statement,
    verify_build_attestation,
)
from app.coding.compose_policy import release_identity
from app.coding.coolify_service import (
    CoolifyServiceClient, CoolifyServiceError, reconcile_created_service,
    submit_approved_service,
)
from tests.coding.test_compose_policy import safe_compose, safe_stack_compose
from tests.coding.test_cost_ledger_postgres import DSN
from tests.coding.test_deployment_admission_postgres import (
    TEST_BUILD_SIGNER, prepared as prepared_fixture,
)
from tests.coding.test_host_capacity_postgres import covered as covered_fixture
from tests.coding.test_validation_controller_postgres import (
    context as context_fixture, result as validation_result,
)
from tests.coding.test_isolated_validation import generated_stack

pytestmark = pytest.mark.skipif(not DSN, reason="set COST_LEDGER_TEST_DSN to local PostgreSQL")
covered = covered_fixture
prepared = prepared_fixture
context = context_fixture


def signed_build(manifest, target, images):
    from datetime import datetime, timedelta, timezone
    from uuid import uuid4

    now = datetime.now(timezone.utc)
    built = ("frontend", "backend") if manifest.release_profile == "react_spring_postgres" else (
        *images,
    )
    external = ("database",) if manifest.release_profile == "react_spring_postgres" else ()
    statement = BuildStatement(
        request_id=manifest.request_id, user_id=target.owner_user_id,
        target_id=manifest.target_id, target_hash=manifest.target_hash,
        source_hash=manifest.validated_source_hash, release_hash=manifest.release_hash,
        compose_hash=manifest.compose_hash, service_images=images,
        built_services=built, external_services=external,
        build_job_id=uuid4(), evidence_hash="e" * 64,
        issued_at=now, expires_at=now + timedelta(minutes=20),
    )
    return SignedBuildAttestation(
        statement=statement,
        signature_hex=TEST_BUILD_SIGNER.sign(canonical_statement(statement)).hex(),
    )


def target_response(request):
    """Mock installed-version read-only target endpoints, never a provider write."""
    values = {
        "/api/v1/teams/current": {"id": 0},
        "/api/v1/servers/server": {"id": 1, "uuid": "server"},
        "/api/v1/projects/project": {"id": 3, "uuid": "project"},
        "/api/v1/projects/project/environment": {
            "id": 2, "uuid": "environment", "project_id": 3,
        },
        "/api/v1/destinations/destination": {
            "uuid": "destination", "server_uuid": "server", "type": "standalone",
            "network": "gca-app-test-target",
        },
    }
    value = values.get(request.url.path)
    return httpx.Response(200, json=value) if value is not None else None


def test_provider_credential_references_must_be_distinct():
    with pytest.raises(ValueError, match="must be separate"):
        deploy.RegisteredTarget(
            target_id="test-target", base_url="http://127.0.0.1:18000", team_id=0,
            inventory_secret_ref="test/inventory",
            configuration_secret_ref="test/configuration",
            deployment_secret_ref="test/deploy",
            recovery_secret_ref="test/configuration",
            project_id="test-project", server_uuid="server", server_id=1,
            destination_uuid="destination", destination_id=7,
            destination_type="App\\Models\\StandaloneDocker",
            destination_network="gca-app-test-target",
            coolify_project_uuid="project", environment_uuid="environment",
            owner_user_id="alice",
        )


def test_coolify_destination_zero_is_valid_but_negative_is_not(prepared):
    payload = prepared[1].model_dump()
    payload["destination_id"] = 0
    assert deploy.RegisteredTarget.model_validate(payload).destination_id == 0
    payload["destination_id"] = -1
    with pytest.raises(ValueError):
        deploy.RegisteredTarget.model_validate(payload)
    payload = prepared[1].model_dump()
    payload["destination_network"] = "coolify"
    with pytest.raises(ValueError):
        deploy.RegisteredTarget.model_validate(payload)
    payload = prepared[1].model_dump()
    payload["approved_external_images"] = {"database": "postgres:latest"}
    with pytest.raises(ValueError, match="digest references"):
        deploy.RegisteredTarget.model_validate(payload)
    payload = prepared[1].model_dump()
    payload["server_id"] = -1
    with pytest.raises(ValueError):
        deploy.RegisteredTarget.model_validate(payload)


@pytest.mark.asyncio
async def test_server_id_is_operator_pinned_when_installed_api_omits_it(prepared):
    def without_id(request):
        known = target_response(request)
        assert known is not None
        if request.url.path == "/api/v1/servers/server":
            return httpx.Response(200, json={"uuid": "server"})
        return known

    async with CoolifyServiceClient(
        prepared[1], "scoped-test-token", transport=httpx.MockTransport(without_id),
    ) as client:
        assert await client._verified_target_ids() == (1, 2)

    def wrong_id(request):
        known = target_response(request)
        assert known is not None
        if request.url.path == "/api/v1/servers/server":
            return httpx.Response(200, json={"uuid": "server", "id": 99})
        return known

    async with CoolifyServiceClient(
        prepared[1], "scoped-test-token", transport=httpx.MockTransport(wrong_id),
    ) as client:
        with pytest.raises(CoolifyServiceError, match="target_identity_ambiguous"):
            await client._verified_target_ids()


@pytest.mark.asyncio
async def test_explicit_registered_destination_is_sent_without_guessing(prepared):
    target = prepared[1]

    def provider(request):
        assert request.url.path == "/api/v1/services"
        assert request.method == "POST"
        assert request.read()
        import json

        payload = json.loads(request.content)
        assert payload["destination_uuid"] == "destination"
        assert payload["instant_deploy"] is False
        assert payload["connect_to_docker_network"] is False
        return httpx.Response(201, json={"uuid": "created-service-1"})

    async with CoolifyServiceClient(
        target, "scoped-test-token", transport=httpx.MockTransport(provider),
    ) as client:
        result = await client.create(request_id=prepared[2].request_id,
                                     compose=safe_compose().encode())
    assert result == "created-service-1"


@pytest.mark.asyncio
async def test_inventory_rejects_sensitive_compose_list_response(prepared):
    def provider(request):
        path = request.url.path
        known = target_response(request)
        if known is not None:
            return known
        assert path == "/api/v1/services"
        return httpx.Response(200, json=[{"docker_compose_raw": "secret"}])

    async with CoolifyServiceClient(
        prepared[1], "inventory-token", transport=httpx.MockTransport(provider),
    ) as client:
        with pytest.raises(CoolifyServiceError, match="overscoped_inventory_token"):
            await client.find_service_candidate(
                request_id=prepared[2].request_id,
                compose_hash=prepared[2].compose_hash,
            )


async def approved_deployment(covered, context, monkeypatch, *, release_hash_override=None,
                              attestation_change=None):
    pool, target, manifest, _, _ = covered
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
    compose_hash = sha256(payload).hexdigest()
    manifest = manifest.model_copy(update={
        "request_id": request.run_id, "compose_hash": compose_hash,
        "validation_claim_id": UUID(outcome["claim_id"]),
        "validated_source_hash": request.source_hash,
        "validation_attestation_hash": result_hash,
        "release_hash": release_identity(request.source_hash, compose_hash, {
            "api": "registry.example/api@sha256:" + "a" * 64,
        }),
    })
    if release_hash_override is not None:
        manifest = manifest.model_copy(update={"release_hash": release_hash_override})
    signed = signed_build(
        manifest, target, {"api": "registry.example/api@sha256:" + "a" * 64},
    )
    if attestation_change in {"wrong_source", "wrong_image", "wrong_run",
                              "wrong_user", "wrong_target", "expired"}:
        from datetime import datetime, timedelta, timezone
        from uuid import uuid4

        changes = {
            "wrong_source": {"source_hash": "f" * 64},
            "wrong_run": {"request_id": uuid4()},
            "wrong_user": {"user_id": "mallory"},
            "wrong_target": {"target_id": "other-target"},
            "wrong_image": {"service_images": {
                "api": "registry.example/api@sha256:" + "b" * 64,
            }},
            "expired": {
                "issued_at": datetime.now(timezone.utc) - timedelta(hours=2),
                "expires_at": datetime.now(timezone.utc) - timedelta(hours=1),
            },
        }
        statement = signed.statement.model_copy(update=changes[attestation_change])
        signed = SignedBuildAttestation(
            statement=statement,
            signature_hex=TEST_BUILD_SIGNER.sign(canonical_statement(statement)).hex(),
        )
    elif attestation_change == "bad_signature":
        signed = signed.model_copy(update={"signature_hex": "0" * 128})
    elif attestation_change == "missing":
        signed = None
    manifest = manifest.model_copy(update={"build_attestation": signed})
    from datetime import datetime, timedelta, timezone

    await deploy.record_user_grant(
        pool, manifest, user_id="alice", authority_reference="test:user-approved-service",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
    )
    row = await deploy.enqueue(pool, manifest, user_id="alice", idempotency_key="service-one")
    return row, payload


async def approved_generated_stack(covered, context, monkeypatch, tmp_path,
                                   *, omit_backend_provenance=False):
    pool, request, host, _ = context
    generated_stack(tmp_path)
    source = isolation.source_archive(tmp_path)
    source_hash = sha256(source).hexdigest()

    async def validated(_host, envelope, _source):
        job_id = envelope["claim_id"].replace("-", "")
        result = validation_result(envelope, validation_job_id=job_id)
        # Mock the source-bound export contract; this fixture is not live build proof.
        if envelope["profile"] in {"react_build", "spring_package"}:
            result["build_artifact"] = {
                "artifact_id": job_id + "-" + "e" * 64 + ".tar",
                "archive_sha256": "e" * 64, "file_set_sha256": "f" * 64,
                "file_count": 1, "content_bytes": 10, "profile": envelope["profile"],
                "validation_job_id": job_id, "claim_id": envelope["claim_id"],
                "source_hash": envelope["source_hash"],
                "expires_at_unix": int(time.time()) + 3600,
            }
        return result

    monkeypatch.setattr(transport, "dispatch", validated)
    proofs = []
    for index, profile in enumerate(sorted(deploy.GENERATED_STACK_PROFILES), start=1):
        step = request.model_copy(update={
            "sequence_no": index, "profile": profile, "source_hash": source_hash,
        })
        outcome = await controller.execute(
            pool, step, user_id="alice", lease_owner="worker-one",
            idempotency_key="generated-stack-" + profile, host=host, source=source,
        )
        assert outcome["outcome"] == "passed"
        async with pool.acquire() as conn:
            result_hash = await conn.fetchval(
                "SELECT result_hash FROM coding_validation_claims WHERE id=$1",
                UUID(outcome["claim_id"]),
            )
        proofs.append(deploy.ValidationProof(
            profile=profile, claim_id=UUID(outcome["claim_id"]), result_hash=result_hash,
        ))
    proofs = tuple(proofs)
    compose = safe_stack_compose().encode()
    compose_hash = sha256(compose).hexdigest()
    images = {
        "frontend": "registry.example/frontend@sha256:" + "a" * 64,
        "backend": "registry.example/backend@sha256:" + "b" * 64,
        "database": "registry.example/postgres@sha256:" + "c" * 64,
    }
    from datetime import datetime, timedelta, timezone

    base = covered[2]
    manifest = deploy.DeploymentManifest.model_validate({
        **base.model_dump(), "request_id": request.run_id,
        "release_profile": "react_spring_postgres", "validation_proofs": proofs,
        "validation_claim_id": None, "validated_source_hash": source_hash,
        "validation_attestation_hash": deploy.validation_set_hash(proofs),
        "compose_hash": compose_hash,
        "release_hash": release_identity(source_hash, compose_hash, images),
        "capacity": base.capacity.model_copy(update={
            "cpu_millis": 1500, "memory_mib": 3072,
        }),
    })
    signed = signed_build(manifest, covered[1], images)
    if omit_backend_provenance:
        statement = signed.statement.model_copy(update={
            "built_services": ("frontend",),
            "external_services": ("backend", "database"),
        })
        signed = SignedBuildAttestation(
            statement=statement,
            signature_hex=TEST_BUILD_SIGNER.sign(canonical_statement(statement)).hex(),
        )
    manifest = manifest.model_copy(update={"build_attestation": signed})
    await deploy.record_user_grant(
        pool, manifest, user_id="alice", authority_reference="test:generated-stack-approved",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
    )
    row = await deploy.enqueue(
        pool, manifest, user_id="alice", idempotency_key="generated-stack-service",
    )
    return row, compose, proofs


@pytest.mark.asyncio
async def test_generated_stack_requires_operator_pinned_database_digest(
        covered, context, monkeypatch, tmp_path):
    row, compose, _ = await approved_generated_stack(
        covered, context, monkeypatch, tmp_path,
    )
    manifest = deploy.DeploymentManifest.model_validate(deploy._json(row["manifest"]))
    assert verify_build_attestation(covered[1], manifest, compose=compose) is None
    for pinned in ({}, {"database": "registry.example/postgres@sha256:" + "f" * 64}):
        unapproved = covered[1].model_copy(update={"approved_external_images": pinned})
        with pytest.raises(BuildProvenanceError, match="not operator approved"):
            verify_build_attestation(unapproved, manifest, compose=compose)


@pytest.mark.asyncio
async def test_generated_stack_requires_all_four_real_profile_results_before_create(
        covered, context, monkeypatch, tmp_path):
    row, compose, proofs = await approved_generated_stack(
        covered, context, monkeypatch, tmp_path,
    )
    posts = []

    def provider(request):
        known = target_response(request)
        if known is not None:
            return known
        assert request.url.path == "/api/v1/services" and request.method == "POST"
        posts.append(request.url.path)
        return httpx.Response(201, json={"uuid": "generated-stack-service-1"})

    outcome = await submit_approved_service(
        covered[0], row["id"], user_id="alice", compose=compose,
        resolve_token=lambda _: "scoped-test-token", transport=httpx.MockTransport(provider),
    )
    assert len(proofs) == 4 and len(posts) == 1
    assert outcome["status"] == "submitted" and outcome["dispatched"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["failed", "expired", "wrong_profile", "wrong_source"])
async def test_generated_stack_missing_passing_profile_never_contacts_provider(
        covered, context, monkeypatch, tmp_path, failure):
    row, compose, proofs = await approved_generated_stack(
        covered, context, monkeypatch, tmp_path,
    )
    updates = {
        "failed": "UPDATE coding_validation_claims SET validation_outcome='failed' WHERE id=$1",
        "expired": "UPDATE coding_validation_claims SET "
                   "result_recorded_at=now()-interval '2 days',"
                   "result_expires_at=now()-interval '1 day' WHERE id=$1",
        "wrong_profile": "UPDATE coding_validation_claims SET request=jsonb_set(request,'{profile}',"
                         "'\"python_compile\"') WHERE id=$1",
        "wrong_source": "UPDATE coding_validation_claims SET request=jsonb_set(request,'{source_hash}',"
                        "'\"" + "f" * 64 + "\"') WHERE id=$1",
    }
    async with covered[0].acquire() as conn:
        await conn.execute(updates[failure], proofs[-1].claim_id)
    with pytest.raises(deploy.DeploymentConflict, match="validation"):
        await submit_approved_service(
            covered[0], row["id"], user_id="alice", compose=compose,
            resolve_token=lambda _: pytest.fail("credential was consulted"),
        )
    async with covered[0].acquire() as conn:
        assert await conn.fetchval(
            "SELECT status FROM coding_deployments WHERE id=$1", row["id"],
        ) == "reserved"


@pytest.mark.asyncio
async def test_generated_stack_requires_signed_provenance_for_both_generated_images(
        covered, context, monkeypatch, tmp_path):
    row, compose, _ = await approved_generated_stack(
        covered, context, monkeypatch, tmp_path, omit_backend_provenance=True,
    )
    with pytest.raises(BuildProvenanceError, match="incomplete"):
        await submit_approved_service(
            covered[0], row["id"], user_id="alice", compose=compose,
            resolve_token=lambda _: pytest.fail("credential was consulted"),
        )
    async with covered[0].acquire() as conn:
        assert await conn.fetchval(
            "SELECT status FROM coding_deployments WHERE id=$1", row["id"],
        ) == "reserved"


@pytest.mark.asyncio
async def test_one_provider_create_then_record_only_submission(covered, context, monkeypatch):
    row, compose = await approved_deployment(covered, context, monkeypatch)
    posts = []

    def provider(request):
        known = target_response(request)
        if known is not None:
            return known
        assert request.url.path == "/api/v1/services"
        assert request.headers["Authorization"] == "Bearer scoped-test-token"
        body = request.read().decode()
        import json

        payload = json.loads(body)
        assert payload["instant_deploy"] is False
        assert payload["connect_to_docker_network"] is False
        assert payload["force_domain_override"] is False
        assert base64.b64decode(payload["docker_compose_raw"]) == compose
        assert payload["description"] == "gca-compose-sha256:" + sha256(compose).hexdigest()
        assert payload["destination_uuid"] == "destination"
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
        known = target_response(request)
        if known is not None:
            return known
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
        known = target_response(request)
        if known is not None:
            return known
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
async def test_unbound_release_is_denied_before_credentials_or_provider_access(
        covered, context, monkeypatch):
    row, compose = await approved_deployment(
        covered, context, monkeypatch, release_hash_override="f" * 64,
    )
    with pytest.raises(ValueError, match="release identity"):
        await submit_approved_service(
            covered[0], row["id"], user_id="alice", compose=compose,
            resolve_token=lambda _: pytest.fail("credential was consulted"),
        )
    async with covered[0].acquire() as conn:
        assert await conn.fetchval(
            "SELECT status FROM coding_deployments WHERE id=$1", row["id"],
        ) == "reserved"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    "missing", "bad_signature", "wrong_source", "wrong_image", "wrong_run",
    "wrong_user", "wrong_target", "expired",
])
async def test_untrusted_or_mismatched_build_statement_cannot_reach_coolify(
        covered, context, monkeypatch, change):
    row, compose = await approved_deployment(
        covered, context, monkeypatch, attestation_change=change,
    )
    with pytest.raises(BuildProvenanceError, match="build attestation"):
        await submit_approved_service(
            covered[0], row["id"], user_id="alice", compose=compose,
            resolve_token=lambda _: pytest.fail("credential was consulted"),
        )
    async with covered[0].acquire() as conn:
        assert await conn.fetchval(
            "SELECT status FROM coding_deployments WHERE id=$1", row["id"],
        ) == "reserved"


@pytest.mark.asyncio
async def test_dispatch_transaction_rechecks_signed_build_without_provider_adapter(
        covered, context, monkeypatch):
    row, _ = await approved_deployment(
        covered, context, monkeypatch, attestation_change="bad_signature",
    )
    with pytest.raises(BuildProvenanceError, match="signature"):
        await deploy.claim_dispatch(covered[0], row["id"], user_id="alice")
    async with covered[0].acquire() as conn:
        assert await conn.fetchval(
            "SELECT status FROM coding_deployments WHERE id=$1", row["id"],
        ) == "reserved"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    "missing_project", "wrong_environment", "wrong_destination_server",
    "wrong_destination_type", "shared_coolify_network",
])
async def test_target_preflight_denies_write_before_claim(
        covered, context, monkeypatch, failure):
    row, compose = await approved_deployment(covered, context, monkeypatch)
    posts = []

    def provider(request):
        path = request.url.path
        if request.method == "POST":
            posts.append(path)
            return httpx.Response(201, json={"uuid": "should-not-exist"})
        if failure == "missing_project" and path == "/api/v1/projects/project":
            return httpx.Response(404)
        if failure == "wrong_environment" and path.endswith("/project/environment"):
            return httpx.Response(200, json={
                "id": 2, "uuid": "another-environment", "project_id": 3,
            })
        if path == "/api/v1/destinations/destination":
            if failure == "wrong_destination_server":
                return httpx.Response(200, json={
                    "uuid": "destination", "server_uuid": "another-server",
                    "type": "standalone", "network": "gca-app-test-target",
                })
            if failure == "wrong_destination_type":
                return httpx.Response(200, json={
                    "uuid": "destination", "server_uuid": "server", "type": "swarm",
                    "network": "gca-app-test-target",
                })
            if failure == "shared_coolify_network":
                return httpx.Response(200, json={
                    "uuid": "destination", "server_uuid": "server",
                    "type": "standalone", "network": "coolify",
                })
        known = target_response(request)
        assert known is not None
        return known

    with pytest.raises(CoolifyServiceError):
        await submit_approved_service(
            covered[0], row["id"], user_id="alice", compose=compose,
            resolve_token=lambda _: "scoped-test-token",
            transport=httpx.MockTransport(provider),
        )
    assert posts == []
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
        known = target_response(request)
        if known is not None:
            return known
        if request.method == "POST":
            names.append(request.url.path)
            raise httpx.ReadTimeout("response lost", request=request)
        service = {
            "name": "gca-" + manifest.request_id.hex, "uuid": "created-service-1",
            "description": "gca-compose-sha256:" + sha256(compose).hexdigest(),
            "server_id": 1, "environment_id": 2,
            "destination_id": 7, "destination_type": "App\\Models\\StandaloneDocker",
            "deleted_at": None, "secrets": "never-return",
            "connect_to_docker_network": False,
        }
        if request.url.path == "/api/v1/services":
            assert request.headers["Authorization"] == "Bearer inventory-token"
            return httpx.Response(200, json=[service])
        assert request.url.path == "/api/v1/services/created-service-1"
        assert request.headers["Authorization"] == "Bearer recovery-token"
        if expose_sensitive[0]:
            # Installed Coolify re-dumps YAML; byte equality is not valid.
            service["docker_compose_raw"] = yaml.safe_dump(
                yaml.safe_load(compose), sort_keys=True,
            )
        return httpx.Response(200, json=service)

    transport = httpx.MockTransport(provider)
    observed_refs = []

    def recovery_token(reference):
        observed_refs.append(reference)
        assert reference in {"test/inventory", "test/recovery"}
        return "inventory-token" if reference == "test/inventory" else "recovery-token"

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
    assert observed_refs == ["test/inventory", "test/recovery",
                             "test/inventory", "test/recovery"]
    assert recovered == {"deployment_id": row["id"], "status": "submitted",
                         "resource_id": "created-service-1", "reconciled": True}
    assert "secrets" not in str(recovered)
    assert names == ["/api/v1/services"]


@pytest.mark.asyncio
async def test_recovery_rejects_shared_or_unreported_destination_attachment(prepared):
    request_id = uuid4()
    compose = safe_compose().encode()
    for attachment in (True, None):
        service = {
            "name": "gca-" + request_id.hex, "uuid": "created-service-1",
            "description": "gca-compose-sha256:" + sha256(compose).hexdigest(),
            "server_id": 1, "environment_id": 2,
            "destination_id": 7, "destination_type": "App\\Models\\StandaloneDocker",
            "deleted_at": None, "docker_compose_raw": compose.decode(),
        }
        if attachment is not None:
            service["connect_to_docker_network"] = attachment

        def provider(request):
            assert request.url.path == "/api/v1/services/created-service-1"
            return httpx.Response(200, json=service)

        async with CoolifyServiceClient(
            prepared[1], "scoped-test-token", transport=httpx.MockTransport(provider),
        ) as client:
            with pytest.raises(CoolifyServiceError, match="shared_destination_network_forbidden"):
                await client.verify_service_content(
                    resource_id="created-service-1", request_id=request_id,
                    compose=compose, server_id=1, environment_id=2,
                )


@pytest.mark.asyncio
async def test_expired_build_statement_blocks_new_write_but_not_read_only_recovery(
        covered, context, monkeypatch):
    from datetime import datetime, timedelta
    from app.coding import build_provenance as provenance

    row, compose = await approved_deployment(covered, context, monkeypatch)
    manifest = deploy.DeploymentManifest.model_validate(deploy._json(row["manifest"]))
    calls = []
    service = {
        "name": "gca-" + manifest.request_id.hex, "uuid": "created-after-expiry",
        "description": "gca-compose-sha256:" + sha256(compose).hexdigest(),
        "server_id": 1, "environment_id": 2, "destination_id": 7,
        "destination_type": "App\\Models\\StandaloneDocker", "deleted_at": None,
        "connect_to_docker_network": False,
    }

    def provider(request):
        known = target_response(request)
        if known is not None:
            return known
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx.Response(503)
        if request.url.path == "/api/v1/services":
            return httpx.Response(200, json=[service])
        assert request.url.path == "/api/v1/services/created-after-expiry"
        return httpx.Response(200, json={**service, "docker_compose_raw": compose.decode()})

    transport = httpx.MockTransport(provider)
    dispatched = await submit_approved_service(
        covered[0], row["id"], user_id="alice", compose=compose,
        resolve_token=lambda _: "scoped-test-token", transport=transport,
    )
    assert dispatched["status"] == "uncertain"

    real_datetime = datetime

    class LaterDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime.now(tz) + timedelta(hours=2)

    monkeypatch.setattr(provenance, "datetime", LaterDateTime)
    with pytest.raises(BuildProvenanceError, match="does not bind"):
        provenance.verify_build_attestation(covered[1], manifest, compose=compose)
    altered = manifest.model_copy(update={
        "build_attestation": manifest.build_attestation.model_copy(update={
            "signature_hex": "0" * 128,
        }),
    })
    with pytest.raises(BuildProvenanceError, match="signature is invalid"):
        provenance.verify_build_attestation(
            covered[1], altered, compose=compose, require_fresh=False,
        )
    result = await reconcile_created_service(
        covered[0], row["id"], user_id="alice", compose=compose,
        resolve_token=lambda _: "scoped-test-token", transport=transport,
    )
    assert result["status"] == "submitted" and result["reconciled"] is True
    assert result["resource_id"] == "created-after-expiry"
    assert calls == [
        ("POST", "/api/v1/services"), ("GET", "/api/v1/services"),
        ("GET", "/api/v1/services/created-after-expiry"),
    ]


@pytest.mark.asyncio
async def test_reconciliation_never_adopts_wrong_or_duplicate_service(
        covered, context, monkeypatch):
    row, compose = await approved_deployment(covered, context, monkeypatch)
    manifest = deploy.DeploymentManifest.model_validate(deploy._json(row["manifest"]))

    def creation(request):
        known = target_response(request)
        if known is not None:
            return known
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
          "server_id": 1, "environment_id": 2,
          "destination_id": 7, "destination_type": "App\\Models\\StandaloneDocker",
          "connect_to_docker_network": False}],
        [{"name": name, "uuid": value, "description": description,
          "docker_compose_raw": compose.decode(),
          "server_id": 1, "environment_id": 2,
          "destination_id": 7, "destination_type": "App\\Models\\StandaloneDocker",
          "connect_to_docker_network": False}
         for value in ("duplicate-1", "duplicate-2")],
    ):
        def listing(request):
            known = target_response(request)
            if known is not None:
                return known
            if request.url.path == "/api/v1/services":
                return httpx.Response(200, json=[
                    {key: value for key, value in service.items()
                     if key != "docker_compose_raw"}
                    for service in services
                ])
            assert request.url.path == "/api/v1/services/wrong-content"
            return httpx.Response(200, json=services[0])

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
        known = target_response(request)
        if known is not None:
            return known
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
                "server_id": 99,
                "environment_id": 2, "destination_id": 7,
                "destination_type": "App\\Models\\StandaloneDocker", "deleted_at": None,
            }])
        return provider(request)

    with pytest.raises(CoolifyServiceError, match="identity_ambiguous"):
        await reconcile_created_service(
            covered[0], row["id"], user_id="alice", compose=compose,
            resolve_token=lambda _: "scoped-test-token",
            transport=httpx.MockTransport(wrong_server),
        )
    def wrong_destination(request):
        if request.url.path == "/api/v1/services":
            return httpx.Response(200, json=[{
                "name": "gca-" + manifest.request_id.hex, "uuid": "wrong-destination",
                "description": "gca-compose-sha256:" + sha256(compose).hexdigest(),
                "server_id": 1,
                "environment_id": 2, "destination_id": 99,
                "destination_type": "App\\Models\\StandaloneDocker",
                "deleted_at": None,
            }])
        return provider(request)

    with pytest.raises(CoolifyServiceError, match="identity_ambiguous"):
        await reconcile_created_service(
            covered[0], row["id"], user_id="alice", compose=compose,
            resolve_token=lambda _: "scoped-test-token",
            transport=httpx.MockTransport(wrong_destination),
        )
    async with covered[0].acquire() as conn:
        assert await conn.fetchval("SELECT status FROM coding_deployments") == "uncertain"
