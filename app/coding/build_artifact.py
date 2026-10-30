"""Inspect build outputs before a trusted builder may package them into images.

This module never extracts an archive or treats an artifact hash as build proof.
The source-bound gVisor validation job must produce these bytes; a later keyless
builder must measure the resulting immutable image independently.
"""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import fcntl
import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tarfile
import time
import uuid
import zipfile


MAX_ARTIFACT_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_ARTIFACT_FILES = 4000
MAX_JAR_ENTRIES = 20_000
ARTIFACT_TTL_SECONDS = 24 * 3600
MAX_STORED_ARTIFACT_BYTES = 512 * 1024 * 1024
MAX_STORED_ARTIFACTS = 8
_JAR_NAME = re.compile(r"target/[A-Za-z0-9][A-Za-z0-9._-]*\.jar")
_JOB_ID = re.compile(r"[0-9a-f]{32}")
_ARTIFACT_ID = re.compile(r"[0-9a-f]{32}-[0-9a-f]{64}\.tar")


class BuildArtifactError(ValueError):
    pass


@dataclass(frozen=True)
class ArtifactManifest:
    profile: str
    archive_sha256: str
    file_hashes: tuple[tuple[str, str], ...]
    file_count: int
    content_bytes: int


def verify_artifact_reference(reference: dict, *, profile: str, source_hash: str,
                              claim_id: str, job_id: str, now: int | None = None) -> dict:
    fields = {"artifact_id", "archive_sha256", "file_set_sha256", "file_count",
              "content_bytes", "profile", "validation_job_id", "claim_id", "source_hash",
              "expires_at_unix"}
    current = int(time.time()) if now is None else now
    if (not isinstance(reference, dict) or set(reference) != fields
            or profile not in {"react_build", "spring_package"}
            or reference["profile"] != profile or reference["source_hash"] != source_hash
            or reference["claim_id"] != claim_id or reference["validation_job_id"] != job_id
            or not _JOB_ID.fullmatch(job_id)
            or any(not isinstance(reference[key], str)
                   or not re.fullmatch(r"[0-9a-f]{64}", reference[key])
                   for key in ("archive_sha256", "file_set_sha256"))
            or reference["artifact_id"] != f"{job_id}-{reference['archive_sha256']}.tar"
            or type(reference["file_count"]) is not int
            or not 1 <= reference["file_count"] <= MAX_ARTIFACT_FILES
            or type(reference["content_bytes"]) is not int
            or not 1 <= reference["content_bytes"] <= MAX_ARTIFACT_ARCHIVE_BYTES
            or type(reference["expires_at_unix"]) is not int
            or not current < reference["expires_at_unix"] <= current + ARTIFACT_TTL_SECONDS + 60):
        raise BuildArtifactError("Build artifact reference does not bind the validation job")
    return dict(reference)


def _private_artifact_directory(journal_dir: Path) -> Path:
    root = journal_dir.lstat()
    if (not stat.S_ISDIR(root.st_mode) or root.st_uid != os.geteuid()
            or root.st_mode & 0o077):
        raise BuildArtifactError("Build artifact journal is not operator private")
    directory = journal_dir / "build-artifacts"
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise BuildArtifactError("Build artifact directory is not operator private")
    return directory


@contextmanager
def _locked_artifact_directory(journal_dir: Path):
    directory = _private_artifact_directory(journal_dir)
    descriptor = os.open(journal_dir / "artifact-store.lock",
                         os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077):
            raise BuildArtifactError("Build artifact lock is not operator private")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield directory
    finally:
        os.close(descriptor)


def _inventory(directory: Path):
    entries = list(directory.iterdir())
    if len(entries) > 128:
        raise BuildArtifactError("Build artifact inventory is unbounded")
    total = 0
    for path in entries:
        info = path.lstat()
        if (not (_ARTIFACT_ID.fullmatch(path.name)
                 or re.fullmatch(r"\.tmp-[0-9a-f]{32}", path.name))
                or not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077):
            raise BuildArtifactError("Unsafe build artifact directory entry")
        total += info.st_size
    return entries, total


def save_build_artifact(journal_dir: Path, job_id: str, payload: bytes,
                        manifest: ArtifactManifest) -> str:
    """Atomically retain exact validated bytes in an owner-only local CAS."""
    if (not _JOB_ID.fullmatch(job_id) or len(payload) > MAX_ARTIFACT_ARCHIVE_BYTES
            or inspect_build_artifact(payload, manifest.profile) != manifest):
        raise BuildArtifactError("Build artifact identity changed before storage")
    artifact_id = f"{job_id}-{manifest.archive_sha256}.tar"
    with _locked_artifact_directory(journal_dir) as directory:
        entries, size = _inventory(directory)
        if (len(entries) >= MAX_STORED_ARTIFACTS
                or size + len(payload) > MAX_STORED_ARTIFACT_BYTES):
            raise BuildArtifactError("Build artifact storage quota exhausted")
        temporary = directory / (".tmp-" + uuid.uuid4().hex)
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, directory / artifact_id)
            dirfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(dirfd)
            finally:
                os.close(dirfd)
        finally:
            temporary.unlink(missing_ok=True)
    return artifact_id


