import hashlib
import json
from pathlib import Path
import tempfile
import time
from uuid import uuid4

import pytest

from app.coding import isolated_validation as runner
from tests.coding.test_isolated_validation import fake_transport, generated_stack, surefire_archive
from tests.coding.test_build_artifact import archive, boot_jar
from app.coding.build_artifact import load_build_artifact


IMAGE = "sha256:" + "a" * 64


@pytest.fixture
def setup(tmp_path):
    workspace = tmp_path / "source"
    (workspace / "app").mkdir(parents=True)
    (workspace / "app/main.py").write_text("answer=42\n")
    state = tmp_path / "journal"
    state.mkdir(mode=0o700)
    policy = {"version": 3, "runtime": "runsc", "images": {"python_compile": IMAGE},
              "state_directory": str(state), "server_uuid": "host-123"}
    binding = {
        "version": 1, "claim_id": str(uuid4()), "run_id": str(uuid4()), "sequence_no": 1,
        "request_hash": "b" * 64, "target_hash": "c" * 64, "server_uuid": "host-123",
        "source_hash": hashlib.sha256(runner.source_archive(workspace)).hexdigest(),
        "image_id": IMAGE, "profile": "python_compile", "timeout_seconds": 10,
        "deadline": int(time.time()) + 180,
    }
    return workspace, policy, binding


