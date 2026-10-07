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
import importlib.util
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
import xml.etree.ElementTree as ET  # nosec B405


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
MAX_REPORT_ARCHIVE_BYTES = 4 * 1024 * 1024
MAX_ARCHIVE_BYTES = MAX_SOURCE_BYTES + MAX_SOURCE_ENTRIES * 1024 + 10240
DOCKER = "/usr/bin/docker"
TOOLCHAIN_REGISTRY = Path("/etc/gca-validation/toolchain-registry.json")
VALIDATION_POLICY = Path('/etc/gca-validation/policy.json')
CLEAN_ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/nonexistent", "CI": "true"}
IMPORT_PHASES = {
    "prepared": {"archive_verified", "cancelled"},
    "archive_verified": {"load_started", "cancelled"},
    "load_started": {"load_uncertain", "loaded"},
    "load_uncertain": {"loaded"},
    "loaded": {"proof_pending", "rejected"},
    "proof_pending": {'publishing', 'rejected'}, 'publishing': {'enrolled'}, 'enrolled': set(),
    'rejected': set(),
    "cancelled": {"archive_verified"},
}
MAX_TOOLCHAIN_IMPORTS = 8


class IsolationError(RuntimeError):
    pass


def _canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def publication_receipt(identity, fingerprint, hashes):
    return {'version': 1, 'intent_id': identity, 'plan_sha256': fingerprint,
            'policy_sha256': hashes['policy_new'], 'registry_sha256': hashes['registry_new'],
            'configuration_published': True, 'image_approved': True}


def _artifact_helper():
    if __package__:
        from app.coding import build_artifact
        return build_artifact
    # Python -I omits the script directory from sys.path. Load the installed,
    # operator-owned stdlib helper by an exact sibling path, never from a workspace.
    path = Path(__file__).resolve().with_name("build_artifact.py")
    for item in (path, *path.parents):
        info = item.stat()
        if info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o022:
            raise IsolationError("Artifact helper is not operator protected")
    spec = importlib.util.spec_from_file_location("gca_build_artifact", path)
    if spec is None or spec.loader is None:
        raise IsolationError("Artifact helper is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    # The reviewed sibling source, not a potentially stale cached bytecode file.
    exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)  # nosec B102
    return module


