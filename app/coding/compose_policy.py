"""Bounded baseline policy for a generated, not-yet-started Coolify Service.

This is not an image scan, a build attestation, or a runtime storage quota. Those
remain independent gates before a created Service may be started for a user.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import hashlib
import re

import yaml

from app.coding.deployment_admission import DeploymentManifest


class ComposePolicyError(ValueError):
    pass


MAX_COMPOSE_BYTES = 131_072
IMAGE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]{1,255}@sha256:[0-9a-f]{64}$")
NAME = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
VOLUME = re.compile(r"^([a-z][a-z0-9_-]{0,62}):(/[a-zA-Z0-9._/-]+)(?::ro)?$")
SECRET_NAME = re.compile(r"(?:PASSWORD|SECRET|TOKEN|API_KEY|PRIVATE_KEY)", re.I)
REQUIRED_REFERENCE = re.compile(r"^\$\{[A-Z][A-Z0-9_]*:\?[^}]*\}$")
MEMORY = re.compile(r"^(\d+)(MiB|GiB)$")
SERVICE_KEYS = {
    "image", "environment", "depends_on", "expose", "volumes", "healthcheck",
    "restart", "command", "entrypoint", "user", "read_only", "cap_drop",
    "deploy", "working_dir",
}


class _UniqueSafeLoader(yaml.SafeLoader):
    pass


def _unique_mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str) or key in result:
            raise ComposePolicyError("duplicate or non-string Compose key")
        result[key] = loader.construct_object(value_node)
    return result


_UniqueSafeLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def _cpu_millis(value) -> int:
    if isinstance(value, bool):
        raise ComposePolicyError("invalid CPU limit")
    try:
        cpu = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ComposePolicyError("invalid CPU limit") from None
    millis = cpu * 1000
    if not cpu.is_finite() or millis != int(millis) or not 1 <= millis <= 64_000:
        raise ComposePolicyError("invalid CPU limit")
    return int(millis)


def _memory_mib(value) -> int:
    if not isinstance(value, str):
        raise ComposePolicyError("invalid memory limit")
    match = MEMORY.fullmatch(value)
    if not match:
        raise ComposePolicyError("invalid memory limit")
    mib = int(match.group(1)) * (1024 if match.group(2) == "GiB" else 1)
    if not 1 <= mib <= 262_144:
        raise ComposePolicyError("invalid memory limit")
    return mib


def validate_compose(compose: bytes, manifest: DeploymentManifest) -> str:
    """Return exact approved UTF-8 Compose; reject host escape and unbounded service use."""
    if not isinstance(compose, bytes) or not 1 <= len(compose) <= MAX_COMPOSE_BYTES:
        raise ComposePolicyError("Compose artifact size is invalid")
    if hashlib.sha256(compose).hexdigest() != manifest.compose_hash:
        raise ComposePolicyError("Compose artifact does not match approved manifest")
    try:
        text = compose.decode("utf-8")
        # Aliases/anchors can obscure policy and greatly amplify parser work.
        if any(isinstance(token, (yaml.tokens.AnchorToken, yaml.tokens.AliasToken))
               for token in yaml.scan(text)):
            raise ComposePolicyError("Compose YAML anchors are not permitted")
        document = yaml.load(text, Loader=_UniqueSafeLoader)  # nosec B506
    except (UnicodeError, yaml.YAMLError, RecursionError):
        raise ComposePolicyError("Compose artifact is invalid YAML") from None
    if not isinstance(document, dict) or set(document) - {"services", "volumes"}:
        raise ComposePolicyError("Compose root contains unsupported authority")
    services = document.get("services")
    volumes = document.get("volumes", {})
    if not isinstance(services, dict) or not 1 <= len(services) <= 12:
        raise ComposePolicyError("Compose must define 1–12 services")
    if not isinstance(volumes, dict) or len(volumes) > 24 or any(
        not NAME.fullmatch(name) or definition not in ({}, None)
        for name, definition in volumes.items()
    ):
        raise ComposePolicyError("only internal named volumes are permitted")
    total_cpu = total_memory = 0
    for name, service in services.items():
        if not isinstance(name, str) or not NAME.fullmatch(name) or not isinstance(service, dict):
            raise ComposePolicyError("invalid service declaration")
        if set(service) - SERVICE_KEYS or not IMAGE.fullmatch(str(service.get("image", ""))):
            raise ComposePolicyError("service requires a digest-pinned image and approved fields")
        dependencies = service.get("depends_on", [])
        if (not isinstance(dependencies, list) or len(dependencies) > len(services)
                or any(not isinstance(item, str) or item == name or item not in services
                       for item in dependencies)):
            raise ComposePolicyError("service dependencies must stay inside this stack")
        exposed = service.get("expose", [])
        if (not isinstance(exposed, list) or len(exposed) > 8
                or any(not re.fullmatch(r"[0-9]{1,5}", str(port))
                       or not 1 <= int(port) <= 65535
                       for port in exposed)):
            raise ComposePolicyError("service exposed ports are invalid")
        restart = service.get("restart", "no")
        if not isinstance(restart, str) or restart not in {"no", "on-failure"}:
            raise ComposePolicyError("service restart policy is not bounded")
        env = service.get("environment", {})
        if not isinstance(env, dict) or len(env) > 64:
            raise ComposePolicyError("service environment must be a bounded mapping")
        for key, value in env.items():
            if not isinstance(key, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", key):
                raise ComposePolicyError("invalid environment key")
            if SECRET_NAME.search(key) and not REQUIRED_REFERENCE.fullmatch(str(value)):
                raise ComposePolicyError("literal secrets are not permitted in Compose")
            if not isinstance(value, (str, int, float, bool)) or len(str(value)) > 2048:
                raise ComposePolicyError("environment value exceeds bounds")
        mounts = service.get("volumes", [])
        if not isinstance(mounts, list) or len(mounts) > 12:
            raise ComposePolicyError("service mounts exceed policy")
        for mount in mounts:
            match = VOLUME.fullmatch(mount) if isinstance(mount, str) else None
            if not match or match.group(1) not in volumes or ".." in match.group(2).split("/"):
                raise ComposePolicyError("host bind or unknown volume is not permitted")
        deployment = service.get("deploy")
        if not isinstance(deployment, dict) or set(deployment) != {"resources"}:
            raise ComposePolicyError("service deployment cannot scale or override placement")
        resources = deployment["resources"]
        if not isinstance(resources, dict) or set(resources) != {"limits"}:
            raise ComposePolicyError("service requires explicit CPU and memory limits")
        limits = resources["limits"]
        if not isinstance(limits, dict) or set(limits) != {"cpus", "memory"}:
            raise ComposePolicyError("service requires explicit CPU and memory limits")
        total_cpu += _cpu_millis(limits["cpus"])
        total_memory += _memory_mib(limits["memory"])
    capacity = manifest.capacity
    if capacity is None or total_cpu > capacity.cpu_millis or total_memory > capacity.memory_mib:
        raise ComposePolicyError("Compose exceeds approved funded host capacity")
    return text
