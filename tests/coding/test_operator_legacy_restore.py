"""Real protected inode/SQLite ownership; mocked daemon and systemd, not live proof."""
import hashlib
import json
import os
from types import SimpleNamespace

import pytest

from app.coding import isolated_validation as isolation, toolchain_loader as loader
from app.coding import operator_legacy_restore as restore, operator_image_restore as shared
from app.coding import operator_cleanup_guard as cleanup
from scripts.restore_legacy_python_toolchain import program, verify
from scripts import restore_legacy_python_toolchain as cli


READ_REGISTRY = restore.registry_record


@pytest.fixture
def target(tmp_path, monkeypatch):
    # The positive lifecycle is historical, not a grant to load after host STOP.
    monkeypatch.setattr(restore.time, 'time', lambda: 1791889200.0)
    state = tmp_path / 'journal'
    state.mkdir(mode=0o700)
    archive = tmp_path / (restore.IMAGE.removeprefix('sha256:') + '.tar')
    body = b'reviewed public synthetic archive'
    archive.write_bytes(body)
    archive.chmod(0o600)
    monkeypatch.setattr(restore, 'ARCHIVE_BYTES', len(body))
    monkeypatch.setattr(restore, 'ARCHIVE_SHA', hashlib.sha256(body).hexdigest())
    from app.coding import toolchain_staging as custody
    record = {'archive_path': str(archive), 'archive_bytes': len(body),
              'archive_sha256': restore.ARCHIVE_SHA,
              'repository_digest': 'registry.example/python@sha256:' + 'a' * 64}
    monkeypatch.setattr(restore, 'registry_record', lambda gateway: record.copy())
    monkeypatch.setattr(cleanup, 'settings', lambda operation: cleanup.AFTER.copy())
    monkeypatch.setattr(restore.os, 'statvfs', lambda path: SimpleNamespace(f_bavail=2 * 1024**3, f_frsize=1))
    process = {'pid': 123, 'boot_id': '12345678-1234-1234-1234-123456789abc', 'start_ticks': 5678}
    monkeypatch.setattr(loader, 'process_identity', lambda: process.copy())
    source = 'fixed nonsensitive test controller'
    authority = {'bundle_sha256': 'a' * 64, 'policy_sha256': 'b' * 64,
                 'registry_sha256': 'c' * 64, 'controller_sha256': hashlib.sha256(source.encode()).hexdigest()}
    value = SimpleNamespace(archive=archive, record=record, source=source, authority=authority,
                            loads=0, launches=0, present=False, live=False, timeout=False, nonzero=False)
    def call(argv, **kwargs):
        if argv[0] == loader.SYSTEMCTL:
            if '--property=MemoryMax' in argv:
                fields = {**loader.UNIT_LIMITS, 'CPUQuotaPerSecUSec': '1s', 'Slice': 'gca-packaging.slice'}
            else:
                fields = {'LoadState': 'loaded' if value.launches else 'not-found',
                          'ActiveState': 'active' if value.live else 'inactive',
                          'SubState': 'running' if value.live else 'exited', 'MainPID': '123' if value.live else '0',
                          'InvocationID': 'b' * 32 if value.launches else '', 'Result': 'success', 'ExecMainStatus': '0'}
            return SimpleNamespace(returncode=0, stdout='\n'.join(k + '=' + v for k, v in fields.items()).encode())
        if argv[0] == loader.SYSTEMD_RUN:
            assert restore.saved(value.journal, isolation)['dispatch'] is not None
            assert '--property=Slice=gca-packaging.slice' in argv
            value.launches += 1
            if value.timeout:
                raise restore.subprocess.TimeoutExpired(argv, 10)
            return SimpleNamespace(returncode=0)
        assert argv[0] == loader.DOCKER
        if 'load' in argv:
            saved = restore.saved(value.journal, isolation)
            assert saved['claim'] is not None and saved['result'] is None
            fd = kwargs['pass_fds'][0]
            assert argv[-1] == '/proc/self/fd/' + str(fd)
            assert os.read(fd, 1024) == body  # the verified open inode, not a path reopened by the CLI
            value.loads += 1
            value.present = True
            if value.nonzero:
                return SimpleNamespace(returncode=1)
            if value.timeout:
                raise restore.subprocess.TimeoutExpired(argv, 240)
            return SimpleNamespace(returncode=0)
        if argv[argv.index('--format') + 1] == '{{json .Id}}':
            return SimpleNamespace(returncode=0 if value.present else 1,
                                   stdout=json.dumps(restore.IMAGE).encode() if value.present else b'',
                                   stderr=b'' if value.present else
                                   ('Error response from daemon: No such image: ' + restore.IMAGE).encode())
        return SimpleNamespace(returncode=0, stdout=json.dumps(
            {'Id': restore.IMAGE, 'Os': 'linux', 'Architecture': 'amd64'}).encode())
    monkeypatch.setattr(shared.subprocess, 'run', call)
    with isolation.Journal(state) as journal:
        value.journal = journal
        value.gateway = {'isolation': isolation, 'loader': loader, 'custody': custody}
        yield value


