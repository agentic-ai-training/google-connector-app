"""Pinned public Trivy data artifacts; streamed private staging, not execution.

Caller owns the existing host journal, cgroup and deadline. Databases are OCI
ARTIFACTS, not executable container images. Neither a digest nor freshness grants
runtime approval. The raw publisher metadata is preserved, never backdated.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import gzip
import os
from pathlib import Path
import stat
import tarfile
import time
import urllib.parse
import urllib.request

from app.coding import runtime_base_acquisition as cas, oci_packager as oci


CATALOG = {
    "vulnerability": {
        "repository": "aquasec/trivy-db", "discovery_tag": "2", "schema": 2,
        "manifest_digest": "sha256:2f2c5cf8bd240e46942462776aa912feeec723cdf50d224f6296278ac6e3fd10",
        "manifest_bytes": 616,
        "archive_digest": "sha256:7f3ba3d6e00f030a48ae163bcdbba68ddade03910ced826cc03400395b6362cc",
        "archive_bytes": 133_148_923,
        "layer_media": "application/vnd.aquasec.trivy.db.layer.v1.tar+gzip",
        "archive_title": "db.tar.gz", "database_name": "trivy.db",
        "database_bytes": 1_607_704_576,
    },
    "java": {
        "repository": "aquasec/trivy-java-db", "discovery_tag": "1", "schema": 1,
        "manifest_digest": "sha256:c670fad2358af7bd2ebeabe1ff9d58cadad9c59fcfd9033729a76894b62e7fab",
        "manifest_bytes": 624,
        "archive_digest": "sha256:92af64e106bbab0fa6edbd6a7e8af70a5b60002c6eff4a338eaa5f23c5a1ab38",
        "archive_bytes": 982_774_609,
        "layer_media": "application/vnd.aquasec.trivy.javadb.layer.v1.tar+gzip",
        "archive_title": "javadb.tar.gz", "database_name": "trivy-java.db",
        "database_bytes": 1_552_633_856,
    },
}
MAX_ARCHIVE = 1024**3
MAX_DATABASE = 2 * 1024**3
MAX_STORE = 8 * 1024**3
CHUNK = 1024**2
EMPTY_CONFIG = "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"


class DatabaseAcquisitionError(ValueError):
    pass


def pin(role):
    if not isinstance(role, str) or role not in CATALOG:
        raise DatabaseAcquisitionError("database is not in the reviewed catalog")
    return CATALOG[role].copy()


class PublicDatabase(cas.PublicRegistry):
    """Same fixed public Docker auth/CDN boundary; no private registry credential."""
    def __init__(self, role):
        self.catalog = pin(role)
        self.deadline = time.monotonic() + 240
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), cas.PublicRedirect())
        query = urllib.parse.urlencode({"service": "registry.docker.io",
                                       "scope": "repository:" + self.catalog["repository"] + ":pull"})
        with self.open("https://" + cas.AUTH_HOST + "/token?" + query, {}) as response:
            body = response.read(32769)
            if len(body) > 32768:
                raise DatabaseAcquisitionError("public pull-token response exceeds bound")
            value = oci._metadata(body)
        self.token = value.get("token")
        if (not isinstance(self.token, str) or not 1 <= len(self.token) <= 16384
                or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-"
                       for c in self.token)):
            raise DatabaseAcquisitionError("public pull token is invalid")


@contextmanager
def protected_stream(path, maximum, *, writable=False):
    cas.protected(path.parent)
    if path.exists() or path.is_symlink():
        cas.protected(path)
    fd = os.open(path, (os.O_RDWR | os.O_CREAT if writable else os.O_RDONLY) | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+b" if writable else "rb") as stream:
        info = os.fstat(fd)
        before = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o077
                or info.st_mode & 0o111 or not 0 <= info.st_size <= maximum
                or (info.st_dev, info.st_ino, info.st_size, info.st_mode, info.st_uid)
                != (before.st_dev, before.st_ino, before.st_size, before.st_mode, before.st_uid)):
            raise DatabaseAcquisitionError("database file size, inode or permission differs")
        yield stream


def fingerprint(stream, *, expected_size=None):
    stream.seek(0)
    checksum, count = hashlib.sha256(), 0
    while body := stream.read(CHUNK):
        checksum.update(body); count += len(body)
        if expected_size is not None and count > expected_size:
            raise DatabaseAcquisitionError("database file exceeds expected size")
    if expected_size is not None and count != expected_size:
        raise DatabaseAcquisitionError("database file size differs")
    return "sha256:" + checksum.hexdigest(), count


def store_bytes(root):
    """Inventory private regular files, including partials; never follow links."""
    cas.protected(root)
    files, directories, total = 0, 0, 0
    for parent, names, children in os.walk(root, followlinks=False):
        directories += 1
        if directories > 16:
            raise DatabaseAcquisitionError("database directory count exceeds bound")
        for name in (*names, *children):
            entry = Path(parent) / name
            cas.protected(entry)
            if entry.is_file():
                files += 1; total += entry.stat().st_size
            if files > 32 or total > MAX_STORE:
                raise DatabaseAcquisitionError("database staging store exceeds bound")
    return total


def acquire_archive(path, expected, registry):
    size, checksum = expected["archive_bytes"], expected["archive_digest"]
    if not 0 < size <= MAX_ARCHIVE:
        raise DatabaseAcquisitionError("database archive exceeds reviewed bound")
    if path.exists() and path.with_name(path.name + ".partial").exists():
        raise DatabaseAcquisitionError("database archive has conflicting complete and partial files")
    if path.exists() or path.is_symlink():
        with protected_stream(path, MAX_ARCHIVE) as stream:
            if fingerprint(stream, expected_size=size)[0] != checksum:
                raise DatabaseAcquisitionError("retained database archive digest differs")
        return
    temporary = path.with_name(path.name + ".partial")
    with protected_stream(temporary, size, writable=True) as stream:
        offset = os.fstat(stream.fileno()).st_size
        stream.seek(0); hashed = hashlib.sha256()
        while body := stream.read(CHUNK): hashed.update(body)
        count = offset
        try:
            if offset < size:
                with registry.blob(checksum, offset=offset) as response:
                    if (response.status != (206 if offset else 200)
                            or offset and response.headers.get("Content-Range") !=
                            f"bytes {offset}-{size-1}/{size}"):
                        raise DatabaseAcquisitionError("database partial lacks exact HTTP range proof")
                    while body := response.read(min(CHUNK, size - count + 1)):
                        count += len(body)
                        if count > size:
                            raise DatabaseAcquisitionError("database response exceeds its pin")
                        if getattr(registry, "deadline", time.monotonic() + 1) <= time.monotonic():
                            raise DatabaseAcquisitionError("database download deadline exhausted")
                        stream.write(body); hashed.update(body)
            if count != size or "sha256:" + hashed.hexdigest() != checksum:
                raise DatabaseAcquisitionError("downloaded database archive fingerprint differs")
        finally:
            stream.flush(); os.fsync(stream.fileno())
    os.rename(temporary, path); cas.sync(path.parent)


def validate_manifest(role, body):
    expected = pin(role)
    if len(body) != expected["manifest_bytes"] or oci.digest(body) != expected["manifest_digest"]:
        raise DatabaseAcquisitionError("database source manifest fingerprint differs")
    value = oci._metadata(body)
    layer = {"mediaType": expected["layer_media"], "digest": expected["archive_digest"],
             "size": expected["archive_bytes"],
             "annotations": {"org.opencontainers.image.title": expected["archive_title"]}}
    config = {"mediaType": "application/vnd.oci.empty.v1+json", "digest": EMPTY_CONFIG,
              "size": 2, "data": "e30="}
    if (set(value) != {"schemaVersion", "mediaType", "artifactType", "config", "layers", "annotations"}
            or type(value["schemaVersion"]) is not int or value["schemaVersion"] != 2
            or value["mediaType"] != oci.OCI_MANIFEST
            or value["artifactType"] != "application/vnd.aquasec.trivy.config.v1+json"
            or value["config"] != config or value["layers"] != [layer]):
        raise DatabaseAcquisitionError("database is not the pinned typed OCI artifact")
    oci._annotations(value)
    return value


def validate_metadata(body, role, now):
    if now.utcoffset() is None:
        raise DatabaseAcquisitionError("database observation needs a timezone")
    if not 0 < len(body) <= 16384:
        raise DatabaseAcquisitionError("database metadata exceeds bound")
    value = oci._metadata(body)
    if (not {"Version", "UpdatedAt", "NextUpdate"} <= set(value)
            or set(value) - {"Version", "UpdatedAt", "NextUpdate", "DownloadedAt"}
            or type(value["Version"]) is not int or value["Version"] != pin(role)["schema"]):
        raise DatabaseAcquisitionError("database schema metadata differs")
    dates = {}
    for name in ("UpdatedAt", "NextUpdate", *(["DownloadedAt"] if "DownloadedAt" in value else [])):
        item = value[name]
        try:
            if not isinstance(item, str) or not 1 <= len(item) <= 64: raise ValueError("date")
            parsed = datetime.fromisoformat(item.replace("Z", "+00:00"))
            if parsed.utcoffset() is None: raise ValueError("date timezone")
            dates[name] = parsed.astimezone(timezone.utc)
        except (ValueError, TypeError, OverflowError):
            raise DatabaseAcquisitionError("database timestamp is invalid") from None
    updated, next_update = dates["UpdatedAt"], dates["NextUpdate"]
    if (not now - timedelta(hours=24) <= updated <= now + timedelta(minutes=5)
            or not now < next_update <= updated + timedelta(hours=72)
            or dates.get("DownloadedAt", updated) > now + timedelta(minutes=5)):
        raise DatabaseAcquisitionError("database is stale, future-dated or expired")
    return value


def fixed_members(source, expected):
    """Parse headers before bodies; do not let PAX/GNU extension sizes allocate.

    Only two short regular filenames are needed. A generic tar parser may consume
    an oversized extension BEFORE returning a member to a caller's size check.
    """
    budget = expected["database_bytes"] + 65536
    total = 0
    def read(size):
        nonlocal total
        if type(size) is not int or not 0 <= size <= CHUNK:
            raise DatabaseAcquisitionError("database archive read exceeds chunk bound")
        try:
            body = source.read(size)
        except (OSError, EOFError):
            raise DatabaseAcquisitionError("database compressed stream is invalid") from None
        total += len(body)
        if total > budget: raise DatabaseAcquisitionError("database expansion exceeds its budget")
        return body
    for number in range(9):
        header = read(512)
        if len(header) != 512: raise DatabaseAcquisitionError("database tar header is incomplete")
        if not any(header):
            while body := read(CHUNK):
                if any(body): raise DatabaseAcquisitionError("database archive has nonzero trailing content")
            return
        try:
            member = tarfile.TarInfo.frombuf(header, encoding="utf-8", errors="strict")
        except (tarfile.TarError, ValueError, UnicodeError):
            raise DatabaseAcquisitionError("database tar header is invalid") from None
        if (number >= 8 or not member.isreg() or not 0 < member.size <= MAX_DATABASE
                or member.name not in {expected["database_name"], "./" + expected["database_name"],
                                       "metadata.json", "./metadata.json"}):
            raise DatabaseAcquisitionError("database tar member is not a fixed regular file")
        class Member:
            remaining = member.size
            def read(inner, maximum):
                size = min(maximum, inner.remaining)
                if size == 0: return b""
                body = read(size)
                if len(body) != size: raise DatabaseAcquisitionError("database tar member is incomplete")
                inner.remaining -= size
                return body
        stream = Member()
        yield member, stream
        if stream.remaining: raise DatabaseAcquisitionError("database member was not fully consumed")
        padding = (-member.size) % 512
        if padding and any(read(padding)):
            raise DatabaseAcquisitionError("database member padding is invalid")
    raise DatabaseAcquisitionError("database tar member count exceeds bound")


def unpack_database(directory, role, *, now):
    """Two fixed regular files only. Interrupted output prefixes are compared."""
    expected = pin(role)
    if not 0 < expected["database_bytes"] <= MAX_DATABASE:
        raise DatabaseAcquisitionError("database expanded size exceeds bound")
    archive = directory / "archive.tar.gz"
    pending, metadata = [], None
    database_hash = None
    seen = set()
    with protected_stream(archive, MAX_ARCHIVE) as source:
        if fingerprint(source, expected_size=expected["archive_bytes"])[0] != expected["archive_digest"]:
            raise DatabaseAcquisitionError("database archive differs before parsing")
        source.seek(0)
        with gzip.GzipFile(fileobj=source, mode="rb") as tar:
            for number, (member, input_stream) in enumerate(fixed_members(tar, expected)):
                name = member.name[2:] if member.name.startswith("./") else member.name
                if (number >= 8 or name not in {expected["database_name"], "metadata.json"}
                        or name in seen or not member.isreg() or member.pax_headers
                        or not 0 < member.size <= MAX_DATABASE):
                    raise DatabaseAcquisitionError("database archive member is unsafe or unexpected")
                seen.add(name)
                if name == "metadata.json":
                    if member.size > 16384: raise DatabaseAcquisitionError("database metadata exceeds bound")
                    metadata = input_stream.read(16385)
                    if len(metadata) != member.size: raise DatabaseAcquisitionError("metadata size differs")
                    validate_metadata(metadata, role, now)
                    continue
                if member.size != expected["database_bytes"]:
                    raise DatabaseAcquisitionError("expanded database size differs from the reviewed pin")
                target = directory / name
                complete = target.exists() or target.is_symlink()
                if complete and target.with_name(target.name + ".partial").exists():
                    raise DatabaseAcquisitionError("database has conflicting complete and partial files")
                output = target if complete else target.with_name(target.name + ".partial")
                with protected_stream(output, member.size, writable=not complete) as destination:
                    offset = os.fstat(destination.fileno()).st_size
                    if complete and offset != member.size: raise DatabaseAcquisitionError("retained database size differs")
                    count, hashed = 0, hashlib.sha256()
                    try:
                        while body := input_stream.read(CHUNK):
                            if count + len(body) > member.size: raise DatabaseAcquisitionError("expanded database exceeds pin")
                            prefix = min(len(body), max(0, offset - count))
                            if prefix and destination.read(prefix) != body[:prefix]:
                                raise DatabaseAcquisitionError("retained database prefix differs")
                            if prefix < len(body): destination.write(body[prefix:])
                            count += len(body); hashed.update(body)
                        if count != member.size: raise DatabaseAcquisitionError("expanded database is incomplete")
                    finally:
                        if not complete: destination.flush(); os.fsync(destination.fileno())
                    database_hash = "sha256:" + hashed.hexdigest()
                if not complete: pending.append((output, target))
        if seen != {expected["database_name"], "metadata.json"} or metadata is None:
            raise DatabaseAcquisitionError("database archive lacks required members")
        if fingerprint(source, expected_size=expected["archive_bytes"])[0] != expected["archive_digest"]:
            raise DatabaseAcquisitionError("database archive changed during parsing")
    for partial, target in pending: os.rename(partial, target); cas.sync(directory)
    cas.write_exact(directory / "metadata.json", metadata)
    return {"database_digest": database_hash, "metadata_digest": oci.digest(metadata),
            "metadata": oci._metadata(metadata)}


def observe_database(root, role, *, now):
    expected = pin(role)
    directory = root / role
    cas.protected(directory)
    manifest = cas.read_file(directory / "manifest.json", expected["manifest_bytes"])
    validate_manifest(role, manifest)
    with protected_stream(directory / "archive.tar.gz", MAX_ARCHIVE) as stream:
        if fingerprint(stream, expected_size=expected["archive_bytes"])[0] != expected["archive_digest"]:
            raise DatabaseAcquisitionError("database archive readback differs")
    def metadata_file(name):
        path = directory / name; cas.protected(path)
        size = path.stat().st_size
        if not 0 < size <= 16384: raise DatabaseAcquisitionError("retained metadata exceeds bound")
        return cas.read_file(path, size)
    metadata = metadata_file("metadata.json")
    validate_metadata(metadata, role, now)
    with protected_stream(directory / expected["database_name"], MAX_DATABASE) as stream:
        database_digest, _ = fingerprint(stream, expected_size=expected["database_bytes"])
    receipt = oci._metadata(metadata_file("receipt.json"))
    intended = {"version": 1, "role": role, "catalog": expected, "database_digest": database_digest,
                "metadata_digest": oci.digest(metadata), "metadata": oci._metadata(metadata),
                "publisher_signature_verified": False, "database_opened_by_scanner": False,
                "scan_completed": False, "runtime_approved": False}
    if receipt != intended: raise DatabaseAcquisitionError("database receipt readback differs")
    return {**receipt, "freshness_verified_at": now.isoformat()}


def acquire_database(root, role, registry, *, now, store_root=None):
    """Fixed caller limits/ownership; no new host capacity or executable authority."""
    expected = pin(role)
    total = store_bytes(store_root or root)
    directory = root / role; directory.mkdir(mode=0o700, exist_ok=True); cas.protected(directory)
    already = sum((directory / name).stat().st_size for name in
                  ("manifest.json", "archive.tar.gz", "archive.tar.gz.partial",
                   expected["database_name"], expected["database_name"] + ".partial",
                   "metadata.json", "receipt.json") if (directory / name).exists())
    required = expected["manifest_bytes"] + expected["archive_bytes"] + expected["database_bytes"] + 32768
    if total + max(0, required - already) > MAX_STORE:
        raise DatabaseAcquisitionError("database acquisition exceeds shared staging capacity")
    body = registry.manifest(expected["manifest_digest"])
    validate_manifest(role, body); cas.write_exact(directory / "manifest.json", body)
    acquire_archive(directory / "archive.tar.gz", expected, registry)
    facts = unpack_database(directory, role, now=now)
    receipt = {"version": 1, "role": role, "catalog": expected, **facts,
               "publisher_signature_verified": False, "database_opened_by_scanner": False,
               "scan_completed": False, "runtime_approved": False}
    cas.write_exact(directory / "receipt.json", oci.canonical(receipt)); store_bytes(store_root or root)
    return observe_database(root, role, now=now)
