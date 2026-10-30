"""Operator-only synthetic transport proof. Does not activate production admission."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import time
from uuid import uuid4

from app.coding import isolated_validation as isolation
from app.coding.validation_transport import (
    IAPValidationHost, ValidationTransportError, _protected, dispatch, observe,
)


async def run(config: Path, image: str):
    config = _protected(config, private=True)
    if config.stat().st_size > 16384:
        raise ValueError("operator config exceeds bound")
    host = IAPValidationHost.model_validate_json(config.read_text())
    claim_id = str(uuid4())
    assert (await observe(host, claim_id))["cleanup_verified"] is False
    with tempfile.TemporaryDirectory(prefix="gca-synthetic-transport-") as directory:
        workspace = Path(directory)
        (workspace / "app").mkdir()
        (workspace / "scripts").mkdir()
        (workspace / "app/main.py").write_text("answer = 42\n")
        source = isolation.source_archive(workspace)
        envelope = {
            "version": 1, "claim_id": claim_id, "run_id": str(uuid4()), "sequence_no": 0,
            "request_hash": "a" * 64, "target_hash": "b" * 64,
            "server_uuid": host.server_uuid, "source_hash": hashlib.sha256(source).hexdigest(),
            "image_id": image, "profile": "python_compile", "timeout_seconds": 20,
            "deadline": int(time.time()) + 180,
        }
        result = await dispatch(host, envelope, source)
        assert result["success"] and result["cleanup_verified"]
        evidence = await observe(host, claim_id)
        assert evidence["cleanup_verified"] and evidence["validation_success"] is None
        assert evidence["validation_job_id"] == result["validation_job_id"]
        try:
            await dispatch(host, envelope, source)
        except ValidationTransportError:
            pass
        else:
            raise AssertionError("Remote host accepted replay")
    print(json.dumps({
        "iap_ssh_host_pin_verified": True, "synthetic_compile_passed": True,
        "remote_replay_rejected": True, "cleanup_observation_bound": True,
        "private_source_used": False, "production_admission_activated": False,
    }, sort_keys=True))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--operator-config", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--confirm-synthetic-operator-probe", action="store_true", required=True)
    args = parser.parse_args()
    asyncio.run(run(args.operator_config, args.image))


if __name__ == "__main__":
    main()
