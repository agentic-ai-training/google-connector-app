"""Real protected filesystem/SQLite transactions; no cloud or privileged installs."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import pytest

from app.coding import operator_bundle as bundle
from scripts.install_packaging_bundle import payload, verify


@pytest.fixture
def host(monkeypatch):
    with tempfile.TemporaryDirectory(prefix=".gca-bundle-test-", dir=Path.home()) as directory:
        base = Path(directory)
        root, releases, state, policy = (base / name for name in ("gca", "releases", "state", "policy.json"))
        root.mkdir(mode=0o755)
        state.mkdir(mode=0o700)
        (state / "slot.lock").touch(mode=0o600)
        database = state / "jobs.sqlite3"
        with sqlite3.connect(database) as conn:
            conn.execute("CREATE TABLE jobs(state TEXT,purpose TEXT)")
        database.chmod(0o600)
        policy.write_text(json.dumps({"state_directory": str(state)}))
        policy.chmod(0o600)
        for name in ("isolated_validation.py", "build_artifact.py"):
            (root / name).write_text("# previous reviewed source\n")
            (root / name).chmod(0o755 if name.startswith("isolated") else 0o644)
        monkeypatch.setattr(bundle, "ROOT", root)
        monkeypatch.setattr(bundle, "RELEASES", releases)
        monkeypatch.setattr(bundle, "POLICY", policy)
        monkeypatch.setattr(bundle, "probe_import", lambda path: None)
        probe = bundle.operate({"operation": "probe"})
        args = SimpleNamespace(apply=True, rollback=None, expected_snapshot_hash=probe["snapshot_hash"])
        request = payload(args)
        yield SimpleNamespace(root=root, releases=releases, state=state, request=request, old=bundle.snapshot(root))


def test_full_bundle_swap_preserves_old_tree_and_idempotent_receipt(host):
    before = (host.state / "jobs.sqlite3").read_bytes()
    result = bundle.operate(host.request)
    assert verify(host.request, result) == result
    assert set(bundle.snapshot(host.root)) == set(bundle.NAMES)
    assert (host.state / "jobs.sqlite3").read_bytes() == before
    previous = host.releases / (result["installation_id"] + ".previous")
    assert bundle.snapshot(previous) == host.old
    again = {**host.request, "expected_snapshot_hash": bundle.operate({"operation": "probe"})["snapshot_hash"]}
    assert bundle.operate(again) == result
    assert len(list(host.releases.iterdir())) == 2


def test_explicit_rollback_restores_whole_original_tree(host):
    result = bundle.operate(host.request)
    rollback = {"operation": "rollback", "installation_id": result["installation_id"]}
    assert verify(rollback, bundle.operate(rollback))["rolled_back"] is True
    assert bundle.snapshot(host.root) == host.old
    assert bundle.operate(rollback)["rolled_back"] is True


@pytest.mark.parametrize("phase", ["prepared", "root_saved", "published"])
def test_interrupted_swap_is_probeable_and_explicitly_recoverable(host, monkeypatch, phase):
    real_phase = bundle.set_phase

    def fail(transaction, value):
        real_phase(transaction, value)
        if value == phase:
            raise InterruptedError("simulated controller loss")

    monkeypatch.setattr(bundle, "set_phase", fail)
    with pytest.raises(InterruptedError):
        bundle.operate(host.request)
    probe = bundle.operate({"operation": "probe"})
    assert probe["interrupted"] is True and probe["phase"] == phase
    with pytest.raises(bundle.BundleError, match="explicit rollback"):
        bundle.operate(host.request)
    monkeypatch.setattr(bundle, "set_phase", real_phase)
    bundle.operate({"operation": "rollback", "installation_id": probe["installation_id"]})
    assert bundle.snapshot(host.root) == host.old


def test_lost_phase_write_after_directory_rename_still_recovers(host, monkeypatch):
    real_phase = bundle.set_phase

    def fail(transaction, value):
        if value == "root_saved":
            raise InterruptedError("rename durable; phase write lost")
        real_phase(transaction, value)

    monkeypatch.setattr(bundle, "set_phase", fail)
    with pytest.raises(InterruptedError):
        bundle.operate(host.request)
    probe = bundle.operate({"operation": "probe"})
    assert not host.root.exists() and probe["phase"] == "prepared"
    monkeypatch.setattr(bundle, "set_phase", real_phase)
    bundle.operate({"operation": "rollback", "installation_id": probe["installation_id"]})
    assert bundle.snapshot(host.root) == host.old


@pytest.mark.parametrize("after", ["save_old", "publish_new"])
def test_abrupt_process_death_after_rename_recovers_without_finally(host, after):
    source = Path(bundle.__file__)
    program = "\n".join([
        "import json,os,pathlib,sys",
        "namespace={'__name__':'controlled_operator_crash_probe'}",
        "exec(compile(pathlib.Path(" + repr(str(source)) + ").read_bytes(),'reviewed_operator','exec'),namespace)",
        "namespace['ROOT']=pathlib.Path(" + repr(str(host.root)) + ")",
        "namespace['RELEASES']=pathlib.Path(" + repr(str(host.releases)) + ")",
        "namespace['POLICY']=pathlib.Path(" + repr(str(bundle.POLICY)) + ")",
        "namespace['probe_import']=lambda path:None",
        "original=os.rename",
        "def terminate(source,destination):",
        "    original(source,destination)",
        "    if " + ("source==namespace['ROOT']" if after == "save_old" else "destination==namespace['ROOT']") + ":",
        "        os._exit(97)",
        "os.rename=terminate",
        "namespace['operate'](json.load(sys.stdin))",
    ])
    result = subprocess.run([sys.executable, "-I", "-B", "-c", program],  # nosec B603
                            input=json.dumps(host.request).encode(), capture_output=True,
                            timeout=10, env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"})
    assert result.returncode == 97
    probe = bundle.operate({"operation": "probe"})
    assert probe["interrupted"] is True
    assert host.root.exists() is (after == "publish_new")
    bundle.operate({"operation": "rollback", "installation_id": probe["installation_id"]})
    assert bundle.snapshot(host.root) == host.old


def test_busy_journal_and_uncertain_jobs_prevent_changes(host):
    fd = os.open(host.state / "slot.lock", os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            bundle.operate(host.request)
    finally:
        os.close(fd)
    with sqlite3.connect(host.state / "jobs.sqlite3") as conn:
        conn.execute("INSERT INTO jobs VALUES('cleanup_uncertain','validation')")
    with pytest.raises(bundle.BundleError, match="unreconciled"):
        bundle.operate(host.request)
    assert bundle.snapshot(host.root) == host.old and not host.releases.exists()


@pytest.mark.parametrize("kind", ["wrong_hash", "unknown_file", "syntax", "symlink", "hardlink"])
def test_unreviewed_changes_are_rejected_before_directory_swap(host, kind):
    request = {**host.request, "files": dict(host.request["files"])}
    if kind == "wrong_hash":
        request["expected_snapshot_hash"] = "0" * 64
    elif kind == "unknown_file":
        request["files"]["evil.py"] = "pass\n"
    elif kind == "syntax":
        request["files"]["oci_packager.py"] = "def broken(:\n"
    elif kind == "symlink":
        (host.root / "evil.py").symlink_to(host.root / "build_artifact.py")
    else:
        os.link(host.root / "build_artifact.py", host.root / "evil.py")
    with pytest.raises((bundle.BundleError, SyntaxError)):
        bundle.operate(request)
    assert not host.releases.exists()


@pytest.mark.parametrize("retained", ["history", "staging"])
def test_old_bundle_rollback_cannot_drop_packaging_cleanup_capability(host, retained):
    result = bundle.operate(host.request)
    if retained == "history":
        with sqlite3.connect(host.state / "jobs.sqlite3") as conn:
            conn.execute("INSERT INTO jobs VALUES('cleaned','image_packaging')")
    else:
        (host.state / "packaging-jobs").mkdir(mode=0o700)
    with pytest.raises(bundle.BundleError, match="rollback"):
        bundle.operate({"operation": "rollback", "installation_id": result["installation_id"]})
    assert set(bundle.snapshot(host.root)) == set(bundle.NAMES)


def test_rollback_refuses_mutated_published_code(host):
    result = bundle.operate(host.request)
    (host.root / "oci_packager.py").write_text("# unrelated operator edit\n")
    with pytest.raises(bundle.BundleError, match="unrecognized"):
        bundle.operate({"operation": "rollback", "installation_id": result["installation_id"]})


def test_real_isolated_import_of_reviewed_staged_sources(host, monkeypatch):
    # Use this test interpreter; production fixes /usr/bin/python3 on Linux.
    real_run = subprocess.run

    def local_run(argv, **kwargs):
        assert argv[:3] == ["/usr/bin/python3", "-I", "-B"]
        return real_run([sys.executable, *argv[1:]], **kwargs)

    monkeypatch.undo()
    monkeypatch.setattr(bundle.subprocess, "run", local_run)
    probe = bundle.probe_import
    for name in bundle.NAMES:
        (host.root / name).write_text(host.request["files"][name])
        (host.root / name).chmod(0o644)
    probe(host.root)
    assert not (host.root / "__pycache__").exists()


def test_public_receipt_does_not_trust_client_passed_boolean(host):
    with pytest.raises(RuntimeError):
        verify(host.request, {"operation": "apply", "passed": True})
    assert payload(SimpleNamespace(apply=False, rollback=None)) == {"operation": "probe"}
    assert hashlib.sha256(json.dumps(host.old, sort_keys=True, separators=(",", ":")).encode()).hexdigest() == (
        host.request["expected_snapshot_hash"])
