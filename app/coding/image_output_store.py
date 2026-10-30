"""Private, bounded, crash-persistent OCI output storage. Never a publishing API.

The authenticated packaging controller supplies the consumed job binding. An
output measurement proves stored bytes, not worker/process cleanup or deployment
authority. Expired objects can be removed, but resource admission must separately
authenticate execution termination before releasing its outstanding obligation.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tarfile
import time
from uuid import UUID, uuid4

from app.coding import oci_packager as oci

MAX_OUTPUT_BYTES = 768 * 1024 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_OUTPUTS = 8
MAX_RECORDS = 4096
HASH = re.compile(r"^[0-9a-f]{64}$")
BINDING_FIELDS = {
    "version", "build_job_id", "dispatch_token", "request_hash", "source_hash",
    "validation_claim_id", "validation_job_id", "profile", "artifact_sha256",
    "base_manifest_digest", "runtime_policy_hash", "execution_deadline_unix", "output_expires_at_unix",
    "timeout_seconds",
    "target_hash", "server_uuid", "project_id",
}


class ImageOutputError(RuntimeError):
    pass


def verify_binding(binding, *, now=None, allow_expired=False):
    now = int(time.time()) if now is None else now
    if not isinstance(binding, dict) or set(binding) != BINDING_FIELDS:
        raise ImageOutputError("image output binding fields are invalid")
    try:
        identifiers = all(str(UUID(binding[key])) == binding[key]
                          for key in ("build_job_id", "dispatch_token", "validation_claim_id"))
    except (ValueError, TypeError, AttributeError):
        identifiers = False
    if (type(binding["version"]) is not int or binding["version"] != 1 or not identifiers
            or any(not isinstance(binding[key], str) or not HASH.fullmatch(binding[key])
                   for key in ("request_hash", "source_hash", "artifact_sha256", "runtime_policy_hash",
                               "target_hash"))
            or not isinstance(binding["server_uuid"], str)
            or not re.fullmatch(r"[A-Za-z0-9-]{1,128}", binding["server_uuid"])
            or not isinstance(binding["project_id"], str)
            or not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", binding["project_id"])
            or not isinstance(binding["validation_job_id"], str)
            or not re.fullmatch(r"[0-9a-f]{32}", binding["validation_job_id"])
            or not isinstance(binding["profile"], str)
            or binding["profile"] not in {"react_build", "spring_package"}
            or not isinstance(binding["base_manifest_digest"], str)
            or not oci.DIGEST.fullmatch(binding["base_manifest_digest"])
            or type(binding["execution_deadline_unix"]) is not int
            or type(binding["output_expires_at_unix"]) is not int
            or type(binding["timeout_seconds"]) is not int or not 1 <= binding["timeout_seconds"] <= 900
            or not binding["output_expires_at_unix"] <= now + 86460
            or (not allow_expired and binding["output_expires_at_unix"] <= now)
            or binding["execution_deadline_unix"] > binding["output_expires_at_unix"]):
        raise ImageOutputError("image output binding is malformed or expired")
    return dict(binding)


def _private(path, *, directory=False):
    info = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if (not expected(info.st_mode) or path.is_symlink() or info.st_uid != os.geteuid()
            or info.st_mode & 0o077 or (not directory and info.st_nlink != 1)):
        raise ImageOutputError("image output store is not owner-private")
    return info


def _sync(directory):
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _locked(root: Path):
    if not root.is_absolute():
        raise ImageOutputError("image store needs an absolute operator path")
    _private(root, directory=True)
    lock_path = root / "store.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    database = None
    try:
        _private(lock_path)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ImageOutputError("image output store is busy") from error
        db_path = root / "outputs.sqlite3"
        fd = os.open(db_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        if _private(db_path).st_size > 16 * 1024 * 1024:
            raise ImageOutputError("image output journal exceeds its bound")
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = root / ("outputs.sqlite3" + suffix)
            if sidecar.exists() or sidecar.is_symlink():
                _private(sidecar)
        database = sqlite3.connect(db_path, timeout=2)
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA journal_mode=WAL")
        database.execute("PRAGMA synchronous=FULL")
        database.execute("""CREATE TABLE IF NOT EXISTS outputs(
            job_id TEXT PRIMARY KEY,binding TEXT NOT NULL,binding_hash TEXT NOT NULL,
            evidence TEXT NOT NULL,archive_hash TEXT NOT NULL,archive_bytes INTEGER NOT NULL,
            expires INTEGER NOT NULL,state TEXT NOT NULL CHECK(state IN ('writing','retained','purged')),
            completed_at TEXT,created_at INTEGER NOT NULL)""")
        yield database
    finally:
        if database:
            database.close()
        os.close(descriptor)


def inspect_layout(payload: bytes, *, profile: str, expected_manifest: str):
    """Rehash the complete OCI layout without extracting any member to disk."""
    if (not isinstance(payload, bytes) or not 1 <= len(payload) <= MAX_OUTPUT_BYTES
            or profile not in {"react_build", "spring_package"}
            or not isinstance(expected_manifest, str) or not oci.DIGEST.fullmatch(expected_manifest)):
        raise ImageOutputError("image output archive bounds or identity are invalid")
    files = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
            for item in archive:
                if (not item.isfile() or item.name in files or len(files) >= 70
                        or not (item.name in {"oci-layout", "index.json"}
                                or re.fullmatch(r"blobs/sha256/[a-f0-9]{64}", item.name))
                        or not 1 <= item.size <= oci.MAX_BASE_BYTES):
                    raise ImageOutputError("unsafe or excessive image output archive member")
                stream = archive.extractfile(item)  # Read only; never filesystem extraction.
                body = stream.read(item.size + 1)
                if len(body) != item.size:
                    raise ImageOutputError("truncated image output archive member")
                files[item.name] = body
        if oci._metadata(files["oci-layout"]) != {"imageLayoutVersion": "1.0.0"}:
            raise ImageOutputError("unsupported image output layout")
        index = oci._metadata(files["index.json"])
        entries = index.get("manifests")
        if (type(index.get("schemaVersion")) is not int or index["schemaVersion"] != 2
                or not isinstance(entries, list) or len(entries) != 1
                or entries[0].get("digest") != expected_manifest):
            raise ImageOutputError("image output index does not bind manifest")

        def blob(checksum):
            if not isinstance(checksum, str) or not oci.DIGEST.fullmatch(checksum):
                raise ImageOutputError("invalid image output blob digest")
            body = files["blobs/sha256/" + checksum.split(":", 1)[1]]
            if oci.digest(body) != checksum:
                raise ImageOutputError("image output blob digest changed")
            return body

        manifest = blob(expected_manifest)
        metadata = oci._metadata(manifest)
        config = blob(metadata["config"]["digest"])
        layers = tuple(blob(item["digest"]) for item in metadata["layers"])
        role = "nginx_static_v1" if profile == "react_build" else "temurin_java17_v1"
        config_data = oci._metadata(config)
        rootfs = config_data.get("rootfs", {})
        runtime = config_data.get("config", {})
        if (type(metadata.get("schemaVersion")) is not int or metadata["schemaVersion"] != 2
                or metadata.get("mediaType") != oci.OCI_MANIFEST
                or not 2 <= len(layers) <= oci.MAX_LAYERS + 1
                or sum(map(len, layers)) > MAX_OUTPUT_BYTES
                or config_data.get("architecture") != "amd64" or config_data.get("os") != "linux"
                or rootfs.get("type") != "layers" or len(rootfs.get("diff_ids", [])) != len(layers)
                or runtime.get("OnBuild") != [] or runtime.get("User") != "65532:65532"
                or runtime.get("Entrypoint") != (["/usr/sbin/nginx"] if role == "nginx_static_v1"
                                               else ["/opt/java/openjdk/bin/java"])):
            raise ImageOutputError("image output runtime contract differs")
        oci._descriptor(metadata["config"], config, {oci.OCI_CONFIG})
        expanded = 0
        for descriptor, body, expected_diff in zip(metadata["layers"], layers, rootfs["diff_ids"]):
            oci._descriptor(descriptor, body, {oci.OCI_LAYER})
            actual, size = oci._layer_diff_id(body, oci.MAX_BASE_UNPACKED_BYTES + 129 * 1024 * 1024 - expanded)
            expanded += size
            if actual != expected_diff:
                raise ImageOutputError("image output rootfs content changed")
        required = {"oci-layout", "index.json", "blobs/sha256/" + expected_manifest.split(":")[1]}
        required.add("blobs/sha256/" + metadata["config"]["digest"].split(":")[1])
        required.update("blobs/sha256/" + item["digest"].split(":")[1] for item in metadata["layers"])
        if set(files) != required or entries[0].get("size") != len(manifest):
            raise ImageOutputError("image output includes unbound content")
        return {"image_manifest_digest": expected_manifest,
                "image_config_digest": oci.digest(config), "output_layer_digest": oci.digest(layers[-1])}
    except (KeyError, TypeError, ValueError, AttributeError, tarfile.TarError, EOFError) as error:
        raise ImageOutputError("image output layout failed content verification") from error


def _measurement(row):
    return {"build_job_id": row["job_id"], "binding_hash": row["binding_hash"],
            "output_archive_sha256": row["archive_hash"], "output_archive_bytes": row["archive_bytes"],
            "output_expires_at_unix": row["expires"], "completed_at": row["completed_at"],
            "evidence": json.loads(row["evidence"])}


def _retained(root, row, binding):
    if (row["state"] != "retained" or row["expires"] <= int(time.time())
            or row["binding"] != oci.canonical(binding).decode()
            or row["binding_hash"] != hashlib.sha256(oci.canonical(binding)).hexdigest()):
        raise ImageOutputError("image output is absent, expired or bound to another dispatch")
    directory = root / row["job_id"]
    _private(directory, directory=True)
    path = directory / "image.oci.tar"
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or info.st_nlink != 1
                or info.st_size != row["archive_bytes"] or info.st_size > MAX_OUTPUT_BYTES):
            raise ImageOutputError("image output file identity changed")
        body = stream.read(MAX_OUTPUT_BYTES + 1)
    if len(body) != row["archive_bytes"] or hashlib.sha256(body).hexdigest() != row["archive_hash"]:
        raise ImageOutputError("retained image output bytes changed")
    measured = inspect_layout(body, profile=binding["profile"],
                              expected_manifest=json.loads(row["evidence"])["image_manifest_digest"])
    if any(measured[key] != json.loads(row["evidence"])[key] for key in measured):
        raise ImageOutputError("retained image output evidence changed")
    return body, _measurement(row)


def save(root: Path, binding: dict, image: oci.PackagedImage):
    """Store measured actual bytes atomically; never return a cleanup/success assertion."""
    binding = verify_binding(binding)
    if type(image) is not oci.PackagedImage:
        raise ImageOutputError("trusted packager output is required")
    expected = {key: binding[key] for key in (
        "profile", "source_hash", "validation_job_id", "artifact_sha256", "base_manifest_digest",
    )}
    expected["claim_id"] = binding["validation_claim_id"]
    if any(image.evidence[key] != value for key, value in expected.items()):
        raise ImageOutputError("packaged image differs from the consumed build binding")
    payload = image.layout_archive()
    measured = inspect_layout(payload, profile=image.profile, expected_manifest=image.manifest_digest)
    if any(image.evidence[key] != value for key, value in measured.items()):
        raise ImageOutputError("packaged image measurements changed")
    checksum = hashlib.sha256(payload).hexdigest()
    identity = oci.canonical(binding).decode()
    with _locked(root) as db:
        row = db.execute("SELECT * FROM outputs WHERE job_id=?", (binding["build_job_id"],)).fetchone()
        if row:
            if row["archive_hash"] != checksum or row["evidence"] != oci.canonical(image.evidence).decode():
                raise ImageOutputError("image output is immutable")
            return _retained(root, row, binding)[1]
        now = int(time.time())
        if now >= binding["execution_deadline_unix"]:
            raise ImageOutputError("image execution authority expired before commit")
        used = db.execute("SELECT count(*),COALESCE(sum(archive_bytes),0) FROM outputs "
                          "WHERE state<>'purged'").fetchone()
        if (used[0] >= MAX_OUTPUTS or used[1] + len(payload) > MAX_TOTAL_BYTES
                or db.execute("SELECT count(*) FROM outputs").fetchone()[0] >= MAX_RECORDS):
            raise ImageOutputError("image output store capacity exhausted")
        with db:
            db.execute("INSERT INTO outputs VALUES(?,?,?,?,?,?,?,'writing',NULL,?)", (
                binding["build_job_id"], identity, hashlib.sha256(identity.encode()).hexdigest(),
                oci.canonical(image.evidence).decode(), checksum, len(payload), binding["output_expires_at_unix"], now,
            ))
        directory = root / binding["build_job_id"]
        directory.mkdir(mode=0o700)  # Existing untracked content must never be adopted.
        _sync(root)
        temporary = directory / (".tmp-" + uuid4().hex)
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, directory / "image.oci.tar")
        temporary.unlink()
        _sync(directory)
        completed = datetime.now(timezone.utc).isoformat()
        if int(time.time()) >= binding["execution_deadline_unix"]:
            raise ImageOutputError("image execution authority expired during commit")
        with db:
            db.execute("UPDATE outputs SET state='retained',completed_at=? WHERE job_id=?",
                       (completed, binding["build_job_id"]))
        return _measurement(db.execute("SELECT * FROM outputs WHERE job_id=?",
                                       (binding["build_job_id"],)).fetchone())


def load(root: Path, binding: dict):
    binding = verify_binding(binding)
    with _locked(root) as db:
        row = db.execute("SELECT * FROM outputs WHERE job_id=?", (binding["build_job_id"],)).fetchone()
        if not row:
            raise ImageOutputError("image output has no durable record")
        return _retained(root, row, binding)


def remove(root: Path, binding: dict, *, now=None, expired_only=False):
    """Remove only exact job objects. This does not prove the worker process stopped."""
    current = int(time.time()) if now is None else now
    # Cleanup must remain possible after output expiry, but binds every original field.
    verify_binding(binding, now=current, allow_expired=True)
    identity = oci.canonical(binding).decode()
    with _locked(root) as db:
        row = db.execute("SELECT * FROM outputs WHERE job_id=?", (binding["build_job_id"],)).fetchone()
        if (not row or row["binding"] != identity
                or row["binding_hash"] != hashlib.sha256(identity.encode()).hexdigest()):
            raise ImageOutputError("image cleanup has no matching dispatch")
        if expired_only and row["expires"] > current:
            raise ImageOutputError("live image output is not eligible for expiry cleanup")
        directory = root / row["job_id"]
        if directory.exists() or directory.is_symlink():
            _private(directory, directory=True)
            paths = list(directory.iterdir())
            if len(paths) > 3:
                raise ImageOutputError("unexpected image output cleanup inventory")
            for path in paths:
                if path.name != "image.oci.tar" and not re.fullmatch(r"\.tmp-[a-f0-9]{32}", path.name):
                    raise ImageOutputError("untracked image output cleanup object")
                _private(path)
            for path in paths:
                path.unlink()
            directory.rmdir()
            _sync(root)
        with db:
            db.execute("UPDATE outputs SET state='purged' WHERE job_id=?", (row["job_id"],))
        observation = {"build_job_id": row["job_id"], "binding_hash": row["binding_hash"],
                       "output_removed": True, "execution_cleanup_verified": False,
                       "observed_at": datetime.fromtimestamp(current, timezone.utc).isoformat()}
        return observation
