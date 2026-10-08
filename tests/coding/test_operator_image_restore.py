"""Real protected custody/SQLite, mocked systemd/Docker; no live-host proof."""
import hashlib
import json
import os
from types import SimpleNamespace

import pytest

from app.coding import operator_image_restore as restore, operator_cleanup_guard as cleanup
from app.coding import operator_readback_repair as repair, toolchain_loader as loader
from app.coding import isolated_validation as isolation
from scripts.restore_reviewed_toolchain import program
from scripts import restore_reviewed_toolchain as restore_cli
from tests.coding.test_operator_readback_repair import broken as broken_fixture, old_source
from tests.coding.test_operator_readback_repair import execute as repair_execute
from tests.coding.test_toolchain_loader import host as host_fixture, running_unit, image_value


host, broken = host_fixture, broken_fixture


@pytest.fixture
def target(broken, monkeypatch):
    value = SimpleNamespace(**broken.__dict__, present=False, loads=0, dispatched=0, live=False,
                            original_live=False, controller_source='reviewed nonsensitive fixture source')
    value.controller_hash = hashlib.sha256(value.controller_source.encode()).hexdigest()
    monkeypatch.setattr(repair, 'installed_source', lambda custody: old_source())
    monkeypatch.setattr(cleanup, 'STOP_UNIX', loader.item(value.journal, value.identity)['intent']['stop_unix'])
    monkeypatch.setattr(cleanup, 'settings', lambda operation: cleanup.AFTER.copy())
    monkeypatch.setattr(restore.os, 'statvfs', lambda path: SimpleNamespace(f_bavail=8 * 1024**3, f_frsize=1))
    def inspect(argv, **kwargs):
        if argv[0] == loader.SYSTEMCTL:
            original = loader.unit_name(value.identity) in argv
            if '--property=MemoryMax' in argv:
                fields = {**loader.UNIT_LIMITS, 'CPUQuotaPerSecUSec': '1s', 'Slice': 'gca-packaging.slice'}
            elif original:
                fields = {**running_unit(value), 'MainPID': '123' if value.original_live else '0', 'SubState': 'exited'}
            elif value.dispatched:
                fields = {**running_unit(value), 'MainPID': '123' if value.live else '0',
                          'SubState': 'running' if value.live else 'exited', 'InvocationID': 'b' * 32}
            else:
                fields = {'LoadState': 'not-found', 'ActiveState': 'inactive', 'SubState': 'dead',
                          'MainPID': '0', 'InvocationID': '', 'Result': 'success', 'ExecMainStatus': '0'}
            return SimpleNamespace(returncode=0, stdout='\n'.join(k+'='+v for k, v in fields.items()).encode())
        if argv[0] == loader.SYSTEMD_RUN:
            value.dispatched += 1
            return SimpleNamespace(returncode=0)
        assert argv[0] == loader.DOCKER
        if 'load' in argv:
            assert kwargs['env'] == loader.CLEAN_ENV
            record = restore.saved(value.journal, value.identity)
            assert record['claim'] and record['result'] is None
            value.loads += 1
            value.present = True
            return SimpleNamespace(returncode=0)
        if argv[argv.index('--format')+1] == '{{json .Id}}':
            return SimpleNamespace(returncode=0 if value.present else 1,
                                   stdout=json.dumps(value.image_id).encode() if value.present else b'\n',
                                   stderr=b'' if value.present else
                                   ('Error response from daemon: No such image: '+value.image_id+'\n').encode())
        assert value.present
        return SimpleNamespace(returncode=0, stdout=json.dumps(image_value(value)).encode())
    monkeypatch.setattr(loader.subprocess, 'run', inspect)
    return value


def prepare(target):
    return restore.prepare(target.journal, target.identity, target.binding, loader, target.controller_hash)


def dispatch(target):
    return restore.dispatch(target.journal, target.identity, target.binding, loader,
                            target.controller_hash, target.controller_source)


def work(target):
    target.live = True
    value = restore.worker(target.journal, target.identity, restore.saved(target.journal, target.identity)['plan_sha256'],
                           target.binding, loader, target.controller_hash)
    target.live = False
    return value