def test_bound_execution_persists_identity_before_create_and_rejects_replay(setup, monkeypatch):
    workspace, policy, binding = setup
    calls = fake_transport(monkeypatch)
    original = runner.docker

    def inspect_intent(*args, **kwargs):
        if args[0] == "create":
            import sqlite3
            with sqlite3.connect(Path(policy["state_directory"]) / "jobs.sqlite3") as db:
                assert db.execute("SELECT claim_id FROM dispatch_claims").fetchone()[0] == (
                    binding["claim_id"]
                )
        return original(*args, **kwargs)

    monkeypatch.setattr(runner, "docker", inspect_intent)
    result = runner.validate(workspace, "python_compile", 10, policy, binding)
    assert result["claim_id"] == binding["claim_id"]
    assert result["source_hash"] == binding["source_hash"]
    assert result["cleanup_verified"] is True
    count = len(calls)
    with pytest.raises(runner.IsolationError, match="already consumed"):
        runner.validate(workspace, "python_compile", 10, policy, binding)
    assert len(calls) == count
    with runner.Journal(Path(policy["state_directory"])) as journal:
        assert journal.db.execute("SELECT deadline FROM jobs").fetchone()[0] == binding["deadline"]
        observed = journal.observe_claim(binding["claim_id"])
        assert observed["cleanup_verified"] is True
        assert observed["validation_success"] is None
        assert observed["validation_job_id"] == result["validation_job_id"]
        assert observed["binding_hash"] == hashlib.sha256(json.dumps(
            binding, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()


@pytest.mark.parametrize("profile", ["react_build", "spring_package"])
def test_bound_build_retains_actual_output_before_cleanup(setup, monkeypatch, profile):
    workspace, policy, binding = setup
    generated_stack(workspace)
    policy["images"] = {profile: IMAGE}
    binding.update(profile=profile,
                   source_hash=hashlib.sha256(runner.source_archive(workspace)).hexdigest())
    payload = (archive({"dist/index.html": b"<html>app</html>"}) if profile == "react_build"
               else archive({"target/app.jar": boot_jar()}))
    calls = fake_transport(monkeypatch)

    def execute(args, _timeout, **kwargs):
        output = "target/app.jar\n" if args[4] == "find" else ""
        if kwargs.get("binary"):
            output = surefire_archive() if args[-1] == "target/surefire-reports" else payload
        return {"success": True, "exit_code": 0, "stdout": output, "stderr": "",
                "output_truncated": False}

    monkeypatch.setattr(runner, "execute_bounded", execute)
    result = runner.validate(workspace, profile, 10, policy, binding)
    reference = result["build_artifact"]
    assert reference["source_hash"] == binding["source_hash"]
    assert reference["claim_id"] == binding["claim_id"]
    assert reference["validation_job_id"] == result["validation_job_id"]
    assert load_build_artifact(Path(policy["state_directory"]), reference["artifact_id"],
                               reference["archive_sha256"]) == payload
    assert result["cleanup_verified"] is True
    assert calls[-1][0][:3] == ("rm", "--force", "--volumes")


@pytest.mark.parametrize("field,value", [
    ("source_hash", "d" * 64), ("image_id", "sha256:" + "e" * 64),
    ("server_uuid", "another-host"), ("profile", "python_unit"),
    ("timeout_seconds", 11), ("timeout_seconds", True), ("deadline", 0),
    ("deadline", 2**60), ("sequence_no", True), ("claim_id", "not-a-claim"),
    ("run_id", "not-a-run"), ("request_hash", "short"), ("version", 9),
])
def test_binding_mismatch_never_calls_docker(setup, monkeypatch, field, value):
    workspace, policy, binding = setup
    calls = fake_transport(monkeypatch)
    with pytest.raises(runner.IsolationError):
        runner.validate(workspace, "python_compile", 10, policy, {**binding, field: value})
    assert calls == []


def test_v3_cannot_use_unbound_legacy_path(setup, monkeypatch):
    workspace, policy, _ = setup
    calls = fake_transport(monkeypatch)
    with pytest.raises(runner.IsolationError, match="Centrally admitted"):
        runner.validate(workspace, "python_compile", 10, policy)
    assert calls == []


def test_changed_source_cannot_consume_claim(setup, monkeypatch):
    workspace, policy, binding = setup
    (workspace / "app/main.py").write_text("answer=43\n")
    calls = fake_transport(monkeypatch)
    with pytest.raises(runner.IsolationError, match="mismatch"):
        runner.validate(workspace, "python_compile", 10, policy, binding)
    with runner.Journal(Path(policy["state_directory"])) as journal:
        assert journal.db.execute("SELECT COUNT(*) FROM dispatch_claims").fetchone()[0] == 0
    assert calls == []


def test_spool_requires_private_exact_claim_record(setup):
    _, policy, binding = setup
    directory = Path(policy["state_directory"]) / "dispatch"
    directory.mkdir(mode=0o700)
    path = directory / (binding["claim_id"] + ".json")
    path.write_text(json.dumps(binding))
    path.chmod(0o600)
    assert runner.load_dispatch(binding["claim_id"], policy) == binding
    path.chmod(0o644)
    with pytest.raises(runner.IsolationError, match="private"):
        runner.load_dispatch(binding["claim_id"], policy)
    path.chmod(0o600)
    path.write_text(json.dumps({**binding, "claim_id": str(uuid4())}))
    with pytest.raises(runner.IsolationError, match="mismatch"):
        runner.load_dispatch(binding["claim_id"], policy)


def test_policy_v3_loaded_from_protected_path(setup):
    workspace, policy, _ = setup
    with tempfile.TemporaryDirectory(prefix=".gca-bound-policy-", dir=Path.home()) as directory:
        root = Path(directory)
        state = root / "journal"
        state.mkdir(mode=0o700)
        policy = {**policy, "state_directory": str(state)}
        config = root / "policy.json"
        config.write_text(json.dumps(policy))
        config.chmod(0o600)
        assert runner.load_policy(config, workspace) == policy


def test_unknown_claim_is_not_cleanup_proof(setup):
    _, policy, binding = setup
    with runner.Journal(Path(policy["state_directory"])) as journal:
        assert journal.observe_claim(binding["claim_id"]) == {
            "claim_id": binding["claim_id"], "state": "unknown", "cleanup_verified": False,
        }


def test_create_uncertainty_consumes_claim_and_retains_binding(setup, monkeypatch):
    workspace, policy, binding = setup
    fake_transport(monkeypatch, fail="create")
    import subprocess
    with pytest.raises(subprocess.TimeoutExpired):
        runner.validate(workspace, "python_compile", 10, policy, binding)
    with runner.Journal(Path(policy["state_directory"])) as journal:
        assert journal.db.execute("SELECT count(*) FROM dispatch_claims").fetchone()[0] == 1
    with pytest.raises(runner.IsolationError, match="already consumed"):
        runner.validate(workspace, "python_compile", 10, policy, binding)


def test_late_container_prevents_false_cleanup_observation(setup, monkeypatch):
    workspace, policy, binding = setup
    fake_transport(monkeypatch)
    runner.validate(workspace, "python_compile", 10, policy, binding)
    monkeypatch.setattr(runner, "docker", lambda *args, **kwargs: b"c" * 64)
    with runner.Journal(Path(policy["state_directory"])) as journal:
        observed = journal.observe_claim(binding["claim_id"])
        assert observed["state"] == "cleanup_uncertain"
        assert observed["cleanup_verified"] is False
