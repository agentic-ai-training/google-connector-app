"""Real synthetic OCI persistence; no registry, signing key or cloud resource."""
from dataclasses import replace
from datetime import datetime
import hashlib
import io
import os
from pathlib import Path
import sqlite3
import tarfile
import time
from uuid import uuid4

import pytest

from app.coding import image_output_store as store
from app.coding import oci_packager as oci
from tests.coding.test_oci_packager import base, case, archive


@pytest.fixture
def output(tmp_path):
    root = tmp_path / "private-images"
    root.mkdir(mode=0o700)
    body, reference, args = case()
    runtime = base()
    image = oci.package_validated_artifact(runtime, body, reference, **args)
    now = int(time.time())
    binding = dict(version=1, build_job_id=str(uuid4()), dispatch_token=str(uuid4()),
                   request_hash="e" * 64, source_hash=image.source_hash,
                   validation_claim_id=image.claim_id, validation_job_id=image.validation_job_id,
                   profile=image.profile, artifact_sha256=image.artifact_sha256,
                   base_manifest_digest=image.base_manifest_digest, runtime_policy_hash="f" * 64,
                   execution_deadline_unix=now + 600, output_expires_at_unix=now + 3600, timeout_seconds=60,
                   target_hash="c" * 64, server_uuid="server", project_id="test-project")
    return root, binding, image


def test_store_recovery_has_exact_bytes_and_immutable_measurements(output):
    root, binding, image = output
    measured = store.save(root, binding, image)
    actual, recovered = store.load(root, binding)
    assert actual == image.layout_archive() and recovered == measured
    assert hashlib.sha256(actual).hexdigest() == measured["output_archive_sha256"]
    assert store.save(root, binding, image) == measured
    assert datetime.fromisoformat(measured["completed_at"]).utcoffset() is not None
    assert "execution_cleanup_verified" not in measured
    assert (root / binding["build_job_id"] / "image.oci.tar").stat().st_mode & 0o777 == 0o600
    assert (root / binding["build_job_id"]).stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("field,value", [("dispatch_token", str(uuid4())), ("request_hash", "a" * 64),
                                        ("runtime_policy_hash", "a" * 64), ("source_hash", "a" * 64)])
def test_another_binding_cannot_retrieve_or_remove_output(output, field, value):
    root, binding, image = output
    store.save(root, binding, image)
    changed = {**binding, field: value}
    with pytest.raises(store.ImageOutputError, match="another dispatch"):
        store.load(root, changed)
    with pytest.raises(store.ImageOutputError, match="matching dispatch"):
        store.remove(root, changed)
    assert store.load(root, binding)[0] == image.layout_archive()


def test_changed_archive_fails_rehash(output):
    root, binding, image = output
    store.save(root, binding, image)
    path = root / binding["build_job_id"] / "image.oci.tar"
    data = bytearray(path.read_bytes())
    data[-1] = 1
    path.write_bytes(data)
    with pytest.raises(store.ImageOutputError, match="bytes changed"):
        store.load(root, binding)


@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "permissions"])
def test_unsafe_output_inode_is_not_loaded(output, unsafe):
    root, binding, image = output
    store.save(root, binding, image)
    path = root / binding["build_job_id"] / "image.oci.tar"
    if unsafe == "symlink":
        path.unlink()
        path.symlink_to(root / "store.lock")
    elif unsafe == "hardlink":
        os.link(path, root / "alias")
    else:
        path.chmod(0o644)
    with pytest.raises((store.ImageOutputError, OSError)):
        store.load(root, binding)


def test_expired_output_removes_exact_files_without_claiming_process_cleanup(output, monkeypatch):
    root, binding, image = output
    store.save(root, binding, image)
    later = binding["output_expires_at_unix"] + 1
    monkeypatch.setattr(store.time, "time", lambda: later)
    with pytest.raises(store.ImageOutputError, match="expired"):
        store.load(root, binding)
    observation = store.remove(root, binding, expired_only=True)
    assert observation["output_removed"] is True and observation["execution_cleanup_verified"] is False
    assert not (root / binding["build_job_id"]).exists()
    assert store.remove(root, binding, expired_only=True)["output_removed"] is True
    with sqlite3.connect(root / "outputs.sqlite3") as db:
        assert db.execute("SELECT state FROM outputs").fetchone()[0] == "purged"


