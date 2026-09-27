import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

import pytest

from app.coding import isolated_validation as runner


IMAGE = "sha256:" + "a" * 64
POLICY = {"version": 1, "runtime": "runsc", "images": {"python_compile": IMAGE}}


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app/main.py").write_text("answer = 42\n")
    return tmp_path


def test_snapshot_excludes_credentials_and_resets_metadata(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app/main.py").write_text("answer = 42\n")
    (tmp_path / ".env").write_text("SECRET=not-for-workload")
    (tmp_path / ".env-private").mkdir()
    (tmp_path / ".env-private/secret").write_text("no")
    (tmp_path / ".gca-local").mkdir()
    (tmp_path / ".gca-local/key").write_text("no")
    with tarfile.open(fileobj=io.BytesIO(runner.source_archive(tmp_path))) as archive:
        assert archive.getnames() == ["app", "app/main.py"]
        assert all(member.uid == 65532 for member in archive.getmembers())
        assert archive.extractfile("app/main.py").read() == b"answer = 42\n"


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_snapshot_rejects_links_and_devices(tmp_path, kind):
    original = tmp_path / "source"
    original.write_text("secret")
    target = tmp_path / "linked"
    if kind == "symlink":
        target.symlink_to(original)
    elif kind == "hardlink":
        os.link(original, target)
    else:
        os.mkfifo(target)
    with pytest.raises(runner.IsolationError):
        runner.source_archive(tmp_path)


def test_snapshot_has_byte_and_entry_caps(tmp_path, monkeypatch):
    (tmp_path / "large").write_text("abc")
    monkeypatch.setattr(runner, "MAX_SOURCE_BYTES", 2)
    with pytest.raises(runner.IsolationError, match="byte limit"):
        runner.source_archive(tmp_path)
    monkeypatch.setattr(runner, "MAX_SOURCE_BYTES", 10)
    monkeypatch.setattr(runner, "MAX_SOURCE_ENTRIES", 0)
    with pytest.raises(runner.IsolationError, match="entry limit"):
        runner.source_archive(tmp_path)


def test_policy_requires_operator_path_and_immutable_images(tmp_path):
    # An operator-owned path under HOME, not world-writable /tmp.
    with tempfile.TemporaryDirectory(prefix=".gca-policy-test-", dir=Path.home()) as directory:
        config = Path(directory) / "policy.json"
        config.write_text(json.dumps(POLICY))
        config.chmod(0o600)
        assert runner.load_policy(config, tmp_path) == POLICY
        config.chmod(0o666)
        with pytest.raises(runner.IsolationError, match="untrusted identity"):
            runner.load_policy(config, tmp_path)
        config.chmod(0o600)
        config.write_text(json.dumps({**POLICY, "images": {"python_compile": "python:latest"}}))
        with pytest.raises(runner.IsolationError, match="immutable"):
            runner.load_policy(config, tmp_path)


def test_policy_inside_repository_is_not_authority(tmp_path):
    config = tmp_path / "policy.json"
    config.write_text(json.dumps(POLICY))
    with pytest.raises(runner.IsolationError, match="outside"):
        runner.load_policy(config, tmp_path)


def fake_transport(monkeypatch, *, fail=None, mounts=None):
    calls = []

    def call(*args, **kwargs):
        calls.append((args, kwargs))
        if args[0] == fail:
            raise subprocess.TimeoutExpired(args, 30)
        if args[0] == "inspect":
            return json.dumps([{"Mounts": mounts or []}]).encode()
        return b""

    monkeypatch.setattr(runner, "docker", call)
    monkeypatch.setattr(runner, "execute_bounded", lambda args, timeout: {
        "success": True, "exit_code": 0, "stdout": "", "stderr": "",
    })
    return calls


def test_validator_copies_snapshot_without_host_mount_or_credentials(tmp_path, monkeypatch):
    (tmp_path / "app").mkdir()
    (tmp_path / "app/main.py").write_text("answer=42")
    calls = fake_transport(monkeypatch)
    result = runner.validate(tmp_path, "python_compile", 10, POLICY)
    assert result["isolation"] == "gvisor"
    create = calls[0][0]
    assert "--runtime=runsc" in create
    assert "--network=none" in create
    assert "--pull=never" in create
    assert not any(item.startswith(("--mount", "--volume")) for item in create)
    assert {item for item in create if item.startswith("--env=")} == {
        "--env=HOME=/tmp", "--env=CI=true",
    }
    assert any(args[0] == "exec" and kwargs["payload"] for args, kwargs in calls)
    assert not any(args[0] == "cp" for args, _ in calls)
    assert calls[-1][0][:3] == ("rm", "--force", "--volumes")


@pytest.mark.parametrize("phase", ["create", "start", "inspect", "exec"])
def test_cleanup_after_control_failure(workspace, monkeypatch, phase):
    calls = fake_transport(monkeypatch, fail=phase)
    with pytest.raises(subprocess.TimeoutExpired):
        runner.validate(workspace, "python_compile", 10, POLICY)
    assert calls[-1][0][0] == "rm"


def test_cleanup_uncertainty_overrides_validation_success(workspace, monkeypatch):
    fake_transport(monkeypatch, fail="rm")
    with pytest.raises(runner.IsolationError, match="cleanup uncertain"):
        runner.validate(workspace, "python_compile", 10, POLICY)


def test_unapproved_profile_never_creates_container(tmp_path, monkeypatch):
    calls = fake_transport(monkeypatch)
    with pytest.raises(runner.IsolationError, match="No approved"):
        runner.validate(tmp_path, "web_build", 10, POLICY)
    assert not calls


def test_implicit_volume_stops_before_start(workspace, monkeypatch):
    calls = fake_transport(monkeypatch, mounts=[{"Type": "volume"}])
    with pytest.raises(runner.IsolationError, match="persistent mount"):
        runner.validate(workspace, "python_compile", 10, POLICY)
    assert not any(args[0] == "start" for args, _ in calls)


def test_empty_python_compile_cannot_claim_success(tmp_path, monkeypatch):
    calls = fake_transport(monkeypatch)
    with pytest.raises(runner.IsolationError, match="No Python source"):
        runner.validate(tmp_path, "python_compile", 10, POLICY)
    assert calls == []


def test_generated_output_is_bounded_but_exit_status_preserved(monkeypatch):
    monkeypatch.setattr(runner, "DOCKER", sys.executable)
    result = runner.execute_bounded(["-c", "import sys; print('x'*300000); sys.exit(3)"], 3)
    assert len(result["stdout"].encode()) == runner.MAX_OUTPUT_BYTES
    assert result["output_truncated"] is True
    assert result["success"] is False
    assert result["exit_code"] == 3


def test_generated_process_deadline_is_enforced(monkeypatch):
    monkeypatch.setattr(runner, "DOCKER", sys.executable)
    with pytest.raises(runner.IsolationError, match="deadline"):
        runner.execute_bounded(["-c", "import time; time.sleep(30)"], 1)