def observe(target):
    return restore.observe(target.journal, target.identity, target.binding, loader, target.controller_hash)


def test_distinct_restoration_claim_does_not_rewrite_original_import_or_approve(target):
    original = loader.item(target.journal, target.identity)
    old_result = loader.custody.document(loader.directory(target.journal, target.identity)/'result.json')
    record = prepare(target)
    assert record['claim'] is None and record['result'] is None and target.loads == 0
    assert prepare(target) == record
    dispatch(target)
    result = work(target)
    assert result['daemon_completion_acknowledged'] and result['image_readback_verified'], result
    assert target.loads == 1 and target.dispatched == 1
    assert observe(target)['restoration_verified'] and not observe(target)['image_approved']
    assert not dispatch(target)['dispatch_repeated'] and target.dispatched == 1
    with pytest.raises(restore.ImageRestoreError, match='already claimed'):
        work(target)
    assert loader.item(target.journal, target.identity) == original
    assert loader.custody.document(loader.directory(target.journal, target.identity)/'result.json') == old_result
    with pytest.raises(isolation.IsolationError, match='toolchain import'):
        target.journal.require_available()


@pytest.mark.parametrize('fault', ['present', 'daemon', 'original_unit', 'claim', 'custody', 'cleanup', 'job', 'proof'])
def test_restoration_never_admits_unknown_completion_or_changed_authority(target, monkeypatch, fault):
    if fault == 'present':
        target.present = True
    elif fault == 'daemon':
        path = loader.directory(target.journal, target.identity)/'result.json'
        body = loader.custody.document(path)
        loader.custody.write_document(path, {**body, 'daemon_completion_acknowledged': False})
    elif fault == 'original_unit':
        target.original_live = True
    elif fault == 'claim':
        path = loader.directory(target.journal, target.identity)/'claim.json'
        body = loader.custody.document(path)
        loader.custody.write_document(path, {**body, 'invocation_id': 'c' * 32})
    elif fault == 'custody':
        path = target.state/'toolchain-imports'/target.identity/'archive.tar'
        path.write_bytes(b'not the retained approved image')
    elif fault == 'cleanup':
        monkeypatch.setattr(cleanup, 'settings', lambda operation: cleanup.BEFORE.copy())
    elif fault == 'job':
        with target.journal.db:
            target.journal.db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',
                                     ('a'*32, 'unfinished', target.image_id, 'python_compile',
                                      2000000000, 'cleanup_uncertain', None, 1, 'validation'))
    else:
        with target.journal.db:
            target.journal.db.execute('CREATE TABLE toolchain_proof_cases(import_id TEXT)')
            target.journal.db.execute('INSERT INTO toolchain_proof_cases VALUES(?)', (target.identity,))
    with pytest.raises((ValueError, RuntimeError)):
        prepare(target)
    assert restore.saved(target.journal, target.identity) is None and target.loads == 0


@pytest.mark.parametrize('unknown', ['auth', 'template', 'timeout', 'nonempty_stdout', 'wrong_image'])
def test_only_exact_daemon_not_found_response_proves_absence(target, monkeypatch, unknown):
    def run(*args, **kwargs):
        if unknown == 'timeout':
            raise loader.subprocess.TimeoutExpired('fixed inspect', 15)
        return SimpleNamespace(returncode=1 if unknown != 'wrong_image' else 0,
                               stdout=b'x' if unknown == 'nonempty_stdout' else b'"sha256:wrong"',
                               stderr=('permission denied' if unknown == 'auth' else 'template error').encode())
    monkeypatch.setattr(loader.subprocess, 'run', run)
    with pytest.raises((ValueError, loader.subprocess.SubprocessError)):
        restore.absence(loader, target.image_id)


