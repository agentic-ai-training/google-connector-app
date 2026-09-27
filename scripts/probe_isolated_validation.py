"""Harmless live launcher proof on the provisioned Linux host; no customer source."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import tempfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--launcher", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--diagnostics", action="store_true")
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location("gca_isolated_validation", args.launcher)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if args.diagnostics:
        # Only this synthetic operator probe may expose Docker diagnostics. Never
        # enable this wrapper around private customer source or image configuration.
        original_run = module.subprocess.run

        def diagnostic_run(*values, **kwargs):
            result = original_run(*values, **kwargs)
            if result.returncode:
                print(result.stderr.decode("utf-8", errors="replace")[:2000])
            return result

        module.subprocess.run = diagnostic_run
    root = Path("/var/lib/gca-validation-probes")
    root.mkdir(mode=0o700, exist_ok=True)
    os.chmod(root, 0o700)
    with tempfile.TemporaryDirectory(dir=root) as directory:
        base = Path(directory)
        workspace = base / "workspace"
        (workspace / "app").mkdir(parents=True)
        (workspace / "scripts").mkdir()
        source = workspace / "app/main.py"
        source.write_text("answer = 42\n")
        config = base / "operator-policy.json"
        config.write_text(json.dumps({
            "version": 1, "runtime": "runsc", "images": {"python_compile": args.image},
        }))
        config.chmod(0o600)
        policy = module.load_policy(config, workspace)
        valid = module.validate(workspace, "python_compile", 20, policy)
        assert valid["success"] is True and valid["exit_code"] == 0
        assert not (workspace / "app/__pycache__").exists()
        source.write_text("def invalid(:\n")
        invalid = module.validate(workspace, "python_compile", 20, policy)
        assert invalid["success"] is False and invalid["exit_code"] != 0
        assert source.read_text() == "def invalid(:\n"
        print(json.dumps({
            "valid_source_passed": True, "syntax_error_failed": True,
            "host_workspace_unchanged_by_validation": True,
            "validation_containers_removed": True,
            "isolation": valid["isolation"], "image_id": args.image,
            "private_source_used": False, "production_worker_activated": False,
        }, sort_keys=True))


if __name__ == "__main__":
    main()
