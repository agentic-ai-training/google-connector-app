"""Operator-only, crash-recoverable installation of reviewed stdlib host files.

Not a model tool. Fixed host paths, no policy edits or generated workloads. The
directory swap is two renames: the interval with no gateway is deliberately closed,
not claimed atomic. A durable external intent and exact old-tree hashes permit
rollback after process death, without deleting the validation journal or artifacts.
"""
import base64
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
import zlib


ROOT = Path("/usr/local/libexec/gca")
RELEASES = Path("/usr/local/libexec/gca-bundle-releases")
POLICY = Path("/etc/gca-validation/policy.json")
REGISTRY = Path('/etc/gca-validation/toolchain-registry.json')
NAMES = ("isolated_validation.py", "build_artifact.py", "oci_packager.py",
         "runtime_image_store.py", "image_output_store.py", "image_packaging_worker.py",
         "isolated_packaging.py", "toolchain_spec.py", "toolchain_archive.py",
         "toolchain_staging.py", "operator_toolchain_host.py", "toolchain_loader.py",
         "toolchain_proof.py", "toolchain_publication.py")
# A completed historical installation is evidence, not a request to install its
# old inventory. Accept only the exact previous protocol and the current one.
LEGACY_NAMES = NAMES[:7]
PRIOR_CUSTODY_NAMES = NAMES[:11]
PRIOR_LOADER_NAMES = NAMES[:12]
PRIOR_PROOF_NAMES = NAMES[:13]
MAX_FRAME = 256000
MAX_DECODED_FRAME = 384000


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


def snapshot(root, *, allow_incomplete=False):
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
    if not allow_incomplete and not {"isolated_validation.py", "build_artifact.py"} <= set(result):
        raise BundleError("previous validation bundle is incomplete")
    return result


def retirements(state):
    path = state / 'bundle-retirements.json'
    if not path.exists():
        return {}
    safe(path, private=True)
    if path.stat().st_size > 65536:
        raise BundleError('bundle retirement audit exceeds bound')
    result = json.loads(path.read_bytes())
    if not isinstance(result, dict) or len(result) > 32:
        raise BundleError('bundle retirement audit differs')
    for identity, item in result.items():
        if (not re.fullmatch(r'[a-f0-9]{64}', identity) or not isinstance(item, dict)
                or set(item) != {'phase', 'files'} or item['phase'] not in {'prepared', 'committed'}
                or not isinstance(item['files'], dict) or not 2 <= len(item['files']) <= 23
                or not {'isolated_validation.py', 'build_artifact.py'} <= set(item['files'])):
            raise BundleError('bundle retirement audit differs')
        for name, metadata in item['files'].items():
            if (name not in NAMES and not re.fullmatch(r'__pycache__/[a-z_]+\.cpython-[0-9]+(?:\.opt-[0-9])?\.pyc', name)):
                raise BundleError('bundle retirement filename differs')
            if (not isinstance(metadata, dict) or set(metadata) != {'hash', 'mode'}
                    or not isinstance(metadata['hash'], str) or not re.fullmatch(r'[a-f0-9]{64}', metadata['hash'])
                    or type(metadata['mode']) is not int or not 0 <= metadata['mode'] <= 0o7777):
                raise BundleError('bundle retirement file evidence differs')
    return result


def retention_inventory(existing, audit):
    result = {}
    entries = list(RELEASES.iterdir()) if RELEASES.exists() else []
    if len(entries) > 32:
        raise BundleError('operator retention inventory exceeds bound')
    for path in entries:
        safe(path)
        if path.name == 'transaction.json' and path.is_file():
            continue
        if not path.is_dir() or not re.fullmatch(r'[a-f0-9]{64}\.previous', path.name):
            raise BundleError('untracked retention object requires review')
        identity = path.name.removesuffix('.previous')
        if audit.get(identity, {}).get('phase') == 'committed':
            raise BundleError('retired bundle unexpectedly reappeared')
        pending = audit.get(identity, {}).get('phase') == 'prepared'
        tree = snapshot(path, allow_incomplete=pending)
        if pending and any(audit[identity]['files'].get(name) != item for name, item in tree.items()):
            raise BundleError('interrupted retirement has foreign remaining files')
        result[identity] = {'snapshot_hash': digest(canonical(tree)), 'file_count': len(tree),
                            'retirement_pending': pending,
                            'retireable': bool(existing and identity != existing['id'])}
    return {'entry_count': len(entries), 'copies': result}


