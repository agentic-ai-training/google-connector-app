"""Fixed-target GCE identity parsing; mocked CLI, no cloud writes."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.coding import gce_identity
from app.coding.validation_transport import IAPValidationHost


def host():
    return IAPValidationHost(
        server_uuid="test-server", project_id="project-ee0c1944-144c-4364-ad4",
        zone="asia-south2-a", instance="gca-coolify-delhi-01",
        instance_id="822115653245959101", ssh_user="operator",
        gcloud_configuration="operator", gcloud_config_directory=Path("/private/config"),
        identity_file=Path("/private/key"), known_hosts_file=Path("/private/known"),
        host_public_key="ssh-ed25519 " + "A" * 44,
        gcloud_binary=Path("/private/gcloud"), ssh_binary=Path("/private/ssh"),
    )


def instance():
    return {
        "id": "822115653245959101", "name": "gca-coolify-delhi-01",
        "status": "RUNNING", "creationTimestamp": "2026-09-19T10:24:34.872-07:00",
        "machineType": "https://www.googleapis.com/compute/v1/projects/p/zones/asia-south2-a/machineTypes/e2-standard-4",
        "zone": "https://www.googleapis.com/compute/v1/projects/p/zones/asia-south2-a",
        "disks": [{"boot": True, "source": "https://www.googleapis.com/compute/v1/projects/p/zones/asia-south2-a/disks/gca-coolify-delhi-01"}],
        "scheduling": {"terminationTime": "2026-10-19T17:24:32Z"},
    }


def disk():
    return {"name": "gca-coolify-delhi-01", "sizeGb": "100",
            "type": "zones/asia-south2-a/diskTypes/pd-balanced", "status": "READY",
            "zone": "https://www.googleapis.com/compute/v1/projects/p/zones/asia-south2-a"}


@pytest.mark.asyncio
async def test_identity_probe_uses_only_pinned_read_targets(monkeypatch):
    calls = []
    monkeypatch.setattr(gce_identity, "_protected", lambda path, **kwargs: path)
    monkeypatch.setattr(Path, "is_file", lambda self: True)
    monkeypatch.setattr(Path, "is_dir", lambda self: True)

    def run(args, **kwargs):
        calls.append(args)
        payload = instance() if "instances" in args else disk()
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload).encode())

    monkeypatch.setattr(gce_identity.subprocess, "run", run)
    found = await gce_identity.read_existing_delhi_vm(host())
    assert found.instance_id == "822115653245959101"
    assert found.machine_type == "e2-standard-4"
    assert found.disk_gib == 100
    assert found.created_at.isoformat() == "2026-09-19T17:24:34.872000+00:00"
    assert len(calls) == 2
    assert all("describe" in args and "--project=project-ee0c1944-144c-4364-ad4" in args
               for args in calls)
    assert all("create" not in args and "delete" not in args for args in calls)


@pytest.mark.asyncio
async def test_identity_probe_surfaces_wrong_disk_and_sanitizes_cli_errors(monkeypatch):
    monkeypatch.setattr(gce_identity, "_protected", lambda path, **kwargs: path)
    monkeypatch.setattr(Path, "is_file", lambda self: True)
    monkeypatch.setattr(Path, "is_dir", lambda self: True)

    def wrong_disk(args, **kwargs):
        payload = instance() if "instances" in args else {**disk(), "sizeGb": "200"}
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload).encode())

    monkeypatch.setattr(gce_identity.subprocess, "run", wrong_disk)
    assert (await gce_identity.read_existing_delhi_vm(host())).disk_gib == 200

    def failed(*args, **kwargs):
        return SimpleNamespace(returncode=1, stdout=b"", stderr=b"sensitive provider detail")

    monkeypatch.setattr(gce_identity.subprocess, "run", failed)
    with pytest.raises(gce_identity.GCEIdentityError, match="^gce_read_unavailable$"):
        await gce_identity.read_existing_delhi_vm(host())


@pytest.mark.asyncio
async def test_identity_probe_rejects_nonapproved_target_before_cli(monkeypatch):
    monkeypatch.setattr(gce_identity, "_protected", lambda *args, **kwargs: pytest.fail("CLI used"))
    with pytest.raises(gce_identity.GCEIdentityError, match="unexpected_operator_target"):
        await gce_identity.read_existing_delhi_vm(host().model_copy(update={"instance_id": "1"}))