def _packaging_helper():
    if __package__:
        from app.coding import isolated_packaging
        return isolated_packaging
    path = Path(__file__).resolve().with_name("isolated_packaging.py")
    for item in (path, *path.parents):
        info = item.lstat()
        if item.is_symlink() or info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o022:
            raise IsolationError("Packaging cleanup helper is not operator protected")
    spec = importlib.util.spec_from_file_location("gca_isolated_packaging", path)
    if spec is None or spec.loader is None:
        raise IsolationError("Packaging cleanup helper is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)  # nosec B102
    return module


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
                CREATE TABLE IF NOT EXISTS toolchain_imports(
                    id TEXT PRIMARY KEY,intent TEXT NOT NULL,phase TEXT NOT NULL,
                    observations TEXT NOT NULL,updated_at INTEGER NOT NULL);
            """)
            # Preserve old jobs as validation work. Different purposes share ONE
            # slot and reaper, never independent locks that can overcommit the host.
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(jobs)")}
            if "purpose" not in columns:
                with self.db:
                    self.db.execute("ALTER TABLE jobs ADD COLUMN purpose TEXT NOT NULL DEFAULT 'validation' "
                                    "CHECK(purpose IN ('validation','image_packaging'))")
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

    def require_available(self, binding: dict | None = None):
        imports = self.toolchain_imports()
        if any(row["phase"] not in {'cancelled', 'enrolled', 'rejected'} for row in imports):
            raise IsolationError("Host slot held by an unreconciled toolchain import")
        self.verify_rejections(imports)
        if any(row['phase'] == 'enrolled' for row in imports):
            self.verify_publications(imports)
        if binding and self.db.execute("SELECT 1 FROM dispatch_claims WHERE claim_id=?",
                                       (self.binding_claim(binding),)).fetchone():
            raise IsolationError("Dispatch claim already consumed; reconcile instead of replaying")
        if self.db.execute("SELECT 1 FROM jobs WHERE state<>'cleaned' LIMIT 1").fetchone():
            raise IsolationError("Validation slot held by an unreconciled job")

    def publication_plan(self, item):
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE name='toolchain_publication_plans'").fetchone():
            raise IsolationError('Enrolled toolchain has no publication plan')
        row = self.db.execute('SELECT * FROM toolchain_publication_plans WHERE import_id=?', (item['id'],)).fetchone()
        if (row is None or not isinstance(row['plan'], str) or not 1 <= len(row['plan'].encode()) <= 196608
                or hashlib.sha256(row['plan'].encode()).hexdigest() != row['plan_sha256']):
            raise IsolationError('Publication plan fingerprint is invalid')
        value = json.loads(row['plan'])
        names = {'policy_old', 'registry_old', 'policy_new', 'registry_new'}
        if (not isinstance(value, dict) or set(value) != {'version', 'intent_id', 'binding', 'documents', 'hashes',
                                                        'custody_sha256', 'proofs_sha256', 'load_sha256'}
                or type(value['version']) is not int or value['version'] != 1 or value['intent_id'] != item['id']
                or not isinstance(value['documents'], dict) or set(value['documents']) != names
                or not isinstance(value['hashes'], dict) or set(value['hashes']) != names
                or not isinstance(value['binding'], dict)
                or set(value['binding']) != {'policy_sha256', 'registry_sha256', 'bundle_sha256'}
                or any(not isinstance(body, str) or not 1 <= len(body.encode()) <= 32768
                       or hashlib.sha256(body.encode()).hexdigest() != value['hashes'][name]
                       for name, body in value['documents'].items())
                or value['binding'].get('policy_sha256') != item['intent']['policy_sha256']
                or value['binding'].get('registry_sha256') != item['intent']['registry_sha256']
                or item['intent']['version'] == 2 and value['binding'].get('bundle_sha256') != item['intent']['bundle_sha256']
                or value['hashes']['policy_old'] != item['intent']['policy_sha256']
                or value['hashes']['registry_old'] != item['intent']['registry_sha256']):
            raise IsolationError('Publication plan source/configuration lineage differs')
        return value, row['plan_sha256']

    def rejection_proofs(self, identity):
        """Hash bounded immutable proof rows and their cleaned job identities.

        This is also usable by the read-only installer. It performs no Docker,
        systemd or native process action and never treats absent results as pass.
        """
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE name='toolchain_proof_cases'").fetchone():
            return hashlib.sha256(_canonical_json([])).hexdigest()
        rows = self.db.execute('SELECT * FROM toolchain_proof_cases WHERE import_id=? '
                               'ORDER BY case_name,attempt LIMIT 7', (identity,)).fetchall()
        if len(rows) > 6:
            raise IsolationError('Rejected proof inventory exceeds bound')
        snapshot = []
        for row in rows:
            job = self.db.execute('SELECT * FROM jobs WHERE id=?', (row['job_id'],)).fetchone()
            if job is None or job['state'] != 'cleaned' or row['result'] is not None and len(row['result']) > 8192:
                raise IsolationError('Rejected proof cleanup or result bound differs')
            snapshot.append({'proof': dict(row), 'job': dict(job)})
        return hashlib.sha256(_canonical_json(snapshot)).hexdigest()

    def verify_rejections(self, imports=None):
        imports = self.toolchain_imports() if imports is None else imports
        rejected = [row for row in imports if row['phase'] == 'rejected']
        if not rejected:
            return
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE name='toolchain_rejections'").fetchone():
            raise IsolationError('Rejected import has no verified rejection receipt')
        rows = self.db.execute('SELECT * FROM toolchain_rejections LIMIT 9').fetchall()
        known = {row['id'] for row in imports}
        if len(rows) > 8 or any(row['import_id'] not in known for row in rows):
            raise IsolationError('Rejection receipt inventory differs')
        for item in rejected:
            row = next((r for r in rows if r['import_id'] == item['id']), None)
            if (row is None or not isinstance(row['receipt'], str) or not 1 <= len(row['receipt'].encode()) <= 8192
                    or hashlib.sha256(row['receipt'].encode()).hexdigest() != row['receipt_sha256']):
                raise IsolationError('Rejection receipt fingerprint differs')
            receipt = json.loads(row['receipt'])
            fields = {'version', 'intent_id', 'binding', 'custody_sha256', 'load_sha256', 'proofs_sha256',
                      'image_approved', 'configuration_published', 'custody_retained', 'image_retained'}
            binding = receipt.get('binding') if isinstance(receipt, dict) else None
            history = item['observations']
            if (not isinstance(receipt, dict) or set(receipt) != fields
                    or type(receipt['version']) is not int or receipt['version'] != 1
                    or receipt['intent_id'] != item['id'] or receipt['image_approved'] is not False
                    or receipt['configuration_published'] is not False or receipt['custody_retained'] is not True
                    or receipt['image_retained'] is not True or not isinstance(binding, dict)
                    or set(binding) != {'bundle_sha256', 'policy_sha256', 'registry_sha256'}
                    or any(not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value)
                           for value in (*binding.values(), *(receipt[key] for key in
                                                             ('custody_sha256', 'load_sha256', 'proofs_sha256'))))
                    or any(binding[key] != item['intent'][key] for key in ('policy_sha256', 'registry_sha256'))
                    or item['intent']['version'] == 2 and binding['bundle_sha256'] != item['intent']['bundle_sha256']
                    or len(history) < 2 or history[-2]['phase'] not in {'loaded', 'proof_pending'}
                    or history[-1]['evidence_sha256'] != row['receipt_sha256']
                    or self.rejection_proofs(item['id']) != receipt['proofs_sha256']):
                raise IsolationError('Rejected receipt/proof lineage differs')

    def verify_publications(self, imports=None):
        imports = self.toolchain_imports() if imports is None else imports
        enrolled = [row for row in imports if row['phase'] == 'enrolled']
        if not enrolled:
            return
        def read(path):
            for item in (path, *path.parents):
                info = item.lstat()
                if item.is_symlink() or info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o022:
                    raise IsolationError('Published configuration is not operator protected')
            if not path.is_file() or path.stat().st_mode & 0o077 or not 1 <= path.stat().st_size <= 32768:
                raise IsolationError('Published configuration bound differs')
            return path.read_bytes()
        policy_body, registry_body = read(VALIDATION_POLICY), read(TOOLCHAIN_REGISTRY)
        policy, registry = json.loads(policy_body), validate_toolchain_registry(json.loads(registry_body))
        remaining = []
        for item in enrolled:
            plan, fingerprint = self.publication_plan(item)
            expected = publication_receipt(item['id'], fingerprint, plan['hashes'])
            history = item['observations']
            if (len(history) < 2 or history[-2]['phase'] != 'publishing'
                    or history[-2]['evidence_sha256'] != fingerprint
                    or history[-1]['evidence_sha256'] != hashlib.sha256(_canonical_json(expected)).hexdigest()
                    or int(time.time()) >= item['intent']['stop_unix']):
                raise IsolationError('Published receipt/lifetime lineage differs')
            new_policy, new_registry = json.loads(plan['documents']['policy_new']), json.loads(plan['documents']['registry_new'])
            image = item['intent']['image_id']
            record = new_registry['images'].get(image)
            profiles = ('react_test', 'react_build') if item['intent']['role'] == 'react' else ('spring_test', 'spring_package')
            if (record is None or registry['images'].get(image) != record
                    or record.get('import_id') != item['id']
                    or record.get('origin') != 'reviewed_operator_archive'
                    or record.get('recipe_sha256') != item['intent']['recipe_sha256']
                    or any(new_policy['images'].get(name) != image for name in profiles)
                    or any(policy.get('images', {}).get(name) != image for name in profiles)
                    or any(policy.get('images', {}).get(name) != image for name, value in new_policy['images'].items()
                           if value == image)):
                raise IsolationError('Published role record/profile differs')
            remaining.append(plan['hashes'])
        current = (hashlib.sha256(policy_body).hexdigest(), hashlib.sha256(registry_body).hexdigest())
        while remaining:
            matches = [value for value in remaining if (value['policy_new'], value['registry_new']) == current]
            if len(matches) != 1:
                raise IsolationError('Published configuration chain has drift or a fork')
            matched = matches[0]
            remaining.remove(matched)
            current = (matched['policy_old'], matched['registry_old'])

    @staticmethod
    def import_intent(value):
        """Fixed operator intent only: no paths, shell, credentials or approval flag."""
        fields = {"version", "role", "host_instance_id", "image_id", "archive_sha256",
                  "archive_bytes", "recipe_sha256", "policy_sha256", "registry_sha256",
                  "stop_unix"}
        version = value.get('version') if isinstance(value, dict) else None
        if version == 2:
            fields.add('bundle_sha256')
        if (not isinstance(value, dict) or set(value) != fields
                or type(version) is not int or version not in (1, 2)
                or not isinstance(value["role"], str) or value["role"] not in {"react", "spring"}
                or not isinstance(value["host_instance_id"], str)
                or not re.fullmatch(r"[1-9][0-9]{0,19}", value["host_instance_id"])
                or not isinstance(value["image_id"], str)
                or not re.fullmatch(r"sha256:[a-f0-9]{64}", value["image_id"])
                or type(value["archive_bytes"]) is not int
                or not 1024 <= value["archive_bytes"] <= 512 * 1024 * 1024
                or type(value["stop_unix"]) is not int or value["stop_unix"] <= 0):
            raise IsolationError("Toolchain import intent differs from bounded contract")
        for field in ("archive_sha256", "recipe_sha256", "policy_sha256", "registry_sha256",
                      *(('bundle_sha256',) if version == 2 else ())):
            if not isinstance(value[field], str) or not re.fullmatch(r"[a-f0-9]{64}", value[field]):
                raise IsolationError("Toolchain import custody fingerprint is invalid")
        body = json.dumps(value, sort_keys=True, separators=(",", ":"))
        return body, hashlib.sha256(body.encode()).hexdigest()

    def toolchain_imports(self):
        rows = self.db.execute("SELECT * FROM toolchain_imports ORDER BY id LIMIT 9").fetchall()
        if len(rows) > MAX_TOOLCHAIN_IMPORTS:
            raise IsolationError("Toolchain import history requires operator review")
        result = []
        for row in rows:
            if (not isinstance(row["intent"], str) or len(row["intent"]) > 2048
                    or not isinstance(row["observations"], str) or len(row["observations"]) > 8192
                    or row["phase"] not in IMPORT_PHASES):
                raise IsolationError("Toolchain import journal is invalid")
            try:
                intent = json.loads(row["intent"])
                body, identity = self.import_intent(intent)
                observations = json.loads(row["observations"])
            except (ValueError, TypeError) as error:
                raise IsolationError("Toolchain import journal is invalid") from error
            if (row["id"] != identity or row["intent"] != body
                    or not isinstance(observations, list) or not 1 <= len(observations) <= 16):
                raise IsolationError("Toolchain import journal fingerprint differs")
            previous = None
            for item in observations:
                if (not isinstance(item, dict) or set(item) != {"phase", "evidence_sha256", "at"}
                        or not isinstance(item["phase"], str)
                        or type(item["at"]) is not int or item["at"] < 0
                        or (previous is None and item["phase"] != "prepared")
                        or (previous is not None and item["phase"] not in IMPORT_PHASES[previous])
                        or not isinstance(item["evidence_sha256"], str)
                        or not re.fullmatch(r"[a-f0-9]{64}", item["evidence_sha256"])):
                    raise IsolationError("Toolchain import journal transition differs")
                previous = item["phase"]
            if (observations[0]["evidence_sha256"] != identity or previous != row["phase"]
                    or row["updated_at"] != observations[-1]["at"]):
                raise IsolationError("Toolchain import journal phase differs")
            result.append({"id": identity, "intent": intent, "phase": previous,
                           "observations": observations, "image_approved": False})
        if sum(item["phase"] not in {'cancelled', 'enrolled', 'rejected'} for item in result) > 1:
            raise IsolationError("Toolchain import journal has overlapping maintenance")
        return result

    def begin_toolchain_import(self, intent):
        """Durable pre-side-effect reservation under the SAME host flock as jobs.

        A matching retry returns intent, not permission to replay docker load.
        Fixed-target/policy/registry read-back is the trusted controller's job.
        """
        body, identity = self.import_intent(intent)
        existing = self.toolchain_imports()
        for item in existing:
            if item["id"] == identity:
                return item
        self.require_available()
        if len(existing) >= MAX_TOOLCHAIN_IMPORTS:
            raise IsolationError("Toolchain import history requires operator review")
        now = int(time.time())
        if intent["stop_unix"] <= now:
            raise IsolationError("Toolchain import host allocation expired")
        observations = [{"phase": "prepared", "evidence_sha256": identity, "at": now}]
        with self.db:
            self.db.execute("INSERT INTO toolchain_imports VALUES(?,?,?,?,?)",
                            (identity, body, "prepared", json.dumps(observations), now))
        return next(item for item in self.toolchain_imports() if item["id"] == identity)

    def record_toolchain_import(self, identity, expected_phase, phase, evidence_sha256):
        """CAS observation by trusted maintenance code, never workload authority.

        Expiry does not clear ambiguous imports. Reconciliation remains legal but
        starting a new Docker load after the fixed host stop is prohibited.
        """
        if (not isinstance(expected_phase, str) or not isinstance(phase, str)
                or not isinstance(evidence_sha256, str)
                or not re.fullmatch(r"[a-f0-9]{64}", evidence_sha256)):
            raise IsolationError("Toolchain observation fingerprint is invalid")
        item = next((row for row in self.toolchain_imports() if row["id"] == identity), None)
        if item is None or expected_phase not in IMPORT_PHASES or phase not in IMPORT_PHASES[expected_phase]:
            raise IsolationError("Toolchain import transition is not permitted")
        history = item["observations"]
        if (len(history) > 1 and item["phase"] == phase and history[-2]["phase"] == expected_phase
                and history[-1]["evidence_sha256"] == evidence_sha256):
            return item
        if item["phase"] != expected_phase or len(history) >= 16:
            raise IsolationError("Toolchain import phase changed; inspect before retry")
        now = int(time.time())
        if expected_phase == 'cancelled':
            # Explicit same-custody reopening only. Cancellation is not a new
            # allocation or permission to reload an uncertain operation.
            self.require_available()
            if (phase != 'archive_verified' or item['intent']['stop_unix'] <= now
                    or not any(entry['phase'] == 'archive_verified'
                               and entry['evidence_sha256'] == evidence_sha256 for entry in history)):
                raise IsolationError('Cancelled import needs exact unexpired verified custody')
        if phase == "load_started" and item["intent"]["stop_unix"] <= now:
            raise IsolationError("Toolchain import host allocation expired")
        history.append({"phase": phase, "evidence_sha256": evidence_sha256, "at": now})
        with self.db:
            changed = self.db.execute(
                "UPDATE toolchain_imports SET phase=?,observations=?,updated_at=? WHERE id=? AND phase=?",
                (phase, json.dumps(history), now, identity, expected_phase)).rowcount
            if changed != 1:
                raise IsolationError("Toolchain import phase changed; inspect before retry")
        return next(row for row in self.toolchain_imports() if row["id"] == identity)

    @staticmethod
    def binding_claim(binding):
        if not isinstance(binding, dict) or ("claim_id" in binding) == ("build_job_id" in binding):
            raise IsolationError("Ambiguous dispatch claim purpose")
        value = binding.get("claim_id", binding.get("build_job_id"))
        try:
            if not isinstance(value, str) or str(uuid.UUID(value)) != value:
                raise ValueError("noncanonical claim")
        except (ValueError, TypeError, AttributeError) as error:
            raise IsolationError("Invalid dispatch claim identity") from error
        return value

    def begin(self, profile: str, image: str, timeout: int, binding: dict | None = None,
              *, purpose: str = "validation") -> dict:
        if (purpose not in {"validation", "image_packaging"}
                or (purpose == "image_packaging" and (profile != "image_packaging" or binding is None
                                                       or "build_job_id" not in binding))
                or (purpose == "validation" and (profile not in PROFILES
                                                  or (binding is not None and "claim_id" not in binding)))):
            raise IsolationError("Journal purpose/profile does not match dispatch")
        if (type(timeout) is not int or not 1 <= timeout <= 1800
                or not isinstance(image, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", image)):
            raise IsolationError("Invalid journal toolchain or timeout")
        now = int(time.time())
        deadline = now + timeout + 90
        if binding is not None:
            self.binding_claim(binding)
            deadline = binding.get("deadline" if purpose == "validation" else "execution_deadline_unix")
            if (type(deadline) is not int or not now < deadline <= now + 1920
                    or not isinstance(binding.get("request_hash"), str)
                    or not re.fullmatch(r"[a-f0-9]{64}", binding["request_hash"])):
                raise IsolationError("Invalid journal request fingerprint or deadline")
        self.require_available(binding)
        job_id = uuid.uuid4().hex
        with self.db:
            self.db.execute("INSERT INTO jobs(id,name,image,profile,deadline,state,container_id,updated_at,purpose) "
                            "VALUES(?,?,?,?,?,'creating',NULL,?,?)",
                            (job_id, ("gca-validation-" if purpose == "validation" else "gca-packaging-") + job_id,
                             image, profile, deadline, now, purpose))
            if binding:
                digest = hashlib.sha256(json.dumps(binding, sort_keys=True,
                                                    separators=(",", ":")).encode()).hexdigest()
                self.db.execute("INSERT INTO dispatch_claims VALUES(?,?,?,?)",
                                (self.binding_claim(binding), binding["request_hash"], digest, job_id))
            self.db.execute("INSERT INTO events(job_id,state,observed_at) VALUES(?,?,?)",
                            (job_id, "creating", now))
        return dict(self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def begin_toolchain_proof(self, identity: str, profile: str, image: str,
                             timeout: int, case: str, source_hash: str, binding_hash: str) -> dict:
        """Operator fixture only: keep the import reservation and ordinary gate closed.

        The fixed proof controller verifies actual fixture bytes before this call.
        This is not an alternative model dispatch or role-approval mechanism.
        Proofs are ordinary validation jobs for the existing identity-safe reaper;
        the proof controller additionally checks native runtime cleanup.
        """
        imports = self.toolchain_imports()
        row = next((item for item in imports if item['id'] == identity), None)
        profiles = {'react': {'react_test', 'react_build'},
                    'spring': {'spring_test', 'spring_package'}}
        now = int(time.time())
        if (row is None or row['phase'] != 'proof_pending'
                or profile not in profiles[row['intent']['role']]
                or image != row['intent']['image_id'] or type(timeout) is not int or timeout != 180
                or now + timeout + 90 >= row['intent']['stop_unix']
                or case not in {profile + '_pass', profile + '_fail'}
                or profile.endswith(('build', 'package')) and case.endswith('_fail')
                or any(not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value)
                       for value in (source_hash, binding_hash))
                or self.db.execute("SELECT 1 FROM jobs WHERE state<>'cleaned' LIMIT 1").fetchone()):
            raise IsolationError('Proof is not the exact reserved idle toolchain maintenance')
        rows = self.db.execute(
            'SELECT * FROM toolchain_proof_cases WHERE import_id=? AND case_name=? ORDER BY attempt',
            (identity, case)).fetchall()
        if (len(rows) >= 2 or any(item['source_sha256'] != source_hash
                                 or item['binding_sha256'] != binding_hash for item in rows)):
            raise IsolationError('Proof attempt bound or source/controller authority differs')
        job_id = uuid.uuid4().hex
        with self.db:
            self.db.execute("INSERT INTO jobs(id,name,image,profile,deadline,state,container_id,updated_at,purpose) "
                            "VALUES(?,?,?,?,?,'creating',NULL,?,'validation')",
                            (job_id, 'gca-validation-' + job_id, image, profile, now + timeout + 90, now))
            self.db.execute('INSERT INTO events(job_id,state,observed_at) VALUES(?,?,?)',
                            (job_id, 'creating', now))
            self.db.execute('INSERT INTO toolchain_proof_cases VALUES(?,?,?,?,?,?,NULL)',
                            (identity, case, len(rows) + 1, job_id, source_hash, binding_hash))
        return dict(self.db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())

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
            "SELECT c.*,j.state,j.name,j.image,j.profile,j.deadline,j.container_id,j.purpose "
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
            elif job["purpose"] == "image_packaging" and job.get("container_id"):
                try:
                    _packaging_helper().require_native_cleanup(job["container_id"])
                except (RuntimeError, OSError, ValueError):
                    self.record(job, "cleanup_uncertain")
        if job["purpose"] == "image_packaging":
            return {"claim_id": claim_id, "packaging_job_id": job["id"], "purpose": job["purpose"],
                    "request_hash": job["request_hash"], "binding_hash": job["binding_hash"],
                    "state": job["state"], "cleanup_verified": job["state"] == "cleaned",
                    "packaging_success": None}
        return {"claim_id": claim_id, "validation_job_id": job["id"],
                "request_hash": job["request_hash"], "binding_hash": job["binding_hash"],
                "state": job["state"], "cleanup_verified": job["state"] == "cleaned",
                "validation_success": None}

    def cleanup(self, job: dict, *, create_confirmed: bool = False, create_not_attempted: bool = False):
        if type(create_not_attempted) is not bool or (create_confirmed and create_not_attempted):
            raise IsolationError("Invalid trusted creation-state evidence")
        ids = docker("ps", "-a", "--no-trunc", "--filter", f"name=^/{job['name']}$",
                     "--format", "{{.ID}}").decode().split()
        if not ids:
            if not create_confirmed and not create_not_attempted and int(time.time()) < job["deadline"]:
                raise IsolationError("Create outcome remains uncertain until the job deadline")
        else:
            if len(ids) != 1 or not re.fullmatch(r"[a-f0-9]{64}", ids[0]):
                raise IsolationError("Ambiguous sandbox identity")
            self.verify_identity(job, ids[0])
            # Persist the exact ID before deletion: a lost rm response must not
            # erase the native identity needed by subsequent reconciliation.
            self.record(job, "cleanup_uncertain", ids[0])
            docker("rm", "--force", "--volumes", ids[0])
        if job["purpose"] == "image_packaging" and not (create_not_attempted and not ids):
            if not job.get("container_id"):
                raise IsolationError("Packaging native identity is unknown; cleanup remains uncertain")
            try:
                _packaging_helper().require_native_cleanup(job["container_id"])
            except (RuntimeError, OSError, ValueError) as error:
                raise IsolationError("Packaging native termination is not verified") from error
        self.record(job, "cleaned")

    def verify_identity(self, job: dict, container_id: str):
        if job.get("container_id") and job["container_id"] != container_id:
            raise IsolationError("Container ID changed; refusing cleanup")
        spec = json.loads(docker("inspect", container_id))[0]
        labels = spec["Config"].get("Labels") or {}
        if (spec["Name"] != "/" + job["name"] or spec["Image"] != job["image"]
                or labels.get("gca.scope") != self.scope
                or labels.get("gca.job") != job["id"]
                or labels.get("gca.purpose") != job["purpose"]):
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
        artifacts_purged = 0
        if (self.directory / "build-artifacts").exists():
            helper = _artifact_helper()
            try:
                artifacts_purged = helper.purge_expired_build_artifacts(self.directory)
            except helper.BuildArtifactError as error:
                raise IsolationError("Build artifact cleanup requires operator inspection") from error
        packaging_purged = 0
        if (self.directory / "packaging-jobs").exists():
            try:
                packaging_purged = _packaging_helper().purge_expired(self)
            except (RuntimeError, OSError, ValueError) as error:
                raise IsolationError("Packaging storage cleanup requires operator inspection") from error
        return {"cleaned_job_ids": cleaned, "uncertain_job_ids": sorted(uncertain),
                "untracked_container_count": untracked,
                "expired_build_artifacts_purged": artifacts_purged,
                "expired_packaging_staging_purged": packaging_purged}


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


def validate_toolchain_registry(document):
    """Typed local-operator provenance; never fabricate a repository pull identity."""
    if (not isinstance(document, dict) or set(document) != {'version', 'images'}
            or type(document['version']) is not int or document['version'] not in (1, 2)
            or not isinstance(document['images'], dict) or not 1 <= len(document['images']) <= 8):
        raise IsolationError('Offline toolchain registry is invalid')
    legacy = {'archive_path', 'archive_sha256', 'archive_bytes', 'repository_digest'}
    operator = {'archive_path', 'archive_sha256', 'archive_bytes', 'origin', 'role', 'import_id',
                'image_index_digest', 'platform_manifest_digest', 'config_digest', 'recipe_sha256',
                'bundle_sha256', 'prior_policy_sha256', 'prior_registry_sha256', 'proofs', 'stop_unix'}
    for identity, record in document['images'].items():
        if (not isinstance(identity, str) or not re.fullmatch(r'sha256:[a-f0-9]{64}', identity)
                or not isinstance(record, dict) or set(record) not in (legacy, operator)
                or not isinstance(record['archive_path'], str) or not Path(record['archive_path']).is_absolute()
                or type(record['archive_bytes']) is not int or not 1 <= record['archive_bytes'] <= 512 * 1024**2
                or not isinstance(record['archive_sha256'], str)
                or not re.fullmatch(r'[a-f0-9]{64}', record['archive_sha256'])):
            raise IsolationError('Offline toolchain record is invalid')
        if set(record) == legacy:
            if (not isinstance(record['repository_digest'], str)
                    or not re.fullmatch(r'[a-zA-Z0-9._:/-]+@sha256:[a-f0-9]{64}', record['repository_digest'])):
                raise IsolationError('Offline repository provenance is invalid')
            continue
        role = record.get('role')
        cases = {'react': {'react_test_pass', 'react_build_pass', 'react_test_fail'},
                 'spring': {'spring_test_pass', 'spring_package_pass', 'spring_test_fail'}}
        if (document['version'] != 2 or record['origin'] != 'reviewed_operator_archive'
                or not isinstance(role, str) or role not in cases
                or record['image_index_digest'] != identity
                or type(record['stop_unix']) is not int or record['stop_unix'] < 1
                or not isinstance(record['proofs'], dict) or set(record['proofs']) != cases[role]
                or any(not isinstance(record[key], str) or not re.fullmatch(r'[a-f0-9]{64}', record[key])
                       for key in ('import_id', 'recipe_sha256', 'bundle_sha256',
                                   'prior_policy_sha256', 'prior_registry_sha256'))
                or any(not isinstance(record[key], str) or not re.fullmatch(r'sha256:[a-f0-9]{64}', record[key])
                       for key in ('platform_manifest_digest', 'config_digest'))
                or any(not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value)
                       for value in record['proofs'].values())):
            raise IsolationError('Offline operator provenance is invalid')
    return document


def ensure_toolchain_image(image_id: str):
    """Reload only an operator-archived exact pin; workload execution never downloads."""
    try:
        observed = docker("image", "inspect", image_id, "--format", "{{.Id}}").decode().strip()
        if observed != image_id:
            raise IsolationError("Toolchain image identity mismatch")
        return
    except IsolationError:
        pass
    registry = TOOLCHAIN_REGISTRY.resolve(strict=True)
    for item in (registry, *registry.parents):
        info = item.stat()
        if info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o022:
            raise IsolationError("Offline toolchain registry is not operator protected")
    if registry.stat().st_size > 32768 or registry.stat().st_mode & 0o077:
        raise IsolationError("Offline toolchain registry is invalid")
    document = json.loads(registry.read_text())
    validate_toolchain_registry(document)
    record = document["images"].get(image_id)
    if isinstance(record, dict) and record.get('origin') == 'reviewed_operator_archive':
        # Never hide a new daemon RPC inside ordinary workload admission. Reuse
        # the explicit same-journal maintenance loader and reconciliation instead.
        raise IsolationError('Operator toolchain is missing; explicit maintenance reconciliation required')
    if (not isinstance(record, dict) or set(record) != {
            "archive_path", "archive_sha256", "archive_bytes", "repository_digest"}
            or not re.fullmatch(r"[0-9a-f]{64}", str(record["archive_sha256"]))
            or not isinstance(record["archive_path"], str)
            or type(record["archive_bytes"]) is not int
            or not 1 <= record["archive_bytes"] <= 512 * 1024 * 1024):
        raise IsolationError("Approved toolchain has no offline archive")
    archive = Path(record["archive_path"])
    if (not archive.is_absolute() or archive.is_symlink()
            or archive.name != image_id.removeprefix("sha256:") + ".tar"):
        raise IsolationError("Offline toolchain archive identity is invalid")
    for item in (archive, *archive.parents):
        info = item.stat()
        if info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o022:
            raise IsolationError("Offline toolchain archive is not operator protected")
    if (not archive.is_file() or archive.stat().st_mode & 0o077
            or archive.stat().st_size != record["archive_bytes"]):
        raise IsolationError("Offline toolchain archive is invalid")
    digest = hashlib.sha256()
    with archive.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    if digest.hexdigest() != record["archive_sha256"]:
        raise IsolationError("Offline toolchain archive digest mismatch")
    docker("image", "load", "--input", str(archive), timeout=60)
    if docker("image", "inspect", image_id, "--format", "{{.Id}}").decode().strip() != image_id:
        raise IsolationError("Reloaded toolchain image identity mismatch")


def execute_bounded(args: list[str], timeout: float, *, binary: bool = False,
                    output_limit: int = MAX_OUTPUT_BYTES, on_started=None) -> dict:
    child = subprocess.Popen(  # nosec B603
        [DOCKER, *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, env=CLEAN_ENV, start_new_session=True,
    )
    output = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + timeout
    truncated = False
    try:
        # Trusted controller hook only, never supplied through a workload frame.
        # The deadline already includes launch/readiness; failures still kill the
        # client and the caller's journal reconciles the actual container.
        if on_started is not None:
            on_started(child)
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
                    remaining = output_limit - sum(map(len, output.values()))
                    output[key.data].extend(chunk[:remaining])
                    truncated = truncated or len(chunk) > remaining
            code = child.wait(timeout=max(0.1, deadline - time.monotonic()))
        return {
            "exit_code": code, "success": code == 0, "output_truncated": truncated,
            **{key: bytes(value) if binary else value.decode("utf-8", errors="replace")
               for key, value in output.items()},
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


def surefire_report_passed(archive_bytes: bytes) -> bool:
    """Require an executed, passing JUnit test; a nonempty XML file is insufficient."""
    if not archive_bytes or len(archive_bytes) > MAX_REPORT_ARCHIVE_BYTES:
        return False
    total_executed = 0
    reports = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:") as archive:
            for member in archive:
                if member.isdir():
                    continue
                if not member.isfile() or member.size > 1_048_576:
                    return False
                if not re.fullmatch(r"target/surefire-reports/TEST-[^/]+\.xml", member.name):
                    continue
                reports += 1
                if reports > 128:
                    return False
                stream = archive.extractfile(member)
                if stream is None:
                    return False
                xml_bytes = stream.read(1_048_577)
                if b"<!DOCTYPE" in xml_bytes.upper() or b"<!ENTITY" in xml_bytes.upper():
                    return False
                root = ET.fromstring(xml_bytes)  # nosec B314
                if root.tag != "testsuite":
                    return False
                counts = {}
                for key in ("tests", "failures", "errors", "skipped"):
                    value = root.get(key)
                    if value is None or not re.fullmatch(r"\d{1,8}", value):
                        return False
                    counts[key] = int(value)
                if (counts["failures"] or counts["errors"]
                        or counts["skipped"] > counts["tests"]):
                    return False
                total_executed += counts["tests"] - counts["skipped"]
    except (tarfile.TarError, ET.ParseError, OSError, ValueError):
        return False
    return reports > 0 and total_executed > 0


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
    check_archive(source, profile)
    if binding is not None:
        verify_dispatch(binding, policy, source, profile, timeout)
    journal.require_available(binding)
    ensure_toolchain_image(policy["images"][profile])
    job = journal.begin(profile, policy["images"][profile], timeout, binding)
    return _execute_archive(source, profile, timeout, policy, journal, job, binding)


def _execute_archive(source: bytes, profile: str, timeout: int, policy: dict, journal: Journal,
                     job: dict, binding: dict | None = None, *, readiness=None,
                     native_cleanup=None, proof_cgroup: str | None = None) -> dict:
    """Shared command implementation; admission stays in the two trusted callers.

    Hooks are Python callables owned by reviewed host code, never frame parameters.
    No toolchain loading, source selection or role approval happens here.
    """
    relative_cwd, command = PROFILES[profile]
    artifact_helper = (_artifact_helper() if binding is not None
                       and profile in {"react_build", "spring_package"} else None)
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
            "--cpus=1", "--pids-limit=" + ('64' if proof_cgroup else '128'), "--ulimit=nofile=256:256",
            *(["--cgroup-parent=" + proof_cgroup, "--ipc=private", "--cgroupns=private"]
              if proof_cgroup else []),
            # Tests/builds intentionally execute binaries/native modules produced
            # inside this disposable sandbox. Docker otherwise defaults tmpfs to
            # noexec; that breaks Node/Rust and can block JNI/native test loading.
            # This is not a host bind mount or packaging's private noexec volume.
            "--tmpfs=/workspace:rw,nosuid,nodev,exec,size=256m,mode=1777",
            # JVM/native tooling extracts transient libraries here. Only these
            # sandbox-internal tmpfs mounts permit execution; nosuid/nodev,
            # memory limits, keylessness and gVisor remain the security boundary.
            "--tmpfs=/tmp:rw,nosuid,nodev,exec,size=64m,mode=1777",
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
        enforcement = readiness(journal, job, container_id) if readiness else None
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
        executed_commands = []
        export_path = "dist" if profile == "react_build" else None
        build_artifact = None
        for fixed_command in (*([PREPARE_COMMANDS[profile]] if profile in PREPARE_COMMANDS else []),
                              command):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise IsolationError("Validation deadline exceeded")
            result = execute_bounded(["exec", "--workdir", workdir, name, *fixed_command],
                                     remaining)
            executed_commands.append(list(fixed_command))
            if not result["success"]:
                break
        if result["success"]:
            if profile in {"spring_test", "spring_package"}:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise IsolationError("Validation deadline exceeded")
                reports = execute_bounded(
                    ["exec", "--workdir", workdir, name, "tar", "-cf", "-",
                     "target/surefire-reports"], remaining, binary=True,
                    output_limit=MAX_REPORT_ARCHIVE_BYTES,
                )
                if (not reports["success"] or reports["output_truncated"]
                        or not surefire_report_passed(reports["stdout"])):
                    result = {"exit_code": 1, "success": False, "output_truncated": False,
                              "stdout": "", "stderr": "No executed passing Surefire test report"}
        if result["success"]:
            postconditions = {
                "react_build": (("test", "-s", "dist/index.html"),),
                "spring_package": (
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
                if profile == "spring_package" and postcondition[0] == "find":
                    paths = artifact["stdout"].splitlines()
                    if (artifact["output_truncated"] or len(paths) != 1
                            or not re.fullmatch(r"target/[A-Za-z0-9][A-Za-z0-9._-]*\.jar", paths[0])):
                        result = {"exit_code": 1, "success": False, "output_truncated": False,
                                  "stdout": "", "stderr": "Expected exactly one package JAR"}
                        break
                    export_path = paths[0]
        if result["success"] and artifact_helper is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise IsolationError("Validation deadline exceeded")
            captured = execute_bounded(
                ["exec", "--workdir", workdir, name, "tar", "-cf", "-", export_path],
                remaining, binary=True,
                output_limit=artifact_helper.MAX_ARTIFACT_ARCHIVE_BYTES,
            )
            if not captured["success"] or captured["output_truncated"]:
                raise IsolationError("Generated artifact export failed or exceeded bounds")
            try:
                manifest = artifact_helper.inspect_build_artifact(captured["stdout"], profile)
                artifact_id = artifact_helper.save_build_artifact(
                    journal.directory, job["id"], captured["stdout"], manifest,
                )
            except (artifact_helper.BuildArtifactError, OSError) as error:
                raise IsolationError("Generated artifact could not be retained safely") from error
            file_set_hash = hashlib.sha256(json.dumps(
                manifest.file_hashes, separators=(",", ":"),
            ).encode()).hexdigest()
            build_artifact = {
                "artifact_id": artifact_id, "archive_sha256": manifest.archive_sha256,
                "file_set_sha256": file_set_hash, "file_count": manifest.file_count,
                "content_bytes": manifest.content_bytes, "profile": profile,
                "validation_job_id": job["id"], "claim_id": binding["claim_id"],
                "source_hash": binding["source_hash"],
                "expires_at_unix": int(time.time()) + artifact_helper.ARTIFACT_TTL_SECONDS,
            }
        return {
            **result, "profile": profile, "isolation": "gvisor",
            "image_id": policy["images"][profile], "network": "none",
            "workspace_mutations_discarded": True,
            "validation_job_id": job["id"], "cleanup_verified": True,
            **({"executed_commands": executed_commands} if readiness else {}),
            **({"native_enforcement": enforcement} if enforcement is not None else {}),
            **({"claim_id": binding["claim_id"], "request_hash": binding["request_hash"],
                "source_hash": binding["source_hash"]} if binding else {}),
            **({"build_artifact": build_artifact} if build_artifact is not None else {}),
        }
    finally:
        # Failure here overrides success. The caller must record cleanup uncertainty.
        try:
            journal.cleanup(job, create_confirmed=create_confirmed)
            if native_cleanup is not None and job.get('container_id'):
                native_cleanup(job['container_id'])
        except (RuntimeError, OSError, subprocess.TimeoutExpired, ValueError, KeyError) as error:
            journal.record(job, "cleanup_uncertain")
            raise IsolationError(f"Sandbox cleanup uncertain for {name}") from error


def retained_artifact(journal: Journal, binding: dict, reference: dict) -> bytes:
    """Read exact stored output for an already consumed, reconciled host claim."""
    if not isinstance(binding, dict) or not isinstance(reference, dict):
        raise IsolationError("Invalid retained artifact request")
    claim_id = binding.get("claim_id")
    if not isinstance(claim_id, str) or str(uuid.UUID(claim_id)) != claim_id:
        raise IsolationError("Invalid retained artifact claim")
    row = journal.db.execute(
        "SELECT c.binding_hash,c.request_hash,j.id,j.state,j.profile FROM dispatch_claims c "
        "JOIN jobs j ON j.id=c.job_id WHERE c.claim_id=? AND j.purpose='validation'", (claim_id,),
    ).fetchone()
    binding_hash = hashlib.sha256(json.dumps(binding, sort_keys=True,
                                            separators=(",", ":")).encode()).hexdigest()
    if (row is None or row["state"] != "cleaned" or row["binding_hash"] != binding_hash
            or row["request_hash"] != binding.get("request_hash")
            or row["profile"] != binding.get("profile")):
        raise IsolationError("Retained artifact claim is unknown, altered or unreconciled")
    helper = _artifact_helper()
    try:
        verified = helper.verify_artifact_reference(
            reference, profile=row["profile"], source_hash=binding["source_hash"],
            claim_id=claim_id, job_id=row["id"],
        )
        return helper.load_build_artifact(journal.directory, verified["artifact_id"],
                                          verified["archive_sha256"])
    except (helper.BuildArtifactError, OSError, KeyError) as error:
        raise IsolationError("Retained artifact is unavailable or unbound") from error


def gateway(stream, policy: dict, *, output=None) -> dict | tuple[dict, bytes] | None:
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
        if (set(request) == {"operation", "binding", "reference"}
                and request["operation"] == "artifact"):
            body = retained_artifact(journal, request["binding"], request["reference"])
            metadata = {"operation": "artifact", "reference": request["reference"],
                        "archive_bytes": len(body)}
            if output is not None:
                # Keep the host slot and upload/transfer alarm until the private bytes
                # are drained: slow clients cannot multiply staging-memory allocations.
                output.write(json.dumps(metadata, separators=(",", ":")).encode() + b"\n")
                output.write(body)
                output.flush()
                return None
            return metadata, body
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
            result = gateway(sys.stdin.buffer, load_policy(config, None), output=sys.stdout.buffer)
            signal.alarm(0)
            if result is not None:
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