@pytest.mark.parametrize('change', ['hash', 'plan', 'missing_image_id', 'original_receipt', 'claim', 'result'])
def test_observer_rejects_corrupted_restoration_or_source_evidence(target, change):
    prepare(target)
    dispatch(target)
    work(target)
    record = restore.saved(target.journal, target.identity)
    if change == 'original_receipt':
        path = loader.directory(target.journal, target.identity)/'result.json'
        body = loader.custody.document(path)
        loader.custody.write_document(path, {**body, 'claim_sha256': 'a'*64})
    else:
        with target.journal.db:
            if change == 'hash':
                target.journal.db.execute('UPDATE toolchain_restorations SET plan_sha256=?', ('a'*64,))
            elif change in {'plan', 'missing_image_id'}:
                plan = record['plan']
                if change == 'plan':
                    plan['controller_sha256'] = 'a'*64
                else:
                    plan['image_absence']['image_id'] = 'sha256:'+'a'*64
                target.journal.db.execute('UPDATE toolchain_restorations SET plan=?,plan_sha256=?',
                                         (restore.canonical(plan).decode(), restore.digest(plan)))
            else:
                value = record[change]
                if change == 'claim':
                    value['invocation_id'] = 'c'*32
                else:
                    value['image_approved'] = True
                target.journal.db.execute('UPDATE toolchain_restorations SET '+change+'=?',
                                         (restore.canonical(value).decode(),))
    with pytest.raises((ValueError, RuntimeError)):
        observe(target)


@pytest.mark.parametrize('fault', ['rpc_failure', 'readback', 'guard_change', 'reappeared'])
def test_failed_restoration_never_repeats_rpc_or_reports_image_approval(target, monkeypatch, fault):
    prepare(target)
    dispatch(target)
    if fault == 'guard_change':
        monkeypatch.setattr(cleanup, 'settings', lambda operation: cleanup.BEFORE.copy())
    elif fault == 'reappeared':
        target.present = True
    else:
        real = loader.subprocess.run
        def run(argv, **kwargs):
            if 'load' in argv and fault == 'rpc_failure':
                target.loads += 1
                return SimpleNamespace(returncode=1)
            if '--format' in argv and argv[argv.index('--format')+1] != '{{json .Id}}' and fault == 'readback':
                return SimpleNamespace(returncode=1, stdout=b'')
            return real(argv, **kwargs)
        monkeypatch.setattr(loader.subprocess, 'run', run)
    receipt = work(target)
    assert receipt['failure_stage'] and not receipt['image_approved']
    assert not observe(target)['restoration_verified']
    loads = target.loads
    with pytest.raises(restore.ImageRestoreError, match='already claimed'):
        work(target)
    assert target.loads == loads and target.dispatched == 1


def test_result_receipt_waits_for_worker_exit_and_current_image_still_must_exist(target):
    prepare(target)
    dispatch(target)
    work(target)
    target.live = True
    assert not observe(target)['restoration_verified']
    target.live = False
    target.present = False
    with pytest.raises(AssertionError):
        observe(target)


def test_insufficient_disk_headroom_is_recorded_before_any_restoration_rpc(target, monkeypatch):
    prepare(target)
    dispatch(target)
    monkeypatch.setattr(restore.os, 'statvfs', lambda path: SimpleNamespace(f_bavail=0, f_frsize=1))
    result = work(target)
    assert result['failure_stage'] == 'disk_headroom' and result['docker_load_invoked'] is False
    assert target.loads == 0 and not observe(target)['restoration_verified']


@pytest.mark.parametrize('boundary', ['before_rpc', 'after_rpc', 'after_receipt'])
def test_real_process_death_preserves_single_use_claim_and_never_reloads(target, monkeypatch, boundary):
    prepare(target)
    dispatch(target)
    real_run, real_record = loader.subprocess.run, restore.record_result
    def run(argv, **kwargs):
        if 'load' in argv and boundary == 'before_rpc':
            os._exit(41)
        value = real_run(argv, **kwargs)
        if 'load' in argv and boundary == 'after_rpc':
            os._exit(41)
        return value
    def record(*args):
        real_record(*args)
        if boundary == 'after_receipt':
            os._exit(41)
    target.journal.__exit__(None, None, None)
    pid = os.fork()
    if pid == 0:
        loader.subprocess.run = run
        restore.record_result = record
        try:
            target.journal.__enter__()
            work(target)
        except BaseException:
            os._exit(99)
        os._exit(0)
    _, status = os.waitpid(pid, 0)
    target.journal.__enter__()
    assert os.waitstatus_to_exitcode(status) == 41
    target.present = boundary != 'before_rpc'
    record = restore.saved(target.journal, target.identity)
    assert record['claim'] is not None
    assert observe(target)['restoration_verified'] is (boundary == 'after_receipt')
    with pytest.raises(restore.ImageRestoreError, match='already claimed'):
        work(target)
    assert target.loads == 0


