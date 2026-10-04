"""Operator-only, crash-recoverable installation of reviewed stdlib host files.

Not a model tool. Fixed host paths, no policy edits or generated workloads. The
directory swap is two renames: the interval with no gateway is deliberately closed,
not claimed atomic. A durable external intent and exact old-tree hashes permit
rollback after process death, without deleting the validation journal or artifacts.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess  # nosec B404
import sys
import tempfile


ROOT = Path("/usr/local/libexec/gca")
RELEASES = Path("/usr/local/libexec/gca-bundle-releases")
POLICY = Path("/etc/gca-validation/policy.json")
NAMES = ("isolated_validation.py", "build_artifact.py", "oci_packager.py",
         "runtime_image_store.py", "image_output_store.py", "image_packaging_worker.py",
         "isolated_packaging.py")
MAX_FRAME = 256000


class BundleError(RuntimeError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(body):
    return hashlib.sha256(body).hexdigest()


def safe(path, *, private=False):
    for item in (path, *path.parents):
        info = item.lstat()
        if (item.is_symlink() or info.st_uid not in {0, os.geteuid()}
                or info.st_mode & 0o022 or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
                or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)):
            raise BundleError("operator bundle path is not protected")
    if private and path.stat().st_mode & 0o077:
        raise BundleError("operator bundle state is not private")
    return path


def sync(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def snapshot(root):
    safe(root)
    result = {}
    for path in sorted(root.iterdir()):
        safe(path)
        if path.name == "__pycache__" and path.is_dir():
            entries = list(path.iterdir())
            if len(entries) > 16:
                raise BundleError("old bundle cache exceeds bound")
            for child in entries:
                if not re.fullmatch(r"[a-z_]+\.cpython-[0-9]+(?:\.opt-[0-9])?\.pyc", child.name):
                    raise BundleError("untracked old bundle cache")
                safe(child)
                if not child.is_file() or child.stat().st_size > 256000:
                    raise BundleError("old bundle cache inode differs")
                result["__pycache__/" + child.name] = {"hash": digest(child.read_bytes()),
                                                     "mode": stat.S_IMODE(child.stat().st_mode)}
        elif path.name in NAMES and path.is_file() and 0 < path.stat().st_size <= 256000:
            result[path.name] = {"hash": digest(path.read_bytes()), "mode": stat.S_IMODE(path.stat().st_mode)}
        else:
            raise BundleError("unknown object in operator bundle")
    if not {"isolated_validation.py", "build_artifact.py"} <= set(result):
        raise BundleError("previous validation bundle is incomplete")
    return result


def write(path, body, mode=0o600):
    fd, temporary = tempfile.mkstemp(prefix=".intent-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        sync(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def record():
    path = RELEASES / "transaction.json"
    if not path.exists():
        return None
    safe(path, private=True)
    if path.stat().st_size > 32768:
        raise BundleError("installation intent exceeds bound")
    result = json.loads(path.read_bytes())
    fields = {"version", "id", "phase", "old", "new"}
    if (set(result) != fields or result["version"] != 1
            or not isinstance(result["id"], str) or not re.fullmatch(r"[0-9a-f]{64}", result["id"])
            or result["phase"] not in {"prepared", "root_saved", "published", "committed", "rolled_back"}
            or not isinstance(result["old"], dict) or not isinstance(result["new"], dict)
            or set(result["new"]) != set(NAMES)):
        raise BundleError("installation intent is invalid")
    return result


def set_phase(transaction, phase):
    transaction["phase"] = phase
    write(RELEASES / "transaction.json", canonical(transaction))


def verified(path, expected):
    if snapshot(path) != expected:
        raise BundleError("installed or saved bundle changed")


def restore(transaction):
    """Exact old-tree restoration, including a killed two-rename swap."""
    previous = RELEASES / (transaction["id"] + ".previous")
    staged = RELEASES / (transaction["id"] + ".staged")
    if previous.exists():
        verified(previous, transaction["old"])
        if ROOT.exists() or ROOT.is_symlink():
            current = snapshot(ROOT)
            if current != transaction["new"]:
                raise BundleError("rollback refuses an unrecognized current bundle")
            if staged.exists():
                raise BundleError("rollback staging destination is occupied")
            os.rename(ROOT, staged)
            sync(ROOT.parent)
            sync(RELEASES)
        os.rename(previous, ROOT)
        sync(ROOT.parent)
        sync(RELEASES)
    else:
        verified(ROOT, transaction["old"])
    verified(ROOT, transaction["old"])
    set_phase(transaction, "rolled_back")
    return {"operation": "rollback", "installation_id": transaction["id"],
            "rolled_back": True, "snapshot_hash": digest(canonical(transaction["old"]))}


def probe_import(directory):
    # Only reviewed operator source; no generated module, Docker or provider import.
    source = ("import runpy; "
              "v=runpy.run_path(" + repr(str(directory / "isolated_validation.py")) + "); "
              "assert v['_artifact_helper']().MAX_STORED_ARTIFACTS==8; "
              "runpy.run_path(" + repr(str(directory / "isolated_packaging.py")) + "); "
              "runpy.run_path(" + repr(str(directory / "image_packaging_worker.py")) + ")")
    result = subprocess.run(  # nosec B603
        ["/usr/bin/python3", "-I", "-B", "-c", source], stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
        env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"}, check=False,
    )
    if result.returncode:
        raise BundleError("reviewed bundle isolated import failed")


def locked_operation(request, state):
    existing = record() if RELEASES.exists() else None
    if request["operation"] == "rollback":
        if existing is None or request["installation_id"] != existing["id"]:
            raise BundleError("rollback installation identity differs")
        return restore(existing)
    if existing and existing["phase"] not in {"committed", "rolled_back"}:
        if request["operation"] == "probe":
            current = snapshot(ROOT) if ROOT.exists() else None
            if current is not None and current not in (existing["old"], existing["new"]):
                raise BundleError("interrupted installation has an unrecognized bundle")
            return {"operation": "probe", "journal_idle": True, "interrupted": True,
                    "installation_id": existing["id"], "phase": existing["phase"],
                    "snapshot_hash": digest(canonical(current)) if current else None}
        raise BundleError("interrupted installation requires explicit rollback")
    current = snapshot(ROOT)
    fingerprint = digest(canonical(current))
    if request["operation"] == "probe":
        return {"operation": "probe", "journal_idle": True, "snapshot_hash": fingerprint,
                "hashes": {name: value["hash"] for name, value in current.items()},
                "last_installation": existing["id"] if existing else None}
    if request["expected_snapshot_hash"] != fingerprint:
        raise BundleError("operator bundle changed since review")
    files = request["files"]
    if not isinstance(files, dict) or set(files) != set(NAMES):
        raise BundleError("exact seven-file reviewed bundle required")
    for name, source in files.items():
        if not isinstance(source, str) or not 0 < len(source.encode()) <= 100000:
            raise BundleError("reviewed source exceeds its bound")
        compile(source, name, "exec")
    new = {name: {"hash": digest(source.encode()), "mode": 0o755 if name == "isolated_validation.py" else 0o644}
           for name, source in files.items()}
    if existing and existing["phase"] == "committed" and current == new == existing["new"]:
        return {"operation": "apply", "installation_id": existing["id"], "journal_idle": True,
                "hashes": {name: value["hash"] for name, value in new.items()},
                "isolated_import_passed": True, "policy_changed": False, "workload_executed": False}
    identity = digest(canonical({"old": current, "new": new}))
    transaction = {"version": 1, "id": identity, "phase": "prepared", "old": current, "new": new}
    if not RELEASES.exists():
        RELEASES.mkdir(mode=0o700)
        sync(RELEASES.parent)
    safe(RELEASES, private=True)
    if len(list(RELEASES.iterdir())) >= 8:
        raise BundleError("operator release retention exhausted")
    staged, previous = (RELEASES / (identity + suffix) for suffix in (".staged", ".previous"))
    if staged.exists() or previous.exists():
        raise BundleError("installation identity already has retained state")
    # Intent precedes staging, not just the directory swap. An interrupted import
    # or copy must also have an inspectable identity and explicit recovery path.
    set_phase(transaction, "prepared")
    staged.mkdir(mode=0o755)
    for name, source in files.items():
        write(staged / name, source.encode(), new[name]["mode"])
    verified(staged, new)
    probe_import(staged)
    sync(staged)
    os.rename(ROOT, previous)
    sync(ROOT.parent)
    sync(RELEASES)
    set_phase(transaction, "root_saved")
    os.rename(staged, ROOT)
    sync(ROOT.parent)
    sync(RELEASES)
    set_phase(transaction, "published")
    verified(ROOT, new)
    probe_import(ROOT)
    set_phase(transaction, "committed")
    return {"operation": "apply", "installation_id": identity, "journal_idle": True,
            "hashes": {name: value["hash"] for name, value in new.items()},
            "isolated_import_passed": True, "policy_changed": False, "workload_executed": False}


def operate(request):
    if not isinstance(request, dict) or request.get("operation") not in {"probe", "apply", "rollback"}:
        raise BundleError("invalid operator bundle operation")
    fields = {"probe": {"operation"}, "apply": {"operation", "expected_snapshot_hash", "files"},
              "rollback": {"operation", "installation_id"}}[request["operation"]]
    if set(request) != fields:
        raise BundleError("invalid operator installation envelope")
    safe(ROOT.parent)
    safe(POLICY)
    if POLICY.stat().st_size > 32768:
        raise BundleError("validation policy exceeds bound")
    state = Path(json.loads(POLICY.read_bytes())["state_directory"])
    if not state.is_absolute():
        raise BundleError("host journal path is not absolute")
    safe(state, private=True)
    if RELEASES.exists():
        safe(RELEASES, private=True)
    safe(state / "slot.lock", private=True)
    lock = os.open(state / "slot.lock", os.O_RDWR | os.O_NOFOLLOW)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        database = safe(state / "jobs.sqlite3", private=True)
        with sqlite3.connect("file:" + str(database) + "?mode=ro", uri=True) as connection:
            if connection.execute("SELECT 1 FROM jobs WHERE state<>'cleaned' LIMIT 1").fetchone():
                raise BundleError("unreconciled host job prevents installation or rollback")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
            if request["operation"] == "rollback" and "purpose" in columns:
                if connection.execute("SELECT 1 FROM jobs WHERE purpose='image_packaging' LIMIT 1").fetchone():
                    raise BundleError("packaging history requires a forward-compatible rollback review")
        if request["operation"] == "rollback" and (state / "packaging-jobs").exists():
            raise BundleError("retained packaging staging prevents old-bundle rollback")
        return locked_operation(request, state)
    finally:
        os.close(lock)


def main():
    if sys.platform != "linux" or os.geteuid() != 0:
        raise BundleError("bundle installation requires operator Linux root")
    body = sys.stdin.buffer.read(MAX_FRAME + 1)
    if len(body) > MAX_FRAME:
        raise BundleError("operator installation frame exceeds bound")
    result = operate(json.loads(body))
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