def test_live_output_is_not_reaped(output):
    root, binding, image = output
    store.save(root, binding, image)
    with pytest.raises(store.ImageOutputError, match="not eligible"):
        store.remove(root, binding, expired_only=True)


def test_partial_commit_is_retained_and_never_blindly_rewritten(output, monkeypatch):
    root, binding, image = output
    original = store._sync

    def interrupted(path):
        if path.name == binding["build_job_id"]:
            raise OSError("simulated interruption after file commit")
        original(path)

    monkeypatch.setattr(store, "_sync", interrupted)
    with pytest.raises(OSError, match="interruption"):
        store.save(root, binding, image)
    monkeypatch.setattr(store, "_sync", original)
    with sqlite3.connect(root / "outputs.sqlite3") as db:
        assert db.execute("SELECT state FROM outputs").fetchone()[0] == "writing"
    with pytest.raises(store.ImageOutputError, match="absent"):
        store.save(root, binding, image)
    store.remove(root, binding)
    with pytest.raises(store.ImageOutputError, match="absent"):
        store.save(root, binding, image)


def test_quota_counts_interrupted_writes(output, monkeypatch):
    root, binding, image = output
    monkeypatch.setattr(store, "MAX_OUTPUTS", 1)
    store.save(root, binding, image)
    with pytest.raises(store.ImageOutputError, match="capacity"):
        store.save(root, {**binding, "build_job_id": str(uuid4()), "dispatch_token": str(uuid4())}, image)


def test_byte_quota_enforced_before_job_or_file_creation(output, monkeypatch):
    root, binding, image = output
    monkeypatch.setattr(store, "MAX_TOTAL_BYTES", 1)
    with pytest.raises(store.ImageOutputError, match="capacity"):
        store.save(root, binding, image)
    with sqlite3.connect(root / "outputs.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM outputs").fetchone()[0] == 0
    assert not (root / binding["build_job_id"]).exists()


def test_cleanup_refuses_untracked_objects(output):
    root, binding, image = output
    store.save(root, binding, image)
    unrelated = root / binding["build_job_id"] / "not-an-image"
    unrelated.write_bytes(b"user-owned data")
    with pytest.raises(store.ImageOutputError, match="untracked"):
        store.remove(root, binding)
    assert unrelated.read_bytes() == b"user-owned data"


def test_layout_rejects_symlinks_duplicate_paths_and_unbound_blobs(output):
    _, _, image = output
    payload = image.layout_archive()
    with tarfile.open(fileobj=io.BytesIO(payload)) as source:
        files = {item.name: source.extractfile(item).read() for item in source}
    files["blobs/sha256/" + "f" * 64] = b"unbound"
    with pytest.raises(store.ImageOutputError, match="unbound"):
        store.inspect_layout(archive(files), profile=image.profile, expected_manifest=image.manifest_digest)
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as target:
        link = tarfile.TarInfo("oci-layout")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        target.addfile(link)
    with pytest.raises(store.ImageOutputError, match="unsafe"):
        store.inspect_layout(stream.getvalue(), profile=image.profile, expected_manifest=image.manifest_digest)


def test_changed_image_cannot_replace_stored_output(output):
    root, binding, image = output
    store.save(root, binding, image)
    with pytest.raises((store.ImageOutputError, oci.ImagePackagingError)):
        store.save(root, binding, replace(image, config=b"{}"))


def test_expired_execution_cannot_commit_new_output(output):
    root, binding, image = output
    binding["execution_deadline_unix"] = int(time.time()) - 1
    with pytest.raises(store.ImageOutputError, match="execution authority"):
        store.save(root, binding, image)


def test_store_lock_contention_is_bounded(output):
    root, binding, image = output
    store.save(root, binding, image)
    with store._locked(root):
        with pytest.raises(store.ImageOutputError, match="busy"):
            store.load(root, binding)


def test_journal_sidecar_symlink_rejected(output):
    root, binding, image = output
    store.save(root, binding, image)
    (root / "outputs.sqlite3-wal").symlink_to(root / "store.lock")
    with pytest.raises(store.ImageOutputError, match="owner-private"):
        store.load(root, binding)


def test_binding_rejects_paths_and_extra_authority(output):
    _, binding, _ = output
    for changed in ({**binding, "build_job_id": "../../outside"}, {**binding, "publish": True},
                    {**binding, "execution_deadline_unix": True}, {**binding, "dispatch_token": None},
                    {**binding, "profile": []}):
        with pytest.raises(store.ImageOutputError):
            store.verify_binding(changed)
