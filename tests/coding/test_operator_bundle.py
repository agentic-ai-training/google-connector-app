"""Real protected filesystem/SQLite transactions; no cloud or privileged installs."""
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import zlib
from types import SimpleNamespace

import pytest

from app.coding import operator_bundle as bundle
from scripts.install_packaging_bundle import payload, verify


@pytest.fixture
def host(monkeypatch):
    with tempfile.TemporaryDirectory(prefix=".gca-bundle-test-", dir=Path.home()) as directory:
        base = Path(directory)
        root, releases, state, policy = (base / name for name in ("gca", "releases", "state", "policy.json"))
        root.mkdir(mode=0o755)
        state.mkdir(mode=0o700)
        (state / "slot.lock").touch(mode=0o600)
        database = state / "jobs.sqlite3"
        with sqlite3.connect(database) as conn:
            conn.execute("CREATE TABLE jobs(state TEXT,purpose TEXT)")
        database.chmod(0o600)
        policy.write_text(json.dumps({"state_directory": str(state)}))
        policy.chmod(0o600)
        for name in ("isolated_validation.py", "build_artifact.py"):
            (root / name).write_text("# previous reviewed source\n")
            (root / name).chmod(0o755 if name.startswith("isolated") else 0o644)
        monkeypatch.setattr(bundle, "ROOT", root)
        monkeypatch.setattr(bundle, "RELEASES", releases)
        monkeypatch.setattr(bundle, "POLICY", policy)
        monkeypatch.setattr(bundle, "probe_import", lambda path: None)
        probe = bundle.operate({"operation": "probe"})
        args = SimpleNamespace(apply=True, rollback=None, expected_snapshot_hash=probe["snapshot_hash"])
        request = payload(args)
        yield SimpleNamespace(root=root, releases=releases, state=state, request=request, old=bundle.snapshot(root))


def test_full_bundle_swap_preserves_old_tree_and_idempotent_receipt(host):
    before = (host.state / "jobs.sqlite3").read_bytes()
    result = bundle.operate(host.request)
    assert verify(host.request, result) == result
    assert set(bundle.snapshot(host.root)) == set(bundle.NAMES)
    assert (host.state / "jobs.sqlite3").read_bytes() == before
    previous = host.releases / (result["installation_id"] + ".previous")
    assert bundle.snapshot(previous) == host.old
    again = {**host.request, "expected_snapshot_hash": bundle.operate({"operation": "probe"})["snapshot_hash"]}
    assert bundle.operate(again) == result
    assert len(list(host.releases.iterdir())) == 2


def historical_installation(host):
    old = bundle.snapshot(host.root)
    for name in bundle.LEGACY_NAMES:
        path = host.root / name
        path.write_text('# historical protected source\n')
        path.chmod(0o755 if name == 'isolated_validation.py' else 0o644)
    new = bundle.snapshot(host.root)
    identity = bundle.digest(bundle.canonical({'old': old, 'new': new}))
    host.releases.mkdir(mode=0o700)
    previous = host.releases / (identity + '.previous')
    previous.mkdir(mode=0o755)
    for name in old:
        path = previous / name
        path.write_text('# previous reviewed source\n')
        path.chmod(old[name]['mode'])
    transaction = {'version': 1, 'id': identity, 'phase': 'committed', 'old': old, 'new': new}
    bundle.write(host.releases / 'transaction.json', bundle.canonical(transaction))
    return transaction


def test_exact_legacy_installation_can_be_probed_and_upgraded(host):
    previous = historical_installation(host)
    probe = bundle.operate({'operation': 'probe'})
    assert probe['last_installation'] == previous['id']
    assert set(probe['hashes']) == set(bundle.LEGACY_NAMES)
    result = bundle.operate({**host.request, 'expected_snapshot_hash': probe['snapshot_hash']})
    assert set(bundle.snapshot(host.root)) == set(bundle.NAMES)
    assert bundle.snapshot(host.releases / (result['installation_id'] + '.previous')) == previous['new']


def test_complete_prior_custody_transaction_remains_probeable(host):
    transaction = historical_installation(host)
    for name in bundle.PRIOR_CUSTODY_NAMES[7:]:
        path = host.root / name
        path.write_text('# prior custody source\n')
        path.chmod(0o644)
    transaction['new'] = bundle.snapshot(host.root)
    previous_id = transaction['id']
    transaction['id'] = bundle.digest(bundle.canonical({'old': transaction['old'], 'new': transaction['new']}))
    (host.releases / (previous_id + '.previous')).rename(host.releases / (transaction['id'] + '.previous'))
    bundle.write(host.releases / 'transaction.json', bundle.canonical(transaction))
    observed = bundle.operate({'operation': 'probe'})
    assert observed['last_installation'] == transaction['id']
    assert len(observed['hashes']) == 11