def test_operator_program_contains_reviewed_helpers_and_no_provider_keys():
    source = program()
    compile(source, 'reviewed_operator_controller', 'exec')
    assert 0 < len(source.encode()) <= 100000
    assert 'g=runpy.run_path' in source and source.endswith('m.main(g)\n')
    assert 'GROQ_API_KEY' not in source and 'GEMINI_API_KEY' not in source


def test_worker_waits_boundedly_for_dispatcher_to_release_same_host_lock(monkeypatch):
    state = SimpleNamespace(calls=0, exits=0)
    class Journal:
        def __init__(self, directory):
            pass
        def __enter__(self):
            state.calls += 1
            if state.calls == 1:
                raise BlockingIOError()
            return self
        def __exit__(self, *args):
            state.exits += 1
    monkeypatch.setattr(restore.time, 'sleep', lambda duration: None)
    with restore.owned_journal(SimpleNamespace(Journal=Journal), 'unused', wait_seconds=20):
        assert state.calls == 2
    assert state.exits == 1


def test_operator_does_not_spin_or_ignore_other_journal_failures(monkeypatch):
    class Journal:
        def __init__(self, directory):
            pass
        def __enter__(self):
            raise BlockingIOError()
    with pytest.raises(restore.ImageRestoreError, match='wait expired'):
        with restore.owned_journal(SimpleNamespace(Journal=Journal), 'unused'):
            pytest.fail('ownership must not be granted')
    def bad_init(directory):
        raise ValueError('invalid protected journal')
    with pytest.raises(ValueError, match='invalid protected journal'):
        with restore.owned_journal(SimpleNamespace(Journal=bad_init), 'unused', wait_seconds=20):
            pytest.fail('invalid journal must not be ignored')


@pytest.fixture
def completed_restoration(target, monkeypatch):
    # Use this fixture's actual source hash; production pins the reviewed live hash.
    monkeypatch.setattr(repair, 'RESTORE_V1_CONTROLLER', target.controller_hash)
    prepare(target)
    dispatch(target)
    work(target)
    real = loader.subprocess.run
    def projected(argv, **kwargs):
        result = real(argv, **kwargs)
        if argv[0] == loader.SYSTEMCTL and '--property=ActiveState' not in argv and '--property=MemoryMax' not in argv:
            keys = {part.split('=', 1)[1] for part in argv if part.startswith('--property=')}
            result.stdout = b'\n'.join(line for line in result.stdout.splitlines()
                                       if line.split(b'=', 1)[0].decode() in keys)
        return result
    monkeypatch.setattr(loader.subprocess, 'run', projected)
    return target


def test_readback_repair_links_separate_restoration_and_never_reloads(completed_restoration):
    target = completed_restoration
    result = repair_execute(target)
    plan, _ = repair.saved_plan(target.journal, target.identity)
    assert result['phase'] == 'loaded' and not result['docker_load_invoked']
    assert len(plan['restoration_evidence_sha256']) == 64
    assert target.loads == 1
    assert repair_execute(target, close=True)['phase'] == 'rejected'
    target.journal.require_available()
    assert target.loads == 1


@pytest.mark.parametrize('fault', ['missing_claim', 'missing_result', 'controller', 'original_receipt',
                                  'absence', 'process', 'invocation', 'enforcement', 'ack', 'approval',
                                  'image', 'live', 'unit_identity', 'plan_hash'])
