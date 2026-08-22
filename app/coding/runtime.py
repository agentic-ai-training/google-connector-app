"""Subprocess boundary for the Rust coding-tool broker.

The Python orchestrator can select a typed tool. It cannot pass a shell command, inherit
application secrets, or reinterpret a failed command as success.
"""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404
import tempfile
from pathlib import Path
from typing import Any


class CodingRuntimeError(RuntimeError):
    """A protocol, policy, or deterministic tool failure."""


class CodingRuntime:
    def __init__(
        self,
        workspace: Path,
        *,
        binary: Path | None = None,
        timeout_seconds: int = 310,
    ) -> None:
        self.workspace = workspace.resolve(strict=True)
        self.binary = (
            binary
            or Path(os.environ.get("CODING_RUNTIME_BINARY", "coding_runtime/target/release/google-connector-coding-runtime"))
        ).resolve()
        self.timeout_seconds = timeout_seconds

    def invoke(self, request: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(request, dict) or not isinstance(request.get("tool"), str):
            raise CodingRuntimeError("coding runtime request must contain a typed tool")
        if not self.binary.is_file():
            raise CodingRuntimeError(f"coding runtime binary is unavailable: {self.binary}")
        with tempfile.TemporaryDirectory(prefix="coding-runtime-home-") as clean_home:
            clean_env = {
                "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                "HOME": clean_home,
                "CI": "true",
            }
            try:
                completed = subprocess.run(  # nosec B603
                    [str(self.binary), "--root", str(self.workspace)],
                    input=json.dumps(request),
                    text=True,
                    capture_output=True,
                    timeout=self.timeout_seconds,
                    check=False,
                    env=clean_env,
                )
            except subprocess.TimeoutExpired as exc:
                raise CodingRuntimeError("coding runtime exceeded its process deadline") from exc
        try:
            response = json.loads(completed.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise CodingRuntimeError("coding runtime returned an invalid response") from exc
        if completed.returncode not in {0, 2}:
            raise CodingRuntimeError("coding runtime terminated unexpectedly")
        if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
            raise CodingRuntimeError("coding runtime response violated its protocol")
        return response