def test_bounded_compressed_real_operator_source_roundtrip(host):
    body = bundle.encode_request(host.request)
    assert len(body) <= bundle.MAX_FRAME == 256000
    assert bundle.decode_request(body) == host.request
    assert len(bundle.canonical(host.request)) <= bundle.MAX_DECODED_FRAME == 384000
    if len(bundle.canonical(host.request)) > bundle.MAX_FRAME:
        assert json.loads(body)['encoding'] == 'zlib-base64'


@pytest.mark.parametrize('fault', ['expansion', 'trailing', 'truncated', 'hash', 'size', 'boolean', 'operation'])
def test_compressed_operator_frames_fail_closed_before_installation(fault):
    body = bundle.canonical({'operation': 'apply', 'files': {'one': 'x' * 300000}})
    compressed = zlib.compress(body)
    frame = {'version': 2, 'encoding': 'zlib-base64', 'decoded_bytes': len(body),
             'decoded_sha256': bundle.digest(body), 'payload': base64.b64encode(compressed).decode()}
    if fault == 'expansion':
        frame['payload'] = base64.b64encode(zlib.compress(b'x' * 1000000)).decode()
    elif fault == 'trailing':
        frame['payload'] = base64.b64encode(compressed + b'foreign').decode()
    elif fault == 'truncated':
        frame['payload'] = base64.b64encode(compressed[:-2]).decode()
    elif fault == 'hash':
        frame['decoded_sha256'] = '0' * 64
    elif fault == 'size':
        frame['decoded_bytes'] = bundle.MAX_DECODED_FRAME + 1
    elif fault == 'boolean':
        frame['version'] = True
    else:
        body = bundle.canonical({'operation': 'probe', 'extra': 'x' * 300000})
        frame.update(decoded_bytes=len(body), decoded_sha256=bundle.digest(body),
                     payload=base64.b64encode(zlib.compress(body)).decode())
    with pytest.raises(bundle.BundleError, match='compressed operator'):
        bundle.decode_request(bundle.canonical(frame))


def test_duplicate_control_keys_are_not_a_valid_encoding_or_operation():
    with pytest.raises(bundle.BundleError, match='duplicate'):
        bundle.decode_request(b'{"operation":"probe","operation":"apply"}')


@pytest.mark.parametrize('mutation', ['partial_inventory', 'foreign_file', 'bad_hash', 'bad_mode', 'wrong_id'])
def test_legacy_compatibility_does_not_accept_unrecognized_transaction(host, mutation):
    transaction = historical_installation(host)
    if mutation == 'partial_inventory':
        transaction['new'].pop('oci_packager.py')
    elif mutation == 'foreign_file':
        transaction['new']['unexpected.py'] = transaction['new'].pop('oci_packager.py')
    elif mutation == 'bad_hash':
        transaction['new']['oci_packager.py']['hash'] = 'not-a-hash'
    elif mutation == 'bad_mode':
        transaction['new']['oci_packager.py']['mode'] = True
    else:
        transaction['id'] = '0' * 64
    bundle.write(host.releases / 'transaction.json', bundle.canonical(transaction))
    with pytest.raises(bundle.BundleError, match='installation'):
        bundle.operate({'operation': 'probe'})


@pytest.mark.parametrize("phase", ["prepared", "archive_verified", "load_started",
                                 "load_uncertain", "loaded", "proof_pending", "unknown"])
def test_pending_import_blocks_bundle_changes_but_remains_probeable(host, phase):
    with sqlite3.connect(host.state / "jobs.sqlite3") as connection:
        connection.execute("CREATE TABLE toolchain_imports(id TEXT,phase TEXT)")
        connection.execute("INSERT INTO toolchain_imports VALUES('fixed-intent',?)", (phase,))
    observed = bundle.operate({"operation": "probe"})
    assert observed["toolchain_import_pending"] is True and observed["journal_idle"] is False
    with pytest.raises(bundle.BundleError, match="toolchain import"):
        bundle.operate(host.request)
    assert bundle.snapshot(host.root) == host.old


def test_cancelled_import_allows_forward_update_but_not_old_bundle_rollback(host):
    with sqlite3.connect(host.state / "jobs.sqlite3") as connection:
        connection.execute("CREATE TABLE toolchain_imports(id TEXT,phase TEXT)")
        connection.execute("INSERT INTO toolchain_imports VALUES('fixed-intent','cancelled')")
    result = bundle.operate(host.request)
    assert bundle.operate({"operation": "probe"})["toolchain_import_pending"] is False
    with pytest.raises(bundle.BundleError, match="forward-compatible"):
        bundle.operate({"operation": "rollback", "installation_id": result["installation_id"]})