def load_build_artifact(journal_dir: Path, artifact_id: str, expected_sha256: str) -> bytes:
    """Read only a still-live exact CAS object; rehash before a build consumes it."""
    if (not _ARTIFACT_ID.fullmatch(artifact_id)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
            or artifact_id.split("-", 1)[1] != expected_sha256 + ".tar"):
        raise BuildArtifactError("Invalid build artifact reference")
    with _locked_artifact_directory(journal_dir) as directory:
        descriptor = os.open(directory / artifact_id, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077 or info.st_size > MAX_ARTIFACT_ARCHIVE_BYTES
                    or info.st_mtime > time.time() + 1
                    or time.time() - info.st_mtime > ARTIFACT_TTL_SECONDS):
                raise BuildArtifactError("Build artifact is unsafe, oversized or expired")
            payload = stream.read(MAX_ARTIFACT_ARCHIVE_BYTES + 1)
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise BuildArtifactError("Build artifact content changed")
    return payload


def purge_expired_build_artifacts(journal_dir: Path, *, now: float | None = None) -> int:
    """Bounded reaper for CAS objects and interrupted temporary writes."""
    current = time.time() if now is None else now
    deleted = 0
    with _locked_artifact_directory(journal_dir) as directory:
        entries, _ = _inventory(directory)
        for path in entries:
            if current - path.lstat().st_mtime > ARTIFACT_TTL_SECONDS:
                path.unlink()
                deleted += 1
    return deleted


def _boot_jar(data: bytes) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            names = {entry.filename for entry in entries}
            if (len(entries) > MAX_JAR_ENTRIES or len(names) != len(entries)
                    or "META-INF/MANIFEST.MF" not in names
                    or not any(name.startswith("BOOT-INF/classes/") and name.endswith(".class")
                               for name in names)
                    or not any(name.startswith("BOOT-INF/lib/") for name in names)):
                return False
            manifest = archive.getinfo("META-INF/MANIFEST.MF")
            if manifest.file_size > 65536:
                return False
            with archive.open(manifest) as stream:
                metadata = stream.read(65537)
                return (b"Start-Class:" in metadata
                        and b"Main-Class: org.springframework.boot.loader" in metadata)
    except (zipfile.BadZipFile, KeyError, OSError, ValueError, RuntimeError,
            NotImplementedError):
        return False


def inspect_build_artifact(archive_bytes: bytes, profile: str) -> ArtifactManifest:
    """Fail closed on unsafe tar members, missing React output or non-Boot JARs."""
    if profile not in {"react_build", "spring_package"}:
        raise BuildArtifactError("No generated artifact contract for validation profile")
    if (not isinstance(archive_bytes, bytes) or not archive_bytes
            or len(archive_bytes) > MAX_ARTIFACT_ARCHIVE_BYTES):
        raise BuildArtifactError("Build artifact archive exceeds its byte limit")
    prefix = "dist" if profile == "react_build" else "target"
    seen = set()
    file_hashes = []
    content_bytes = 0
    jar_count = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:") as archive:
            for member in archive:
                raw = member.name.rstrip("/")
                path = PurePosixPath(raw)
                if (raw != path.as_posix() or path.is_absolute() or ".." in path.parts
                        or raw in seen or (raw != prefix and not raw.startswith(prefix + "/"))
                        or any(part.startswith(".env") or part.endswith((".pem", ".key", ".p12"))
                               for part in path.parts)
                        or member.sparse is not None or member.size < 0):
                    raise BuildArtifactError("Build artifact contains an unsafe path")
                seen.add(raw)
                if member.isdir():
                    continue
                if not member.isfile() or len(file_hashes) >= MAX_ARTIFACT_FILES:
                    raise BuildArtifactError("Build artifact contains an unsafe member")
                if profile == "spring_package" and not _JAR_NAME.fullmatch(raw):
                    raise BuildArtifactError("Spring artifact must contain only one Boot JAR")
                content_bytes += member.size
                if content_bytes > MAX_ARTIFACT_ARCHIVE_BYTES:
                    raise BuildArtifactError("Build artifact content exceeds its byte limit")
                stream = archive.extractfile(member)
                if stream is None:
                    raise BuildArtifactError("Build artifact member cannot be read")
                digest = hashlib.sha256()
                jar_bytes = bytearray() if profile == "spring_package" else None
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
                    if jar_bytes is not None:
                        jar_bytes.extend(chunk)
                if member.size == 0 or (jar_bytes is not None and not _boot_jar(bytes(jar_bytes))):
                    raise BuildArtifactError("Build artifact has an empty or invalid file")
                jar_count += jar_bytes is not None
                file_hashes.append((raw, digest.hexdigest()))
    except (tarfile.TarError, OSError, ValueError) as error:
        if isinstance(error, BuildArtifactError):
            raise
        raise BuildArtifactError("Build artifact archive is invalid") from None
    if (profile == "react_build" and "dist/index.html" not in dict(file_hashes)):
        raise BuildArtifactError("React artifact lacks its built entry page")
    if profile == "spring_package" and jar_count != 1:
        raise BuildArtifactError("Spring artifact requires exactly one Boot JAR")
    return ArtifactManifest(
        profile=profile, archive_sha256=hashlib.sha256(archive_bytes).hexdigest(),
        file_hashes=tuple(sorted(file_hashes)), file_count=len(file_hashes),
        content_bytes=content_bytes,
    )
