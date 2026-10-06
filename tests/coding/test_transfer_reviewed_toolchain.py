"""No cloud calls; real frame serialization and owned-process cleanup contracts."""
import io
import json
from types import SimpleNamespace

import pytest

from scripts import transfer_reviewed_toolchain as client
from app.coding import operator_toolchain_host as gateway


class Pipe(io.BytesIO):
    def close(self):
        self.was_closed = True


class Process:
    def __init__(self, body, code=0, pid=123456):
        self.stdin, self.stdout = Pipe(), Pipe(body)
        self.code, self.pid, self.waited = code, pid, False

    def poll(self):
        return self.code

    def wait(self, timeout):
        self.waited = True
        return self.code


def evidence(operation, **extra):
    return {"operation": operation, "production_policy_changed": False, **extra}


@pytest.fixture
def transport(monkeypatch):
    processes = []
    results = []
    monkeypatch.setattr(client, "command", lambda host: (["fixed-ssh", "old-command"], {"HOME": "/nonexistent"}))

    def spawn(argv, **options):
        assert options["start_new_session"] is True
        assert options["stderr"] == client.subprocess.DEVNULL
        assert argv[0] == "fixed-ssh"
        assert "/usr/local/libexec/gca/operator_toolchain_host.py" in argv[-1]
        process = Process(json.dumps(results.pop(0)).encode())
        processes.append(process)
        return process

    monkeypatch.setattr(client.subprocess, "Popen", spawn)
    return SimpleNamespace(processes=processes, results=results)


def test_default_probe_is_only_one_fixed_control_operation(transport):
    transport.results.append(evidence("probe", imports=[]))
    assert client.rpc(None, "probe")["imports"] == []
    stream = io.BytesIO(transport.processes[0].stdin.getvalue())
    parsed = gateway.frame(stream)
    assert parsed == {"version": 1, "operation": "probe", "bundle_sha256": client.bundle_hash()}
    assert stream.read() == b""
    assert transport.processes[0].waited


def test_prepare_role_is_a_control_field_and_never_an_export_instruction(transport):
    transport.results.append(evidence("prepare", reservation={"phase": "prepared"}))
    client.rpc(None, "prepare", role="react", policy_sha256="b" * 64, registry_sha256="c" * 64)
    parsed = gateway.frame(io.BytesIO(transport.processes[0].stdin.getvalue()))
    assert parsed["role"] == "react"
    assert parsed["policy_sha256"] == "b" * 64 and len(transport.processes) == 1


@pytest.mark.parametrize("wrong", [{"operation": "approve", "production_policy_changed": False},
                                  {"operation": "probe", "production_policy_changed": True},
                                  {"operation": "probe"}])
def test_unconfirmed_or_wrong_authority_response_never_counts_as_success(transport, wrong):
    transport.results.append(wrong)
    with pytest.raises(RuntimeError, match="evidence differs"):
        client.rpc(None, "probe")
    assert transport.processes[0].waited


def test_receive_without_export_reconciles_only_exact_unapproved_custody(transport):
    result = {"intent_id": "b" * 64, "archive": {"image_loaded": False, "image_approved": False}}
    transport.results.append(evidence("receive", custody=result))
    assert client.rpc(None, "receive", intent_id="b" * 64)["custody"] == result
    assert len(transport.processes) == 1


@pytest.mark.parametrize("field,value", [("intent_id", "c" * 64), ("image_loaded", True), ("image_approved", True)])
def test_receive_cannot_substitute_intents_or_claim_load_or_approval(transport, field, value):
    result = {"intent_id": "b" * 64, "archive": {"image_loaded": False, "image_approved": False}}
    (result if field == "intent_id" else result["archive"])[field] = value
    transport.results.append(evidence("receive", custody=result))
    with pytest.raises(RuntimeError, match="receipt differs"):
        client.rpc(None, "receive", intent_id="b" * 64)


def test_cleanup_terminates_only_the_owned_live_process_group(monkeypatch):
    process = Process(b"", code=None)
    killed = []
    monkeypatch.setattr(client.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    client.stop_owned(process)
    assert killed == [(process.pid, client.signal.SIGKILL)] and process.waited
    assert process.stdin.was_closed and process.stdout.was_closed


def test_failed_remote_exit_keeps_result_unconfirmed(monkeypatch, transport):
    def spawn(*args, **kwargs):
        return Process(json.dumps(evidence("probe")).encode(), code=7)

    monkeypatch.setattr(client.subprocess, "Popen", spawn)
    with pytest.raises(RuntimeError, match="unconfirmed"):
        client.rpc(None, "probe")


@pytest.mark.parametrize("already_staged", [False, True])
def test_export_uses_only_reviewed_immutable_id_and_handles_reconciled_input_not_consumed(monkeypatch, already_staged):
    import hashlib
    body = b"fixed-public-archive-stream"
    image = "sha256:" + "a" * 64
    archive = {"archive_sha256": hashlib.sha256(body).hexdigest(), "archive_bytes": len(body),
               "image_index_digest": image, "image_loaded": False, "image_approved": False}
    reviewed = {"image_id": image, "archive_sha256": archive["archive_sha256"], "archive_bytes": len(body)}
    monkeypatch.setattr(client.catalog, "REVIEWED_ARCHIVES", {"react": reviewed})
    monkeypatch.setattr(client, "inspect", lambda role: image)
    monkeypatch.setattr(client, "command", lambda host: (["fixed-ssh", "old-command"], {}))
    processes = []

    class Input(Pipe):
        def write(self, data):
            if already_staged and data == body:
                raise BrokenPipeError("prior staged input not consumed")
            return super().write(data)

    def spawn(argv, **options):
        assert options["start_new_session"] is True
        if argv[0] == client.DOCKER:
            assert argv == [client.DOCKER, "image", "save", image]
            assert options["env"] == client.ENV
            process = Process(body, pid=123457)
        else:
            receipt = {"intent_id": "b" * 64, "archive": archive}
            process = Process(json.dumps(evidence("receive", custody=receipt)).encode())
            process.stdin = Input()
        processes.append(process)
        return process

    monkeypatch.setattr(client.subprocess, "Popen", spawn)
    result = client.rpc(None, "receive", export_role="react", intent_id="b" * 64)
    assert result["custody"]["archive"] == archive
    assert len(processes) == 2 and all(process.waited for process in processes)
    data = io.BytesIO(processes[1].stdin.getvalue())
    assert gateway.frame(data)["intent_id"] == "b" * 64
    assert data.read() == (b"" if already_staged else body)
