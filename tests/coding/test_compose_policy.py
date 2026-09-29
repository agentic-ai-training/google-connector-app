"""Generated-service Compose policy uses exact bytes and no provider calls."""
from hashlib import sha256
from uuid import uuid4

import pytest

from app.coding.compose_policy import ComposePolicyError, validate_compose
from app.coding.deployment_admission import DeploymentManifest
from app.coding.host_capacity import CapacityRequest
from datetime import datetime, timedelta, timezone


def approved(text: str, *, cpu=1500, memory=3072):
    data = text.encode()
    manifest = DeploymentManifest(
        request_id=uuid4(), target_id="test-target", target_hash="a" * 64,
        release_hash="b" * 64, compose_hash=sha256(data).hexdigest(),
        validation_attestation_hash="c" * 64, estimated_upper_bound_paise=0,
        capacity=CapacityRequest(pool_id="test-host", cpu_millis=cpu,
                                 memory_mib=memory, storage_mib=10240),
        planned_end_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    return data, manifest


def safe_compose():
    return ("services:\n"
            "  api:\n"
            "    image: registry.example/api@sha256:" + "a" * 64 + "\n"
            "    environment:\n"
            "      DATABASE_PASSWORD: '${DB_PASSWORD:?required}'\n"
            "    volumes: [dbdata:/data]\n"
            "    deploy:\n"
            "      resources:\n"
            "        limits: {cpus: '1.0', memory: 2GiB}\n"
            "volumes:\n"
            "  dbdata: {}\n")


def test_exact_digest_pinned_bounded_compose():
    data, manifest = approved(safe_compose())
    assert validate_compose(data, manifest) == safe_compose()
    with pytest.raises(ComposePolicyError, match="does not match"):
        validate_compose(data + b"\n", manifest)
    with pytest.raises(ComposePolicyError, match="capacity"):
        validate_compose(data, manifest.model_copy(update={
            "capacity": manifest.capacity.model_copy(update={"memory_mib": 1024}),
        }))


@pytest.mark.parametrize("fragment", [
    "    privileged: true\n",
    "    network_mode: host\n",
    "    ports: ['5432:5432']\n",
    "    build: .\n",
    "    labels: {traefik.enable: 'true'}\n",
    "    volumes: [/var/run/docker.sock:/var/run/docker.sock]\n",
    "    environment: {DATABASE_PASSWORD: plaintext}\n",
    "    deploy: {replicas: 100, resources: {limits: {cpus: '1.0', memory: 2GiB}}}\n",
    "    depends_on: [outside_service]\n",
    "    restart: always\n",
])
def test_host_escape_or_literal_secret_denied(fragment):
    text = safe_compose().replace("    volumes: [dbdata:/data]\n", fragment)
    data, manifest = approved(text)
    with pytest.raises(ComposePolicyError):
        validate_compose(data, manifest)


def test_alias_duplicate_key_and_unpinned_image_denied():
    for text in (
        safe_compose() + "services: {}\n",
        safe_compose().replace("api:\n", "api: &api\n"),
        safe_compose().replace("registry.example/api@sha256:" + "a" * 64,
                               "registry.example/api:latest"),
    ):
        data, manifest = approved(text)
        with pytest.raises(ComposePolicyError):
            validate_compose(data, manifest)


def test_replica_multiplier_is_not_hidden_inside_deployment_limits():
    text = safe_compose().replace(
        "    deploy:\n", "    deploy:\n      replicas: 100\n",
    )
    data, manifest = approved(text)
    with pytest.raises(ComposePolicyError, match="cannot scale"):
        validate_compose(data, manifest)


def test_malformed_restart_and_extreme_port_are_policy_failures_not_crashes():
    for fragment in ("    restart: [always]\n", "    expose: ['" + "9" * 5000 + "']\n"):
        text = safe_compose().replace("    volumes: [dbdata:/data]\n", fragment)
        data, manifest = approved(text)
        with pytest.raises(ComposePolicyError):
            validate_compose(data, manifest)
