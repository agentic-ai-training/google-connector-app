"""Real protected custody/rename/crash tests; fake OCI bytes never execute."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import pytest

from app.coding import isolated_validation as isolation
from app.coding import toolchain_staging as stage
from tests.coding.test_toolchain_archive import fixture, archive


@pytest.fixture(params=("react", "spring"))
def host(request):
    # Production-like protected ancestry, not the world-writable /tmp tree.
    with tempfile.TemporaryDirectory(prefix=".gca-stage-test-", dir=Path.home()) as name:
        state = Path(name)
        role = request.param
        files, image = fixture(role=role)
        body = archive(files)
        request = {"version": 1, "role": role, "host_instance_id": "822115653245959101",
                   "image_id": image, "archive_sha256": stage.checksum(body), "archive_bytes": len(body),
                   "recipe_sha256": "c" * 64, "policy_sha256": "d" * 64,
                   "registry_sha256": "e" * 64, "stop_unix": int(time.time()) + 3600}
        with isolation.Journal(state) as journal:
            item = journal.begin_toolchain_import(request)
            yield journal, item, body, state / "toolchain-imports" / item["id"]


def test_persisted_archive_readback_receipt_and_journal_remain_unapproved(host):
    journal, item, body, directory = host
    receipt = stage.stage_archive(journal, item["id"], io.BytesIO(body))
    assert (directory / "archive.tar").read_bytes() == body
    assert not (directory / "archive.partial").exists()
    assert receipt["archive"]["image_loaded"] is receipt["archive"]["image_approved"] is False
    assert stage.document(directory / "custody.json") == receipt
    current = journal.toolchain_imports()[0]
    assert current["phase"] == "archive_verified"
    assert current["observations"][-1]["evidence_sha256"] == stage.checksum(stage.canonical(receipt))
    with pytest.raises(isolation.IsolationError, match="toolchain import"):
        journal.require_available()


class NeverRead:
    def read(self, size):
        raise AssertionError("Recovered custody must not upload again")


def test_complete_partial_recovers_without_reupload_and_exact_receipt_retry_is_safe(host):
    journal, item, body, directory = host
    stage.context(journal, item["id"])
    (directory / "archive.partial").write_bytes(body)
    (directory / "archive.partial").chmod(0o600)
    first = stage.stage_archive(journal, item["id"], NeverRead())
    assert stage.stage_archive(journal, item["id"], NeverRead()) == first
    assert len(journal.toolchain_imports()[0]["observations"]) == 2


@pytest.mark.parametrize("fault", ["after_promote", "after_receipt", "after_journal"])
def test_abrupt_death_during_custody_publication_recovers_without_blind_transfer(fault):
    with tempfile.TemporaryDirectory(prefix=".gca-stage-death-", dir=Path.home()) as name:
        state = Path(name)
        files, image = fixture()
        body = archive(files)
        request = {"version": 1, "role": "react", "host_instance_id": "822115653245959101",
                   "image_id": image, "archive_sha256": stage.checksum(body), "archive_bytes": len(body),
                   "recipe_sha256": "c" * 64, "policy_sha256": "d" * 64,
                   "registry_sha256": "e" * 64, "stop_unix": int(time.time()) + 3600}
        with isolation.Journal(state) as journal:
            item = journal.begin_toolchain_import(request)
            _, directory = stage.context(journal, item["id"])
            (directory / "archive.partial").write_bytes(body)
            (directory / "archive.partial").chmod(0o600)
        code = "\n".join([
            "import os,pathlib,sys",
            "from app.coding.isolated_validation import Journal",
            "from app.coding import toolchain_staging as s",
            "fault=sys.argv[3]",
            "real_sync=s.sync",
            "def sync(path):",
            " real_sync(path)",
            " if fault=='after_promote' and (path/'archive.tar').exists(): os._exit(29)",
            "s.sync=sync",
            "real_write=s.write_document",
            "def write(path,value):",
            " real_write(path,value)",
            " if fault=='after_receipt' and path.name=='custody.json': os._exit(29)",
            "s.write_document=write",
            "with Journal(pathlib.Path(sys.argv[1])) as journal:",
            " s.stage_archive(journal,sys.argv[2])",
            " os._exit(29)",
        ])
        result = subprocess.run([sys.executable, "-c", code, str(state), item["id"], fault],
                                capture_output=True, timeout=15, check=False)
        assert result.returncode == 29, result.stderr
        with isolation.Journal(state) as journal:
            receipt = stage.stage_archive(journal, item["id"], NeverRead())
            assert receipt["archive"]["archive_sha256"] == request["archive_sha256"]
            assert journal.toolchain_imports()[0]["phase"] == "archive_verified"


def test_incomplete_stream_stays_unverified_until_explicit_hash_bound_quarantine(host):
    journal, item, body, directory = host
    prefix = body[:700]
    with pytest.raises(ValueError):
        stage.stage_archive(journal, item["id"], io.BytesIO(prefix))
    assert journal.toolchain_imports()[0]["phase"] == "prepared"
    assert not (directory / "archive.tar").exists()
    size, sha = stage.file_hash(directory / "archive.partial")
    with pytest.raises(stage.ToolchainStagingError):
        stage.quarantine_partial(journal, item["id"], expected_sha256="f" * 64, expected_bytes=size)
    assert stage.quarantine_partial(journal, item["id"], expected_sha256=sha, expected_bytes=size)["quarantined"]
    assert (directory / ("quarantine-" + sha + ".tar")).read_bytes() == prefix
    assert stage.quarantine_partial(journal, item["id"], expected_sha256=sha, expected_bytes=size)["quarantined"]
    receipt = stage.stage_archive(journal, item["id"], io.BytesIO(body))
    assert receipt["archive"]["archive_bytes"] == len(body)


@pytest.mark.parametrize("fault", ["after_intent", "after_rename"])
def test_quarantine_interruption_requires_explicit_same_custody_reconciliation(host, monkeypatch, fault):
    journal, item, body, directory = host
    stage.context(journal, item["id"])
    partial = directory / "archive.partial"
    partial.write_bytes(body[:700])
    partial.chmod(0o600)
    size, sha = stage.file_hash(partial)
    real_write, real_sync = stage.write_document, stage.sync

    def write(path, value):
        real_write(path, value)
        if fault == "after_intent" and value["entries"][-1]["phase"] == "prepared":
            raise InterruptedError("lost operator after durable intent")

    def sync(path):
        real_sync(path)
        if fault == "after_rename" and (path / ("quarantine-" + sha + ".tar")).exists():
            raise InterruptedError("lost operator after durable rename")

    monkeypatch.setattr(stage, "write_document", write)
    monkeypatch.setattr(stage, "sync", sync)
    with pytest.raises(InterruptedError):
        stage.quarantine_partial(journal, item["id"], expected_sha256=sha, expected_bytes=size)
    monkeypatch.setattr(stage, "write_document", real_write)
    monkeypatch.setattr(stage, "sync", real_sync)
    with pytest.raises(stage.ToolchainStagingError, match="reconciliation"):
        stage.stage_archive(journal, item["id"], NeverRead())
    stage.quarantine_partial(journal, item["id"], expected_sha256=sha, expected_bytes=size)
    stage.stage_archive(journal, item["id"], io.BytesIO(body))


def test_control_prefix_write_recovers_but_foreign_bytes_are_never_truncated(host):
    journal, item, body, directory = host
    first = stage.stage_archive(journal, item["id"], io.BytesIO(body))
    pending = directory / "custody.pending"
    pending.write_bytes(stage.canonical(first)[:40])
    pending.chmod(0o600)
    assert stage.stage_archive(journal, item["id"], NeverRead()) == first
    pending.write_bytes(b"foreign-prefix")
    pending.chmod(0o600)
    with pytest.raises(stage.ToolchainStagingError, match="needs review"):
        stage.stage_archive(journal, item["id"], NeverRead())
    assert pending.read_bytes() == b"foreign-prefix"


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "world_readable", "fifo", "unknown"])
def test_unsafe_staging_inode_never_becomes_verified(host, kind):
    journal, item, body, directory = host
    stage.context(journal, item["id"])
    partial = directory / "archive.partial"
    if kind == "symlink":
        partial.symlink_to(directory.parent)
    elif kind == "hardlink":
        original = journal.directory / "original"
        original.write_bytes(body)
        original.chmod(0o600)
        os.link(original, partial)
    elif kind == "fifo":
        os.mkfifo(partial, 0o600)
    elif kind == "unknown":
        (directory / "unreviewed.sh").touch(mode=0o600)
    else:
        partial.write_bytes(body)
        partial.chmod(0o644)
    with pytest.raises(stage.ToolchainStagingError):
        stage.stage_archive(journal, item["id"], NeverRead())
    assert journal.toolchain_imports()[0]["phase"] == "prepared"


@pytest.mark.parametrize("fault", ["changed_archive", "wrong_count", "malformed"])
def test_wrong_stream_or_reserved_custody_cannot_publish_receipt(host, fault):
    journal, item, body, directory = host
    if fault == "changed_archive":
        body = body[:-1] + b"changed"
    elif fault == "wrong_count":
        body = body[:-512]
    else:
        body = b"invalid" * 200
    with pytest.raises(ValueError):
        stage.stage_archive(journal, item["id"], io.BytesIO(body))
    assert not (directory / "custody.json").exists()
    assert journal.toolchain_imports()[0]["phase"] == "prepared"


def test_source_cannot_return_an_unbounded_buffer_before_writer_checks_it(host):
    journal, item, body, directory = host

    class Oversize:
        def read(self, size):
            return b"x" * (size + 1)

    with pytest.raises(stage.ToolchainStagingError, match="byte contract"):
        stage.stage_archive(journal, item["id"], Oversize())
    assert (directory / "archive.partial").stat().st_size == 0


def test_no_new_upload_after_expiry_but_existing_complete_bytes_can_be_reconciled(host, monkeypatch):
    journal, item, body, directory = host
    monkeypatch.setattr(stage.time, "time", lambda: item["intent"]["stop_unix"] + 1)
    with pytest.raises(stage.ToolchainStagingError, match="unexpired"):
        stage.stage_archive(journal, item["id"], io.BytesIO(body))
    (directory / "archive.partial").write_bytes(body)
    (directory / "archive.partial").chmod(0o600)
    assert stage.stage_archive(journal, item["id"], NeverRead())["archive"]["image_loaded"] is False


def test_quarantine_count_is_bounded_and_never_pruned(host):
    journal, item, body, directory = host
    stage.context(journal, item["id"])
    for index in range(4):
        partial = directory / "archive.partial"
        partial.write_bytes(bytes([index]) * 70)
        partial.chmod(0o600)
        size, sha = stage.file_hash(partial)
        if index < 3:
            stage.quarantine_partial(journal, item["id"], expected_sha256=sha, expected_bytes=size)
        else:
            with pytest.raises(stage.ToolchainStagingError, match="bound"):
                stage.quarantine_partial(journal, item["id"], expected_sha256=sha, expected_bytes=size)
    assert len(stage.quarantine_audit(directory)["entries"]) == 3
    assert len(list(directory.glob("quarantine-*.tar"))) == 3


def test_unknown_intent_or_inactive_journal_has_no_filesystem_authority(host):
    journal, item, body, directory = host
    with pytest.raises(stage.ToolchainStagingError):
        stage.stage_archive(journal, "f" * 64, io.BytesIO(body))
    with pytest.raises(stage.ToolchainStagingError):
        stage.stage_archive(None, item["id"], io.BytesIO(body))
    assert not directory.exists()


def test_low_disk_headroom_rejects_before_creating_a_partial(host, monkeypatch):
    journal, item, body, directory = host
    from types import SimpleNamespace
    monkeypatch.setattr(stage.shutil, "disk_usage", lambda path: SimpleNamespace(free=len(body)))
    with pytest.raises(stage.ToolchainStagingError, match="headroom"):
        stage.stage_archive(journal, item["id"], NeverRead())
    assert not (directory / "archive.partial").exists()


def test_complete_archive_is_not_accepted_from_receipt_or_filename_alone(host):
    journal, item, body, directory = host
    stage.stage_archive(journal, item["id"], io.BytesIO(body))
    stored = directory / "archive.tar"
    changed = bytearray(stored.read_bytes())
    changed[-1] = 1
    stored.write_bytes(changed)
    with pytest.raises(ValueError):
        stage.stage_archive(journal, item["id"], NeverRead())


def test_pending_quarantine_control_prefix_can_be_reconstructed_only_from_exact_partial(host):
    journal, item, body, directory = host
    stage.context(journal, item["id"])
    partial = directory / "archive.partial"
    partial.write_bytes(body[:700])
    partial.chmod(0o600)
    size, sha = stage.file_hash(partial)
    expected = {"version": 1, "entries": [{"sha256": sha, "bytes": size, "phase": "prepared"}]}
    pending = directory / "quarantine.pending"
    pending.write_bytes(stage.canonical(expected)[:40])
    pending.chmod(0o600)
    with pytest.raises(stage.ToolchainStagingError):
        stage.stage_archive(journal, item["id"], NeverRead())
    stage.quarantine_partial(journal, item["id"], expected_sha256=sha, expected_bytes=size)
    assert stage.quarantine_audit(directory)["entries"][0]["phase"] == "committed"
    stage.stage_archive(journal, item["id"], io.BytesIO(body))


def test_expired_or_revoked_import_phase_cannot_be_replaced_by_staging(host):
    journal, item, body, directory = host
    journal.record_toolchain_import(item["id"], "prepared", "cancelled", "f" * 64)
    with pytest.raises(stage.ToolchainStagingError, match="phase"):
        stage.stage_archive(journal, item["id"], NeverRead())
    assert not directory.exists()


def test_reader_returning_after_deadline_never_writes_its_late_buffer(host, monkeypatch):
    journal, item, body, directory = host
    clock = [0]
    monkeypatch.setattr(stage.time, "monotonic", lambda: clock[0])

    class Late:
        def read(self, size):
            clock[0] = 1000
            return body[:size]

    with pytest.raises(stage.ToolchainStagingError, match="byte contract"):
        stage.stage_archive(journal, item["id"], Late(), timeout_seconds=1)
    assert (directory / "archive.partial").stat().st_size == 0


@pytest.mark.parametrize("fault", ["after_intent", "after_rename", "control_prefix"])
def test_real_process_death_during_quarantine_can_reconcile_without_deleting_bytes(fault):
    with tempfile.TemporaryDirectory(prefix=".gca-quarantine-death-", dir=Path.home()) as name:
        state = Path(name)
        files, image = fixture()
        body = archive(files)
        request = {"version": 1, "role": "react", "host_instance_id": "822115653245959101",
                   "image_id": image, "archive_sha256": stage.checksum(body), "archive_bytes": len(body),
                   "recipe_sha256": "c" * 64, "policy_sha256": "d" * 64,
                   "registry_sha256": "e" * 64, "stop_unix": int(time.time()) + 3600}
        with isolation.Journal(state) as journal:
            item = journal.begin_toolchain_import(request)
            _, directory = stage.context(journal, item["id"])
            partial = directory / "archive.partial"
            partial.write_bytes(body[:700])
            partial.chmod(0o600)
            size, sha = stage.file_hash(partial)
        code = "\n".join([
            "import os,pathlib,sys",
            "from app.coding.isolated_validation import Journal",
            "from app.coding import toolchain_staging as s",
            "fault=sys.argv[4]",
            "real_write=s.write_document",
            "def write(path,value):",
            " if fault=='control_prefix':",
            "  pending=path.with_suffix('.pending')",
            "  fd=os.open(pending,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)",
            "  os.write(fd,s.canonical(value)[:40]);os.fsync(fd);os._exit(31)",
            " real_write(path,value)",
            " if fault=='after_intent': os._exit(31)",
            "s.write_document=write",
            "real_sync=s.sync",
            "def sync(path):",
            " real_sync(path)",
            " if fault=='after_rename' and list(path.glob('quarantine-*.tar')): os._exit(31)",
            "s.sync=sync",
            "with Journal(pathlib.Path(sys.argv[1])) as journal:",
            " s.quarantine_partial(journal,sys.argv[2],expected_sha256=sys.argv[3],expected_bytes=700)",
        ])
        result = subprocess.run([sys.executable, "-c", code, str(state), item["id"], sha, fault],
                                capture_output=True, timeout=15, check=False)
        assert result.returncode == 31, result.stderr
        with isolation.Journal(state) as journal:
            with pytest.raises(stage.ToolchainStagingError):
                stage.stage_archive(journal, item["id"], NeverRead())
            stage.quarantine_partial(journal, item["id"], expected_sha256=sha, expected_bytes=size)
            assert (directory / ("quarantine-" + sha + ".tar")).read_bytes() == body[:700]
            assert not partial.exists()
            stage.stage_archive(journal, item["id"], io.BytesIO(body))