def prepare(t):
    return restore.prepare(t.journal, t.gateway, t.authority)


def dispatch(t):
    return restore.dispatch(t.journal, t.gateway, t.authority, t.source)


def work(t):
    t.live = True
    value = restore.worker(t.journal, t.gateway, t.authority, restore.digest(prepare(t)['plan']))
    t.live = False
    return value


def test_one_journalled_load_then_explicit_positive_reconciliation(target):
    target.journal.require_available()
    record = prepare(target)
    assert record['dispatch'] is None and target.loads == 0
    with pytest.raises(isolation.IsolationError, match='legacy image restoration'):
        target.journal.require_available()
    assert prepare(target) == record
    assert dispatch(target)['dispatch_acknowledged']
    assert work(target)['daemon_completion_acknowledged']
    assert target.loads == 1 and target.launches == 1
    with pytest.raises(isolation.IsolationError, match='legacy image restoration'):
        target.journal.require_available()  # exited worker and image presence are not reconciliation
    value = restore.observe(target.journal, target.gateway, target.authority, reconcile=True)
    assert value['phase'] == 'completed' and value['unit']['MainPID'] == '0'
    target.journal.require_available()
    assert not dispatch(target)['dispatch_acknowledged']
    assert target.loads == 1 and target.launches == 1
    with pytest.raises(restore.LegacyRestoreError, match='consumed'):
        work(target)


@pytest.mark.parametrize('mode', ['timeout', 'nonzero'])
def test_daemon_uncertainty_is_retained_without_retry_even_if_image_is_present(target, mode):
    prepare(target)
    dispatch(target)
    setattr(target, mode, True)
    result = work(target)
    assert result['docker_load_invoked'] and not result['daemon_completion_acknowledged']
    assert result['failure_stage'] == 'load_rpc' and target.present
    with pytest.raises(restore.LegacyRestoreError, match='independently verified'):
        restore.observe(target.journal, target.gateway, target.authority, reconcile=True)
    with pytest.raises(isolation.IsolationError, match='legacy image restoration'):
        target.journal.require_available()
    assert not dispatch(target)['dispatch_acknowledged'] and target.launches == 1
    with pytest.raises(restore.LegacyRestoreError, match='consumed'):
        work(target)


def test_dispatch_timeout_keeps_exact_single_use_reservation(target):
    prepare(target)
    target.timeout = True
    assert not dispatch(target)['dispatch_acknowledged']
    assert restore.saved(target.journal, isolation)['dispatch'] is not None
    assert not dispatch(target)['dispatch_acknowledged'] and target.launches == 1
    with pytest.raises(restore.LegacyRestoreError, match='cannot be cancelled'):
        restore.observe(target.journal, target.gateway, target.authority, cancel=True)


def test_cancel_only_never_dispatched_plan(target):
    prepare(target)
    assert restore.observe(target.journal, target.gateway, target.authority, cancel=True)['phase'] == 'cancelled'
    target.journal.require_available()
    assert not dispatch(target)['dispatch_acknowledged'] and target.launches == 0


