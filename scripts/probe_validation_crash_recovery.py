"""Operator fault-injection proof; uses only a synthetic disposable job namespace."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def load(path):
    spec = importlib.util.spec_from_file_location("gca_crash_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--launcher", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--crash-child", type=Path)
    args = parser.parse_args()
    runner = load(args.launcher)
    if args.crash_child:
        base = args.crash_child
        policy = runner.load_policy(base / "policy.json", base / "workspace")
        # Abrupt process loss at the execution boundary bypasses all finally blocks.
        runner.execute_bounded = lambda *unused: os._exit(97)
        runner.validate(base / "workspace", "python_compile", 20, policy)
        raise RuntimeError("Crash injection did not execute")
    root = Path("/var/lib/gca-validation-probes")
    root.mkdir(mode=0o700, exist_ok=True)
    base = Path(tempfile.mkdtemp(dir=root))
    workspace = base / "workspace"
    state = base / "journal"
    state.mkdir(mode=0o700)
    (workspace / "app").mkdir(parents=True)
    (workspace / "scripts").mkdir()
    (workspace / "app/main.py").write_text("answer = 42\n")
    config = base / "policy.json"
    config.write_text(json.dumps({
        "version": 2, "runtime": "runsc", "state_directory": str(state),
        "images": {"python_compile": args.image},
    }))
    config.chmod(0o600)
    policy = runner.load_policy(config, workspace)
    passed = False
    try:
        child = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--launcher", str(args.launcher),
             "--image", args.image, "--crash-child", str(base)],
            timeout=60, check=False, capture_output=True, env=runner.CLEAN_ENV,
        )
        assert child.returncode == 97, "Child failed before the intended crash boundary"
        with runner.Journal(state) as journal:
            rows = journal.db.execute("SELECT * FROM jobs").fetchall()
            assert len(rows) == 1 and rows[0]["state"] == "running"
            job = dict(rows[0])
            data = json.loads(runner.docker("inspect", job["name"]))[0]
            assert data["State"]["Running"] is True
        try:
            runner.validate(workspace, "python_compile", 20, policy)
        except runner.IsolationError as error:
            assert "unreconciled" in str(error)
        else:
            raise AssertionError("Uncertain job did not retain the slot")
        with runner.Journal(state) as journal:
            # Advance only this test journal's deadline, not the host clock.
            with journal.db:
                journal.db.execute("UPDATE jobs SET deadline=0 WHERE id=?", (job["id"],))
            result = journal.reap()
            assert result["cleaned_job_ids"] == [job["id"]]
            assert not result["uncertain_job_ids"]
        retried = runner.validate(workspace, "python_compile", 20, policy)
        assert retried["success"] is True and retried["cleanup_verified"] is True
        passed = True
        print(json.dumps({
            "abrupt_process_exit": 97, "running_container_survived_controller_crash": True,
            "durable_intent_survived": True, "unreconciled_slot_blocked_new_work": True,
            "expired_owned_container_removed": True, "subsequent_validation_passed": True,
            "private_source_used": False, "production_worker_activated": False,
        }, sort_keys=True))
    finally:
        # Never delete the only recovery record if cleanup is uncertain.
        with runner.Journal(state) as journal:
            with journal.db:
                journal.db.execute("UPDATE jobs SET deadline=0 WHERE state<>'cleaned'")
            cleanup = journal.reap()
        if cleanup["uncertain_job_ids"] or cleanup["untracked_container_count"]:
            raise RuntimeError(f"Probe cleanup uncertain; retained operator evidence at {base}")
        if passed:
            shutil.rmtree(base)
        else:
            print(f"Retained failed probe recovery evidence at {base}")


if __name__ == "__main__":
    main()