def test_explicit_rollback_restores_whole_original_tree(host):
    result = bundle.operate(host.request)
    rollback = {"operation": "rollback", "installation_id": result["installation_id"]}
    assert verify(rollback, bundle.operate(rollback))["rolled_back"] is True
    assert bundle.snapshot(host.root) == host.old
    assert bundle.operate(rollback)["rolled_back"] is True


@pytest.mark.parametrize("phase", ["prepared", "root_saved", "published"])
def test_interrupted_swap_is_probeable_and_explicitly_recoverable(host, monkeypatch, phase):
    real_phase = bundle.set_phase

    def fail(transaction, value):
        real_phase(transaction, value)
        if value == phase:
            raise InterruptedError("simulated controller loss")

    monkeypatch.setattr(bundle, "set_phase", fail)
    with pytest.raises(InterruptedError):
        bundle.operate(host.request)
    probe = bundle.operate({"operation": "probe"})
    assert probe["interrupted"] is True and probe["phase"] == phase
    with pytest.raises(bundle.BundleError, match="explicit rollback"):
        bundle.operate(host.request)
    monkeypatch.setattr(bundle, "set_phase", real_phase)
    bundle.operate({"operation": "rollback", "installation_id": probe["installation_id"]})
    assert bundle.snapshot(host.root) == host.old


def test_lost_phase_write_after_directory_rename_still_recovers(host, monkeypatch):
    real_phase = bundle.set_phase

    def fail(transaction, value):
        if value == "root_saved":
            raise InterruptedError("rename durable; phase write lost")
        real_phase(transaction, value)

    monkeypatch.setattr(bundle, "set_phase", fail)
    with pytest.raises(InterruptedError):
        bundle.operate(host.request)
    probe = bundle.operate({"operation": "probe"})
    assert not host.root.exists() and probe["phase"] == "prepared"
    monkeypatch.setattr(bundle, "set_phase", real_phase)
    bundle.operate({"operation": "rollback", "installation_id": probe["installation_id"]})
    assert bundle.snapshot(host.root) == host.old


