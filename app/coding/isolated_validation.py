#!/usr/bin/env python3
"""Trusted, keyless Linux validation launcher. Not a model-selectable shell tool.

Only this controller talks to Docker. Workloads receive an ephemeral source copy,
never Docker access, host mounts, credentials, network, or deployment authority.
Toolchain images must be prebuilt, reviewed, and pinned by the operator.
"""
from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess  # nosec B404
import tarfile
import time
import uuid


PROFILES = {
    "python_compile": (".", ("python3", "-I", "-m", "compileall", "-q", "app", "scripts")),
    "python_unit": (".", ("python3", "-m", "pytest", "tests", "-q")),
    "python_lint": (".", ("flake8", "app", "--max-line-length=100")),
    "web_lint": ("web", ("npm", "run", "lint")),
    "web_build": ("web", ("npm", "run", "build")),
    "flutter_analyze": ("mobile", ("flutter", "analyze")),
    "flutter_test": ("mobile", ("flutter", "test")),
    "rust_format": ("coding_runtime", ("cargo", "fmt", "--check")),
    "rust_test": ("coding_runtime", ("cargo", "test", "--locked", "--offline")),
}
EXCLUDED_DIRS = {
    ".git", ".gca-local", ".ssh", ".aws", ".config", ".codex", ".venv", "venv",
    "node_modules", "target", "__pycache__", ".pytest_cache", ".next",
}
MAX_SOURCE_BYTES = 32 * 1024 * 1024
MAX_SOURCE_ENTRIES = 5000
MAX_OUTPUT_BYTES = 256_000
DOCKER = "/usr/bin/docker"
CLEAN_ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/nonexistent", "CI": "true"}


class IsolationError(RuntimeError):
    pass


def sensitive_name(name: str) -> bool:
    return (
        name.startswith(".env") or name.endswith((".pem", ".key", ".p12"))
        or name.lower() in {"credentials.json", "token.json", "google-oauth-web.json"}
    )


def load_policy(path: Path, workspace: Path) -> dict:
    path = path.resolve(strict=True)
    if path.is_relative_to(workspace):
        raise IsolationError("Operator policy must be outside the workspace")
    for item in (path, *path.parents):
        info = item.stat()
        if info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o022:
            raise IsolationError("Operator policy path is writable by an untrusted identity")
    if not path.is_file() or path.stat().st_size > 32768:
        raise IsolationError("Invalid operator policy file")
    policy = json.loads(path.read_text())
    if set(policy) != {"version", "runtime", "images"}:
        raise IsolationError("Unexpected operator policy fields")
    if policy["version"] != 1 or policy["runtime"] != "runsc":
        raise IsolationError("Managed execution requires the runsc policy")
    if not isinstance(policy["images"], dict):
        raise IsolationError("Invalid image registry")
    for profile, image in policy["images"].items():
        if profile not in PROFILES or not isinstance(image, str) or not re.fullmatch(
            r"sha256:[a-f0-9]{64}", image,
        ):
            raise IsolationError("Profiles require immutable, local image IDs")
    return policy


def source_archive(root: Path) -> bytes:
    """Copy a controller-owned snapshot; reject links/special files, bound all bytes.

    Known credential paths are excluded, not a promise to detect every secret embedded
    in source. Source authorization and secret scanning remain upstream requirements.
    The controller must not run any code or allow concurrent writers to this snapshot.
    """
    output = io.BytesIO()
    count = 0
    total = 0
    with tarfile.open(fileobj=output, mode="w") as archive:
        for directory, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = sorted(
                name for name in dirs if name not in EXCLUDED_DIRS and not sensitive_name(name)
            )
            for name in [*dirs, *sorted(files)]:
                if sensitive_name(name):
                    continue
                path = Path(directory) / name
                info = path.lstat()
                count += 1
                if count > MAX_SOURCE_ENTRIES:
                    raise IsolationError("Source entry limit exceeded")
                if stat.S_ISLNK(info.st_mode) or not (
                    stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)
                ):
                    raise IsolationError("Source links and special files are forbidden")
                item = tarfile.TarInfo(path.relative_to(root).as_posix())
                item.uid = item.gid = 65532
                item.mode = 0o755 if info.st_mode & 0o100 else 0o644
                if stat.S_ISDIR(info.st_mode):
                    item.type = tarfile.DIRTYPE
                    item.mode = 0o755
                    archive.addfile(item)
                else:
                    if info.st_nlink != 1:
                        raise IsolationError("Hard-linked source is forbidden")
                    total += info.st_size
                    if total > MAX_SOURCE_BYTES:
                        raise IsolationError("Source byte limit exceeded")
                    with path.open("rb") as stream:
                        opened = os.fstat(stream.fileno())
                        if (opened.st_ino, opened.st_dev, opened.st_size) != (
                            info.st_ino, info.st_dev, info.st_size,
                        ):
                            raise IsolationError("Source changed during snapshot")
                        item.size = info.st_size
                        archive.addfile(item, stream)
    return output.getvalue()


