"""Custody gateway authority tests; real journals/files, no GCP or image loading."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import pytest

from app.coding import operator_bundle as bundle, operator_toolchain_host as host
from app.coding import toolchain_spec as catalog, toolchain_staging as custody
from scripts.install_packaging_bundle import payload
from tests.coding.test_toolchain_archive import fixture, archive


@pytest.fixture
def target(monkeypatch):
    with tempfile.TemporaryDirectory(prefix=".gca-op-host-test-", dir=Path.home()) as name:
        root = Path(name)
        state = root / "state"
        state.mkdir(mode=0o700)
        policy = root / "policy.json"
        registry = root / "registry.json"
        policy.write_text(json.dumps({"version": 2, "runtime": "runsc",
                                      "state_directory": str(state), "images": {"python_compile": "sha256:" + "a" * 64}}))
        registry.write_text(json.dumps({"version": 1, "images": {"sha256:" + "a" * 64: {"existing": True}}}))
        policy.chmod(0o600)
        registry.chmod(0o600)
        monkeypatch.setattr(host, "POLICY", policy)
        monkeypatch.setattr(host, "REGISTRY", registry)
        monkeypatch.setattr(host, "snapshot", lambda: "a" * 64)
        monkeypatch.setattr(host, "target_identity", lambda: None)
        files, image = fixture()
        body = archive(files)
        reviewed = {"image_id": image, "archive_bytes": len(body), "archive_sha256": custody.checksum(body),
                    "recipe_sha256": "c" * 64}
        monkeypatch.setattr(host, "REVIEWED_ARCHIVES", {"react": reviewed})
        yield SimpleNamespace(state=state, policy=policy, registry=registry, body=body, reviewed=reviewed)


def request(operation, **values):
    return {"version": 1, "operation": operation, "bundle_sha256": "a" * 64, **values}


def prepared(target):
    probe = host.operate(request("probe"), io.BytesIO())
    return host.operate(request("prepare", role="react", policy_sha256=probe["policy_sha256"],
                                registry_sha256=probe["registry_sha256"]), io.BytesIO())["reservation"]


def test_probe_prepare_receive_remain_custody_only_and_preserve_registry_and_policy(target):
    before = (target.policy.read_bytes(), target.registry.read_bytes())
    item = prepared(target)
    assert item["intent"]["stop_unix"] == host.STOP_UNIX
    assert item["intent"]["host_instance_id"] == host.TARGET_INSTANCE
    receipt = host.operate(request("receive", intent_id=item["id"]), io.BytesIO(target.body))
    assert receipt["custody"]["archive"]["image_loaded"] is False
    assert receipt["custody"]["archive"]["image_approved"] is False
    assert (target.policy.read_bytes(), target.registry.read_bytes()) == before
    probe = host.operate(request("probe"), io.BytesIO())
    assert probe["imports"][0]["phase"] == "archive_verified"
    assert host.operate(request("receive", intent_id=item["id"]), io.BytesIO())["custody"] == receipt["custody"]


@pytest.mark.parametrize("operation", ["load", "approve", "deploy", "shell", "cancel", "publish"])
def test_custody_gateway_cannot_grant_execution_or_policy_authority(target, operation):
    with pytest.raises(host.OperatorToolchainError, match="authority"):
        host.operate(request(operation), io.BytesIO())
    assert not (target.state / "jobs.sqlite3").exists()


@pytest.mark.parametrize("extra", [{"path": "/etc/passwd"}, {"command": "docker load"},
                                   {"approved": True}, {"image_id": "sha256:" + "b" * 64}])
def test_prepare_cannot_substitute_paths_commands_images_or_approval(target, extra):
    probe = host.operate(request("probe"), io.BytesIO())
    with pytest.raises(host.OperatorToolchainError, match="authority"):
        host.operate(request("prepare", role="react", policy_sha256=probe["policy_sha256"],
                             registry_sha256=probe["registry_sha256"], **extra), io.BytesIO())


def test_stale_policy_or_registry_or_source_cannot_create_an_import(target):
    probe = host.operate(request("probe"), io.BytesIO())
    for field in ("bundle_sha256", "policy_sha256", "registry_sha256"):
        candidate = request("prepare", role="react", policy_sha256=probe["policy_sha256"],
                            registry_sha256=probe["registry_sha256"])
        candidate[field] = "b" * 64
        with pytest.raises(host.OperatorToolchainError):
            host.operate(candidate, io.BytesIO())
    assert host.operate(request("probe"), io.BytesIO())["imports"] == []


def test_configuration_is_rechecked_after_obtaining_shared_slot(target, monkeypatch):
    real = host.configuration
    calls = []

    def changed():
        calls.append(True)
        policy, p, r = real()
        return policy, p, r if len(calls) == 1 else "b" * 64

    monkeypatch.setattr(host, "configuration", changed)
    with pytest.raises(host.OperatorToolchainError, match="before slot"):
        host.operate(request("probe"), io.BytesIO())


def test_changed_configuration_blocks_transfer_of_old_reserved_intent(target):
    item = prepared(target)
    target.registry.write_bytes(target.registry.read_bytes() + b" ")
    with pytest.raises(host.OperatorToolchainError, match="reserved"):
        host.operate(request("receive", intent_id=item["id"]), io.BytesIO(target.body))


def test_explicit_preload_cancel_after_config_drift_preserves_bytes_and_releases_slot(target):
    item = prepared(target)
    host.operate(request("receive", intent_id=item["id"]), io.BytesIO(target.body))
    stored = target.state / "toolchain-imports" / item["id"] / "archive.tar"
    target.registry.write_bytes(target.registry.read_bytes() + b" ")
    result = host.operate(request("cancel_preload", intent_id=item["id"], expected_phase="archive_verified"), io.BytesIO())
    assert result["reservation"]["phase"] == "cancelled" and result["custody_retained"] is True
    assert stored.read_bytes() == target.body
    with host.isolation.Journal(target.state) as journal:
        journal.require_available()


def test_cancel_cannot_erase_a_load_that_already_started(target):
    item = prepared(target)
    host.operate(request("receive", intent_id=item["id"]), io.BytesIO(target.body))
    with host.isolation.Journal(target.state) as journal:
        journal.record_toolchain_import(item["id"], "archive_verified", "load_started", "b" * 64)
    with pytest.raises(host.isolation.IsolationError):
        host.operate(request("cancel_preload", intent_id=item["id"], expected_phase="archive_verified"), io.BytesIO())
    with host.isolation.Journal(target.state) as journal:
        assert journal.toolchain_imports()[0]["phase"] == "load_started"


def test_unreviewed_role_or_secondary_overlapping_import_is_not_accepted(target):
    probe = host.operate(request("probe"), io.BytesIO())
    with pytest.raises(host.OperatorToolchainError, match="catalog"):
        host.operate(request("prepare", role="unknown", policy_sha256=probe["policy_sha256"],
                             registry_sha256=probe["registry_sha256"]), io.BytesIO())
    item = prepared(target)
    assert prepared(target)["id"] == item["id"]
    assert len(host.operate(request("probe"), io.BytesIO())["imports"]) == 1


@pytest.mark.parametrize("body", [b"", b"\x00\x00", (4097).to_bytes(4, "big"),
                                  (5).to_bytes(4, "big") + b"{}", (2).to_bytes(4, "big") + b"xx"])
def test_bad_frames_do_not_reach_control_authority(body):
    with pytest.raises(host.OperatorToolchainError):
        host.frame(io.BytesIO(body))


def test_frame_preserves_archive_stream_and_rejects_duplicate_control_keys():
    body = custody.canonical(request("receive", intent_id="b" * 64))
    stream = io.BytesIO(len(body).to_bytes(4, "big") + body + b"archive-bytes")
    assert host.frame(stream)["operation"] == "receive"
    assert stream.read() == b"archive-bytes"
    body = b'{"version":1,"version":2}'
    with pytest.raises(host.OperatorToolchainError):
        host.frame(io.BytesIO(len(body).to_bytes(4, "big") + body))


def test_control_operation_rejects_unexpected_binary_body(target):
    with pytest.raises(host.OperatorToolchainError, match="unexpected"):
        host.operate(request("probe"), io.BytesIO(b"unexpected"))


def test_expanded_catalog_and_recipes_are_explicit_and_frame_bound_is_unchanged():
    from datetime import datetime, timezone
    assert int(datetime(2026, 10, 19, 17, 24, 32, tzinfo=timezone.utc).timestamp()) == host.STOP_UNIX
    assert bundle.NAMES == host.BUNDLE_NAMES and len(bundle.NAMES) == 13
    assert bundle.MAX_FRAME == 256000
    args = SimpleNamespace(apply=True, rollback=None, expected_snapshot_hash="a" * 64)
    body = bundle.encode_request(payload(args))
    assert len(body) <= bundle.MAX_FRAME
    assert bundle.decode_request(body) == payload(args)
    root = Path(__file__).resolve().parents[2]
    for role in ("react", "spring"):
        directory = root / "toolchains" / role
        names = [".dockerignore", "Dockerfile"] + (["package.json", "package-lock.json"] if role == "react"
                  else ["pom.xml", "seed/main/java/com/example/OfflineProofApplication.java",
                        "seed/test/java/com/example/OfflineProofTest.java"])
        hashes = {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in names}
        assert custody.checksum(custody.canonical(hashes)) == catalog.REVIEWED_ARCHIVES[role]["recipe_sha256"]


def test_real_isolated_import_uses_only_complete_reviewed_siblings_and_ignores_bytecode():
    with tempfile.TemporaryDirectory(prefix=".gca-op-import-", dir=Path.home()) as name:
        directory = Path(name)
        source = Path(host.__file__).parent
        for filename in host.BUNDLE_NAMES:
            (directory / filename).write_bytes((source / filename).read_bytes())
            (directory / filename).chmod(0o755 if filename == "isolated_validation.py" else 0o644)
        script = directory / "operator_toolchain_host.py"
        # runpy with -I -B exercises the real protected bootstrap, not a mock.
        code = "import runpy; m=runpy.run_path(" + repr(str(script)) + "); assert len(m['REVIEWED_ARCHIVES'])==2"
        result = subprocess.run([sys.executable, "-I", "-B", "-c", code], capture_output=True,
                                timeout=15, check=False)
        assert result.returncode == 0, result.stderr
        (directory / "toolchain_staging.py").chmod(0o666)
        result = subprocess.run([sys.executable, "-I", "-B", "-c", code], capture_output=True,
                                timeout=15, check=False)
        assert result.returncode != 0


def test_target_checks_only_two_public_metadata_fields_and_closes_connection(monkeypatch):
    calls = []

    class Response:
        status = 200

        def read(self, size):
            assert size == 129
            return (host.TARGET_PROJECT if len(calls) == 1 else host.TARGET_INSTANCE).encode()

        def getheader(self, name):
            assert name == "Metadata-Flavor"
            return "Google"

    class Connection:
        def __init__(self, address, timeout):
            assert address == "169.254.169.254" and timeout == 3

        def request(self, method, path, headers):
            assert method == "GET" and headers == {"Metadata-Flavor": "Google"}
            calls.append(path)

        def getresponse(self):
            return Response()

        def close(self):
            calls.append("closed")

    monkeypatch.setattr(host.http.client, "HTTPConnection", Connection)
    host.target_identity()
    assert calls == ["/computeMetadata/v1/project/project-id", "/computeMetadata/v1/instance/id", "closed"]
