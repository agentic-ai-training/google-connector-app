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


@pytest.mark.parametrize('fault', [None, 'approval', 'published', 'custody', 'hash', 'identity'])
def test_rejection_client_never_confuses_terminal_recovery_with_approval(transport, fault):
    result = {'version': 1, 'intent_id': 'b' * 64, 'image_approved': False, 'configuration_published': False,
              'custody_retained': True, 'image_retained': True, 'replayed_observation': False,
              'custody_sha256': 'c' * 64, 'load_sha256': 'd' * 64, 'proofs_sha256': 'e' * 64}
    if fault == 'approval':
        result['image_approved'] = True
    elif fault == 'published':
        result['configuration_published'] = True
    elif fault == 'custody':
        result['custody_retained'] = False
    elif fault == 'hash':
        result['load_sha256'] = 'invalid'
    elif fault == 'identity':
        result['intent_id'] = 'a' * 64
    transport.results.append(evidence('reject', rejection=result, image_approved=False, validation_policy_changed=False))
    if fault:
        with pytest.raises(RuntimeError, match='rejection evidence'):
            client.rpc(None, 'reject', intent_id='b' * 64)
    else:
        assert client.rpc(None, 'reject', intent_id='b' * 64)['rejection'] == result
    assert len(transport.processes) == 1


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


def fixture_receipt(case='react_test_pass'):
    return {'version': 1, 'intent_id': 'b' * 64, 'case': case, 'job_id': 'd' * 32,
            'source_sha256': client.proof.FIXTURES[case.split('_', 1)[0]][case.rsplit('_', 1)[1]][1],
            'binding_sha256': 'a' * 64, 'native_enforcement_sha256': 'c' * 64,
            'result_sha256': 'd' * 64, 'exit_code': 1 if case.endswith('_fail') else 0,
            'passed': True, 'cleanup_verified': True, 'observed_at_unix': 1}


@pytest.mark.parametrize('role', ['react', 'spring'])
@pytest.mark.parametrize('negative', [False, True])
def test_proof_transports_only_fixed_fixture_bytes_without_image_export(transport, role, negative):
    case = client.proof.CASES[role][-1 if negative else 0]
    result = {**fixture_receipt(case), 'replayed_observation': False, 'image_approved': False}
    transport.results.append(evidence('proof_run', proof=result, image_approved=False))
    assert client.rpc(None, 'proof_run', fixture_role=role, intent_id='b' * 64, case=case)['proof'] == result
    stream = io.BytesIO(transport.processes[0].stdin.getvalue())
    parsed = gateway.frame(stream)
    assert parsed['case'] == case and 'role' not in parsed
    assert stream.read() == client.fixture_archive(role, negative=negative)[0]
    assert len(transport.processes) == 1


@pytest.mark.parametrize('field,value', [('intent_id', 'a' * 64), ('image_approved', True),
                                        ('source_sha256', 'c' * 64), ('cleanup_verified', False),
                                        ('passed', 1), ('job_id', 7), ('version', True)])
def test_forged_fixture_result_never_counts_as_proof(transport, field, value):
    result = {**fixture_receipt(), 'replayed_observation': False, 'image_approved': False, field: value}
    transport.results.append(evidence('proof_run', proof=result, image_approved=False))
    with pytest.raises(RuntimeError, match='fixed-fixture evidence'):
        client.rpc(None, 'proof_run', fixture_role='react', intent_id='b' * 64, case='react_test_pass')


@pytest.mark.parametrize('role', ['react', 'spring'])
def test_observe_validates_each_fixed_case_and_does_not_invent_missing_result(transport, role):
    cases = {case: fixture_receipt(case) for case in client.proof.CASES[role]}
    result = {'intent_id': 'b' * 64, 'phase': 'proof_pending', 'cases': cases,
              'all_fixed_cases_passed': True, 'image_approved': False}
    transport.results.append(evidence('proof_observe', proof=result, image_approved=False))
    assert client.rpc(None, 'proof_observe', intent_id='b' * 64)['proof']['all_fixed_cases_passed'] is True
    cases[next(iter(cases))] = None
    transport.results.append(evidence('proof_observe', proof=result, image_approved=False))
    with pytest.raises(RuntimeError, match='fixed-fixture evidence'):
        client.rpc(None, 'proof_observe', intent_id='b' * 64)


