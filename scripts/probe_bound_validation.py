"""Synthetic operator proof of central-claim host binding; not production admission."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
from uuid import uuid4


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--launcher", type=Path, required=True)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location("gca_isolated_validation", args.launcher)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = Path("/var/lib/gca-validation-probes")
    root.mkdir(mode=0o700, exist_ok=True)
    os.chmod(root, 0o700)
    base = Path(tempfile.mkdtemp(dir=root))
    try:
        workspace = base / "workspace"
        (workspace / "app").mkdir(parents=True)
        (workspace / "scripts").mkdir()
        (workspace / "app/main.py").write_text("answer=42\n")
        state = base / "journal"
        state.mkdir(mode=0o700)
        spool = state / "dispatch"
        spool.mkdir(mode=0o700)
        config = base / "policy.json"
        config.write_text(json.dumps({
            "version": 3, "runtime": "runsc", "images": {"python_compile": args.image},
            "state_directory": str(state), "server_uuid": "synthetic-operator-probe",
        }))
        config.chmod(0o600)
        policy = module.load_policy(config, workspace)
        binding = {
            "version": 1, "claim_id": str(uuid4()), "run_id": str(uuid4()), "sequence_no": 0,
            "request_hash": "a" * 64, "target_hash": "b" * 64,
            "server_uuid": "synthetic-operator-probe",
            "source_hash": hashlib.sha256(module.source_archive(workspace)).hexdigest(),
            "image_id": args.image, "profile": "python_compile", "timeout_seconds": 20,
            "deadline": int(time.time()) + 180,
        }
        path = spool / (binding["claim_id"] + ".json")
        path.write_text(json.dumps(binding))
        path.chmod(0o600)
        envelope = module.load_dispatch(binding["claim_id"], policy)
        for invalid in (None, {**envelope, "source_hash": "c" * 64}):
            try:
                module.validate(workspace, "python_compile", 20, policy, invalid)
            except module.IsolationError:
                pass
            else:
                raise AssertionError("Unbound or changed source unexpectedly executed")
        result = module.validate(workspace, "python_compile", 20, policy, envelope)
        assert result["success"] and result["cleanup_verified"]
        assert result["claim_id"] == binding["claim_id"]
        try:
            module.validate(workspace, "python_compile", 20, policy, envelope)
        except module.IsolationError as error:
            assert "already consumed" in str(error)
        else:
            raise AssertionError("Replay unexpectedly executed")
        with module.Journal(state) as journal:
            observed = journal.observe_claim(binding["claim_id"])
        assert observed["cleanup_verified"] is True
        assert observed["validation_success"] is None
        assert not (workspace / "app/__pycache__").exists()
        print(json.dumps({
            "bound_synthetic_compile_passed": True, "source_mismatch_rejected": True,
            "unbound_execution_rejected": True, "replay_rejected": True,
            "cleanup_observed": True, "lost_result_not_inferred_as_test_success": True,
            "production_admission_activated": False, "private_source_used": False,
        }, sort_keys=True))
    except BaseException:
        print(f"Retained failed probe recovery evidence at {base}")
        raise
    else:
        shutil.rmtree(base)


if __name__ == "__main__":
    main()
