"""Read-only, fixed-target GCE identity probe for importing the existing Delhi VM."""
from datetime import datetime, timezone
import json
import subprocess  # nosec B404

from pydantic import BaseModel, ConfigDict, ValidationError

from app.coding.validation_transport import IAPValidationHost, _protected


class GCEIdentityError(RuntimeError):
    """Sanitized operator probe failure; do not log CLI stdout/stderr."""


class VerifiedExistingVM(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    project_id: str
    instance_id: str
    instance_name: str
    zone: str
    machine_type: str
    disk_type: str
    disk_gib: int
    status: str
    created_at: datetime
    stop_at: datetime


async def read_existing_delhi_vm(host: IAPValidationHost) -> VerifiedExistingVM:
    """No provider write, billing claim or model-supplied GCE target."""
    from asyncio import to_thread

    if (host.project_id != "project-ee0c1944-144c-4364-ad4"
            or host.instance != "gca-coolify-delhi-01" or host.zone != "asia-south2-a"
            or host.instance_id != "822115653245959101"):
        raise GCEIdentityError("unexpected_operator_target")
    gcloud = _protected(host.gcloud_binary)
    config = _protected(host.gcloud_config_directory, private=True)
    if not gcloud.is_file() or not config.is_dir():
        raise GCEIdentityError("operator_cli_unavailable")
    env = {"HOME": "/nonexistent", "PATH": "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin",
           "CLOUDSDK_CONFIG": str(config), "CLOUDSDK_CORE_DISABLE_PROMPTS": "1"}

    def read(kind, name, fields):
        arguments = [str(gcloud), "--configuration=" + host.gcloud_configuration,
                     "compute", kind, "describe", name, "--zone=" + host.zone,
                     "--project=" + host.project_id, "--format=json(" + fields + ")"]
        try:
            response = subprocess.run(  # nosec B603
                arguments, env=env, capture_output=True, timeout=20, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise GCEIdentityError("gce_read_unavailable") from None
        if response.returncode or len(response.stdout) > 65536:
            raise GCEIdentityError("gce_read_unavailable")
        try:
            return json.loads(response.stdout)
        except (ValueError, UnicodeError, RecursionError):
            raise GCEIdentityError("gce_identity_invalid") from None

    instance = await to_thread(
        read, "instances", host.instance,
        "id,name,status,creationTimestamp,machineType,zone,disks,scheduling",
    )
    disk = await to_thread(read, "disks", host.instance, "name,sizeGb,type,status,zone")
    try:
        creation = datetime.fromisoformat(instance["creationTimestamp"].replace("Z", "+00:00"))
        stop = datetime.fromisoformat(instance["scheduling"]["terminationTime"].replace(
            "Z", "+00:00",
        ))
        attachments = instance["disks"]
        if (len(attachments) != 1 or attachments[0]["boot"] is not True
                or not attachments[0]["source"].endswith("/disks/" + host.instance)
                or disk["name"] != host.instance or disk["status"] != "READY"):
            raise ValueError("disk identity")
        verified = VerifiedExistingVM(
            project_id=host.project_id, instance_id=str(instance["id"]),
            instance_name=instance["name"], zone=host.zone,
            machine_type=instance["machineType"].rsplit("/", 1)[-1],
            disk_type=disk["type"].rsplit("/", 1)[-1],
            disk_gib=int(disk["sizeGb"]), status=instance["status"],
            created_at=creation.astimezone(timezone.utc), stop_at=stop.astimezone(timezone.utc),
        )
        if (not instance["zone"].endswith("/zones/" + host.zone)
                or not disk["zone"].endswith("/zones/" + host.zone)
                or not instance["machineType"].endswith("/machineTypes/e2-standard-4")):
            raise ValueError("compute identity")
        return verified
    except (KeyError, TypeError, ValueError, IndexError, AttributeError, ValidationError):
        raise GCEIdentityError("gce_identity_mismatch") from None
