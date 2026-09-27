#!/usr/bin/env python3
"""Trusted, keyless Linux validation launcher. Not a model-selectable shell tool.

Only this controller talks to Docker. Workloads receive an ephemeral source copy,
never Docker access, host mounts, credentials, network, or deployment authority.
Toolchain images must be prebuilt, reviewed, and pinned by the operator.
"""
from __future__ import annotations

import argparse
import fcntl
import io
import json
import os
from pathlib import Path
import re
import selectors
import signal
import sqlite3
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


def load_policy(path: Path, workspace: Path | None) -> dict:
    path = path.resolve(strict=True)
    if workspace is not None and path.is_relative_to(workspace):
        raise IsolationError("Operator policy must be outside the workspace")
    for item in (path, *path.parents):
        info = item.stat()
        if info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o022:
            raise IsolationError("Operator policy path is writable by an untrusted identity")
    if not path.is_file() or path.stat().st_size > 32768:
        raise IsolationError("Invalid operator policy file")
    policy = json.loads(path.read_text())
    if not isinstance(policy, dict) or set(policy) != {
        "version", "runtime", "images", "state_directory",
    }:
        raise IsolationError("Unexpected operator policy fields")
    if policy["version"] != 2 or policy["runtime"] != "runsc":
        raise IsolationError("Managed execution requires the runsc policy")
    if not isinstance(policy["images"], dict):
        raise IsolationError("Invalid image registry")
    for profile, image in policy["images"].items():
        if profile not in PROFILES or not isinstance(image, str) or not re.fullmatch(
            r"sha256:[a-f0-9]{64}", image,
        ):
            raise IsolationError("Profiles require immutable, local image IDs")
    if not isinstance(policy["state_directory"], str):
        raise IsolationError("Invalid journal directory")
    state = Path(policy["state_directory"]).resolve(strict=True)
    if workspace is not None and (state.is_relative_to(workspace)
                                  or workspace.is_relative_to(state)):
        raise IsolationError("Journal and workspace must have separate directory trees")
    for item in (state, *state.parents):
        info = item.stat()
        if info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o022:
            raise IsolationError("Journal directory is not operator protected")
    if not state.is_dir() or state.stat().st_mode & 0o077:
        raise IsolationError("Journal directory must have mode 0700")
    policy["state_directory"] = str(state)
    return policy


