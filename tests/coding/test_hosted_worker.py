import io
import json
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.coding import worker


def _zip(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return output.getvalue()


def test_safe_archive_extraction_denies_traversal(tmp_path):
    with pytest.raises(worker.CodingWorkerError, match="traversal"):
        worker._safe_extract_zip(_zip([("../secret", "no")]), tmp_path)
    assert not (tmp_path.parent / "secret").exists()


def test_hosted_plan_denies_infrastructure_and_migration_edits():
    for path in (".github/workflows/ci.yml", "Dockerfile", "migrations/versions/016.py"):
        with pytest.raises(worker.CodingWorkerError, match="cannot modify"):
            worker._validate_hosted_plan({
                "actions": [{"tool": "apply_exact_patch", "path": path}]
            })
    worker._validate_hosted_plan({
        "actions": [{"tool": "apply_exact_patch", "path": "app/safe.py"}]
    })
    with pytest.raises(worker.CodingWorkerError, match="cannot modify"):
        worker._validate_hosted_plan({
            "actions": [{
                "tool": "create_file", "path": ".github/workflows/unsafe.yml",
                "expected_absent": True, "content": "name: unsafe",
            }]
        })
    worker._validate_hosted_plan({
        "actions": [{
            "tool": "create_file", "path": "tests/test_safe.py",
            "expected_absent": True, "content": "def test_safe(): assert True",
        }]
    })


def test_hosted_planner_forwards_only_dedicated_key_to_local_runner(
    tmp_path, monkeypatch,
):
    binary = tmp_path / "gca-local"
    binary.write_text("placeholder")
    binary.chmod(0o755)
    settings = SimpleNamespace(coding_local_runner_binary=str(binary))
    monkeypatch.setattr(worker, "get_settings", lambda: settings)

    def fake_run(arguments, **kwargs):
        assert arguments[0] == str(binary)
        assert kwargs["env"]["CODING_GROQ_API_KEY"] == "dedicated"
        assert "GROQ_API_KEY" not in kwargs["env"]
        assert "DATABASE_URL" not in kwargs["env"]
        return subprocess.CompletedProcess(
            arguments, 0, json.dumps({"ok": True, "status": "planned"}), ""
        )

    monkeypatch.setattr(worker.subprocess, "run", fake_run)
    assert worker._run_local(["doctor"], coding_key="dedicated")["ok"] is True


def test_hosted_execution_child_receives_no_model_key(tmp_path, monkeypatch):
    binary = tmp_path / "gca-local"
    binary.write_text("placeholder")
    binary.chmod(0o755)
    settings = SimpleNamespace(coding_local_runner_binary=str(binary))
    monkeypatch.setattr(worker, "get_settings", lambda: settings)

    def fake_run(arguments, **kwargs):
        assert "CODING_GROQ_API_KEY" not in kwargs["env"]
        return subprocess.CompletedProcess(
            arguments, 0, json.dumps({"ok": True, "status": "applied"}), ""
        )

    monkeypatch.setattr(worker.subprocess, "run", fake_run)
    assert worker._run_local(["execute-plan"])["status"] == "applied"
