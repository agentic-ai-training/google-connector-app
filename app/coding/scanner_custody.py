"""Bounded custody of a previously signature-verified scanner, never execution.

The operator-reviewed receipt pin binds the earlier real Sigstore check. A caller
cannot grant signature authority by supplying a JSON boolean. Host files remain
private non-executable data; an execution profile and real scan are separate.
"""
import hashlib
import os
from pathlib import Path
import stat

from app.coding import runtime_base_acquisition as cas, oci_packager as oci


BINARY_BYTES = 172_765_344
BINARY_SHA256 = "93f9da8e4ba5e0c1c76d8234ed2494cf9afb0a96fd21953e424bb795f3299b8e"
SIGNATURE_RECEIPT_SHA256 = "712dc1f6a1d34e65d2c54390ba25da4942220e6bdbe9bf5ea8786b9927cfa112"
CHUNK = 1024 * 1024
MAX_RECEIPT = 16384


class ScannerCustodyError(RuntimeError):
    pass


def open_data(path):
    """Reject links/public/executable data before obtaining a protected descriptor."""
    path = cas.protected(Path(path))
    before = path.lstat()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o177
                or not 0 <= info.st_size <= BINARY_BYTES
                or (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink)
                != (before.st_dev, before.st_ino, before.st_mode, before.st_uid, before.st_nlink)):
            raise ScannerCustodyError("scanner custody inode differs")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def fingerprint(stream, length):
    """Measure exactly a bounded prefix; preserve stream position for transfer."""
    if type(length) is not int or not 0 <= length <= BINARY_BYTES:
        raise ScannerCustodyError("scanner custody length differs")
    position = stream.tell()
    digest = hashlib.sha256()
    try:
        stream.seek(0)
        remaining = length
        while remaining:
            body = stream.read(min(CHUNK, remaining))
            if not body:
                raise ScannerCustodyError("scanner custody prefix is truncated")
            digest.update(body)
            remaining -= len(body)
        return digest.hexdigest()
    finally:
        stream.seek(position)


def verified_local_source(root):
    """Rehash actual signature-bound bytes, not an unchecked saved success flag."""
    root = cas.protected(Path(root))
    receipt_body = read_metadata(root / "verified-assets.json")
    if hashlib.sha256(receipt_body).hexdigest() != SIGNATURE_RECEIPT_SHA256:
        raise ScannerCustodyError("reviewed scanner signature receipt differs")
    receipt = oci._metadata(receipt_body)
    if (receipt.get("signature_verified") is not True
            or receipt.get("binary_sha256") != BINARY_SHA256
            or receipt.get("binary_bytes") != BINARY_BYTES
            or any(receipt.get(key) is not False for key in
                   ("scanner_executed", "scan_completed", "runtime_approved",
                    "binary_executable_on_host"))):
        raise ScannerCustodyError("scanner signature receipt binding differs")
    stream = open_data(root / "trivy.bin")
    if (os.fstat(stream.fileno()).st_size != BINARY_BYTES
            or fingerprint(stream, BINARY_BYTES) != BINARY_SHA256):
        stream.close()
        raise ScannerCustodyError("signature-bound scanner binary differs")
    return stream


def read_metadata(path):
    size = cas.protected(path).stat().st_size
    if not 1 <= size <= MAX_RECEIPT:
        raise ScannerCustodyError("scanner custody metadata exceeds bound")
    return cas.read_file(path, size)


def receipt():
    return {"version": 1, "binary_sha256": BINARY_SHA256, "binary_bytes": BINARY_BYTES,
            "reviewed_signature_receipt_sha256": SIGNATURE_RECEIPT_SHA256,
            "transfer_complete": True, "host_signature_verification_performed": False,
            "binary_executable_on_host": False, "scanner_executed": False,
            "scan_completed": False, "runtime_approved": False}


def observe(root):
    root = cas.protected(Path(root))
    complete, partial = root / "trivy.bin", root / "trivy.bin.partial"
    if complete.is_symlink() or partial.is_symlink():
        raise ScannerCustodyError("scanner custody links are forbidden")
    if complete.exists() and partial.exists():
        raise ScannerCustodyError("scanner custody complete/partial conflict")
    if not complete.exists() and not partial.exists():
        return {"complete": False, "offset": 0, "prefix_sha256": hashlib.sha256(b"").hexdigest()}
    path = complete if complete.exists() else partial
    with open_data(path) as stream:
        length = os.fstat(stream.fileno()).st_size
        checksum = fingerprint(stream, length)
    if complete.exists():
        if length != BINARY_BYTES or checksum != BINARY_SHA256:
            raise ScannerCustodyError("retained scanner binary differs")
        marker = root / "custody.json"
        if marker.exists() and read_metadata(marker) != oci.canonical(receipt()):
            raise ScannerCustodyError("scanner custody receipt differs")
    return {"complete": complete.exists(), "offset": length, "prefix_sha256": checksum}


def receive(root, source, *, offset, prefix_sha256):
    """Explicit prefix-bound continuation; truncated/corrupt bytes never promote."""
    root = cas.protected(Path(root))
    current = observe(root)
    if type(offset) is not int or current["offset"] != offset or current["prefix_sha256"] != prefix_sha256:
        raise ScannerCustodyError("scanner custody continuation differs")
    complete, partial = root / "trivy.bin", root / "trivy.bin.partial"
    if current["complete"]:
        if source.read(1):
            raise ScannerCustodyError("completed scanner transfer has extra bytes")
    else:
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_APPEND, 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != offset
                    or info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o177):
                raise ScannerCustodyError("scanner partial inode differs")
            with os.fdopen(fd, "ab", closefd=False) as stream:
                remaining = BINARY_BYTES - offset
                while remaining:
                    body = source.read(min(CHUNK, remaining))
                    if not body:
                        raise ScannerCustodyError("scanner transfer interrupted; inspect partial before retry")
                    stream.write(body)
                    remaining -= len(body)
                stream.flush()
                os.fsync(fd)
                if source.read(1):
                    raise ScannerCustodyError("scanner transfer exceeds exact binary size")
        finally:
            os.close(fd)
        with open_data(partial) as stream:
            if fingerprint(stream, BINARY_BYTES) != BINARY_SHA256:
                raise ScannerCustodyError("transferred scanner fingerprint differs")
        os.rename(partial, complete)
        cas.sync(root)
    cas.write_exact(root / "custody.json", oci.canonical(receipt()))
    return {**observe(root), "receipt": receipt()}