def retire(request, state, existing, audit, current, inventory):
    identity = request['installation_id']
    if (not isinstance(identity, str) or not re.fullmatch(r'[a-f0-9]{64}', identity)
            or existing is None or identity == existing['id']):
        raise BundleError('current installation predecessor cannot be retired')
    if request['expected_retention_hash'] != digest(canonical(inventory)):
        raise BundleError('operator retention snapshot changed')
    if any(item['phase'] == 'prepared' and key != identity for key, item in audit.items()):
        raise BundleError('prior bundle retirement needs reconciliation')
    target = RELEASES / (identity + '.previous')
    item = audit.get(identity)
    if item is None:
        if len(audit) >= 32 or identity not in inventory['copies']:
            raise BundleError('retirement identity absent or audit capacity exhausted')
        item = {'phase': 'prepared', 'files': snapshot(target)}
        audit[identity] = item
        if len(canonical(audit)) > 65536:
            raise BundleError('bundle retirement audit exceeds bound')
        write(state / 'bundle-retirements.json', canonical(audit))
    if target.exists() or target.is_symlink():
        tree = snapshot(target, allow_incomplete=True)
        if any(item['files'].get(name) != metadata for name, metadata in tree.items()):
            raise BundleError('obsolete bundle changed before retirement')
        for name in sorted(tree):
            path = safe(target / name)
            path.unlink()
            sync(path.parent)
        cache = target / '__pycache__'
        if cache.exists():
            safe(cache)
            cache.rmdir()
            sync(target)
        target.rmdir()
        sync(RELEASES)
    if snapshot(ROOT) != current:
        raise BundleError('active bundle changed during retirement')
    item['phase'] = 'committed'
    write(state / 'bundle-retirements.json', canonical(audit))
    return {'operation': 'retire', 'installation_id': identity, 'retired': True,
            'active_bundle_preserved': True, 'data_journal_changed': False,
            'snapshot_hash': digest(canonical(current)), 'journal_idle': True}


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
    if (not isinstance(result, dict) or set(result) != fields or type(result["version"]) is not int
            or result["version"] != 1
            or not isinstance(result["id"], str) or not re.fullmatch(r"[0-9a-f]{64}", result["id"])
            or result["phase"] not in {"prepared", "root_saved", "published", "committed", "rolled_back"}
            or not isinstance(result["old"], dict) or not isinstance(result["new"], dict)
            or set(result["new"]) not in (set(LEGACY_NAMES), set(PRIOR_CUSTODY_NAMES),
                                         set(PRIOR_LOADER_NAMES), set(PRIOR_PROOF_NAMES), set(NAMES))):
        raise BundleError("installation intent is invalid")
    for tree in (result['old'], result['new']):
        if not 2 <= len(tree) <= 23 or not {'isolated_validation.py', 'build_artifact.py'} <= set(tree):
            raise BundleError('installation tree is invalid')
        for name, item in tree.items():
            if ((name not in NAMES and not re.fullmatch(
                    r'__pycache__/[a-z_]+\.cpython-[0-9]+(?:\.opt-[0-9])?\.pyc', name))
                    or not isinstance(item, dict) or set(item) != {'hash', 'mode'}
                    or not isinstance(item['hash'], str) or not re.fullmatch(r'[a-f0-9]{64}', item['hash'])
                    or type(item['mode']) is not int or not 0 <= item['mode'] <= 0o7777):
                raise BundleError('installation tree evidence is invalid')
    if result['id'] != digest(canonical({'old': result['old'], 'new': result['new']})):
        raise BundleError('installation intent identity differs')
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
              "runpy.run_path(" + repr(str(directory / "image_packaging_worker.py")) + "); "
              "runpy.run_path(" + repr(str(directory / "operator_toolchain_host.py")) + ")")
    result = subprocess.run(  # nosec B603
        ["/usr/bin/python3", "-I", "-B", "-c", source], stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
        env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"}, check=False,
    )
    if result.returncode:
        raise BundleError("reviewed bundle isolated import failed")


