"""Real worker logic/store proofs; enforced Linux/container launch is not simulated as live."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.coding import image_packaging_worker as worker, image_output_store as outputs
from app.coding import oci_packager as oci
from tests.coding.test_oci_packager import case
from tests.coding.test_runtime_image_store import write_store


def fake_network(monkeypatch, *, sysfs, proc=('lo',), sockets=((1, 'lo'),)):
    real_iterdir, real_open = Path.iterdir, Path.open

    def iterdir(path):
        if str(path) == '/sys/class/net':
            if sysfs is None:
                raise FileNotFoundError('fixed missing sysfs')
            return iter(SimpleNamespace(name=name) for name in sysfs)
        return real_iterdir(path)

    def open_path(path, *args, **kwargs):
        if str(path) == '/proc/net/dev':
            return io.StringIO('Inter-| Receive\n face | bytes\n' +
                               '\n'.join(name + ': 0 0 0 0' for name in proc))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'iterdir', iterdir)
    monkeypatch.setattr(Path, 'open', open_path)
    monkeypatch.setattr(worker.socket, 'if_nameindex', lambda: list(sockets))


def test_proof_backed_loopback_without_sysfs_requires_proc_and_socket_evidence(monkeypatch):
    fake_network(monkeypatch, sysfs=None)
    worker.require_guest_network(proof_backed=True)
    with pytest.raises(worker.PackagingWorkerError, match='sysfs evidence unavailable'):
        worker.require_guest_network()


@pytest.mark.parametrize('sysfs,proc,sockets', [
    (('lo', 'eth0'), ('lo',), ((1, 'lo'),)),
    (None, ('lo', 'eth0'), ((1, 'lo'),)),
    (None, ('lo',), ((1, 'lo'), (2, 'eth0'))),
    (None, (), ((1, 'lo'),)),
    (None, ('lo',), ()),
    (None, ('lo', 'lo'), ((1, 'lo'),)),
    (None, ('lo',), ((0, 'lo'),)),
])
def test_any_nonloopback_missing_or_conflicting_evidence_rejects(monkeypatch, sysfs, proc, sockets):
    fake_network(monkeypatch, sysfs=sysfs, proc=proc, sockets=sockets)
    with pytest.raises(worker.PackagingWorkerError, match='network must be isolated'):
        worker.require_guest_network(proof_backed=True)


def test_native_guest_requires_all_three_loopback_observations(monkeypatch):
    fake_network(monkeypatch, sysfs=('lo',))
    worker.require_guest_network()


@pytest.fixture
def context(monkeypatch):
    with tempfile.TemporaryDirectory(prefix=".gca-packaging-worker-", dir=Path.home()) as folder:
        root = Path(folder)
        base, policy, workspace = write_store(root)
        output = root / "images"
        output.mkdir(mode=0o700)
        body, reference, _ = case()
        now = int(time.time())
        binding = dict(version=1, build_job_id=str(uuid4()), dispatch_token=str(uuid4()),
                       request_hash="e" * 64, source_hash=reference["source_hash"],
                       validation_claim_id=reference["claim_id"], validation_job_id=reference["validation_job_id"],
                       profile=reference["profile"], artifact_sha256=reference["archive_sha256"],
                       base_manifest_digest=base.approved_manifest_digest,
                       runtime_policy_hash=hashlib.sha256(policy.read_bytes()).hexdigest(),
                       execution_deadline_unix=now + 600, output_expires_at_unix=now + 3600, timeout_seconds=60,
                       target_hash="c" * 64, server_uuid="server", project_id="test-project")
        monkeypatch.setattr(worker.signal, "alarm", lambda seconds: None)
        yield output, policy, workspace, body, reference, binding


def assemble(context, **changes):
    output, policy, workspace, body, reference, binding = context
    values = dict(binding=binding, reference=reference, artifact=body, runtime_policy_path=policy,
                  output_root=output, workspace=workspace)
    return worker.assemble_and_retain(**{**values, **changes})


def test_real_assembly_reaches_durable_image_store(context):
    result = assemble(context)
    body, recovered = outputs.load(context[0], context[-1])
    assert result == recovered
    assert result["output_archive_sha256"] == hashlib.sha256(body).hexdigest()
    assert result["evidence"]["generated_code_executed"] is False
    assert "execution_cleanup_verified" not in result
    assert b"private source" not in oci.canonical(result)


def test_failure_stage_is_visible_but_exception_content_is_not(monkeypatch, capsys):
    monkeypatch.setattr(worker.sys, 'argv', ['worker', '--runtime-policy', '/fixed/policy',
                                           '--output-root', '/fixed/output', '--workspace', '/fixed/workspace'])
    monkeypatch.setattr(worker, '_keyless_guard', lambda: None)
    monkeypatch.setattr(worker.signal, 'alarm', lambda seconds: None)

    def reject(**kwargs):
        kwargs['on_phase']('guest_privileges')
        raise worker.PackagingWorkerError('PRIVATE_CONTENT_SENTINEL_DO_NOT_PUBLISH')

    monkeypatch.setattr(worker, 'require_enforcement', reject)
    assert worker.main() == 1
    output = capsys.readouterr().out
    assert 'PRIVATE_CONTENT_SENTINEL' not in output
    assert json.loads(output) == {'packaging_state': 'failed', 'failure_stage': 'guest_privileges',
                                 'failure_category': 'PackagingWorkerError', 'private_details_omitted': True}


def test_real_assembly_reports_only_fixed_stage_labels(context):
    phases = []
    output, policy, workspace, body, reference, binding = context
    worker.assemble_and_retain(binding, reference, body, runtime_policy_path=policy,
                              output_root=output, workspace=workspace, on_phase=phases.append)
    assert phases == ['artifact_lineage', 'runtime_policy', 'runtime_base', 'oci_assembly', 'durable_output_store']


@pytest.mark.parametrize("change", ["payload", "reference", "policy", "base"])
def test_altered_input_or_policy_creates_no_output(context, change):
    alterations = {
        "payload": {"artifact": b"wrong"},
        "reference": {"reference": {**context[4], "archive_sha256": "f" * 64}},
        "policy": {"binding": {**context[5], "runtime_policy_hash": "f" * 64}},
        "base": {"binding": {**context[5], "base_manifest_digest": "sha256:" + "f" * 64}},
    }
    with pytest.raises((worker.PackagingWorkerError, ValueError)):
        assemble(context, **alterations[change])
    assert not (context[0] / context[-1]["build_job_id"]).exists()


def test_fixed_binary_request_and_readonly_observation(context):
    output, policy, workspace, body, reference, binding = context
    request = dict(operation="assemble", binding=binding, reference=reference, archive_bytes=len(body))
    receipt = worker.serve(io.BytesIO(oci.canonical(request) + b"\n" + body),
                          runtime_policy_path=policy, output_root=output, workspace=workspace)
    read = worker.serve(io.BytesIO(oci.canonical(dict(operation="observe", binding=binding)) + b"\n"),
                       runtime_policy_path=policy, output_root=output, workspace=workspace)
    assert receipt == read


@pytest.mark.parametrize("payload", [
    {"operation": "shell", "command": "print secrets"},
    {"operation": "assemble", "binding": {}, "reference": {}, "archive_bytes": True},
    {"operation": "assemble", "binding": {}, "reference": {}, "archive_bytes": 129 * 1024 * 1024},
    {"operation": "assemble", "binding": {}, "reference": {}, "archive_bytes": 10, "cwd": "/"},
])
def test_protocol_cannot_acquire_shell_paths_or_unbounded_inputs(context, payload):
    with pytest.raises(worker.PackagingWorkerError):
        worker.serve(io.BytesIO(json.dumps(payload).encode() + b"\n"), runtime_policy_path=context[1],
                     output_root=context[0], workspace=context[2])


def test_incomplete_transfer_creates_no_image(context):
    binding = context[-1]
    request = dict(operation="assemble", binding=binding, reference=context[4], archive_bytes=len(context[3]))
    with pytest.raises(worker.PackagingWorkerError, match="truncated"):
        worker.serve(io.BytesIO(oci.canonical(request) + b"\n" + b"short"),
                     runtime_policy_path=context[1], output_root=context[0], workspace=context[2])


def test_worker_cannot_retain_private_images_inside_generated_repository(context):
    within = context[2] / "private-images"
    within.mkdir(mode=0o700)
    with pytest.raises(oci.ImagePackagingError, match="outside"):
        assemble(context, output_root=within)
    assert list(within.iterdir()) == []


def test_environment_and_platform_fail_closed_before_private_input(monkeypatch):
    monkeypatch.setattr(worker.sys, "platform", "darwin")
    with pytest.raises(worker.PackagingWorkerError, match="Linux"):
        worker.require_enforcement()
    monkeypatch.setattr(worker.sys, "platform", "linux")
    monkeypatch.setattr(worker.os, "geteuid", lambda: 65532)
    monkeypatch.setattr(worker.os, "environ", {"CODING_GROQ_API_KEY": "synthetic-not-a-secret"})
    with pytest.raises(worker.PackagingWorkerError, match="keyless"):
        worker.require_enforcement()


def test_isolated_python_bootstrap_reads_only_reviewed_siblings():
    # Help loads the protected bundle but does not run the worker or read private data.
    result = subprocess.run([sys.executable, "-I", str(Path(worker.__file__)), "--help"],
                            env={"PATH": "/usr/bin:/bin"}, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr.decode()
    assert b"--runtime-policy" in result.stdout


def test_cli_without_enforcement_does_not_consume_request_or_print_private_data(context):
    result = subprocess.run([
        sys.executable, "-I", str(Path(worker.__file__)), "--runtime-policy", str(context[1]),
        "--output-root", str(context[0]), "--workspace", str(context[2]),
    ], input=b"private-payload-that-must-not-be-printed", env={"PATH": "/usr/bin:/bin"},
        capture_output=True, timeout=10)
    assert result.returncode == 1 and b"private-payload" not in result.stdout + result.stderr
    assert json.loads(result.stdout)["private_details_omitted"] is True
