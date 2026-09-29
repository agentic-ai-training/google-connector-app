#!/usr/bin/env python3
"""Trusted, keyless Linux validation launcher. Not a model-selectable shell tool.

Only this controller talks to Docker. Workloads receive an ephemeral source copy,
never Docker access, host mounts, credentials, network, or deployment authority.
Toolchain images must be prebuilt, reviewed, and pinned by the operator.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
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
import sys
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
    # Generated-app convention: frontend = Vite/React, backend = Maven/Spring.
    # Both require operator-reviewed, dependency-complete OFFLINE toolchain images.
    "react_test": ("frontend", ("node_modules/.bin/vitest", "run",
                               "--passWithNoTests=false", "--allowOnly=false",
                               "--maxWorkers=1")),
    "react_build": ("frontend", ("node_modules/.bin/vite", "build")),
    "spring_test": ("backend", ("mvn", "-o", "-B", "-q",
                               "-Dmaven.repo.local=/opt/maven-repo",
                               "-DfailIfNoTests=true", "test")),
    "spring_package": ("backend", ("mvn", "-o", "-B", "-q",
                                  "-Dmaven.repo.local=/opt/maven-repo",
                                  "-DfailIfNoTests=true", "package")),
}
PREPARE_COMMANDS = {
    "react_test": ("npm", "ci", "--offline", "--ignore-scripts", "--no-audit", "--no-fund",
                   "--cache=/opt/npm-cache"),
    "react_build": ("npm", "ci", "--offline", "--ignore-scripts", "--no-audit", "--no-fund",
                    "--cache=/opt/npm-cache"),
}
EXCLUDED_DIRS = {
    ".git", ".gca-local", ".ssh", ".aws", ".config", ".codex", ".venv", "venv",
    "node_modules", "target", "dist", "__pycache__", ".pytest_cache", ".next",
}
MAX_SOURCE_BYTES = 32 * 1024 * 1024
MAX_SOURCE_ENTRIES = 5000
MAX_OUTPUT_BYTES = 256_000
MAX_ARCHIVE_BYTES = MAX_SOURCE_BYTES + MAX_SOURCE_ENTRIES * 1024 + 10240
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
    fields = {"version", "runtime", "images", "state_directory"}
    if isinstance(policy, dict) and policy.get("version") == 3:
        fields.add("server_uuid")
    if not isinstance(policy, dict) or set(policy) != fields:
        raise IsolationError("Unexpected operator policy fields")
    if policy["version"] not in {2, 3} or policy["runtime"] != "runsc":
        raise IsolationError("Managed execution requires the runsc policy")
    if policy["version"] == 3 and not re.fullmatch(
        r"[a-zA-Z0-9-]{1,128}", str(policy["server_uuid"]),
    ):
        raise IsolationError("Invalid bound host identity")
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


def load_dispatch(claim_id: str, policy: dict) -> dict:
    """Read trusted transport's private spool, never a file in generated source.

    This is an OS trust boundary, not a signature verifier: only the authenticated
    controller may populate the operator-owned spool after central admission.
    """
    if str(uuid.UUID(claim_id)) != claim_id or policy["version"] != 3:
        raise IsolationError("Dispatch requires a canonical claim and bound host policy")
    directory = Path(policy["state_directory"]) / "dispatch"
    path = directory / (claim_id + ".json")
    for item in (directory, path):
        info = item.lstat()
        if (stat.S_ISLNK(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077):
            raise IsolationError("Dispatch spool must be private and operator owned")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_size > 8192 or info.st_uid != os.geteuid()
                or info.st_mode & 0o077):
            raise IsolationError("Invalid dispatch spool record")
        binding = json.loads(stream.read(8193))
    if not isinstance(binding, dict) or binding.get("claim_id") != claim_id:
        raise IsolationError("Dispatch spool claim mismatch")
    return binding


def verify_dispatch(binding: dict, policy: dict, source: bytes, profile: str, timeout: int):
    fields = {"version", "claim_id", "run_id", "sequence_no", "request_hash", "target_hash",
              "server_uuid", "source_hash", "image_id", "profile", "timeout_seconds",
              "deadline"}
    if (not isinstance(binding, dict) or set(binding) != fields
            or type(binding["version"]) is not int or binding["version"] != 1):
        raise IsolationError("Invalid dispatch envelope")
    for field in ("claim_id", "run_id"):
        try:
            if str(uuid.UUID(binding[field])) != binding[field]:
                raise ValueError("Noncanonical UUID")
        except (ValueError, TypeError, AttributeError) as error:
            raise IsolationError("Invalid dispatch identity") from error
    for field in ("request_hash", "target_hash", "source_hash"):
        if not isinstance(binding[field], str) or not re.fullmatch(r"[a-f0-9]{64}", binding[field]):
            raise IsolationError("Invalid dispatch fingerprint")
    now = int(time.time())
    if (type(binding["sequence_no"]) is not int or binding["sequence_no"] < 0
            or type(binding["deadline"]) is not int
            or not now + timeout + 90 <= binding["deadline"] <= now + 1920
            or type(binding["timeout_seconds"]) is not int
            or binding["timeout_seconds"] != timeout
            or binding["profile"] != profile
            or binding["server_uuid"] != policy.get("server_uuid")
            or binding["image_id"] != policy["images"].get(profile)
            or binding["source_hash"] != hashlib.sha256(source).hexdigest()):
        raise IsolationError("Dispatch source, host, toolchain or deadline mismatch")


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
                CREATE TABLE IF NOT EXISTS dispatch_claims(
                    claim_id TEXT PRIMARY KEY,request_hash TEXT NOT NULL,
                    binding_hash TEXT NOT NULL,job_id TEXT NOT NULL UNIQUE);
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

    def begin(self, profile: str, image: str, timeout: int, binding: dict | None = None) -> dict:
        if binding and self.db.execute("SELECT 1 FROM dispatch_claims WHERE claim_id=?",
                                       (binding["claim_id"],)).fetchone():
            raise IsolationError("Dispatch claim already consumed; reconcile instead of replaying")
        if self.db.execute("SELECT 1 FROM jobs WHERE state<>'cleaned' LIMIT 1").fetchone():
            raise IsolationError("Validation slot held by an unreconciled job")
        job_id = uuid.uuid4().hex
        now = int(time.time())
        with self.db:
            self.db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,'creating',NULL,?)",
                            (job_id, "gca-validation-" + job_id, image, profile,
                             binding["deadline"] if binding else now + timeout + 90, now))
            if binding:
                digest = hashlib.sha256(json.dumps(binding, sort_keys=True,
                                                    separators=(",", ":")).encode()).hexdigest()
                self.db.execute("INSERT INTO dispatch_claims VALUES(?,?,?,?)",
                                (binding["claim_id"], binding["request_hash"], digest, job_id))
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

    def observe_claim(self, claim_id: str) -> dict:
        """Cleanup evidence only. Losing the result never proves validation success."""
        if str(uuid.UUID(claim_id)) != claim_id:
            raise IsolationError("Invalid observation claim")
        row = self.db.execute(
            "SELECT c.*,j.state,j.name,j.image,j.profile,j.deadline,j.container_id "
            "FROM dispatch_claims c JOIN jobs j ON j.id=c.job_id WHERE c.claim_id=?",
            (claim_id,),
        ).fetchone()
        if row is None:
            # Not found is not permission for the central controller to release or retry.
            return {"claim_id": claim_id, "state": "unknown", "cleanup_verified": False}
        job = {**dict(row), "id": row["job_id"]}
        if job["state"] == "cleaned":
            ids = docker("ps", "-a", "--no-trunc", "--filter", f"label=gca.scope={self.scope}",
                         "--filter", f"label=gca.job={job['id']}",
                         "--format", "{{.ID}}").decode().split()
            if ids:
                self.record(job, "cleanup_uncertain")
        return {"claim_id": claim_id, "validation_job_id": job["id"],
                "request_hash": job["request_hash"], "binding_hash": job["binding_hash"],
                "state": job["state"], "cleanup_verified": job["state"] == "cleaned",
                "validation_success": None}

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


def execute_bounded(args: list[str], timeout: float) -> dict:
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


def validate(workspace: Path, profile: str, timeout: int, policy: dict,
             binding: dict | None = None) -> dict:
    with Journal(Path(policy["state_directory"])) as journal:
        return _validate(workspace, profile, timeout, policy, journal, binding)


def _validate(workspace: Path, profile: str, timeout: int, policy: dict, journal: Journal,
              binding: dict | None = None) -> dict:
    root = workspace.resolve(strict=True)
    return validate_archive(source_archive(root), profile, timeout, policy, journal, binding)


def check_archive(source: bytes, profile: str):
    """Validate a bounded plain tar without ever extracting it on the trusted host."""
    if profile not in PROFILES:
        raise IsolationError("Unknown validation profile")
    if len(source) > MAX_ARCHIVE_BYTES:
        raise IsolationError("Source archive limit exceeded")
    names = set()
    source_files = set()
    total = 0
    with tarfile.open(fileobj=io.BytesIO(source), mode="r:") as archive:
        for item in archive:
            path = Path(item.name)
            if (path.is_absolute() or ".." in path.parts or not path.parts
                    or path.as_posix() != item.name.rstrip("/") or item.name in names
                    or any(part in EXCLUDED_DIRS or sensitive_name(part) for part in path.parts)
                    or not (item.isfile() or item.isdir()) or item.sparse is not None
                    or item.size < 0 or item.mode & 0o7000
                    or item.offset_data + item.size > len(source)):
                raise IsolationError("Forbidden source archive member")
            names.add(item.name)
            if item.isfile():
                source_files.add(item.name)
            total += item.size
            if len(names) > MAX_SOURCE_ENTRIES or total > MAX_SOURCE_BYTES:
                raise IsolationError("Source archive content limit exceeded")
    relative_cwd, _ = PROFILES[profile]
    if relative_cwd != "." and not any(name.startswith(relative_cwd + "/") for name in names):
        raise IsolationError("Validation working directory is missing")
    if profile == "python_compile" and not any(
        name.startswith(("app/", "scripts/")) and name.endswith(".py") for name in source_files
    ):
        raise IsolationError("No Python source exists for the compile profile")
    if profile in {"react_test", "react_build"}:
        required = {"frontend/package.json", "frontend/package-lock.json",
                    "frontend/index.html"}
        if not required <= source_files or not any(
            name.startswith("frontend/src/") and name.endswith((".tsx", ".jsx"))
            for name in source_files
        ):
            raise IsolationError("React source and locked dependencies are required")
        if profile == "react_test" and not any(
            name.startswith("frontend/src/") and name.endswith(
                (".test.ts", ".test.tsx", ".test.js", ".test.jsx",
                 ".spec.ts", ".spec.tsx", ".spec.js", ".spec.jsx")
            ) for name in source_files
        ):
            raise IsolationError("React test profile requires a test source")
    if profile in {"spring_test", "spring_package"}:
        if ("backend/pom.xml" not in source_files
                or not any(name.startswith("backend/src/main/java/") and name.endswith(".java")
                           for name in source_files)
                or not any(name.startswith("backend/src/test/java/") and name.endswith(".java")
                           for name in source_files)):
            raise IsolationError("Spring source, Maven POM and tests are required")


def validate_archive(source: bytes, profile: str, timeout: int, policy: dict, journal: Journal,
                     binding: dict | None = None) -> dict:
    if policy["version"] == 3 and binding is None:
        raise IsolationError("Centrally admitted dispatch is required on this host")
    if binding is not None and policy["version"] != 3:
        raise IsolationError("Dispatch requires a bound host policy")
    if profile not in PROFILES or profile not in policy["images"]:
        raise IsolationError("No approved offline toolchain for this validation profile")
    if type(timeout) is not int or not 1 <= timeout <= 1800:
        raise IsolationError("Validation timeout must be within 1..1800 seconds")
    relative_cwd, command = PROFILES[profile]
    check_archive(source, profile)
    if binding is not None:
        verify_dispatch(binding, policy, source, profile, timeout)
    job = journal.begin(profile, policy["images"][profile], timeout, binding)
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
        workdir = "/workspace" + ("" if relative_cwd == "." else "/" + relative_cwd)
        deadline = time.monotonic() + timeout
        result = None
        for fixed_command in (*([PREPARE_COMMANDS[profile]] if profile in PREPARE_COMMANDS else []),
                              command):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise IsolationError("Validation deadline exceeded")
            result = execute_bounded(["exec", "--workdir", workdir, name, *fixed_command],
                                     remaining)
            if not result["success"]:
                break
        if result["success"]:
            postconditions = {
                "react_build": (("test", "-s", "dist/index.html"),),
                "spring_test": (("find", "target/surefire-reports", "-maxdepth", "1", "-type",
                                 "f", "-name", "TEST-*.xml", "-size", "+0c", "-print"),),
                "spring_package": (
                    ("find", "target/surefire-reports", "-maxdepth", "1", "-type", "f",
                     "-name", "TEST-*.xml", "-size", "+0c", "-print"),
                    ("find", "target", "-maxdepth", "1", "-type", "f", "-name", "*.jar",
                     "-size", "+0c", "-print"),
                ),
            }
            for postcondition in postconditions.get(profile, ()):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise IsolationError("Validation deadline exceeded")
                artifact = execute_bounded(["exec", "--workdir", workdir, name,
                                            *postcondition], remaining)
                if not artifact["success"] or (postcondition[0] == "find"
                                               and not artifact["stdout"].strip()):
                    result = {"exit_code": 1, "success": False, "output_truncated": False,
                              "stdout": "", "stderr": "Expected validation artifact is absent"}
                    break
        return {
            **result, "profile": profile, "isolation": "gvisor",
            "image_id": policy["images"][profile], "network": "none",
            "workspace_mutations_discarded": True,
            "validation_job_id": job["id"], "cleanup_verified": True,
            **({"claim_id": binding["claim_id"], "request_hash": binding["request_hash"],
                "source_hash": binding["source_hash"]} if binding else {}),
        }
    finally:
        # Failure here overrides success. The caller must record cleanup uncertainty.
        try:
            journal.cleanup(job, create_confirmed=create_confirmed)
        except (IsolationError, subprocess.TimeoutExpired, ValueError, KeyError) as error:
            journal.record(job, "cleanup_uncertain")
            raise IsolationError(f"Sandbox cleanup uncertain for {name}") from error


def gateway(stream, policy: dict) -> dict:
    """Fixed SSH command; authenticated controller input, never a public listener.

    Hold the one host slot while accepting source: concurrent uploads cannot multiply
    staging memory. Source is never extracted to the host or retained in the journal.
    """
    if policy["version"] != 3:
        raise IsolationError("Gateway requires centrally bound host policy v3")
    with Journal(Path(policy["state_directory"])) as journal:
        header = stream.readline(8193)
        if len(header) > 8192 or not header.endswith(b"\n"):
            raise IsolationError("Invalid gateway framing")
        request = json.loads(header)
        if not isinstance(request, dict):
            raise IsolationError("Invalid gateway request")
        if set(request) == {"operation", "claim_id"} and request["operation"] == "observe":
            if not isinstance(request["claim_id"], str):
                raise IsolationError("Invalid observation claim")
            return journal.observe_claim(request["claim_id"])
        if (set(request) != {"operation", "binding", "archive_bytes"}
                or request["operation"] != "validate"
                or type(request["archive_bytes"]) is not int
                or not 0 < request["archive_bytes"] <= MAX_ARCHIVE_BYTES
                or not isinstance(request["binding"], dict)):
            raise IsolationError("Invalid gateway dispatch")
        source = stream.read(request["archive_bytes"])
        if len(source) != request["archive_bytes"]:
            raise IsolationError("Truncated source transfer")
        binding = request["binding"]
        profile, timeout = binding.get("profile"), binding.get("timeout_seconds")
        if not isinstance(profile, str) or profile not in PROFILES or type(timeout) is not int:
            raise IsolationError("Invalid gateway profile")
        # The SSH controller is the authority. The exact same verifier is used for
        # spooled operator invocations; no shell or arbitrary target path is accepted.
        signal.alarm(0)
        return validate_archive(source, profile, timeout, policy, journal, binding)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--profile", choices=tuple(PROFILES))
    parser.add_argument("--timeout", type=int)
    parser.add_argument("--reap", action="store_true")
    parser.add_argument("--claim-id")
    parser.add_argument("--observe-claim")
    parser.add_argument("--gateway", action="store_true")
    args = parser.parse_args()
    try:
        config = Path(os.environ["GCA_VALIDATION_CONFIG"])
        if args.gateway:
            if any((args.workspace, args.profile, args.timeout, args.claim_id,
                    args.reap, args.observe_claim)):
                raise IsolationError("Gateway cannot accept another operation")
            # Abrupt upload timeout creates no container; flock is released on exit.
            signal.alarm(45)
            result = gateway(sys.stdin.buffer, load_policy(config, None))
            signal.alarm(0)
            print(json.dumps(result))
            return 0
        if args.observe_claim:
            if args.workspace or args.profile or args.timeout or args.claim_id or args.reap:
                raise IsolationError("Observation cannot execute a workload")
            policy = load_policy(config, None)
            with Journal(Path(policy["state_directory"])) as journal:
                result = journal.observe_claim(args.observe_claim)
            print(json.dumps(result))
            return 0
        if args.reap:
            if args.workspace or args.profile or args.timeout or args.claim_id:
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
        policy = load_policy(config, workspace)
        binding = load_dispatch(args.claim_id, policy) if args.claim_id else None
        result = validate(workspace, args.profile, args.timeout, policy, binding)
        print(json.dumps(result))
        return 0
    except (IsolationError, OSError, ValueError, KeyError, sqlite3.Error, tarfile.TarError,
            subprocess.TimeoutExpired) as error:
        # Do not expose input paths, provider secrets, or subprocess output.
        message = str(error) if isinstance(error, IsolationError) else "Isolation setup failed"
        print(json.dumps({"success": False, "error": message, "isolation": "unavailable"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