@pytest.mark.parametrize('fault', ['digest', 'inode', 'permissions', 'hardlink', 'symlink'])
def test_worker_claim_precedes_custody_failure_and_never_loads_bad_archive(target, fault):
    prepare(target)
    dispatch(target)
    if fault == 'digest':
        target.archive.write_bytes(b'x' * target.record['archive_bytes'])
    elif fault == 'inode':
        body = target.archive.read_bytes()
        target.archive.unlink()
        target.archive.write_bytes(body)
        target.archive.chmod(0o600)
    elif fault == 'permissions':
        target.archive.chmod(0o644)
    elif fault == 'hardlink':
        os.link(target.archive, target.archive.parent / 'second-link')
    else:
        other = target.archive.parent / 'other-archive'
        target.archive.rename(other)
        target.archive.symlink_to(other)
    result = work(target)
    assert result['failure_stage'] == 'archive_custody' and not result['docker_load_invoked']
    assert restore.saved(target.journal, isolation)['claim'] is not None and target.loads == 0


@pytest.mark.parametrize('field', ['plan', 'dispatch', 'claim', 'result', 'reconciliation'])
def test_any_retained_receipt_tampering_blocks_admission(target, field):
    prepare(target)
    dispatch(target)
    work(target)
    restore.observe(target.journal, target.gateway, target.authority, reconcile=True)
    with target.journal.db:
        target.journal.db.execute('UPDATE legacy_toolchain_restorations SET ' + field + '=?',
                                 ('{"version":1,"tampered":true}',))
    with pytest.raises(isolation.IsolationError):
        target.journal.require_available()


def test_wrong_controller_source_never_dispatches(target):
    prepare(target)
    with pytest.raises(restore.LegacyRestoreError, match='worker source differs'):
        restore.dispatch(target.journal, target.gateway, target.authority, target.source + 'changed')
    assert target.launches == target.loads == 0


def test_present_image_never_creates_load_authority(target):
    target.present = True
    with pytest.raises(restore.LegacyRestoreError, match='fresh missing-image'):
        prepare(target)
    assert restore.saved(target.journal, isolation) is None


def test_changed_cleanup_guard_prevents_load(target, monkeypatch):
    prepare(target)
    dispatch(target)
    monkeypatch.setattr(cleanup, 'settings', lambda operation: cleanup.BEFORE.copy())
    assert work(target)['failure_stage'] == 'cleanup_guard' and target.loads == 0


def test_real_registry_reader_contract_is_bytes_then_document(target):
    document = {'version': 1, 'images': {restore.IMAGE: target.record}}
    gateway = {**target.gateway, 'publication': SimpleNamespace(
        read_configuration=lambda path: (json.dumps(document).encode(), document))}
    assert READ_REGISTRY(gateway) == target.record


def test_live_worker_prevents_reconciliation(target):
    prepare(target)
    dispatch(target)
    work(target)
    target.live = True
    with pytest.raises(restore.LegacyRestoreError, match='independently verified'):
        restore.observe(target.journal, target.gateway, target.authority, reconcile=True)


def test_validated_framing_and_stdlib_remote_program():
    source = program()
    compile(source, 'reviewed_legacy_controller', 'exec')
    assert len(source.encode()) <= 120000
    assert 'm.main(g)' in source
    value = {'operation': 'reconcile', 'prepared': True, 'identity': 'a' * 64, 'phase': 'completed',
             'image_approval_changed': False, 'allocation_changed': False}
    assert verify({'operation': 'reconcile'}, value) == value
    with pytest.raises(RuntimeError, match='terminal evidence'):
        verify({'operation': 'reconcile'}, {**value, 'phase': 'reserved'})


def test_historical_controller_never_accepts_unknown_source():
    with pytest.raises(RuntimeError, match='not reviewed'):
        cli.frozen_program('f' * 64)


def test_historical_controller_verifies_exact_blob_hash(monkeypatch):
    expected = next(iter(cli.REVIEWED_PROGRAMS))
    def corrupted(argv, **kwargs):
        assert argv[-1] == 'refs/gca/legacy-restorations/' + expected
        return SimpleNamespace(returncode=0, stdout=b'print("not the reviewed controller")')
    monkeypatch.setattr(cli.subprocess, 'run', corrupted)
    with pytest.raises(RuntimeError, match='custody differs'):
        cli.frozen_program(expected)
