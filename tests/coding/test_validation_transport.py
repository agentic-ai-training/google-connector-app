import json
from pathlib import Path
import sys
from uuid import uuid4

import pytest

from app.coding import validation_transport as transport
from tests.coding.test_validation_dispatch import setup as setup_fixture

setup = setup_fixture


def host(tmp_path):
    key = tmp_path / "identity"
    key.write_text("not-a-real-private-key")
    key.chmod(0o600)
    known = tmp_path / "known_hosts"
    public = "ssh-ed25519 " + "a" * 68
    known.write_text("compute.123456 " + public + "\n")
    config = tmp_path / "gcloud"
    config.mkdir(mode=0o700)
    return transport.IAPValidationHost(
        server_uuid="host-123", project_id="project-example", zone="asia-south2-a",
        instance="validation-host", instance_id="123456", ssh_user="trusted_operator",
        gcloud_configuration="scoped-auth", gcloud_config_directory=config,
        identity_file=key, known_hosts_file=known, host_public_key=public,
        gcloud_binary=Path(sys.executable), ssh_binary=Path(sys.executable),
    )


def test_command_is_fixed_pinned_and_has_no_agent_or_inherited_provider_env(tmp_path, monkeypatch):
    target = host(tmp_path)
    monkeypatch.setattr(transport, "_protected", lambda path, **kwargs: path)
    monkeypatch.setenv("GROQ_API_KEY", "must-not-leak")
    args, env = transport.command(target)
    assert args[-1] == transport.REMOTE_COMMAND
    assert "-oStrictHostKeyChecking=yes" in args
    assert "-oIdentityAgent=none" in args and "-oForwardAgent=no" in args
    assert "GROQ_API_KEY" not in env and "DATABASE_URL" not in env
    assert any("start-iap-tunnel validation-host 22" in arg for arg in args)
    target.known_hosts_file.write_text(target.known_hosts_file.read_text() + "* ssh-rsa extra\n")
    with pytest.raises(transport.ValidationTransportError, match="pin_missing"):
        transport.command(target)


@pytest.mark.asyncio
async def test_dispatch_preserves_exact_frame_and_validates_response(setup, tmp_path, monkeypatch):
    workspace, _, binding = setup
    source = transport.isolation.source_archive(workspace)
    target = host(tmp_path)

    async def exchange(host_, payload, timeout):
        header, archive = payload.split(b"\n", 1)
        assert archive == source
        assert json.loads(header)["binding"] == binding
        assert timeout == binding["timeout_seconds"] + 90
        return {**binding, "success": True, "exit_code": 0, "cleanup_verified": True,
                "isolation": "gvisor", "validation_job_id": "a" * 32, "network": "none",
                "workspace_mutations_discarded": True}

    monkeypatch.setattr(transport, "_exchange", exchange)
    assert (await transport.dispatch(target, binding, source))["success"]
    with pytest.raises(transport.ValidationTransportError, match="binding_mismatch"):
        await transport.dispatch(target, {**binding, "server_uuid": "another-host"}, source)


@pytest.mark.asyncio
@pytest.mark.parametrize("program,category", [
    ("print('x'*10000000)", "output_limit"),
    ("import time; time.sleep(30)", "uncertain"),
    ("print('not-json')", "invalid_host_response"),
    ("import sys; print('secret',file=sys.stderr); sys.exit(2)", "unconfirmed"),
])
async def test_transport_failures_are_bounded_and_sanitized(monkeypatch, program, category):
    monkeypatch.setattr(transport, "command", lambda host_: (
        [sys.executable, "-c", program], {},
    ))
    with pytest.raises(transport.ValidationTransportError, match=category) as error:
        await transport._exchange(None, b"", 1)
    assert "secret" not in str(error.value)


@pytest.mark.asyncio
async def test_observation_is_read_only_typed_request(tmp_path, monkeypatch):
    target = host(tmp_path)
    claim_id = str(uuid4())

    async def exchange(host_, payload, timeout):
        assert json.loads(payload) == {"operation": "observe", "claim_id": claim_id}
        assert timeout == 45
        return {"claim_id": claim_id, "state": "unknown", "cleanup_verified": False}

    monkeypatch.setattr(transport, "_exchange", exchange)
    assert (await transport.observe(target, claim_id))["cleanup_verified"] is False


def test_writable_transport_path_is_rejected(tmp_path):
    with pytest.raises(transport.ValidationTransportError, match="not_protected"):
        tmp_path.chmod(0o777)
        transport._protected(tmp_path)
