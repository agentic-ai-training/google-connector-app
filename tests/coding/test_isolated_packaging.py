"""Real journal/artifact/OCI proofs; Docker, privilege and mounts are simulated.

These tests never claim a real Linux/gVisor launch. Live enforcement and storage
mount/termination evidence remain a separate operator proof before activation.
"""
import hashlib
import io
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

import pytest

from app.coding import isolated_packaging as host, isolated_validation as isolation
from app.coding import image_packaging_worker as worker, image_output_store as outputs
from app.coding import build_artifact as artifacts, oci_packager as oci
from tests.coding.test_oci_packager import case
from tests.coding.test_runtime_image_store import write_store
from tests.coding.test_validation_journal import Engine, CID, IMAGE


class PackagingEngine(Engine):
    def __init__(self):
        super().__init__()
        self.journal = None
        self.calls = []
        self.mutate = None
        self.create_uncertain = False

    def __call__(self, *args, **kwargs):
        self.calls.append(args)
        if args[0] == "create":
            job = dict(self.journal.db.execute("SELECT * FROM jobs WHERE purpose='image_packaging'").fetchone())
            self.add(job, self.journal.scope)
            spec = self.containers[CID]
            mounts = []
            for index, value in enumerate(args):
                if value == "--mount":
                    fields = args[index + 1].split(",")
                    pairs = dict(item.split("=", 1) for item in fields if "=" in item)
                    mounts.append({"Type": "bind", "Source": pairs["src"], "Destination": pairs["dst"],
                                   "RW": "readonly" not in fields, "Propagation": "rprivate"})
            spec["Mounts"] = mounts
            spec["Config"].update(User="65532:65532", Entrypoint=["/usr/bin/env"],
                                   Cmd=list(args[args.index(IMAGE) + 1:]), OpenStdin=False, Tty=False)
            spec["HostConfig"] = dict(Runtime="runsc", NetworkMode="none", ReadonlyRootfs=True,
                                       CapAdd=None, CapDrop=["ALL"], SecurityOpt=["no-new-privileges:true"],
                                       Memory=3 * 1024**3, MemorySwap=3 * 1024**3, NanoCpus=1_000_000_000,
                                       PidsLimit=16, CgroupParent=host.CGROUP_PARENT,
                                       Ulimits=[{"Name": "nofile", "Hard": 64, "Soft": 64}],
                                       LogConfig={"Type": "none"}, Privileged=False, Devices=[],
                                       DeviceRequests=None, PidMode="", IpcMode="private", UTSMode="",
                                       CgroupnsMode="private", PortBindings={}, PublishAllPorts=False, GroupAdd=None)
            if self.mutate:
                self.mutate(spec)
            if self.create_uncertain:
                raise isolation.IsolationError("Docker create failed; outcome unknown")
            return CID.encode()
        return super().__call__(*args, **kwargs)


