import hashlib
import io
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace

import pytest

from app.coding import scanner_custody as custody, oci_packager as oci
from app.coding import runtime_base_acquisition as cas
from scripts import transfer_runtime_scanner as cli


@pytest.fixture
def target(monkeypatch):
    body = b"exact-public-scanner-test-data" * 13
    monkeypatch.setattr(custody, "BINARY_BYTES", len(body))
    monkeypatch.setattr(custody, "BINARY_SHA256", hashlib.sha256(body).hexdigest())
    with tempfile.TemporaryDirectory(prefix=".gca-scanner-custody-", dir=Path.home()) as value:
        root = Path(value)
        root.chmod(0o700)
        yield SimpleNamespace(root=root, body=body)


def test_complete_transfer_is_private_non_executable_and_not_a_scan(target):
    initial = custody.observe(target.root)
    result = custody.receive(target.root, io.BytesIO(target.body), offset=0,
                             prefix_sha256=initial["prefix_sha256"])
    assert result["complete"] and result["prefix_sha256"] == custody.BINARY_SHA256
    assert (target.root / "trivy.bin").read_bytes() == target.body
    assert (target.root / "trivy.bin").stat().st_mode & 0o177 == 0
    assert result["receipt"] == custody.receipt()
    for key in ("scanner_executed", "scan_completed", "runtime_approved",
                "binary_executable_on_host", "host_signature_verification_performed"):
        assert result["receipt"][key] is False
    assert custody.receive(target.root, io.BytesIO(b""), offset=len(target.body),
                           prefix_sha256=custody.BINARY_SHA256) == result


def test_interrupted_transfer_requires_matching_prefix(target):
    initial = custody.observe(target.root)
    with pytest.raises(custody.ScannerCustodyError, match="interrupted"):
        custody.receive(target.root, io.BytesIO(target.body[:7]), offset=0,
                        prefix_sha256=initial["prefix_sha256"])
    current = custody.observe(target.root)
    assert current["offset"] == 7 and not current["complete"]
    with pytest.raises(custody.ScannerCustodyError, match="continuation"):
        custody.receive(target.root, io.BytesIO(target.body[7:]), offset=7, prefix_sha256="0" * 64)
    result = custody.receive(target.root, io.BytesIO(target.body[7:]), offset=7,
                             prefix_sha256=current["prefix_sha256"])
    assert result["complete"] and not (target.root / "trivy.bin.partial").exists()


@pytest.mark.parametrize("fault", ["wrong_hash", "extra_bytes"])
def test_inexact_transfer_never_promotes(target, fault):
    body = b"x" * len(target.body) if fault == "wrong_hash" else target.body + b"extra"
    with pytest.raises(custody.ScannerCustodyError):
        custody.receive(target.root, io.BytesIO(body), offset=0,
                        prefix_sha256=hashlib.sha256(b"").hexdigest())
    assert not (target.root / "trivy.bin").exists()


def test_completed_partial_reconciles_without_retransmitting(target):
    path = target.root / "trivy.bin.partial"
    path.write_bytes(target.body)
    path.chmod(0o600)
    result = custody.receive(target.root, io.BytesIO(b""), offset=len(target.body),
                             prefix_sha256=custody.BINARY_SHA256)
    assert result["complete"] and not path.exists()


@pytest.mark.parametrize("fault", ["symlink", "broken_link", "hardlink", "public", "executable", "oversized"])
def test_unsafe_custody_inode_is_rejected(target, fault):
    path = target.root / "trivy.bin.partial"
    path.write_bytes(target.body[:7])
    path.chmod(0o600)
    if fault == "symlink":
        path.rename(target.root / "other")
        path.symlink_to(target.root / "other")
    elif fault == "broken_link":
        path.unlink()
        path.symlink_to(target.root / "missing")
    elif fault == "hardlink":
        os.link(path, target.root / "other")
    elif fault == "public":
        path.chmod(0o644)
    elif fault == "executable":
        path.chmod(0o700)
    else:
        path.write_bytes(target.body + b"extra")
    with pytest.raises((custody.ScannerCustodyError, cas.RuntimeAcquisitionError)):
        custody.observe(target.root)


def test_complete_and_partial_conflict_is_not_silently_removed(target):
    for name in ("trivy.bin", "trivy.bin.partial"):
        path = target.root / name
        path.write_bytes(target.body)
        path.chmod(0o600)
    with pytest.raises(custody.ScannerCustodyError, match="conflict"):
        custody.observe(target.root)
    assert (target.root / "trivy.bin.partial").exists()


def test_receipt_boolean_alone_does_not_grant_signature_authority(target, monkeypatch):
    receipt = {"signature_verified": True, "binary_sha256": custody.BINARY_SHA256,
               "binary_bytes": len(target.body), "scanner_executed": False, "scan_completed": False,
               "runtime_approved": False, "binary_executable_on_host": False}
    body = oci.canonical(receipt)
    path = target.root / "verified-assets.json"
    path.write_bytes(body)
    path.chmod(0o600)
    binary = target.root / "trivy.bin"
    binary.write_bytes(target.body)
    binary.chmod(0o600)
    with pytest.raises(custody.ScannerCustodyError, match="reviewed"):
        custody.verified_local_source(target.root)
    monkeypatch.setattr(custody, "SIGNATURE_RECEIPT_SHA256", hashlib.sha256(body).hexdigest())
    with custody.verified_local_source(target.root) as stream:
        assert stream.read() == target.body
    binary.write_bytes(b"x" * len(target.body))
    with pytest.raises(custody.ScannerCustodyError, match="binary differs"):
        custody.verified_local_source(target.root)


def test_fingerprint_is_bounded_and_preserves_offset(target):
    stream = io.BytesIO(target.body)
    stream.seek(7)
    assert custody.fingerprint(stream, 7) == hashlib.sha256(target.body[:7]).hexdigest()
    assert stream.tell() == 7
    for length in (-1, True, len(target.body) + 1):
        with pytest.raises(custody.ScannerCustodyError):
            custody.fingerprint(stream, length)


def test_transfer_result_cannot_claim_scan_or_approval(target):
    request = {"operation": "transfer", "authority": {"bundle_sha256": "a" * 64}}
    result = {"operation": "transfer", "source_snapshot": "a" * 64,
              "scanner_executed": False, "scan_completed": False, "runtime_approved": False,
              "custody": {"complete": True, "offset": len(target.body),
                          "prefix_sha256": custody.BINARY_SHA256, "receipt": custody.receipt()}}
    assert cli.verify(request, result) == result
    for key in ("scanner_executed", "scan_completed", "runtime_approved"):
        with pytest.raises(RuntimeError, match="unconfirmed"):
            cli.verify(request, {**result, key: True})
    with pytest.raises(RuntimeError, match="proof differs"):
        cli.verify(request, {**result, "source_snapshot": "b" * 64})


def test_operator_source_has_fixed_target_guard_and_compiles():
    source = cli.program()
    compile(source, "reviewed-custody", "exec")
    assert len(source.encode()) < 100000
    assert "journal.require_available()" in source
    assert "host.require_host_limits()" in source
    assert "g['target_identity']()" in source
    assert "g['STOP_UNIX']" in source
    assert "/var/lib/gca-runtime-scanner-v1" in source


def test_unreviewed_program_cannot_transfer():
    with pytest.raises(RuntimeError, match="not source reviewed"):
        cli.frozen_program("0" * 64)
