"""Protected, crash-recoverable archive custody. No Docker, approval or network.

Only a trusted operator controller may call this under Journal's existing host
flock. Generated code never supplies this directory, intent or input stream.
Deadlines are cooperative; a network reader also needs its caller's watchdog.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import time

from app.coding.isolated_validation import Journal
from app.coding.toolchain_archive import inspect_archive, MAX_ARCHIVE_BYTES, MAX_METADATA_BYTES
from app.coding.toolchain_spec import unique_object


MAX_QUARANTINES = 3
MAX_CONTROL_BYTES = 8192
MIN_FREE_BYTES = 64 * 1024 * 1024


class ToolchainStagingError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def checksum(body):
    return hashlib.sha256(body).hexdigest()


def protected(path, *, private=False):
    for current in (path, *path.parents):
        info = current.lstat()
        if (stat.S_ISLNK(info.st_mode) or info.st_uid not in {0, os.geteuid()}
                or info.st_mode & 0o022 or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
                or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)):
            raise ToolchainStagingError("staging path is not operator protected")
    if private and path.stat().st_mode & 0o077:
        raise ToolchainStagingError("staging custody is not private")
    return path


def sync(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def file_stream(path):
    protected(path, private=True)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077
            or info.st_uid not in {0, os.geteuid()} or info.st_size > MAX_ARCHIVE_BYTES):
        os.close(fd)
        raise ToolchainStagingError("staging file inode or bound differs")
    return os.fdopen(fd, "rb")


def file_hash(path):
    with file_stream(path) as stream:
        digest = hashlib.sha256()
        size = 0
        while body := stream.read(MAX_METADATA_BYTES):
            size += len(body)
            if size > MAX_ARCHIVE_BYTES:
                raise ToolchainStagingError("staging file grew beyond bound")
            digest.update(body)
    return size, digest.hexdigest()


def document(path):
    with file_stream(path) as stream:
        body = stream.read(MAX_CONTROL_BYTES + 1)
    if len(body) > MAX_CONTROL_BYTES:
        raise ToolchainStagingError("staging receipt exceeds bound")
    try:
        value = json.loads(body, object_pairs_hook=unique_object)
    except (ValueError, UnicodeError, RecursionError):
        raise ToolchainStagingError("staging receipt is invalid") from None
    if canonical(value) != body:
        raise ToolchainStagingError("staging receipt is not canonical")
    return value


def write_document(path, value):
    body = canonical(value)
    if len(body) > MAX_CONTROL_BYTES:
        raise ToolchainStagingError("staging receipt exceeds bound")
    pending = path.with_suffix(".pending")
    if pending.exists() or pending.is_symlink():
        # Resume only a byte-exact prefix of this independently reconstructed
        # receipt. Never truncate/replace foreign bytes or infer a committed write.
        with file_stream(pending) as stream:
            prefix = stream.read(MAX_CONTROL_BYTES + 1)
        if not body.startswith(prefix):
            raise ToolchainStagingError("interrupted staging receipt needs review")
        fd = os.open(pending, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
        with os.fdopen(fd, "wb") as stream:
            stream.write(body[len(prefix):])
            stream.flush()
            os.fsync(stream.fileno())
    else:
        fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
    if path.exists() or path.is_symlink():
        protected(path, private=True)
    os.replace(pending, path)
    sync(path.parent)


def context(journal, identity, *, readback=False):
    if (not isinstance(journal, Journal) or journal.db is None or journal.lock is None
            or not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{64}", identity)):
        raise ToolchainStagingError("active trusted host journal required")
    item = next((row for row in journal.toolchain_imports() if row["id"] == identity), None)
    phases = {"prepared", "archive_verified"}
    if readback:
        phases |= {'cancelled', 'load_started', 'load_uncertain', 'adopting', 'loaded', 'proof_pending',
                   'publishing', 'enrolled', 'rejected'}
    if item is None or item["phase"] not in phases:
        raise ToolchainStagingError("import phase does not permit staging")
    protected(journal.directory, private=True)
    root = journal.directory / "toolchain-imports"
    if not readback:
        root.mkdir(mode=0o700, exist_ok=True)
    protected(root, private=True)
    sync(root.parent)
    entries = list(root.iterdir())
    known = {row["id"] for row in journal.toolchain_imports()}
    if len(entries) > 8 or any(path.name not in known or not path.is_dir() for path in entries):
        raise ToolchainStagingError("untracked import directory requires review")
    directory = root / identity
    if not readback:
        directory.mkdir(mode=0o700, exist_ok=True)
    protected(directory, private=True)
    sync(root)
    allowed = {"archive.tar", "archive.partial", "custody.json", "custody.pending",
               "quarantine.json", "quarantine.pending"}
    entries = list(directory.iterdir())
    if len(entries) > 9:
        raise ToolchainStagingError("staging inventory exceeds bound")
    total = 0
    for path in entries:
        protected(path, private=True)
        if (not path.is_file() or (path.name not in allowed
                                  and not re.fullmatch(r"quarantine-[a-f0-9]{64}\.tar", path.name))):
            raise ToolchainStagingError("untracked staging object requires review")
        total += path.stat().st_size
    if total > MAX_ARCHIVE_BYTES + 4 * MAX_CONTROL_BYTES:
        raise ToolchainStagingError("staging inventory bytes exceed bound")
    return item, directory


def quarantine_audit(directory):
    path = directory / "quarantine.json"
    value = document(path) if path.exists() else {"version": 1, "entries": []}
    if (not isinstance(value, dict) or set(value) != {"version", "entries"}
            or type(value["version"]) is not int or value["version"] != 1
            or not isinstance(value["entries"], list) or len(value["entries"]) > MAX_QUARANTINES):
        raise ToolchainStagingError("quarantine audit differs")
    seen = set()
    for index, item in enumerate(value["entries"]):
        if (not isinstance(item, dict) or set(item) != {"sha256", "bytes", "phase"}
                or not isinstance(item["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"])
                or item["sha256"] in seen or type(item["bytes"]) is not int
                or not 0 <= item["bytes"] <= MAX_ARCHIVE_BYTES
                or item["phase"] not in ("prepared", "committed")
                or (item["phase"] == "prepared" and index != len(value["entries"]) - 1)):
            raise ToolchainStagingError("quarantine audit entry differs")
        seen.add(item["sha256"])
        if item["phase"] == "committed":
            if file_hash(directory / ("quarantine-" + item["sha256"] + ".tar")) != (item["bytes"], item["sha256"]):
                raise ToolchainStagingError("retained quarantine custody differs")
    files = {path.name for path in directory.glob("quarantine-*.tar")}
    allowed = {"quarantine-" + item["sha256"] + ".tar" for item in value["entries"]}
    if files - allowed:
        raise ToolchainStagingError("untracked quarantine requires review")
    return value


def quarantine_partial(journal, identity, *, expected_sha256, expected_bytes):
    """Explicit hash-bound restart. Preserve rejected bytes; never prune/delete."""
    if (not isinstance(expected_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_sha256)
            or type(expected_bytes) is not int or not 0 <= expected_bytes <= MAX_ARCHIVE_BYTES):
        raise ToolchainStagingError("exact rejected-transfer custody required")
    item, directory = context(journal, identity)
    if item["phase"] != "prepared" or (directory / "archive.tar").exists():
        raise ToolchainStagingError("verified archive cannot be quarantined")
    audit = quarantine_audit(directory)
    entry = {"sha256": expected_sha256, "bytes": expected_bytes, "phase": "prepared"}
    partial = directory / "archive.partial"
    retained = directory / ("quarantine-" + expected_sha256 + ".tar")
    existing = next((row for row in audit["entries"] if row["sha256"] == expected_sha256), None)
    if existing:
        if existing["bytes"] != expected_bytes:
            raise ToolchainStagingError("quarantine custody changed")
        if existing["phase"] == "committed":
            if partial.exists():
                raise ToolchainStagingError("new partial requires fresh operator review")
            if (directory / "quarantine.pending").exists():
                write_document(directory / "quarantine.json", audit)
            return {"quarantined": True, "image_loaded": False, "image_approved": False}
        entry = existing
    else:
        if (len(audit["entries"]) >= MAX_QUARANTINES
                or any(row["phase"] == "prepared" for row in audit["entries"])
                or file_hash(partial) != (expected_bytes, expected_sha256)):
            raise ToolchainStagingError("quarantine bound or partial fingerprint differs")
        audit["entries"].append(entry)
        write_document(directory / "quarantine.json", audit)
    if partial.exists() and not retained.exists():
        if file_hash(partial) != (expected_bytes, expected_sha256):
            raise ToolchainStagingError("partial custody changed during recovery")
        os.rename(partial, retained)
        sync(directory)
    elif partial.exists() or not retained.exists():
        raise ToolchainStagingError("ambiguous quarantine rename requires review")
    if file_hash(retained) != (expected_bytes, expected_sha256):
        raise ToolchainStagingError("retained quarantine custody differs")
    entry["phase"] = "committed"
    write_document(directory / "quarantine.json", audit)
    return {"quarantined": True, "image_loaded": False, "image_approved": False}


class Tee:
    def __init__(self, source, destination, maximum, deadline):
        self.source, self.destination = source, destination
        self.maximum, self.deadline, self.count = maximum, deadline, 0

    def read(self, size):
        if type(size) is not int or not 0 <= size <= MAX_METADATA_BYTES or time.monotonic() >= self.deadline:
            raise ToolchainStagingError("staging read bound or deadline exceeded")
        body = self.source.read(size)
        if (not isinstance(body, bytes) or len(body) > size or self.count + len(body) > self.maximum
                or time.monotonic() >= self.deadline):
            raise ToolchainStagingError("staging input differs from byte contract")
        self.destination.write(body)
        self.count += len(body)
        return body


def verify_archive(path, intent, timeout_seconds):
    with file_stream(path) as stream:
        evidence = inspect_archive(stream, role=intent["role"], image_id=intent["image_id"],
                                   timeout_seconds=timeout_seconds)
        if (evidence["archive_sha256"] != intent["archive_sha256"]
                or evidence["archive_bytes"] != intent["archive_bytes"]):
            raise ToolchainStagingError("complete archive differs from reserved custody")
        # A complete file recovered after writer death might only be in the kernel
        # cache. Flush its data before publishing any durable custody observation.
        os.fsync(stream.fileno())
    return evidence


def verify_retained_custody(journal, identity, *, timeout_seconds=120):
    """Read existing bytes/audit at any reserved phase; never reopen or load.

    A cancelled record or receipt filename alone is not reusable custody. Require
    the complete archive, exact original receipt, settled quarantine and a matching
    earlier journal observation. No missing directory is created by this reader.
    """
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 300:
        raise ToolchainStagingError('bounded custody readback required')
    item, directory = context(journal, identity, readback=True)
    if any((directory / name).exists() for name in
           ('archive.partial', 'custody.pending', 'quarantine.pending')):
        raise ToolchainStagingError('retained custody has unsettled state')
    audit = quarantine_audit(directory)
    if any(entry['phase'] != 'committed' for entry in audit['entries']):
        raise ToolchainStagingError('retained quarantine is unsettled')
    receipt = {'version': 1, 'intent_id': identity,
               'archive': verify_archive(directory / 'archive.tar', item['intent'], timeout_seconds),
               'quarantine_audit_sha256': checksum(canonical(audit))}
    if (document(directory / 'custody.json') != receipt or
            not any(entry['phase'] == 'archive_verified' and
                    entry['evidence_sha256'] == checksum(canonical(receipt)) for entry in item['observations'])):
        raise ToolchainStagingError('retained custody differs from verified journal observation')
    return receipt


def stage_archive(journal, identity, source=None, *, timeout_seconds=120):
    """Stage or reconcile one exact archive. A resumed transfer never runs Docker.

    Complete partial/final files are re-read and verified, not trusted from names.
    Incomplete bytes require explicit hash-bound quarantine before another stream.
    A caller that started an exporter must stop it if recovery consumes no input.
    """
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 300:
        raise ToolchainStagingError("bounded staging timeout required")
    item, directory = context(journal, identity)
    intent = item["intent"]
    audit = quarantine_audit(directory)
    if any(row["phase"] == "prepared" for row in audit["entries"]):
        raise ToolchainStagingError("quarantine rename requires explicit reconciliation")
    if (directory / "quarantine.pending").exists():
        raise ToolchainStagingError("quarantine control write requires reconciliation")
    archive, partial = directory / "archive.tar", directory / "archive.partial"
    if archive.exists():
        if partial.exists():
            raise ToolchainStagingError("ambiguous archive promotion requires review")
        evidence = verify_archive(archive, intent, timeout_seconds)
    else:
        if item["phase"] != "prepared":
            raise ToolchainStagingError("journaled verified archive is missing")
        if not partial.exists():
            if source is None or intent["stop_unix"] <= int(time.time()):
                raise ToolchainStagingError("fresh stream requires unexpired operator intent")
            retained = sum(row["bytes"] for row in audit["entries"])
            if (retained + intent["archive_bytes"] > MAX_ARCHIVE_BYTES
                    or shutil.disk_usage(directory).free < intent["archive_bytes"] + MIN_FREE_BYTES):
                raise ToolchainStagingError("insufficient bounded staging headroom")
            fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as output:
                tee = Tee(source, output, intent["archive_bytes"], time.monotonic() + timeout_seconds)
                evidence = inspect_archive(tee, role=intent["role"], image_id=intent["image_id"],
                                           timeout_seconds=timeout_seconds)
                if (evidence["archive_sha256"] != intent["archive_sha256"]
                        or evidence["archive_bytes"] != intent["archive_bytes"]):
                    raise ToolchainStagingError("stream differs from reserved custody")
                output.flush()
                os.fsync(output.fileno())
            sync(directory)
        # Read back the actual persisted file, including after controller death.
        evidence = verify_archive(partial, intent, timeout_seconds)
        os.rename(partial, archive)
        sync(directory)
    receipt = {"version": 1, "intent_id": identity, "archive": evidence,
               "quarantine_audit_sha256": checksum(canonical(audit))}
    receipt_path = directory / "custody.json"
    if receipt_path.exists() and document(receipt_path) != receipt:
        raise ToolchainStagingError("existing archive receipt changed")
    write_document(receipt_path, receipt)
    if item["phase"] == "archive_verified":
        if item["observations"][-1]["evidence_sha256"] != checksum(canonical(receipt)):
            raise ToolchainStagingError("archive receipt differs from journal observation")
    else:
        journal.record_toolchain_import(identity, "prepared", "archive_verified", checksum(canonical(receipt)))
    return receipt


def reuse_custody(journal, identity, source_identity, *, timeout_seconds=120):
    """Copy exact protected retained bytes to a fresh source-bound intent.

    No caller path, hard link, image loading or deletion is supported. The source
    must be terminal and independently reverified; all normal destination quota,
    expiry, partial-file and readback checks still apply.
    """
    if any(not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value)
           for value in (identity, source_identity)):
        raise ToolchainStagingError('reuse requires exact retained intent IDs')
    rows = {row['id']: row for row in journal.toolchain_imports()}
    destination, source = rows.get(identity), rows.get(source_identity)
    fields = ('role', 'host_instance_id', 'image_id', 'archive_sha256', 'archive_bytes', 'recipe_sha256', 'stop_unix')
    if (identity == source_identity or destination is None or source is None
            or destination['phase'] not in {'prepared', 'archive_verified'}
            or source['phase'] not in {'cancelled', 'rejected', 'enrolled'}
            or any(destination['intent'][key] != source['intent'][key] for key in fields)):
        raise ToolchainStagingError('reuse requires matching terminal source and fresh destination')
    journal.verify_rejections()
    journal.verify_publications()
    verify_retained_custody(journal, source_identity, timeout_seconds=timeout_seconds)
    _, directory = context(journal, source_identity, readback=True)
    with file_stream(directory / 'archive.tar') as stream:
        return stage_archive(journal, identity, stream, timeout_seconds=timeout_seconds)