@pytest.fixture
def context(monkeypatch):
    with tempfile.TemporaryDirectory(prefix=".gca-packaging-host-", dir=Path.home()) as folder:
        root = Path(folder)
        base, runtime_policy, workspace = write_store(root)
        journal_path = root / "journal"
        journal_path.mkdir(mode=0o700)
        validation_policy = root / "validation-policy.json"
        validation_policy.write_bytes(oci.canonical({"version": 3, "server_uuid": "server",
            "runtime": "runsc", "images": {"python_compile": IMAGE}, "state_directory": str(journal_path)}))
        validation_policy.chmod(0o600)
        bundle = Path(host.__file__).parent
        policy = dict(version=1, server_uuid="server", project_id="test-project", image_id=IMAGE,
                      validation_policy_path=str(validation_policy), runtime_policy_path=str(runtime_policy),
                      bundle_directory=str(bundle), bundle_hashes={
                          name + ".py": hashlib.sha256((bundle / (name + ".py")).read_bytes()).hexdigest()
                          for name in host.BUNDLE_NAMES})
        policy_path = root / "host-policy.json"
        policy_path.write_bytes(oci.canonical(policy))
        policy_path.chmod(0o600)
        body, reference, _ = case()
        now = int(time.time())
        validation_binding = {"version": 1, "claim_id": reference["claim_id"], "run_id": str(uuid4()),
                              "sequence_no": 1, "request_hash": "f" * 64, "target_hash": "c" * 64,
                              "source_hash": reference["source_hash"], "server_uuid": "server",
                              "image_id": IMAGE, "profile": "react_build", "timeout_seconds": 20,
                              "deadline": now + 120}
        with isolation.Journal(journal_path) as journal:
            validated = journal.begin("react_build", IMAGE, 20, validation_binding)
            reference["validation_job_id"] = validated["id"]
            reference["artifact_id"] = artifacts.save_build_artifact(
                journal_path, validated["id"], body, artifacts.inspect_build_artifact(body, "react_build"))
            journal.record(validated, "cleaned")
        binding = dict(version=1, build_job_id=str(uuid4()), dispatch_token=str(uuid4()),
                       request_hash="e" * 64, source_hash=reference["source_hash"],
                       validation_claim_id=reference["claim_id"], validation_job_id=reference["validation_job_id"],
                       profile="react_build", artifact_sha256=reference["archive_sha256"],
                       base_manifest_digest=base.approved_manifest_digest,
                       runtime_policy_hash=hashlib.sha256(runtime_policy.read_bytes()).hexdigest(),
                       execution_deadline_unix=now + 600, output_expires_at_unix=now + 3600, timeout_seconds=60,
                       target_hash="c" * 64, server_uuid="server", project_id="test-project")
        engine = PackagingEngine()
        monkeypatch.setattr(isolation, "docker", engine)
        monkeypatch.setattr(isolation, "ensure_toolchain_image", lambda image: None)
        monkeypatch.setattr(host.signal, "alarm", lambda timeout: None)
        monkeypatch.setattr(host, "require_host_limits", lambda: None)
        # Privileged ownership/mount operations are simulated, never exercised on macOS.
        monkeypatch.setattr(host.os, "chown", lambda *args, **kwargs: None)
        monkeypatch.setattr(host.os, "fchown", lambda *args: None)
        mounted = set()

        def storage(directory, *, create=False):
            work = directory / "work"
            if create:
                work.mkdir(mode=0o700)
                (directory / "storage.img").write_bytes(b"simulated-private-ext4-volume")
                (directory / "storage.img").chmod(0o600)
                mounted.add(str(directory))
            return work

        monkeypatch.setattr(host, "storage", storage)
        monkeypatch.setattr(host, "storage_mount", lambda directory: str(directory) in mounted)

        def unmount(operation, *arguments):
            assert operation == "umount"
            work = Path(arguments[0])
            assert work.parent.is_relative_to(journal_path / "packaging-jobs")
            mounted.remove(str(work.parent))
            # Simulate the empty host mountpoint after an ext4 volume is unmounted.
            # All objects here were created by this isolated temporary fixture.
            shutil.rmtree(work)
            work.mkdir(mode=0o700)

        monkeypatch.setattr(host, "_storage_command", unmount)

        def execute(args, timeout, **kwargs):
            assert args == ["start", "-a", CID] and 0 < timeout <= 60 and kwargs["output_limit"] == 16384
            job = engine.journal.db.execute("SELECT id FROM jobs WHERE purpose='image_packaging'").fetchone()[0]
            work = journal_path / "packaging-jobs" / job / "work"
            frame = (work / "input/request.frame").read_bytes()
            request, payload = frame.split(b"\n", 1)
            request = json.loads(request)
            result = worker.assemble_and_retain(
                request["binding"], request["reference"], payload,
                runtime_policy_path=work / "input/runtime-policy.json", output_root=work / "output",
                workspace=work / "workspace")
            return dict(success=True, exit_code=0, output_truncated=False, stdout=oci.canonical(result).decode(),
                        stderr="")

        monkeypatch.setattr(isolation, "execute_bounded", execute)
        yield dict(root=root, journal=journal_path, policy=policy, policy_path=policy_path, binding=binding,
                   validation_binding=validation_binding, reference=reference, body=body, base=base, engine=engine,
                   runtime_policy=runtime_policy, workspace=workspace)