def test_readback_repair_rejects_changed_or_incomplete_restoration_before_audit(completed_restoration, monkeypatch, fault):
    target = completed_restoration
    record = restore.saved(target.journal, target.identity)
    plan, claim, result = record['plan'], record['claim'], record['result']
    if fault in {'live', 'unit_identity'}:
        real = loader.subprocess.run
        def observe_unit(argv, **kwargs):
            observed = real(argv, **kwargs)
            if argv[0] == loader.SYSTEMCTL and restore.unit_name(target.identity) in argv:
                observed.stdout = observed.stdout.replace(b'MainPID=0', b'MainPID=123') if fault == 'live' else \
                    observed.stdout.replace(b'InvocationID=' + b'b' * 32, b'InvocationID=' + b'c' * 32)
            return observed
        monkeypatch.setattr(loader.subprocess, 'run', observe_unit)
    else:
        column = 'plan'
        if fault in {'missing_claim', 'missing_result'}:
            column = fault.removeprefix('missing_')
            value = None
        else:
            if fault == 'controller':
                plan['controller_sha256'] = 'a' * 64
            elif fault == 'original_receipt':
                plan['original_load_sha256'] = 'a' * 64
            elif fault == 'absence':
                plan['image_absence']['missing'] = False
            elif fault in {'process', 'invocation', 'enforcement'}:
                column = 'claim'
                if fault == 'process':
                    claim['process']['pid'] = True
                elif fault == 'invocation':
                    claim['invocation_id'] = 'unknown'
                else:
                    claim['enforcement']['Slice'] = 'unbounded.slice'
            elif fault in {'ack', 'approval', 'image'}:
                column = 'result'
                if fault == 'ack':
                    result['daemon_completion_acknowledged'] = False
                elif fault == 'approval':
                    result['image_approved'] = True
                else:
                    result['image']['id'] = 'sha256:' + 'a' * 64
            value = restore.canonical({'plan': plan, 'claim': claim, 'result': result}[column]).decode()
        with target.journal.db:
            target.journal.db.execute('UPDATE toolchain_restorations SET ' + column + '=?', (value,))
            if column == 'plan':
                target.journal.db.execute('UPDATE toolchain_restorations SET plan_sha256=?',
                                         ('a' * 64 if fault == 'plan_hash' else repair.digest(value.encode()),))
    with pytest.raises((ValueError, RuntimeError)):
        repair_execute(target)
    assert repair.saved_plan(target.journal, target.identity) is None
    assert loader.item(target.journal, target.identity)['phase'] == 'load_uncertain'
    assert target.loads == 1


def test_repair_retry_rejects_removed_restoration_evidence(completed_restoration):
    target = completed_restoration
    repair_execute(target)
    with target.journal.db:
        target.journal.db.execute('DROP TABLE toolchain_restorations')
    with pytest.raises(repair.ReadbackRepairError, match='restoration fingerprint'):
        repair_execute(target, close=True)
    assert loader.item(target.journal, target.identity)['phase'] == 'loaded'


def test_frozen_controller_is_recovered_by_exact_content_hash(monkeypatch):
    body = b"print('nonsensitive reviewed fixture')\n"
    expected = hashlib.sha256(body).hexdigest()
    monkeypatch.setattr(restore_cli, 'REVIEWED_SOURCES', {expected})
    calls = []
    def read_blob(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=body)
    monkeypatch.setattr(restore_cli.subprocess, 'run', read_blob)
    assert restore_cli.frozen_program(expected).encode() == body
    assert calls[0][0][-1] == 'refs/gca/operator-restorations/' + expected
    assert '--no-replace-objects' in calls[0][0]
    assert calls[0][1]['env']['GIT_CONFIG_GLOBAL'] == '/dev/null'


@pytest.mark.parametrize('fault', ['unreviewed', 'changed', 'missing', 'oversize'])
def test_frozen_controller_never_accepts_unknown_or_changed_source(monkeypatch, fault):
    monkeypatch.setattr(restore_cli.subprocess, 'run', lambda *a, **k:
                        SimpleNamespace(returncode=1 if fault == 'missing' else 0,
                                        stdout=b'x' * 100001 if fault == 'oversize' else b'pass\n'))
    with pytest.raises(RuntimeError):
        restore_cli.frozen_program('a' * 64 if fault == 'unreviewed' else repair.RESTORE_V1_CONTROLLER)
