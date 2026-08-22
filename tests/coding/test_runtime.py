import json
import subprocess
from pathlib import Path

import pytest

from app.coding.runtime import CodingRuntime, CodingRuntimeError


def test_runtime_rejects_missing_binary(tmp_path):
    runtime = CodingRuntime(tmp_path, binary=tmp_path / "missing")
    with pytest.raises(CodingRuntimeError, match="binary is unavailable"):
        runtime.invoke({"tool": "git_status"})


def test_runtime_rejects_untyped_request(tmp_path):
    runtime = CodingRuntime(tmp_path, binary=tmp_path / "missing")
    with pytest.raises(CodingRuntimeError, match="typed tool"):
        runtime.invoke({"path": "app"})


def test_runtime_invokes_typed_protocol(tmp_path, monkeypatch):
    binary = tmp_path / "runtime"
    binary.write_text("placeholder")
    binary.chmod(0o755)

    def fake_run(arguments, **kwargs):
        assert arguments == [str(binary), "--root", str(tmp_path)]
        assert json.loads(kwargs["input"]) == {"tool": "git_status"}
        assert "GROQ_API_KEY" not in kwargs["env"]
        return subprocess.CompletedProcess(arguments, 0, '{"ok":true,"result":{}}', "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    response = CodingRuntime(tmp_path, binary=binary).invoke({"tool": "git_status"})
    assert response["ok"] is True