def docker(*args: str, payload: bytes | None = None, timeout: int = 30) -> bytes:
    result = subprocess.run(  # nosec B603
        [DOCKER, *args], input=payload, capture_output=True, check=False,
        timeout=timeout, env=CLEAN_ENV,
    )
    if result.returncode:
        # Docker errors can contain paths or image configuration: do not echo stderr.
        raise IsolationError(f"Docker {args[0]} failed; inspect trusted host logs")
    if len(result.stdout) > 1_048_576:
        raise IsolationError("Docker control response exceeded bounds")
    return result.stdout


def execute_bounded(args: list[str], timeout: int) -> dict:
    child = subprocess.Popen(  # nosec B603
        [DOCKER, *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, env=CLEAN_ENV, start_new_session=True,
    )
    output = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + timeout
    truncated = False
    try:
        with selectors.DefaultSelector() as selector:
            for name in output:
                selector.register(getattr(child, name), selectors.EVENT_READ, name)
            while selector.get_map():
                if time.monotonic() >= deadline:
                    raise IsolationError("Validation deadline exceeded")
                for key, _ in selector.select(timeout=0.1):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    remaining = MAX_OUTPUT_BYTES - sum(map(len, output.values()))
                    output[key.data].extend(chunk[:remaining])
                    truncated = truncated or len(chunk) > remaining
            code = child.wait(timeout=max(0.1, deadline - time.monotonic()))
        return {
            "exit_code": code, "success": code == 0, "output_truncated": truncated,
            **{key: value.decode("utf-8", errors="replace") for key, value in output.items()},
        }
    finally:
        if child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait(timeout=5)
        for stream in (child.stdout, child.stderr):
            stream.close()


def validate(workspace: Path, profile: str, timeout: int, policy: dict) -> dict:
    if profile not in PROFILES or profile not in policy["images"]:
        raise IsolationError("No approved offline toolchain for this validation profile")
    if not 1 <= timeout <= 1800:
        raise IsolationError("Validation timeout must be within 1..1800 seconds")
    root = workspace.resolve(strict=True)
    relative_cwd, command = PROFILES[profile]
    if not (root / relative_cwd).is_dir():
        raise IsolationError("Validation working directory is missing")
    source = source_archive(root)
    if profile == "python_compile":
        with tarfile.open(fileobj=io.BytesIO(source)) as archive:
            if not any(
                name.startswith(("app/", "scripts/")) and name.endswith(".py")
                for name in archive.getnames()
            ):
                raise IsolationError("No Python source exists for the compile profile")
    name = "gca-validation-" + uuid.uuid4().hex
    # Set intent before create: a lost response still requires cleanup of this exact ID.
    try:
        docker(
            "create", "--name", name, "--pull=never", "--runtime=runsc",
            "--label=gca.purpose=validation", "--network=none", "--read-only",
            "--cap-drop=ALL", "--security-opt=no-new-privileges:true",
            "--user=65532:65532", "--memory=1g", "--memory-swap=1g",
            "--cpus=1", "--pids-limit=128", "--ulimit=nofile=256:256",
            "--tmpfs=/workspace:rw,nosuid,nodev,size=256m,mode=1777",
            "--tmpfs=/tmp:rw,nosuid,nodev,size=64m,mode=1777",
            "--env=HOME=/tmp", "--env=CI=true", "--log-driver=none",
            "--entrypoint=/bin/sh", policy["images"][profile], "-ec",
            f"exec sleep {timeout + 60}",
        )
        spec = json.loads(docker("inspect", name))[0]
        if any(mount.get("Type") in {"bind", "volume"} for mount in spec["Mounts"]):
            raise IsolationError("Toolchain image requested a forbidden persistent mount")
        docker("start", name)
        # Docker cp cannot resolve runsc's sandbox-internal tmpfs directory. Extract
        # our link-free bounded archive *inside* the sandbox, as the unprivileged UID.
        docker(
            "exec", "-i", "--workdir", "/workspace", name,
            "tar", "--no-same-owner", "--no-same-permissions", "-xf", "-",
            payload=source,
        )
        result = execute_bounded([
            "exec", "--workdir", "/workspace" + (
                "" if relative_cwd == "." else "/" + relative_cwd
            ), name, *command,
        ], timeout)
        return {
            **result, "profile": profile, "isolation": "gvisor",
            "image_id": policy["images"][profile], "network": "none",
            "workspace_mutations_discarded": True,
        }
    finally:
        # Failure here overrides success. The caller must record cleanup uncertainty.
        try:
            docker("rm", "--force", "--volumes", name)
        except (IsolationError, subprocess.TimeoutExpired) as error:
            raise IsolationError(f"Sandbox cleanup uncertain for {name}") from error


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--profile", choices=tuple(PROFILES), required=True)
    parser.add_argument("--timeout", type=int, required=True)
    args = parser.parse_args()
    try:
        config = Path(os.environ["GCA_VALIDATION_CONFIG"])
        workspace = args.workspace.resolve(strict=True)
        result = validate(workspace, args.profile, args.timeout, load_policy(config, workspace))
        print(json.dumps(result))
        return 0
    except (IsolationError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        # Do not expose input paths, provider secrets, or subprocess output.
        message = str(error) if isinstance(error, IsolationError) else "Isolation setup failed"
        print(json.dumps({"success": False, "error": message, "isolation": "unavailable"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