def assemble(context):
    with isolation.Journal(context["journal"]) as journal:
        context["engine"].journal = journal
        return host.assemble(journal, context["policy"], context["binding"],
                             context["validation_binding"], context["reference"])


def test_actual_artifact_to_private_oci_recovery_with_simulated_host(context):
    measurement = assemble(context)
    assert measurement["execution_cleanup_verified"] is True
    assert measurement["evidence"]["generated_code_executed"] is False
    assert context["engine"].removed == [CID]
    with isolation.Journal(context["journal"]) as journal:
        assert host.recover(journal, context["binding"]) == measurement
        with pytest.raises(isolation.IsolationError, match="consumed"):
            host.assemble(journal, context["policy"], context["binding"],
                          context["validation_binding"], context["reference"])
    job = measurement["packaging_job_id"]
    work = context["journal"] / "packaging-jobs" / job / "work"
    for name, checksum in context["policy"]["bundle_hashes"].items():
        assert hashlib.sha256((work / "input/bundle" / name).read_bytes()).hexdigest() == checksum
    archive, _ = outputs.load(work / "output", context["binding"])
    assert hashlib.sha256(archive).hexdigest() == measurement["output_archive_sha256"]


@pytest.mark.parametrize("change", ["host", "policy", "base", "reference", "validation"])
def test_foreign_inputs_cannot_launch_a_container(context, change):
    if change == "host":
        context["binding"]["server_uuid"] = "other-host"
    elif change == "policy":
        context["binding"]["runtime_policy_hash"] = "0" * 64
    elif change == "base":
        context["binding"]["base_manifest_digest"] = "sha256:" + "0" * 64
    elif change == "reference":
        context["reference"]["claim_id"] = str(uuid4())
    else:
        context["validation_binding"]["source_hash"] = "0" * 64
    with pytest.raises((host.PackagingHostError, artifacts.BuildArtifactError, isolation.IsolationError)):
        assemble(context)
    assert not any(call[0] == "create" for call in context["engine"].calls)


@pytest.mark.parametrize("field,value", [
    ("Runtime", "runc"), ("ReadonlyRootfs", False), ("NetworkMode", "host"), ("Privileged", True),
    ("CgroupParent", "system.slice"), ("MemorySwap", 4 * 1024**3), ("PidsLimit", 100),
    ("Ulimits", [{"Name": "nofile", "Hard": 1024, "Soft": 1024}]),
])
def test_wrong_container_enforcement_never_starts_worker(context, field, value):
    context["engine"].mutate = lambda spec: spec["HostConfig"].update({field: value})
    with pytest.raises(host.PackagingHostError, match="enforcement"):
        assemble(context)
    assert context["engine"].removed == [CID]


def test_unexpected_volume_cannot_escape_private_mount_set(context):
    context["engine"].mutate = lambda spec: spec["Mounts"].append({"Type": "volume", "Source": "/secrets"})
    with pytest.raises(host.PackagingHostError, match="mounts"):
        assemble(context)
    assert context["engine"].removed == [CID]


def test_shared_mount_propagation_never_starts_worker(context):
    context["engine"].mutate = lambda spec: spec["Mounts"][0].update(Propagation="shared")
    with pytest.raises(host.PackagingHostError, match="mounts"):
        assemble(context)
    assert context["engine"].removed == [CID]