@pytest.mark.parametrize("after", ["save_old", "publish_new"])
def test_abrupt_process_death_after_rename_recovers_without_finally(host, after):
    source = Path(bundle.__file__)
    program = "\n".join([
        "import json,os,pathlib,sys",
        "namespace={'__name__':'controlled_operator_crash_probe'}",
        "exec(compile(pathlib.Path(" + repr(str(source)) + ").read_bytes(),'reviewed_operator','exec'),namespace)",
        "namespace['ROOT']=pathlib.Path(" + repr(str(host.root)) + ")",
        "namespace['RELEASES']=pathlib.Path(" + repr(str(host.releases)) + ")",
        "namespace['POLICY']=pathlib.Path(" + repr(str(bundle.POLICY)) + ")",
        "namespace['probe_import']=lambda path:None",
        "original=os.rename",
        "def terminate(source,destination):",
        "    original(source,destination)",
        "    if " + ("source==namespace['ROOT']" if after == "save_old" else "destination==namespace['ROOT']") + ":",
        "        os._exit(97)",
        "os.rename=terminate",
        "namespace['operate'](json.load(sys.stdin))",
    ])
    result = subprocess.run([sys.executable, "-I", "-B", "-c", program],  # nosec B603
                            input=json.dumps(host.request).encode(), capture_output=True,
                            timeout=10, env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"})
    assert result.returncode == 97
    probe = bundle.operate({"operation": "probe"})
    assert probe["interrupted"] is True
    assert host.root.exists() is (after == "publish_new")
    bundle.operate({"operation": "rollback", "installation_id": probe["installation_id"]})
    assert bundle.snapshot(host.root) == host.old


def test_busy_journal_and_uncertain_jobs_prevent_changes(host):
    fd = os.open(host.state / "slot.lock", os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            bundle.operate(host.request)
    finally:
        os.close(fd)
    with sqlite3.connect(host.state / "jobs.sqlite3") as conn:
        conn.execute("INSERT INTO jobs VALUES('cleanup_uncertain','validation')")
    with pytest.raises(bundle.BundleError, match="unreconciled"):
        bundle.operate(host.request)
    assert bundle.snapshot(host.root) == host.old and not host.releases.exists()


@pytest.mark.parametrize("kind", ["wrong_hash", "unknown_file", "syntax", "symlink", "hardlink"])
def test_unreviewed_changes_are_rejected_before_directory_swap(host, kind):
    request = {**host.request, "files": dict(host.request["files"])}
    if kind == "wrong_hash":
        request["expected_snapshot_hash"] = "0" * 64
    elif kind == "unknown_file":
        request["files"]["evil.py"] = "pass\n"
    elif kind == "syntax":
        request["files"]["oci_packager.py"] = "def broken(:\n"
    elif kind == "symlink":
        (host.root / "evil.py").symlink_to(host.root / "build_artifact.py")
    else:
        os.link(host.root / "build_artifact.py", host.root / "evil.py")
    with pytest.raises((bundle.BundleError, SyntaxError)):
        bundle.operate(request)
    assert not host.releases.exists()


@pytest.mark.parametrize("retained", ["history", "staging"])
def test_old_bundle_rollback_cannot_drop_packaging_cleanup_capability(host, retained):
    result = bundle.operate(host.request)
    if retained == "history":
        with sqlite3.connect(host.state / "jobs.sqlite3") as conn:
            conn.execute("INSERT INTO jobs VALUES('cleaned','image_packaging')")
    else:
        (host.state / "packaging-jobs").mkdir(mode=0o700)
    with pytest.raises(bundle.BundleError, match="rollback"):
        bundle.operate({"operation": "rollback", "installation_id": result["installation_id"]})
    assert set(bundle.snapshot(host.root)) == set(bundle.NAMES)


def test_rollback_refuses_mutated_published_code(host):
    result = bundle.operate(host.request)
    (host.root / "oci_packager.py").write_text("# unrelated operator edit\n")
    with pytest.raises(bundle.BundleError, match="unrecognized"):
        bundle.operate({"operation": "rollback", "installation_id": result["installation_id"]})


def test_real_isolated_import_of_reviewed_staged_sources(host, monkeypatch):
    # Use this test interpreter; production fixes /usr/bin/python3 on Linux.
    real_run = subprocess.run

    def local_run(argv, **kwargs):
        assert argv[:3] == ["/usr/bin/python3", "-I", "-B"]
        return real_run([sys.executable, *argv[1:]], **kwargs)

    monkeypatch.undo()
    monkeypatch.setattr(bundle.subprocess, "run", local_run)
    probe = bundle.probe_import
    for name in bundle.NAMES:
        (host.root / name).write_text(host.request["files"][name])
        (host.root / name).chmod(0o644)
    probe(host.root)
    assert not (host.root / "__pycache__").exists()


def test_public_receipt_does_not_trust_client_passed_boolean(host):
    with pytest.raises(RuntimeError):
        verify(host.request, {"operation": "apply", "passed": True})
    assert payload(SimpleNamespace(apply=False, rollback=None)) == {"operation": "probe"}


def obsolete_copy(host):
    first = bundle.operate(host.request)
    current = bundle.operate({'operation': 'probe'})
    second_request = {**host.request, 'expected_snapshot_hash': current['snapshot_hash'],
                      'files': {**host.request['files'], 'build_artifact.py':
                                host.request['files']['build_artifact.py'] + '\n# operator revision\n'}}
    second = bundle.operate(second_request)
    probe = bundle.operate({'operation': 'probe'})
    return first['installation_id'], second['installation_id'], {
        'operation': 'retire', 'installation_id': first['installation_id'],
        'expected_snapshot_hash': probe['snapshot_hash'],
        'expected_retention_hash': probe['retention_hash']}


def test_retirement_preserves_active_predecessor_and_data_journal(host):
    obsolete, latest, request = obsolete_copy(host)
    active = bundle.snapshot(host.root)
    predecessor = host.releases / (latest + '.previous')
    before = bundle.snapshot(predecessor)
    database = (host.state / 'jobs.sqlite3').read_bytes()
    receipt = bundle.operate(request)
    assert verify(request, receipt)['retired'] is True
    assert not (host.releases / (obsolete + '.previous')).exists()
    assert bundle.snapshot(host.root) == active
    assert bundle.snapshot(predecessor) == before
    assert (host.state / 'jobs.sqlite3').read_bytes() == database
    probe = bundle.operate({'operation': 'probe'})
    assert probe['retention_inventory']['entry_count'] == 2
    assert probe['retirement_pending'] == []
    # A lost response is reconciled using a new read-only snapshot, not a stale request.
    assert bundle.operate({**request, 'expected_retention_hash': probe['retention_hash']}) == receipt
    with pytest.raises(bundle.BundleError, match='retention snapshot changed'):
        bundle.operate(request)


@pytest.mark.parametrize('kind', ['latest', 'active_changed', 'retention_changed', 'foreign_file', 'symlink'])
def test_retirement_rejects_unreviewed_state(host, kind):
    obsolete, latest, request = obsolete_copy(host)
    if kind == 'latest':
        request['installation_id'] = latest
    elif kind == 'active_changed':
        request['expected_snapshot_hash'] = '0' * 64
    elif kind == 'retention_changed':
        request['expected_retention_hash'] = '0' * 64
    elif kind == 'foreign_file':
        (host.releases / (obsolete + '.previous') / 'user-data.txt').write_text('preserve\n')
    else:
        (host.releases / (obsolete + '.previous') / 'foreign').symlink_to(host.root)
    with pytest.raises(bundle.BundleError):
        bundle.operate(request)
    assert (host.releases / (obsolete + '.previous')).exists()


def test_interrupted_retirement_reconciles_only_exact_remaining_files(host, monkeypatch):
    obsolete, latest, request = obsolete_copy(host)
    real_unlink = Path.unlink
    target = host.releases / (obsolete + '.previous')

    def interrupt(path, *args, **kwargs):
        result = real_unlink(path, *args, **kwargs)
        if path.parent == target:
            raise InterruptedError('operator terminated after unlink')
        return result

    monkeypatch.setattr(Path, 'unlink', interrupt)
    with pytest.raises(InterruptedError):
        bundle.operate(request)
    monkeypatch.setattr(Path, 'unlink', real_unlink)
    probe = bundle.operate({'operation': 'probe'})
    assert probe['retirement_pending'] == [obsolete]
    assert probe['retention_inventory']['copies'][obsolete]['file_count'] == 1
    with pytest.raises(bundle.BundleError, match='reconciliation'):
        bundle.operate({'operation': 'rollback', 'installation_id': latest})
    with pytest.raises(bundle.BundleError, match='reconciliation'):
        bundle.operate({**host.request, 'expected_snapshot_hash': probe['snapshot_hash']})
    request['expected_retention_hash'] = probe['retention_hash']
    assert bundle.operate(request)['retired']
    assert not target.exists()


def test_committed_retirement_refuses_reappeared_directory(host):
    obsolete, _, request = obsolete_copy(host)
    bundle.operate(request)
    (host.releases / (obsolete + '.previous')).mkdir(mode=0o700)
    with pytest.raises(bundle.BundleError, match='reappeared'):
        bundle.operate({'operation': 'probe'})


def test_abrupt_retirement_process_death_keeps_durable_intent(host):
    obsolete, latest, request = obsolete_copy(host)
    program = '\n'.join([
        'import json,os,pathlib,sys',
        "namespace={'__name__':'controlled_operator_retirement_crash'}",
        'exec(compile(pathlib.Path(' + repr(str(Path(bundle.__file__))) +
        ").read_bytes(),'reviewed_operator','exec'),namespace)",
        "namespace['ROOT']=pathlib.Path(" + repr(str(host.root)) + ')',
        "namespace['RELEASES']=pathlib.Path(" + repr(str(host.releases)) + ')',
        "namespace['POLICY']=pathlib.Path(" + repr(str(bundle.POLICY)) + ')',
        'original=pathlib.Path.unlink',
        'def terminate(path,*args,**kwargs):',
        '    original(path,*args,**kwargs)',
        '    if path.parent==pathlib.Path(' + repr(str(host.releases / (obsolete + '.previous'))) + '):',
        '        os._exit(97)',
        'pathlib.Path.unlink=terminate',
        "namespace['operate'](json.load(sys.stdin))",
    ])
    result = subprocess.run([sys.executable, '-I', '-B', '-c', program],  # nosec B603
                            input=json.dumps(request).encode(), capture_output=True, timeout=10,
                            env={'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent'})
    assert result.returncode == 97
    probe = bundle.operate({'operation': 'probe'})
    assert probe['retirement_pending'] == [obsolete]
    assert (host.releases / (latest + '.previous')).exists()
    assert bundle.operate({**request, 'expected_retention_hash': probe['retention_hash']})['retired']


def test_retirement_cli_payload_requires_both_fresh_fingerprints(host):
    obsolete, _, request = obsolete_copy(host)
    args = SimpleNamespace(apply=False, rollback=None, retire=obsolete,
                           expected_snapshot_hash=request['expected_snapshot_hash'],
                           expected_retention_hash=request['expected_retention_hash'])
    assert payload(args) == request
    assert hashlib.sha256(json.dumps(host.old, sort_keys=True, separators=(",", ":")).encode()).hexdigest() == (
        host.request["expected_snapshot_hash"])
