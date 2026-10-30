import io
import json
import tarfile

import pytest

from app.coding import isolated_validation as runner
from tests.coding.test_validation_dispatch import setup as setup_fixture
from tests.coding.test_isolated_validation import fake_transport

setup = setup_fixture


def frame(binding, source):
    return io.BytesIO(json.dumps({"operation": "validate", "binding": binding,
                                 "archive_bytes": len(source)}).encode() + b"\n" + source)


def test_gateway_validates_and_observes_without_host_extraction(setup, monkeypatch):
    workspace, policy, binding = setup
    fake_transport(monkeypatch)
    source = runner.source_archive(workspace)
    result = runner.gateway(frame(binding, source), policy)
    assert result["success"] and result["claim_id"] == binding["claim_id"]
    observed = runner.gateway(io.BytesIO(json.dumps({
        "operation": "observe", "claim_id": binding["claim_id"],
    }).encode() + b"\n"), policy)
    assert observed["cleanup_verified"] and observed["validation_success"] is None
    with pytest.raises(runner.IsolationError, match="consumed"):
        runner.gateway(frame(binding, source), policy)


@pytest.mark.parametrize("payload", [
    b"x" * 9000, b"{}", b"[]\n", b'{"operation":"shell"}\n',
    b'{"operation":"validate","binding":{},"archive_bytes":true}\n',
    b'{"operation":"validate","binding":{},"archive_bytes":100}\n',
])
def test_malformed_gateway_never_calls_docker(setup, monkeypatch, payload):
    _, policy, _ = setup
    calls = fake_transport(monkeypatch)
    with pytest.raises((runner.IsolationError, ValueError)):
        runner.gateway(io.BytesIO(payload), policy)
    assert calls == []


def test_gateway_rejects_legacy_policy(setup):
    _, policy, _ = setup
    with pytest.raises(runner.IsolationError, match="policy v3"):
        runner.gateway(io.BytesIO(b""), {**policy, "version": 2})


@pytest.mark.parametrize("name,type_,link", [
    ("../escape", tarfile.REGTYPE, ""), ("/root/escape", tarfile.REGTYPE, ""),
    ("app/link", tarfile.SYMTYPE, "/etc"), ("app/link", tarfile.LNKTYPE, "/etc/passwd"),
    ("app/fifo", tarfile.FIFOTYPE, ""), (".env", tarfile.REGTYPE, ""),
    (".git/config", tarfile.REGTYPE, ""),
])
def test_archive_rejects_unsafe_members(name, type_, link):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        item = tarfile.TarInfo(name)
        item.type, item.linkname = type_, link
        archive.addfile(item)
    with pytest.raises(runner.IsolationError):
        runner.check_archive(output.getvalue(), "python_compile")


def test_directory_named_python_file_is_not_compile_success():
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        item = tarfile.TarInfo("app/not_source.py")
        item.type = tarfile.DIRTYPE
        archive.addfile(item)
    with pytest.raises(runner.IsolationError, match="No Python source"):
        runner.check_archive(output.getvalue(), "python_compile")
