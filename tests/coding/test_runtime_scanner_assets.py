from contextlib import contextmanager
import io
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
from types import SimpleNamespace
import urllib.request

import pytest

from app.coding import runtime_scanner_assets as scanner, oci_packager as oci
from app.coding import runtime_base_acquisition as cas


def archive(binary, *, member_name="trivy", member_type=tarfile.REGTYPE, duplicate=False):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as target:
        for _ in range(2 if duplicate else 1):
            item = tarfile.TarInfo(member_name)
            item.type = member_type
            item.linkname = "elsewhere"
            item.size = len(binary) if member_type == tarfile.REGTYPE else 0
            target.addfile(item, io.BytesIO(binary) if item.size else None)
    return stream.getvalue()


@pytest.fixture
def data(monkeypatch):
    binary = b"\x7fELF\x02\x01\x01" + b"\0" * 11 + b"\x3e\x00" + b"scanner-test-data"
    monkeypatch.setattr(scanner, "BINARY_BYTES", len(binary))
    monkeypatch.setattr(scanner, "BINARY_SHA256", hashlib.sha256(binary).hexdigest())
    contents = {list(scanner.ASSETS)[0]: archive(binary), list(scanner.ASSETS)[1]: b'{"test":true}'}
    pins = {name: {"size": len(body), "digest": oci.digest(body)} for name, body in contents.items()}
    monkeypatch.setattr(scanner, "ASSETS", pins)
    class Client:
        calls = 0
        @contextmanager
        def blob(self, checksum, *, offset=0):
            self.calls += 1
            body = next(value for value in contents.values() if oci.digest(value) == checksum)
            response = io.BytesIO(body[offset:])
            response.status = 206 if offset else 200
            response.headers = {"Content-Range": f"bytes {offset}-{len(body)-1}/{len(body)}"}
            yield response
    with tempfile.TemporaryDirectory(prefix=".gca-scanner-test-", dir=Path.home()) as directory:
        root = Path(directory); root.chmod(0o700)
        yield SimpleNamespace(root=root, contents=contents, binary=binary, client=Client())


def test_acquisition_is_immutable_data_not_execution_or_approval(data):
    result = scanner.acquire_assets(data.root, data.client)
    assert result["signature_verified"] is False
    assert not result["scanner_executed"] and not result["scan_completed"]
    assert not result["runtime_approved"]
    assert data.client.calls == 2
    assert scanner.acquire_assets(data.root, data.client) == result
    assert data.client.calls == 2
    assert not (data.root / "trivy.bin").exists()


def test_existing_tampered_asset_is_never_overwritten(data):
    scanner.acquire_assets(data.root, data.client)
    name = list(scanner.ASSETS)[1]
    (data.root / name).write_bytes(b"x" * scanner.ASSETS[name]["size"])
    with pytest.raises(cas.RuntimeAcquisitionError):
        scanner.acquire_assets(data.root, data.client)
    assert (data.root / name).read_bytes() == b"x" * scanner.ASSETS[name]["size"]


def test_observer_rehashes_retained_files_without_signature_claim(data):
    scanner.acquire_assets(data.root, data.client)
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in data.root.iterdir()}
    assert scanner.observe_assets(data.root)["signature_verified"] is False
    assert before == {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before}


@pytest.mark.parametrize("url", ["http://github.com/a", "https://evil.test/a",
                                 "https://github.com:444/a", "https://u:p@github.com/a",
                                 "https://github.com/a#fragment", "https://github.com:invalid/a"])
def test_unapproved_network_endpoints_rejected(url):
    with pytest.raises(scanner.ScannerAssetError):
        scanner.secure_url(url)


def test_cdn_redirect_never_forwards_credentials():
    request = urllib.request.Request("https://github.com/a",
                                     headers={"Authorization": "secret", "Cookie": "secret"})
    result = scanner.PublicRedirect().redirect_request(
        request, None, 302, "public redirect", {}, "https://release-assets.githubusercontent.com/a")
    assert result.get_header("Authorization") is None and result.get_header("Cookie") is None


def test_only_expected_elf_bytes_are_returned(data):
    assert scanner.extract_binary(data.contents[list(scanner.ASSETS)[0]]) == data.binary