def verify_enrolled(connection, state):
    """Read-only publication check using the protected current/predecessor reader.

    No new Journal is opened: the installer already owns the same flock and a
    read-only SQLite connection. An interrupted rename may require the exact
    recorded predecessor source, never arbitrary request/workspace code.
    """
    source_path = ROOT / 'isolated_validation.py'
    expected = None
    if not ROOT.exists():
        transaction = record()
        if transaction is None:
            raise BundleError('published history has no protected source reader')
        source_path = RELEASES / (transaction['id'] + '.previous') / 'isolated_validation.py'
        expected = transaction['old'].get('isolated_validation.py', {}).get('hash')
    safe(source_path)
    if not source_path.is_file() or not 1 <= source_path.stat().st_size <= 100000:
        raise BundleError('publication reader source bound differs')
    body = source_path.read_bytes()
    if expected is not None and digest(body) != expected:
        raise BundleError('publication predecessor reader differs')
    namespace = {'__name__': 'gca_installer_publication_reader', '__file__': str(source_path), '__package__': None}
    exec(compile(body, str(source_path), 'exec'), namespace)  # nosec B102
    namespace['VALIDATION_POLICY'] = POLICY
    namespace['TOOLCHAIN_REGISTRY'] = REGISTRY
    reader = namespace.get('Journal')
    if reader is None or not callable(getattr(reader, 'verify_publications', None)):
        raise BundleError('protected source cannot interpret published history')
    connection.row_factory = sqlite3.Row
    journal = reader.__new__(reader)
    journal.directory, journal.db, journal.lock = state, connection, None
    journal.verify_publications()
    if callable(getattr(journal, 'verify_rejections', None)):
        journal.verify_rejections()
    elif connection.execute("SELECT 1 FROM toolchain_imports WHERE phase='rejected' LIMIT 1").fetchone():
        raise BundleError('protected source cannot interpret rejection history')


def reviewed_files(files):
    """Reproduce a complete candidate tree; no hash-only or caller-passed claim."""
    if not isinstance(files, dict) or set(files) != set(NAMES):
        raise BundleError("exact reviewed operator bundle required")
    for name, source in files.items():
        if not isinstance(source, str) or not 0 < len(source.encode()) <= 100000:
            raise BundleError("reviewed source exceeds its bound")
        compile(source, name, "exec")
    return {name: {"hash": digest(source.encode()),
                   "mode": 0o755 if name == "isolated_validation.py" else 0o644}
            for name, source in files.items()}


def resume_forward(request, state, transaction):
    """Finish this exact recorded update, never restore an older launcher.

    Durable phase writes can lag renames. The actual protected trees decide which
    step remains, but never authorize a new candidate or removal of unknown data.
    The caller already owns the host flock and verified no pending host work.
    """
    if (transaction is None or request['installation_id'] != transaction['id']
            or transaction['phase'] == 'rolled_back'
            or request['expected_old_snapshot_hash'] != digest(canonical(transaction['old']))):
        raise BundleError('forward resume installation identity differs')
    new = reviewed_files(request['files'])
    if new != transaction['new']:
        raise BundleError('forward resume source differs from recorded candidate')
    if any(item['phase'] == 'prepared' for item in retirements(state).values()):
        raise BundleError('bundle retirement needs reconciliation before forward resume')
    safe(RELEASES, private=True)
    staged = RELEASES / (transaction['id'] + '.staged')
    previous = RELEASES / (transaction['id'] + '.previous')
    entries = list(RELEASES.iterdir())
    # An interrupted swap temporarily has one extra staging tree. No new release
    # allocation or retention-limit increase is granted by recovery.
    if len(entries) > 9:
        raise BundleError('forward resume retention inventory exceeds bound')
    for path in entries:
        safe(path)
        if path.name == 'transaction.json' and path.is_file():
            continue
        if path == staged and path.is_dir():
            continue
        if not path.is_dir() or not re.fullmatch(r'[a-f0-9]{64}\.previous', path.name):
            raise BundleError('forward resume has an untracked retention object')
        snapshot(path)
    current = snapshot(ROOT) if ROOT.exists() or ROOT.is_symlink() else None
    if current not in (None, transaction['old'], new):
        raise BundleError('forward resume refuses an unrecognized current bundle')
    if previous.exists() or previous.is_symlink():
        verified(previous, transaction['old'])
        if current == transaction['old']:
            raise BundleError('forward resume has contradictory old-tree ownership')
    elif current != transaction['old']:
        raise BundleError('forward resume is missing its protected predecessor')
    if ((transaction['phase'] in {'published', 'committed'} and current != new)
            or (transaction['phase'] == 'root_saved' and current == transaction['old'])):
        raise BundleError('forward resume durable phase contradicts its trees')
    if current == new:
        if staged.exists() or staged.is_symlink():
            raise BundleError('forward resume staging destination is occupied')
        probe_import(ROOT)
    else:
        if staged.exists() or staged.is_symlink():
            remaining = snapshot(staged, allow_incomplete=True)
            if any(new.get(name) != metadata for name, metadata in remaining.items()):
                raise BundleError('forward resume staged prefix differs')
        else:
            if current is None:
                raise BundleError('forward resume lost its complete staging tree')
            staged.mkdir(mode=0o755)
            sync(RELEASES)
            remaining = {}
        if current is None and remaining != new:
            raise BundleError('forward resume saved root lacks a complete staging tree')
        for name, source in request['files'].items():
            if name not in remaining:
                write(staged / name, source.encode(), new[name]['mode'])
        verified(staged, new)
        probe_import(staged)
        sync(staged)
        if current is not None:
            os.rename(ROOT, previous)
            sync(ROOT.parent)
            sync(RELEASES)
        set_phase(transaction, 'root_saved')
        os.rename(staged, ROOT)
        sync(ROOT.parent)
        sync(RELEASES)
        set_phase(transaction, 'published')
        verified(ROOT, new)
        probe_import(ROOT)
    verified(previous, transaction['old'])
    verified(ROOT, new)
    set_phase(transaction, 'committed')
    return {'operation': 'resume_forward', 'installation_id': transaction['id'],
            'journal_idle': True, 'forward_resumed': True, 'predecessor_preserved': True,
            'hashes': {name: value['hash'] for name, value in new.items()},
            'isolated_import_passed': True, 'policy_changed': False,
            'workload_executed': False, 'data_journal_changed': False}


