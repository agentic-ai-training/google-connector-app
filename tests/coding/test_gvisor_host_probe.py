import json
import subprocess

import pytest

from scripts import probe_gvisor_host as probe


IMAGE = "sha256:" + "a" * 64


def inspect_result():
    return [{
        "HostConfig": {
            "Runtime": "runsc", "NetworkMode": "none", "ReadonlyRootfs": True,
            "Privileged": False, "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"], "Memory": 134217728,
            "MemorySwap": 134217728, "NanoCpus": 500000000, "PidsLimit": 32,
            "Binds": None,
        },
        "Mounts": [{"Type": "tmpfs"}], "Config": {"User": "65532:65532"},
        "State": {"ExitCode": 0},
    }]


def fake_docker(monkeypatch, *, change=None, fail=None):
    calls = []

    def invoke(*args, **kwargs):
        calls.append(args)
        if args[0] == fail:
            raise subprocess.TimeoutExpired(["docker", *args], 30)
        if args[0] == "inspect":
            data = inspect_result()
            if change:
                change(data[0])
            return json.dumps(data)
        if args[0] == "start":
            return "filesystem_and_egress_checks_passed\n"
        return ""

    monkeypatch.setattr(probe, "docker", invoke)
    return calls


def test_probe_uses_only_immutable_local_image_and_bounded_controls(monkeypatch):
    calls = fake_docker(monkeypatch)
    result = probe.probe(IMAGE)
    assert all(result["checks"].values())
    assert result["broker_integrated"] is False
    create = calls[0]
    for required in (
        "--pull=never", "--runtime=runsc", "--network=none", "--read-only",
        "--memory=128m", "--memory-swap=128m", "--cpus=0.5", "--pids-limit=32",
        "--user=65532:65532", "--log-driver=none",
    ):
        assert required in create
    assert not any(arg.startswith(("--volume", "--mount", "--env")) for arg in create)
    assert calls[-1][:3] == ("rm", "--force", "--volumes")
    assert calls[-1][-1] == create[create.index("--name") + 1]


@pytest.mark.parametrize("image", ["redis:7-alpine", "sha256:" + "x" * 64, ""])
def test_probe_rejects_mutable_or_invalid_image(monkeypatch, image):
    calls = fake_docker(monkeypatch)
    with pytest.raises(ValueError, match="immutable"):
        probe.probe(image)
    assert calls == []


@pytest.mark.parametrize("field,value", [
    ("Runtime", "runc"), ("NetworkMode", "bridge"), ("ReadonlyRootfs", False),
    ("Privileged", True), ("Memory", 0), ("MemorySwap", -1), ("NanoCpus", 0),
    ("PidsLimit", 0), ("Binds", ["/host:/host"]),
])
def test_probe_rejects_runtime_configuration_drift(monkeypatch, field, value):
    calls = fake_docker(
        monkeypatch, change=lambda row: row["HostConfig"].update({field: value}),
    )
    with pytest.raises(RuntimeError, match="Isolation checks failed"):
        probe.probe(IMAGE)
    assert calls[-1][0] == "rm"


def test_probe_rejects_image_implicit_persistent_volume(monkeypatch):
    fake_docker(monkeypatch, change=lambda row: row.update(Mounts=[{"Type": "volume"}]))
    with pytest.raises(RuntimeError, match="no_persistent_volumes"):
        probe.probe(IMAGE)


@pytest.mark.parametrize("phase", ["create", "start", "inspect"])
def test_probe_cleans_up_on_lost_response_or_timeout(monkeypatch, phase):
    calls = fake_docker(monkeypatch, fail=phase)
    with pytest.raises(subprocess.TimeoutExpired):
        probe.probe(IMAGE)
    assert calls[-1][:3] == ("rm", "--force", "--volumes")


def test_probe_does_not_accept_printed_success_after_failed_command(monkeypatch):
    fake_docker(monkeypatch, change=lambda row: row["State"].update(ExitCode=31))
    with pytest.raises(RuntimeError, match="probe_passed"):
        probe.probe(IMAGE)