class Journal:
    """Single host slot with crash-persistent intent, separate from financial authority.

    A live process holds flock. A dead process releases flock but not its SQLite row.
    A row with uncertain cleanup still occupies the slot until reconciled.
    """

    def __init__(self, directory: Path):
        self.directory = directory
        self.lock = None
        self.db = None

    def __enter__(self):
        self.lock = os.open(self.directory / "slot.lock", os.O_CREAT | os.O_RDWR
                            | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            database = self.directory / "jobs.sqlite3"
            fd = os.open(database, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            self.db = sqlite3.connect(database, timeout=5)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs(
                    id TEXT PRIMARY KEY,name TEXT NOT NULL UNIQUE,image TEXT NOT NULL,
                    profile TEXT NOT NULL,deadline INTEGER NOT NULL,state TEXT NOT NULL,
                    container_id TEXT,updated_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS events(
                    sequence INTEGER PRIMARY KEY,job_id TEXT NOT NULL,
                    state TEXT NOT NULL,observed_at INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS jobs_deadline ON jobs(state,deadline);
            """)
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO metadata VALUES('scope',?)", (uuid.uuid4().hex,))
            self.scope = self.db.execute("SELECT value FROM metadata WHERE key='scope'").fetchone()[0]
            if not re.fullmatch(r"[a-f0-9]{32}", self.scope):
                raise IsolationError("Journal host identity is invalid")
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        if self.db:
            self.db.close()
        if self.lock is not None:
            os.close(self.lock)
        self.db = self.lock = None

    def begin(self, profile: str, image: str, timeout: int) -> dict:
        if self.db.execute("SELECT 1 FROM jobs WHERE state<>'cleaned' LIMIT 1").fetchone():
            raise IsolationError("Validation slot held by an unreconciled job")
        job_id = uuid.uuid4().hex
        now = int(time.time())
        with self.db:
            self.db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,'creating',NULL,?)",
                            (job_id, "gca-validation-" + job_id, image, profile,
                             now + timeout + 90, now))
            self.db.execute("INSERT INTO events(job_id,state,observed_at) VALUES(?,?,?)",
                            (job_id, "creating", now))
        return dict(self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def record(self, job: dict, state: str, container_id: str | None = None):
        if state not in {"running", "cleanup_uncertain", "cleaned"}:
            raise IsolationError("Invalid journal transition")
        now = int(time.time())
        with self.db:
            self.db.execute("UPDATE jobs SET state=?,container_id=COALESCE(?,container_id),"
                            "updated_at=? WHERE id=?", (state, container_id, now, job["id"]))
            self.db.execute("INSERT INTO events(job_id,state,observed_at) VALUES(?,?,?)",
                            (job["id"], state, now))
        job["state"] = state
        if container_id:
            job["container_id"] = container_id

    def cleanup(self, job: dict, *, create_confirmed: bool = False):
        ids = docker("ps", "-a", "--no-trunc", "--filter", f"name=^/{job['name']}$",
                     "--format", "{{.ID}}").decode().split()
        if not ids:
            if not create_confirmed and int(time.time()) < job["deadline"]:
                raise IsolationError("Create outcome remains uncertain until the job deadline")
        else:
            if len(ids) != 1 or not re.fullmatch(r"[a-f0-9]{64}", ids[0]):
                raise IsolationError("Ambiguous sandbox identity")
            self.verify_identity(job, ids[0])
            docker("rm", "--force", "--volumes", ids[0])
        self.record(job, "cleaned")

    def verify_identity(self, job: dict, container_id: str):
        if job.get("container_id") and job["container_id"] != container_id:
            raise IsolationError("Container ID changed; refusing cleanup")
        spec = json.loads(docker("inspect", container_id))[0]
        labels = spec["Config"].get("Labels") or {}
        if (spec["Name"] != "/" + job["name"] or spec["Image"] != job["image"]
                or labels.get("gca.scope") != self.scope
                or labels.get("gca.job") != job["id"]
                or labels.get("gca.purpose") != "validation"):
            raise IsolationError("Sandbox identity mismatch; refusing cleanup")

    def reap(self) -> dict:
        # Include late-created containers even if an earlier absence was reconciled.
        observed = docker("ps", "-a", "--no-trunc", "--filter", f"label=gca.scope={self.scope}",
                          "--format", '{{.ID}} {{.Label "gca.job"}}').decode().splitlines()
        if len(observed) > 128:
            raise IsolationError("Reaper inventory exceeds bounded batch")
        jobs = {row["id"]: dict(row) for row in self.db.execute(
            "SELECT * FROM jobs WHERE state<>'cleaned' AND deadline<=? LIMIT 128",
            (int(time.time()),),
        )}
        untracked = 0
        uncertain = set()
        for observation in observed:
            parts = observation.split()
            if len(parts) != 2 or not re.fullmatch(r"[a-f0-9]{64}", parts[0]):
                untracked += 1
                continue
            container_id, job_id = parts
            row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                untracked += 1
            if row and row["deadline"] <= int(time.time()):
                job = dict(row)
                try:
                    self.verify_identity(job, container_id)
                    if job_id not in uncertain:
                        jobs[job_id] = job
                except (IsolationError, subprocess.TimeoutExpired, ValueError, KeyError):
                    self.record(job, "cleanup_uncertain")
                    uncertain.add(job_id)
                    jobs.pop(job_id, None)
        cleaned = []
        for job in jobs.values():
            try:
                self.cleanup(job)
                cleaned.append(job["id"])
            except (IsolationError, subprocess.TimeoutExpired, ValueError, KeyError):
                self.record(job, "cleanup_uncertain")
                uncertain.add(job["id"])
        return {"cleaned_job_ids": cleaned, "uncertain_job_ids": sorted(uncertain),
                "untracked_container_count": untracked}


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
    with Journal(Path(policy["state_directory"])) as journal:
        return _validate(workspace, profile, timeout, policy, journal)


def _validate(workspace: Path, profile: str, timeout: int, policy: dict, journal: Journal) -> dict:
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
    job = journal.begin(profile, policy["images"][profile], timeout)
    name = job["name"]
    create_confirmed = False
    # Set intent before create: a lost response still requires cleanup of this exact ID.
    try:
        container_id = docker(
            "create", "--name", name, "--pull=never", "--runtime=runsc",
            "--label=gca.purpose=validation", "--network=none", "--read-only",
            f"--label=gca.scope={journal.scope}", f"--label=gca.job={job['id']}",
            "--cap-drop=ALL", "--security-opt=no-new-privileges:true",
            "--user=65532:65532", "--memory=1g", "--memory-swap=1g",
            "--cpus=1", "--pids-limit=128", "--ulimit=nofile=256:256",
            "--tmpfs=/workspace:rw,nosuid,nodev,size=256m,mode=1777",
            "--tmpfs=/tmp:rw,nosuid,nodev,size=64m,mode=1777",
            "--env=HOME=/tmp", "--env=CI=true", "--log-driver=none",
            "--entrypoint=/bin/sh", policy["images"][profile], "-ec",
            f'remaining=$(({job["deadline"]} - $(date +%s))); '
            'test "$remaining" -gt 0; exec sleep "$remaining"',
        ).decode().strip()
        if not re.fullmatch(r"[a-f0-9]{64}", container_id):
            raise IsolationError("Invalid container creation evidence")
        create_confirmed = True
        spec = json.loads(docker("inspect", name))[0]
        if any(mount.get("Type") in {"bind", "volume"} for mount in spec["Mounts"]):
            raise IsolationError("Toolchain image requested a forbidden persistent mount")
        docker("start", name)
        journal.record(job, "running", container_id)
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
            "validation_job_id": job["id"], "cleanup_verified": True,
        }
    finally:
        # Failure here overrides success. The caller must record cleanup uncertainty.
        try:
            journal.cleanup(job, create_confirmed=create_confirmed)
        except (IsolationError, subprocess.TimeoutExpired, ValueError, KeyError) as error:
            journal.record(job, "cleanup_uncertain")
            raise IsolationError(f"Sandbox cleanup uncertain for {name}") from error


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--profile", choices=tuple(PROFILES))
    parser.add_argument("--timeout", type=int)
    parser.add_argument("--reap", action="store_true")
    args = parser.parse_args()
    try:
        config = Path(os.environ["GCA_VALIDATION_CONFIG"])
        if args.reap:
            if args.workspace or args.profile or args.timeout:
                raise IsolationError("Reaper cannot accept a validation request")
            policy = load_policy(config, None)
            try:
                with Journal(Path(policy["state_directory"])) as journal:
                    result = journal.reap()
            except BlockingIOError:
                print(json.dumps({"deferred": "active_validation"}))
                return 0
            print(json.dumps(result))
            return 2 if result["uncertain_job_ids"] or result["untracked_container_count"] else 0
        if args.workspace is None or args.profile is None or args.timeout is None:
            raise IsolationError("Validation requires workspace, profile and timeout")
        workspace = args.workspace.resolve(strict=True)
        result = validate(workspace, args.profile, args.timeout, load_policy(config, workspace))
        print(json.dumps(result))
        return 0
    except (IsolationError, OSError, ValueError, KeyError, sqlite3.Error,
            subprocess.TimeoutExpired) as error:
        # Do not expose input paths, provider secrets, or subprocess output.
        message = str(error) if isinstance(error, IsolationError) else "Isolation setup failed"
        print(json.dumps({"success": False, "error": message, "isolation": "unavailable"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
