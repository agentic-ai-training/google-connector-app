import hashlib
import json
from types import SimpleNamespace
import time
from uuid import uuid4

import pytest

from app.coding import validation_transport as transport


def case():
    body = b"synthetic archive bytes"
    job = "a" * 32
    checksum = hashlib.sha256(body).hexdigest()
    binding = {"server_uuid": "host-123", "profile": "react_build",
               "source_hash": "b" * 64, "claim_id": str(uuid4())}
    reference = {"artifact_id": job + "-" + checksum + ".tar", "archive_sha256": checksum,
                 "file_set_sha256": "c" * 64, "file_count": 1, "content_bytes": 10,
                 "profile": binding["profile"], "validation_job_id": job,
                 "claim_id": binding["claim_id"], "source_hash": binding["source_hash"],
                 "expires_at_unix": int(time.time()) + 3600}
    return body, binding, reference


@pytest.mark.asyncio
async def test_binary_private_transfer_is_exact_and_bounded(monkeypatch):
    body, binding, reference = case()
    target = SimpleNamespace(server_uuid="host-123")

    async def exchange(host, payload, timeout, **options):
        request = json.loads(payload)
        assert request == {"operation": "artifact", "binding": binding, "reference": reference}
        assert timeout == 120 and options["binary"]
        assert options["response_limit"] == 128 * 1024 * 1024 + 8192
        header = {"operation": "artifact", "reference": reference, "archive_bytes": len(body)}
        return json.dumps(header).encode() + b"\n" + body

    monkeypatch.setattr(transport, "_exchange", exchange)
    assert await transport.fetch_artifact(target, binding, reference) == body


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["bytes", "size", "reference", "framing", "boolean"])
async def test_artifact_transfer_rejects_changed_evidence(monkeypatch, change):
    body, binding, reference = case()

    async def exchange(*args, **kwargs):
        header = {"operation": "artifact", "reference": reference, "archive_bytes": len(body)}
        output = body
        if change == "bytes":
            output = body[:-1] + b"x"
        if change == "size":
            header["archive_bytes"] -= 1
        if change == "boolean":
            header["archive_bytes"] = True
        if change == "reference":
            header["reference"] = {**reference, "source_hash": "f" * 64}
        if change == "framing":
            return b"not a framed artifact"
        return json.dumps(header).encode() + b"\n" + output

    monkeypatch.setattr(transport, "_exchange", exchange)
    with pytest.raises(transport.ValidationTransportError):
        await transport.fetch_artifact(SimpleNamespace(server_uuid="host-123"), binding, reference)


@pytest.mark.asyncio
async def test_wrong_host_fails_before_private_transfer(monkeypatch):
    _, binding, reference = case()

    async def forbidden(*args, **kwargs):
        raise AssertionError("transfer must not be attempted")

    monkeypatch.setattr(transport, "_exchange", forbidden)
    with pytest.raises(transport.ValidationTransportError, match="host_binding"):
        await transport.fetch_artifact(SimpleNamespace(server_uuid="another-host"), binding, reference)