@pytest.mark.parametrize("fault", ["path", "link", "duplicate", "not_elf", "wrong_arch"])
def test_unsafe_archive_cannot_supply_scanner(data, monkeypatch, fault):
    binary = data.binary
    kwargs = {}
    if fault == "path": kwargs["member_name"] = "../trivy"
    if fault == "link": kwargs["member_type"] = tarfile.SYMTYPE
    if fault == "duplicate": kwargs["duplicate"] = True
    if fault == "not_elf": binary = b"not-an-elf-file-at-all"
    if fault == "wrong_arch": binary = binary[:18] + b"\xb7\0" + binary[20:]
    body = archive(binary, **kwargs)
    monkeypatch.setattr(scanner, "BINARY_BYTES", len(binary))
    name = list(scanner.ASSETS)[0]
    monkeypatch.setitem(scanner.ASSETS, name, {"size": len(body), "digest": oci.digest(body)})
    with pytest.raises(scanner.ScannerAssetError):
        scanner.extract_binary(body)


def fake_verifier(data, monkeypatch, *, failure=None, version="4.5.0"):
    environment = data.root / "verifier"; environment.mkdir(mode=0o700)
    bindir = environment / "bin"; bindir.mkdir(mode=0o700)
    executable = bindir / "python"; executable.write_bytes(b"test-interpreter"); executable.chmod(0o700)
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        assert kwargs["env"].get("HOME") == str(data.root / "verifier-home")
        assert set(kwargs["env"]) <= {"PATH", "HOME", "PYTHONDONTWRITEBYTECODE",
                                       "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME"}
        if failure == "timeout": raise subprocess.TimeoutExpired(args, 120)
        return SimpleNamespace(returncode=1 if failure == "rejected" else 0,
                               stdout=(version + "\n").encode() if len(calls) == 1 else b"OK",
                               stderr=b"private-error-not-for-output" if failure else b"")
    monkeypatch.setattr(scanner.subprocess, "run", run)
    return executable, calls


def test_signature_gate_uses_exact_identity_and_does_not_execute_scanner(data, monkeypatch):
    scanner.acquire_assets(data.root, data.client)
    python, calls = fake_verifier(data, monkeypatch)
    result = scanner.verify_signature(data.root, python)
    assert result["signature_verified"]
    assert not result["scanner_executed"] and not result["scan_completed"] and not result["runtime_approved"]
    assert not result["binary_executable_on_host"]
    assert (data.root / "trivy.bin").read_bytes() == data.binary
    assert (data.root / "trivy.bin").stat().st_mode & 0o111 == 0
    args = calls[1][0]
    assert scanner.IDENTITY in args and scanner.ISSUER in args
    assert scanner.ASSETS[list(scanner.ASSETS)[0]]["digest"] == args[-1]
    assert "--staging" not in args and "--trust-config" not in args
    assert json.loads((data.root / "verified-assets.json").read_bytes()) == result


@pytest.mark.parametrize("failure,version", [("timeout", "4.5.0"), ("rejected", "4.5.0"),
                                             (None, "wrong-version")])
def test_verification_failure_does_not_extract_or_write_success(data, monkeypatch, failure, version):
    scanner.acquire_assets(data.root, data.client)
    python, _ = fake_verifier(data, monkeypatch, failure=failure, version=version)
    with pytest.raises(scanner.ScannerAssetError):
        scanner.verify_signature(data.root, python)
    assert not (data.root / "trivy.bin").exists()
    assert not (data.root / "verified-assets.json").exists()


def test_existing_executable_permission_is_not_misreported(data, monkeypatch):
    scanner.acquire_assets(data.root, data.client)
    python, _ = fake_verifier(data, monkeypatch)
    path = data.root / "trivy.bin"; path.write_bytes(data.binary); path.chmod(0o700)
    with pytest.raises(scanner.ScannerAssetError, match="executable permission"):
        scanner.verify_signature(data.root, python)
    assert not (data.root / "verified-assets.json").exists()


def test_signed_archive_member_size_is_exact_not_a_relaxed_capacity_bound(data, monkeypatch):
    monkeypatch.setattr(scanner, "BINARY_BYTES", len(data.binary) + 1)
    with pytest.raises(scanner.ScannerAssetError, match="member is invalid"):
        scanner.extract_binary(data.contents[list(scanner.ASSETS)[0]])


def test_expected_binary_fingerprint_is_independent_of_tar_member_format(data, monkeypatch):
    monkeypatch.setattr(scanner, "BINARY_SHA256", "0" * 64)
    with pytest.raises(scanner.ScannerAssetError):
        scanner.extract_binary(data.contents[list(scanner.ASSETS)[0]])


def test_asset_download_deadline_is_enforced_before_network():
    client = scanner.PublicAssets(); client.deadline = 0
    with pytest.raises(scanner.ScannerAssetError, match="deadline"):
        with client.blob(scanner.ASSETS[list(scanner.ASSETS)[0]]["digest"]):
            pytest.fail("expired acquisition must not yield a response")