def locked_operation(request, state):
    existing = record() if RELEASES.exists() else None
    if request['operation'] == 'resume_forward':
        return resume_forward(request, state, existing)
    if request["operation"] == "rollback":
        if any(item['phase'] == 'prepared' for item in retirements(state).values()):
            raise BundleError('bundle retirement needs reconciliation before rollback')
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
                    "old_snapshot_hash": digest(canonical(existing['old'])),
                    "candidate_snapshot_hash": digest(canonical(existing['new'])),
                    "forward_resume_supported": set(existing['new']) == set(NAMES),
                    "snapshot_hash": digest(canonical(current)) if current else None}
        raise BundleError("interrupted installation requires explicit rollback or exact forward resume")
    current = snapshot(ROOT)
    fingerprint = digest(canonical(current))
    audit = retirements(state)
    inventory = retention_inventory(existing, audit)
    if request["operation"] == "probe":
        return {"operation": "probe", "journal_idle": True, "snapshot_hash": fingerprint,
                "hashes": {name: value["hash"] for name, value in current.items()},
                "last_installation": existing["id"] if existing else None,
                "retention_hash": digest(canonical(inventory)), "retention_inventory": inventory,
                "retirement_pending": [key for key, item in audit.items() if item['phase'] == 'prepared']}
    if request["expected_snapshot_hash"] != fingerprint:
        raise BundleError("operator bundle changed since review")
    if request['operation'] == 'retire':
        return retire(request, state, existing, audit, current, inventory)
    if any(item['phase'] == 'prepared' for item in audit.values()):
        raise BundleError('bundle retirement needs reconciliation before installation')
    files = request["files"]
    new = reviewed_files(files)
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
    if not isinstance(request, dict) or request.get("operation") not in {
        "probe", "apply", "rollback", "retire", "resume_forward",
    }:
        raise BundleError("invalid operator bundle operation")
    fields = {"probe": {"operation"}, "apply": {"operation", "expected_snapshot_hash", "files"},
              "rollback": {"operation", "installation_id"},
              "resume_forward": {"operation", "installation_id", "expected_old_snapshot_hash", "files"},
              "retire": {"operation", "installation_id", "expected_snapshot_hash", "expected_retention_hash"}}[request["operation"]]
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
        pending_imports = False
        with sqlite3.connect("file:" + str(database) + "?mode=ro", uri=True) as connection:
            if connection.execute("SELECT 1 FROM jobs WHERE state<>'cleaned' LIMIT 1").fetchone():
                raise BundleError("unreconciled host job prevents installation or rollback")
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='toolchain_imports'").fetchone():
                # Read-only observer; never expire/delete maintenance on controller
                # loss. Old bundles lack this barrier, so rollback after any import
                # history requires a forward-compatible operator review.
                imports = connection.execute("SELECT phase FROM toolchain_imports LIMIT 9").fetchall()
                pending_imports = len(imports) > 8 or any(row[0] not in {'cancelled', 'enrolled', 'rejected'} for row in imports)
                if not pending_imports and any(row[0] in {'enrolled', 'rejected'} for row in imports):
                    try:
                        verify_enrolled(connection, state)
                    except (RuntimeError, OSError, ValueError, KeyError, TypeError, sqlite3.Error):
                        pending_imports = True
                if pending_imports and request["operation"] != "probe":
                    raise BundleError("unreconciled toolchain import prevents bundle mutation")
                if request["operation"] == "rollback" and imports:
                    raise BundleError("toolchain import history requires forward-compatible rollback review")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
            if request["operation"] == "rollback" and "purpose" in columns:
                if connection.execute("SELECT 1 FROM jobs WHERE purpose='image_packaging' LIMIT 1").fetchone():
                    raise BundleError("packaging history requires a forward-compatible rollback review")
        if request["operation"] == "rollback" and (state / "packaging-jobs").exists():
            raise BundleError("retained packaging staging prevents old-bundle rollback")
        result = locked_operation(request, state)
        if request["operation"] == "probe":
            result["toolchain_import_pending"] = pending_imports
            result["journal_idle"] = not pending_imports
        return result
    finally:
        os.close(lock)


