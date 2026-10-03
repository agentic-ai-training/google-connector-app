"""Bounded protocol tests; no real IAP credentials or VM calls."""
import hashlib
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.coding import image_build_transport as transport, validation_transport as exchange
from app.coding import image_output_store as outputs, oci_packager as oci
from tests.coding.test_image_packaging_worker import context as context_fixture, assemble

context = context_fixture


def measurement(context):
    receipt = assemble(context)
    return {**receipt, "packaging_job_id": "a" * 32, "execution_cleanup_verified": True}


@pytest.mark.asyncio
async def test_fixed_framing_uses_host_artifact_not_upload_or_shell(context, monkeypatch):
    binding = context[-1]
    expected = measurement(context)
    host = SimpleNamespace(server_uuid=binding["server_uuid"], project_id=binding["project_id"])

    async def send(host_, payload, timeout, **kwargs):
        assert kwargs == {"gateway": "packaging"} and timeout == binding["timeout_seconds"] + 90
        request = json.loads(payload)
        assert set(request) == {"operation", "binding", "validation_binding", "reference"}
        assert request["operation"] == "assemble" and request["binding"] == binding
        return expected

    monkeypatch.setattr(exchange, "_exchange", send)
    validation_binding = dict(server_uuid=binding["server_uuid"], claim_id=binding["validation_claim_id"],
                              source_hash=binding["source_hash"], profile=binding["profile"])
    assert await transport.assemble(host, binding, validation_binding, context[4]) == expected


@pytest.mark.parametrize("field,value", [
    ("binding_hash", "0" * 64), ("execution_cleanup_verified", 1), ("packaging_job_id", "invalid"),
    ("output_archive_bytes", True), ("output_archive_bytes", outputs.MAX_OUTPUT_BYTES + 1),
    ("output_expires_at_unix", 0), ("completed_at", "2026-01-01T00:00:00"),
])
def test_invalid_measurement_is_not_a_success(context, field, value):
    data = measurement(context)
    data[field] = value
    with pytest.raises(exchange.ValidationTransportError):
        transport.verify_measurement(context[-1], data)


def test_altered_runtime_evidence_is_rejected(context):
    data = measurement(context)
    data["evidence"]["generated_code_executed"] = True
    with pytest.raises(exchange.ValidationTransportError, match="evidence"):
        transport.verify_measurement(context[-1], data)


@pytest.mark.asyncio
async def test_cleanup_requires_both_process_and_volume_removal(context, monkeypatch):
    binding = context[-1]
    host = SimpleNamespace(server_uuid=binding["server_uuid"], project_id=binding["project_id"])
    result = dict(build_job_id=binding["build_job_id"], packaging_job_id="a" * 32,
                  binding_hash=hashlib.sha256(oci.canonical(binding)).hexdigest(),
                  execution_cleanup_verified=True, output_removed=True, staging_removed=False,
                  observed_at=datetime.now(timezone.utc).isoformat())

    async def send(*args, **kwargs):
        return result

    monkeypatch.setattr(exchange, "_exchange", send)
    with pytest.raises(exchange.ValidationTransportError, match="cleanup_binding"):
        await transport.cleanup(host, binding)
    result["staging_removed"] = True
    assert await transport.cleanup(host, binding) == result


@pytest.mark.asyncio
async def test_foreign_host_cannot_read_or_remove_private_images(context):
    host = SimpleNamespace(server_uuid="another-host", project_id=context[-1]["project_id"])
    for operation in (transport.recover, transport.cleanup):
        with pytest.raises(exchange.ValidationTransportError, match="host_binding"):
            await operation(host, context[-1])


@pytest.mark.asyncio
async def test_packaging_transport_cannot_select_arbitrary_remote_command(monkeypatch):
    monkeypatch.setattr(exchange, "command", lambda host: (["ssh", "pinned-host", "validation-command"], {}))

    async def spawn(*args, **kwargs):
        assert args == ("ssh", "pinned-host", exchange.REMOTE_PACKAGING_COMMAND)
        assert kwargs["env"] == {} and kwargs["start_new_session"] is True
        raise OSError("test stops before spawning")

    monkeypatch.setattr(exchange.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(OSError, match="before spawning"):
        await exchange._exchange(None, b"{}\n", 1, gateway="packaging")
    for invalid in ("bash", "sudo rm", None, {}):
        with pytest.raises(exchange.ValidationTransportError, match="unsupported_fixed_host_gateway"):
            await exchange._exchange(None, b"{}\n", 1, gateway=invalid)