def test_wrong_fixture_role_is_rejected_before_any_transport(transport):
    with pytest.raises(RuntimeError, match='fixture role/case'):
        client.rpc(None, 'proof_run', fixture_role='spring', intent_id='b' * 64, case='react_test_pass')
    assert transport.processes == []


@pytest.mark.parametrize('fault', [None, 'execution', 'approval', 'identity', 'attempt', 'mixed_role', 'output'])
def test_diagnostic_transport_is_read_only_bounded_and_role_specific(transport, fault):
    item = {'job_id': 'd' * 32, 'attempt': 1, 'raw_result_available': True, 'receipt_available': True,
            'passed': False, 'success': False, 'exit_code': 1, 'cleanup_verified': True,
            'result_sha256': 'e' * 64, 'stdout': 'compile failed', 'stderr': '',
            'executed_commands': [['mvn', '-o', 'test']], 'diagnostic_excerpt_truncated': False,
            'output_truncated_at_execution': False}
    result = {'version': 1, 'intent_id': 'b' * 64, 'phase': 'proof_pending',
              'execution_invoked': False, 'image_approved': False, 'cases': {'spring_test_pass': item}}
    if fault == 'execution':
        result['execution_invoked'] = True
    elif fault == 'approval':
        result['image_approved'] = True
    elif fault == 'identity':
        result['intent_id'] = 'a' * 64
    elif fault == 'attempt':
        item['attempt'] = True
    elif fault == 'mixed_role':
        result['cases']['react_test_pass'] = item
    elif fault == 'output':
        item['stdout'] = '\U0001f642' * 700
    transport.results.append(evidence('proof_diagnostics', proof=result, image_approved=False))
    if fault:
        with pytest.raises(RuntimeError, match='diagnostic evidence'):
            client.rpc(None, 'proof_diagnostics', intent_id='b' * 64)
    else:
        assert client.rpc(None, 'proof_diagnostics', intent_id='b' * 64)['proof'] == result
    assert len(transport.processes) == 1
    stream = io.BytesIO(transport.processes[0].stdin.getvalue())
    assert gateway.frame(stream)['operation'] == 'proof_diagnostics' and stream.read() == b''


@pytest.mark.parametrize('fault', [None, 'configuration_published', 'image_approved', 'intent_id'])
def test_publication_preparation_cannot_claim_policy_write_or_image_approval(transport, fault):
    result = {'intent_id': 'b' * 64, 'phase': 'prepared', 'plan_sha256': 'c' * 64,
              'policy_sha256': 'd' * 64, 'registry_sha256': 'e' * 64,
              'image_approved': False, 'configuration_published': False}
    if fault:
        result[fault] = 'a' * 64 if fault == 'intent_id' else True
    transport.results.append(evidence('publish_prepare', publication=result, image_approved=False))
    if fault:
        with pytest.raises(RuntimeError, match='publication preparation evidence'):
            client.rpc(None, 'publish_prepare', intent_id='b' * 64)
    else:
        assert client.rpc(None, 'publish_prepare', intent_id='b' * 64)['publication'] == result


@pytest.mark.parametrize('fault', [None, 'version', 'configuration_published', 'policy_sha256', 'plan_sha256'])
def test_explicit_publication_accepts_only_exact_verified_receipt(transport, fault):
    result = {'version': 1, 'intent_id': 'b' * 64, 'plan_sha256': 'c' * 64,
              'policy_sha256': 'd' * 64, 'registry_sha256': 'e' * 64,
              'image_approved': True, 'configuration_published': True, 'replayed_observation': False}
    if fault:
        result[fault] = True if fault == 'version' else False if fault == 'configuration_published' else 'wrong'
    transport.results.append(evidence('publish', publication=result, image_approved=True, validation_policy_changed=True))
    if fault:
        with pytest.raises(RuntimeError, match='verified publication evidence'):
            client.rpc(None, 'publish', intent_id='b' * 64, plan_sha256='c' * 64)
    else:
        assert client.rpc(None, 'publish', intent_id='b' * 64, plan_sha256='c' * 64)['publication'] == result
