"""Trusted IAP/SSH validation transport. Never expose its target config to a model.

    central committed claim -> pinned host gateway -> bounded evidence

No arbitrary remote command, caller-selected path, SSH agent forwarding or deployment
credentials. Transport failure is ambiguous execution, never an automatic retry.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from pathlib import Path
import shlex
import signal
import stat

from pydantic import BaseModel, ConfigDict, Field

from app.coding import isolated_validation as isolation


REMOTE_COMMAND = (
    "sudo -n /usr/bin/env GCA_VALIDATION_CONFIG=/etc/gca-validation/policy.json "
    "/usr/bin/python3 -I /usr/local/libexec/gca/isolated_validation.py --gateway"
)
MAX_RESPONSE_BYTES = 2_000_000


class ValidationTransportError(RuntimeError):
    """Sanitized transport category. Raw remote stderr may contain private data."""


def _failure_category(stderr: bytes) -> str:
    for markers, category in (
        ((b"Permission denied (publickey)",), "ssh_authentication"),
        ((b"Host key verification failed", b"REMOTE HOST IDENTIFICATION"), "host_identity"),
        ((b"ModuleNotFoundError", b"ImportError"), "operator_cli_installation"),
        ((b"PERMISSION_DENIED", b"do not have permission"), "iap_authorization"),
        ((b"password is required", b"a terminal is required"), "gateway_privilege"),
        ((b"failed to connect to backend",), "iap_backend"),
    ):
        if any(marker in stderr for marker in markers):
            return category
    return "host_execution_unconfirmed"


class IAPValidationHost(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    server_uuid: str = Field(pattern=r"^[a-zA-Z0-9-]{1,128}$")
    project_id: str = Field(pattern=r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
    zone: str = Field(pattern=r"^[a-z]+-[a-z]+[0-9]+-[a-z]$")
    instance: str = Field(pattern=r"^[a-z][a-z0-9-]{0,61}[a-z0-9]$")
    instance_id: str = Field(pattern=r"^[0-9]{1,24}$")
    ssh_user: str = Field(pattern=r"^[a-z_][a-z0-9_-]{0,63}$")
    gcloud_configuration: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    gcloud_config_directory: Path
    identity_file: Path
    known_hosts_file: Path
    host_public_key: str = Field(pattern=r"^ssh-ed25519 [A-Za-z0-9+/]{40,100}={0,2}$")
    gcloud_binary: Path = Path("/usr/bin/gcloud")
    ssh_binary: Path = Path("/usr/bin/ssh")


def _protected(path: Path, *, private=False) -> Path:
    if not path.is_absolute():
        raise ValidationTransportError("operator_path_must_be_absolute")
    path = path.resolve(strict=True)
    for entry in (path, *path.parents):
        info = entry.stat()
        if info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o022:
            raise ValidationTransportError("operator_path_not_protected")
    if private and path.stat().st_mode & 0o077:
        raise ValidationTransportError("credential_path_not_private")
    return path


def command(host: IAPValidationHost) -> tuple[list[str], dict]:
    key = _protected(host.identity_file, private=True)
    known = _protected(host.known_hosts_file)
    config = _protected(host.gcloud_config_directory, private=True)
    gcloud = _protected(host.gcloud_binary)
    ssh = _protected(host.ssh_binary)
    if (not key.is_file() or not known.is_file() or not config.is_dir()
            or not gcloud.is_file() or not ssh.is_file()
            or not gcloud.stat().st_mode & stat.S_IXUSR or not ssh.stat().st_mode & stat.S_IXUSR):
        raise ValidationTransportError("operator_transport_files_invalid")
    alias = "compute." + host.instance_id
    if known.stat().st_size > 1024 or known.read_text().strip() != (
        alias + " " + host.host_public_key
    ):
        raise ValidationTransportError("host_key_pin_missing")
    proxy = shlex.join([
        str(gcloud), "--configuration=" + host.gcloud_configuration,
        "compute", "start-iap-tunnel", host.instance, "22", "--listen-on-stdin",
        "--project=" + host.project_id, "--zone=" + host.zone, "--quiet",
    ])
    args = [
        str(ssh), "-F", "/dev/null", "-T", "-oBatchMode=yes", "-oIdentitiesOnly=yes",
        "-oIdentityAgent=none", "-oForwardAgent=no", "-oClearAllForwardings=yes",
        "-oStrictHostKeyChecking=yes", "-oUpdateHostKeys=no",
        "-oUserKnownHostsFile=" + str(known), "-oGlobalKnownHostsFile=/dev/null",
        "-oHostKeyAlias=" + alias, "-oHostKeyAlgorithms=ssh-ed25519",
        "-oConnectTimeout=15", "-oServerAliveInterval=15", "-oServerAliveCountMax=2",
        "-oProxyCommand=" + proxy, "-i", str(key), "-l", host.ssh_user,
        host.instance, REMOTE_COMMAND,
    ]
    env = {"HOME": "/nonexistent", "PATH": "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin",
           "CLOUDSDK_CONFIG": str(config), "CLOUDSDK_CORE_DISABLE_PROMPTS": "1"}
    return args, env


async def _exchange(host: IAPValidationHost, payload: bytes, timeout: int) -> dict:
    args, environment = command(host)
    process = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, env=environment, start_new_session=True,
    )

    async def write():
        try:
            process.stdin.write(payload)
            await process.stdin.drain()
        finally:
            process.stdin.close()

    async def read(stream):
        body = bytearray()
        while chunk := await stream.read(65536):
            if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                raise ValidationTransportError("host_output_limit")
            body.extend(chunk)
        return bytes(body)

    tasks = [asyncio.create_task(write()), asyncio.create_task(read(process.stdout)),
             asyncio.create_task(read(process.stderr))]
    try:
        async with asyncio.timeout(timeout):
            _, output, stderr = await asyncio.gather(*tasks)
            if await process.wait() != 0:
                raise ValidationTransportError(_failure_category(stderr))
        try:
            result = json.loads(output)
        except (ValueError, UnicodeError, RecursionError):
            raise ValidationTransportError("invalid_host_response") from None
        if not isinstance(result, dict):
            raise ValidationTransportError("invalid_host_response")
        return result
    except (TimeoutError, BrokenPipeError, ConnectionResetError):
        raise ValidationTransportError("host_transport_uncertain") from None
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        # Keep draining after cancellation/overflow: waiting with full PIPE buffers
        # can otherwise deadlock even after the child has been killed.
        async def discard(stream):
            while await stream.read(65536):
                pass

        try:
            await asyncio.wait_for(asyncio.gather(
                discard(process.stdout), discard(process.stderr), process.wait(),
            ), timeout=5)
        except TimeoutError:
            raise ValidationTransportError("local_transport_cleanup_uncertain") from None


async def dispatch(host: IAPValidationHost, envelope: dict, source: bytes) -> dict:
    """Call once, only with the envelope returned by a committed admission claim."""
    if (envelope.get("server_uuid") != host.server_uuid
            or envelope.get("source_hash") != hashlib.sha256(source).hexdigest()
            or len(source) > isolation.MAX_ARCHIVE_BYTES
            or type(envelope.get("timeout_seconds")) is not int
            or not 1 <= envelope["timeout_seconds"] <= 1800):
        raise ValidationTransportError("dispatch_binding_mismatch")
    header = json.dumps({"operation": "validate", "binding": envelope,
                         "archive_bytes": len(source)}, separators=(",", ":")).encode() + b"\n"
    if len(header) > 8192:
        raise ValidationTransportError("dispatch_header_limit")
    result = await _exchange(host, header + source, envelope["timeout_seconds"] + 90)
    if (result.get("claim_id") != envelope["claim_id"]
            or result.get("request_hash") != envelope["request_hash"]
            or result.get("source_hash") != envelope["source_hash"]
            or result.get("image_id") != envelope["image_id"]
            or result.get("profile") != envelope["profile"]
            or result.get("isolation") != "gvisor"
            or result.get("network") != "none"
            or result.get("workspace_mutations_discarded") is not True
            or not re.fullmatch(r"[a-f0-9]{32}", str(result.get("validation_job_id", "")))
            or result.get("cleanup_verified") is not True
            or type(result.get("success")) is not bool
            or type(result.get("exit_code")) is not int
            or result["success"] != (result["exit_code"] == 0)):
        raise ValidationTransportError("host_result_binding_mismatch")
    return result


async def observe(host: IAPValidationHost, claim_id: str) -> dict:
    from uuid import UUID
    if str(UUID(claim_id)) != claim_id:
        raise ValidationTransportError("invalid_claim_identity")
    payload = json.dumps({"operation": "observe", "claim_id": claim_id}).encode() + b"\n"
    return await _exchange(host, payload, 45)