def main():
    if sys.platform != "linux" or os.geteuid() != 0:
        raise BundleError("bundle installation requires operator Linux root")
    body = sys.stdin.buffer.read(MAX_FRAME + 1)
    if len(body) > MAX_FRAME:
        raise BundleError("operator installation frame exceeds bound")
    result = operate(decode_request(body))
    print(json.dumps(result, sort_keys=True))


def encode_request(request):
    body = canonical(request)
    if len(body) <= MAX_FRAME:
        return body
    if request.get('operation') not in {'apply', 'resume_forward'} or len(body) > MAX_DECODED_FRAME:
        raise BundleError('decoded operator frame exceeds bound')
    frame = canonical({'version': 2, 'encoding': 'zlib-base64', 'decoded_bytes': len(body),
                       'decoded_sha256': digest(body),
                       'payload': base64.b64encode(zlib.compress(body, 9)).decode('ascii')})
    if len(frame) > MAX_FRAME:
        raise BundleError('compressed operator frame exceeds bound')
    return frame


def decode_request(body):
    if not isinstance(body, bytes) or len(body) > MAX_FRAME:
        raise BundleError('operator frame exceeds bound')

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise BundleError('operator frame contains duplicate keys')
            result[key] = value
        return result

    request = json.loads(body, object_pairs_hook=unique)
    if isinstance(request, dict) and request.get('encoding') == 'zlib-base64':
        if (set(request) != {'version', 'encoding', 'decoded_bytes', 'decoded_sha256', 'payload'}
                or type(request['version']) is not int or request['version'] != 2
                or type(request['decoded_bytes']) is not int
                or not MAX_FRAME < request['decoded_bytes'] <= MAX_DECODED_FRAME
                or not isinstance(request['payload'], str)
                or not isinstance(request['decoded_sha256'], str)
                or not re.fullmatch(r'[a-f0-9]{64}', request['decoded_sha256'])):
            raise BundleError('compressed operator contract differs')
        compressed = base64.b64decode(request['payload'], validate=True)
        decoder = zlib.decompressobj()
        expanded = decoder.decompress(compressed, request['decoded_bytes'] + 1)
        if (len(expanded) != request['decoded_bytes'] or decoder.unconsumed_tail
                or decoder.unused_data or not decoder.eof or digest(expanded) != request['decoded_sha256']):
            raise BundleError('compressed operator content differs')
        request = json.loads(expanded, object_pairs_hook=unique)
        if not isinstance(request, dict) or request.get('operation') not in {'apply', 'resume_forward'}:
            raise BundleError('compressed operator operation differs')
    return request


if __name__ == "__main__":
    main()