def test_ambiguous_create_persists_slot_then_reaper_uses_exact_identity(context):
    context["engine"].create_uncertain = True
    # Simulate provider unavailability during cleanup, not verified absence.
    original = context["engine"].__class__.__call__

    def fail_cleanup(engine, *args, **kwargs):
        if args[0] == "ps":
            raise isolation.IsolationError("Docker unavailable")
        return original(engine, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(PackagingEngine, "__call__", fail_cleanup)
        with pytest.raises(host.PackagingHostError, match="cleanup"):
            assemble(context)
    with isolation.Journal(context["journal"]) as journal:
        with pytest.raises(isolation.IsolationError, match="unreconciled"):
            journal.begin("python_compile", IMAGE, 20)
        journal.db.execute("UPDATE jobs SET deadline=0 WHERE purpose='image_packaging'")
        assert len(journal.reap()["cleaned_job_ids"]) == 1


def test_worker_failure_does_not_return_success(context, monkeypatch):
    monkeypatch.setattr(isolation, "execute_bounded", lambda *args, **kwargs: dict(
        success=False, output_truncated=False, stdout="", stderr="private-error-not-to-return"))
    with pytest.raises(host.PackagingHostError, match="successful result") as error:
        assemble(context)
    assert "private-error" not in str(error.value) and context["engine"].removed == [CID]


def test_operator_policy_requires_exact_reviewed_bundle_and_same_host(context):
    policy, validation = host.load_policy(context["policy_path"])
    assert policy == context["policy"] and validation["server_uuid"] == "server"
    policy["bundle_hashes"]["image_packaging_worker.py"] = "0" * 64
    context["policy_path"].write_bytes(oci.canonical(policy))
    with pytest.raises(host.PackagingHostError, match="bundle changed"):
        host.load_policy(context["policy_path"])


def test_gateway_rejects_arbitrary_shell_and_caller_paths(context):
    validation = dict(state_directory=str(context["journal"]))
    with pytest.raises(host.PackagingHostError, match="unsupported"):
        host.gateway(io.BytesIO(b'{"operation":"shell","command":"echo private"}\n'),
                     context["policy"], validation)


def test_help_bootstraps_only_protected_reviewed_modules():
    result = subprocess.run([sys.executable, "-I", host.__file__, "--help"],
                            env={"PATH": "/usr/bin:/bin"}, capture_output=True, timeout=10)
    assert result.returncode == 0 and b"--policy" in result.stdout


def test_cli_without_linux_root_does_not_consume_or_echo_private_input(context):
    result = subprocess.run([sys.executable, "-I", host.__file__, "--policy", str(context["policy_path"])],
                            input=b"private-input", env={"PATH": "/usr/bin:/bin"}, capture_output=True, timeout=10)
    assert result.returncode == 1 and b"private-input" not in result.stdout + result.stderr
    assert json.loads(result.stdout)["private_details_omitted"] is True


@pytest.mark.parametrize("value", ["/usr/lib", "/proc", "/gca/input", "/unsafe,bind", "/var/../etc", "relative"])
def test_unsafe_mount_paths_are_rejected_before_create(context, value):
    policy = json.loads(context["runtime_policy"].read_bytes())
    policy["roles"]["nginx_static_v1"]["layout_directory"] = value
    with isolation.Journal(context["journal"]) as journal:
        job = journal.begin("image_packaging", IMAGE, 60, context["binding"], purpose="image_packaging")
        with pytest.raises(host.PackagingHostError):
            host.prepare(journal, job, context["policy"], context["binding"], context["reference"],
                         context["body"], oci.canonical(policy), context["base"])


def test_verified_storage_removal_is_idempotent_and_keeps_consumed_claim(context):
    measurement = assemble(context)
    with isolation.Journal(context["journal"]) as journal:
        removed = host.remove(journal, context["binding"])
        assert removed["output_removed"] is True and removed["staging_removed"] is True
        assert removed["execution_cleanup_verified"] is True
        assert removed == host.remove(journal, context["binding"])
        assert not (context["journal"] / "packaging-jobs" / measurement["packaging_job_id"]).exists()
        with pytest.raises(isolation.IsolationError, match="consumed"):
            journal.begin("image_packaging", IMAGE, 60, context["binding"], purpose="image_packaging")


def test_busy_filesystem_does_not_prove_storage_removal(context, monkeypatch):
    measurement = assemble(context)

    def busy(*args):
        raise host.PackagingHostError("private packaging filesystem operation is unconfirmed")

    monkeypatch.setattr(host, "_storage_command", busy)
    with isolation.Journal(context["journal"]) as journal:
        with pytest.raises(host.PackagingHostError, match="unconfirmed"):
            host.remove(journal, context["binding"])
        assert (context["journal"] / "packaging-jobs" / measurement["packaging_job_id"] / "storage.img").exists()
        assert journal.db.execute("SELECT count(*) FROM events WHERE state='storage_removed'").fetchone()[0] == 0


def test_expiry_reaper_removes_only_terminated_expired_packaging_volume(context, monkeypatch):
    measurement = assemble(context)
    with isolation.Journal(context["journal"]) as journal:
        assert journal.reap()["expired_packaging_staging_purged"] == 0
        monkeypatch.setattr(host.time, "time", lambda: context["binding"]["output_expires_at_unix"] + 1)
        assert journal.reap()["expired_packaging_staging_purged"] == 1
        assert journal.reap()["expired_packaging_staging_purged"] == 0
        assert not (context["journal"] / "packaging-jobs" / measurement["packaging_job_id"]).exists()


def test_private_output_cannot_be_adopted_or_removed_with_foreign_binding(context):
    measurement = assemble(context)
    altered = {**context["binding"], "dispatch_token": str(uuid4())}
    with isolation.Journal(context["journal"]) as journal:
        for operation in (host.recover, host.remove):
            with pytest.raises(host.PackagingHostError):
                operation(journal, altered)
        assert (context["journal"] / "packaging-jobs" / measurement["packaging_job_id"] / "storage.img").exists()


def test_untracked_host_object_blocks_cleanup(context):
    measurement = assemble(context)
    directory = context["journal"] / "packaging-jobs" / measurement["packaging_job_id"]
    (directory / "unexpected").write_bytes(b"must-not-be-deleted")
    with isolation.Journal(context["journal"]) as journal:
        with pytest.raises(host.PackagingHostError, match="unknown object"):
            host.remove(journal, context["binding"])
    assert (directory / "unexpected").read_bytes() == b"must-not-be-deleted"


@pytest.mark.parametrize("change", ["foreign_backing", "missing_noexec", "wrong_type"])
def test_kernel_mount_identity_rejects_other_filesystems(tmp_path, monkeypatch, change):
    directory = tmp_path / "job"
    mount = f"23 20 7:3 / {directory}/work rw,nosuid,nodev,noexec - ext4 /dev/loop7 rw"
    backing = str(directory / "storage.img")
    if change == "foreign_backing":
        backing = str(tmp_path / "another-job/storage.img")
    elif change == "missing_noexec":
        mount = mount.replace(",noexec", "")
    else:
        mount = mount.replace("- ext4", "- xfs")
    original = Path.read_text

    def read(path, *args, **kwargs):
        if str(path) == "/proc/self/mountinfo":
            return mount
        if str(path) == "/sys/block/loop7/loop/backing_file":
            return backing
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    with pytest.raises(host.PackagingHostError):
        host.storage_mount(directory)


@pytest.mark.parametrize("value", ["memory", "swap", "cpu", "pids", "occupied"])
def test_shared_host_limit_or_ownership_mismatch_rejected_before_join(monkeypatch, value):
    values = {"memory.max": str(3 * 1024**3), "memory.swap.max": "0", "cpu.max": "100000 100000",
              "pids.max": "64", "cgroup.procs": ""}
    alterations = {"memory": ("memory.max", "max"), "swap": ("memory.swap.max", "1"),
                   "cpu": ("cpu.max", "200000 100000"), "pids": ("pids.max", "1000"),
                   "occupied": ("cgroup.procs", "99999999")}
    field, new = alterations[value]
    values[field] = new
    monkeypatch.setattr(host.sys, "platform", "linux")
    monkeypatch.setattr(host.os, "geteuid", lambda: 0)
    monkeypatch.setattr(Path, "read_text", lambda path, *args, **kwargs: values[path.name])
    monkeypatch.setattr(Path, "write_text", lambda *args, **kwargs: pytest.fail("Invalid limits were joined"))
    with pytest.raises(host.PackagingHostError):
        host.require_host_limits()
