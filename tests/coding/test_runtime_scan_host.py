"""Real journal/output bytes with simulated Docker, mounts and kernel proof.

No scanner runs; these tests do not establish native Linux execution or CVE coverage.
"""
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time

import pytest

from app.coding import runtime_scan_host as host, runtime_scan_worker as worker
from app.coding import isolated_packaging as native, isolated_validation as isolation
from tests.coding.test_validation_journal import Engine, CID, scan_binding
from tests.coding.test_runtime_scan_worker import inventory, readiness


class ScanEngine(Engine):
    def __init__(self):
        super().__init__()
        self.journal = None
        self.mutate = None
        self.uncertain = False
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append(args)
        if args[0] == 'create':
            job_id = next(x.split('=', 2)[2] for x in args if x.startswith('--label=gca.job='))
            job = dict(self.journal.db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())
            if self.uncertain:
                raise isolation.IsolationError('synthetic ambiguous create')
            self.add(job, self.journal.scope)
            spec = self.containers[CID]
            mounts = []
            for index, value in enumerate(args):
                if value == '--mount':
                    fields = args[index + 1].split(',')
                    pairs = dict(item.split('=', 1) for item in fields if '=' in item)
                    mounts.append({'Type': 'bind', 'Source': pairs['src'], 'Destination': pairs['dst'],
                                   'RW': 'readonly' not in fields, 'Propagation': 'rprivate'})
            spec.update(Id=CID, State={'Running': True, 'Pid': 123}, Mounts=mounts)
            spec['Config'].update(User='65532:65532', Entrypoint=['/usr/bin/env'],
                                  Cmd=list(args[args.index(job['image']) + 1:]), OpenStdin=False, Tty=False)
            spec['HostConfig'] = dict(Runtime='runsc', NetworkMode='none', ReadonlyRootfs=True,
                CapAdd=None, CapDrop=['ALL'], SecurityOpt=['no-new-privileges:true'], Tmpfs={'/tmp': host.TMPFS},
                Memory=3 * 1024**3, MemorySwap=3 * 1024**3, NanoCpus=1_000_000_000, PidsLimit=64,
                CgroupParent='gcapackaging.slice', Ulimits=[{'Name': 'nofile', 'Hard': 64, 'Soft': 64}],
                RestartPolicy={'Name': 'no', 'MaximumRetryCount': 0}, LogConfig={'Type': 'none'},
                Privileged=False, Devices=[], DeviceRequests=None, PidMode='', IpcMode='private', UTSMode='',
                CgroupnsMode='private', PortBindings={}, PublishAllPorts=False, GroupAdd=None)
            if self.mutate:
                self.mutate(spec)
            return CID.encode()
        return super().__call__(*args, **kwargs)


@pytest.fixture
def context(monkeypatch):
    # Mocked positive execution belongs to the historical approved term only.
    monkeypatch.setattr(time, 'time', lambda: 1791889200.0)
    with tempfile.TemporaryDirectory(prefix='.gca-scan-host-', dir=Path.home()) as name:
        root = Path(name)
        root.chmod(0o700)
        state = root / 'journal'
        state.mkdir(mode=0o700)
        source = root / 'synthetic-public.bin'
        source.write_bytes(b'synthetic-public-data')
        source.chmod(0o600)
        engine = ScanEngine()
        monkeypatch.setattr(isolation, 'docker', engine)
        monkeypatch.setattr(isolation, 'ensure_toolchain_image', lambda image: None)
        monkeypatch.setattr(native, 'require_host_limits', lambda: None)
        monkeypatch.setattr(native, 'require_native_cleanup', lambda cid: None)
        monkeypatch.setattr(host.signal, 'alarm', lambda value: None)
        monkeypatch.setattr(native.os, 'chown', lambda *args: None)
        monkeypatch.setattr(native.os, 'fchown', lambda *args: None)
        # Native disk admission is simulated; no 2-GiB volume is allocated locally.
        monkeypatch.setattr(host.shutil, 'disk_usage', lambda path: SimpleNamespace(free=8 * 1024**3))
        mounted = set()

        def storage(directory, *, create=False):
            if create:
                (directory / 'work').mkdir(mode=0o700)
                (directory / 'storage.img').write_bytes(b'synthetic-ext4-not-mounted')
                (directory / 'storage.img').chmod(0o600)
                mounted.add(str(directory))
            return directory / 'work'

        monkeypatch.setattr(native, 'storage', storage)
        monkeypatch.setattr(native, 'storage_mount', lambda directory: str(directory) in mounted)
        paths = {'scanner.bin': (source, source.stat().st_size, hashlib.sha256(source.read_bytes()).hexdigest())}
        monkeypatch.setattr(host, 'input_plan', lambda journal, binding: paths)
        def proof(spec, binding, *, deadline_field):
            assert deadline_field == 'deadline'
            return {**readiness(binding), 'container_id': CID,
                    'native_cgroup': '/gcapackaging.slice/docker-' + CID + '.scope'}
        monkeypatch.setattr(native, 'verify_native_worker', proof)

        def execute(args, timeout, **options):
            assert args == ['start', '-a', CID] and 0 < timeout <= 270 and options['output_limit'] == 16384
            options['on_started'](SimpleNamespace(poll=lambda: None))
            job = engine.journal.db.execute("SELECT id FROM jobs WHERE state='running'").fetchone()
            directory = state / 'runtime-scan-jobs' / job[0]
            binding = json.loads((directory / 'binding.json').read_bytes())
            body = worker.canonical(inventory())
            result = {'version': 1, 'binding_hash': host.digest(worker.canonical(binding)),
                'report_sha256': host.digest(body), 'report_bytes': len(body), 'scanner_exit_code': 0,
                'scanner_duration_ms': 100, 'scanner_executed': True, 'native_cleanup_verified': False,
                'scan_completed': False, 'runtime_approved': False, 'metrics': worker.report_metrics(body, binding)}
            native._write(directory / 'work/output/report.json', body)
            native._write(directory / 'work/output/worker-result.json', worker.canonical(result))
            return {'success': True, 'output_truncated': False, 'stdout': worker.canonical(result).decode()}
        monkeypatch.setattr(isolation, 'execute_bounded', execute)
        worker_body = Path(worker.__file__).read_bytes()
        with isolation.Journal(state) as journal:
            engine.journal = journal
            yield journal, engine, scan_binding(), worker_body


def test_dispatch_recover_replay_and_actual_output_hash(context):
    journal, engine, binding, source = context
    result = host.dispatch(journal, binding, source)
    assert result['native_cleanup_verified'] and result['report_custody_verified']
    assert result['scan_completed'] is False and result['runtime_approved'] is False
    assert engine.removed == [CID]
    assert host.recover(journal, binding) == result
    with pytest.raises(isolation.IsolationError, match='already consumed'):
        host.dispatch(journal, binding, source)
    assert len([x for x in engine.calls if x[0] == 'create']) == 1


@pytest.mark.parametrize('field,value', [('NetworkMode', 'bridge'), ('MemorySwap', 4 * 1024**3),
    ('Privileged', True), ('CapAdd', ['SYS_ADMIN']), ('PidsLimit', 65),
    ('Tmpfs', {'/tmp': 'rw,size=1g'}), ('CgroupParent', 'other.slice'),
    ('RestartPolicy', {'Name': 'always', 'MaximumRetryCount': 0})])
def test_enforcement_mismatch_never_starts_scanner(context, field, value):
    journal, engine, binding, source = context
    engine.mutate = lambda spec: spec['HostConfig'].update({field: value})
    with pytest.raises(host.RuntimeScanHostError, match='enforcement'):
        host.dispatch(journal, binding, source)
    assert engine.removed == [CID]
    assert journal.db.execute('SELECT COUNT(*) FROM runtime_scan_outputs').fetchone()[0] == 0


def test_changed_worker_source_is_rejected_before_container_create(context):
    journal, engine, binding, _ = context
    with pytest.raises(host.RuntimeScanHostError, match='worker source'):
        host.dispatch(journal, binding, b'print("not reviewed")')
    assert not any(call[0] == 'create' for call in engine.calls)
    assert journal.observe_runtime_scan(binding['scan_id'], binding['role'], binding['stage'])['cleanup_verified']


def test_ambiguous_create_keeps_the_shared_slot(context):
    journal, engine, binding, source = context
    engine.uncertain = True
    with pytest.raises(host.RuntimeScanHostError, match='cleanup remains uncertain'):
        host.dispatch(journal, binding, source)
    observation = journal.observe_runtime_scan(binding['scan_id'], binding['role'], binding['stage'])
    assert not observation['cleanup_verified'] and observation['state'] == 'cleanup_uncertain'
    with pytest.raises(isolation.IsolationError):
        journal.require_available()


def test_native_cleanup_failure_cannot_publish_report(context, monkeypatch):
    journal, engine, binding, source = context
    def denied(cid):
        raise native.PackagingHostError('synthetic native helper still alive')
    monkeypatch.setattr(native, 'require_native_cleanup', denied)
    with pytest.raises(host.RuntimeScanHostError, match='cleanup remains uncertain'):
        host.dispatch(journal, binding, source)
    assert engine.removed == [CID]
    assert journal.db.execute('SELECT COUNT(*) FROM runtime_scan_outputs').fetchone()[0] == 0


def test_corrupt_report_or_result_cannot_be_recovered_as_success(context):
    journal, _, binding, source = context
    result = host.dispatch(journal, binding, source)
    report = journal.directory / 'runtime-scan-jobs' / result['job_id'] / 'work/output/report.json'
    report.write_bytes(b'{}')
    with pytest.raises(worker.ScanWorkerError):
        host.recover(journal, binding)


def test_recovery_after_controller_response_loss_uses_same_durable_job(context):
    journal, _, binding, source = context
    result = host.dispatch(journal, binding, source)
    directory = journal.directory
    # A new SQLite/flock owner can observe the exact original result. No dispatch.
    journal.__exit__(None, None, None)
    with isolation.Journal(directory) as reopened:
        assert host.recover(reopened, binding) == result


def test_total_retained_volume_allowance_is_shared_with_packaging(context):
    journal, _, _, _ = context
    root = journal.directory / 'packaging-jobs'
    root.mkdir(mode=0o700)
    for number in range(host.MAX_JOBS):
        (root / str(number)).mkdir(mode=0o700)
    with pytest.raises(host.RuntimeScanHostError, match='capacity exhausted'):
        host.jobs_root(journal)
    with pytest.raises(native.PackagingHostError, match='capacity exhausted'):
        native._jobs_root(journal)


def test_partial_stream_copy_never_claims_pinned_source(context):
    journal, _, _, _ = context
    source = journal.directory.parent / 'synthetic-public.bin'
    target = journal.directory / 'partial-copy'
    with pytest.raises(host.RuntimeScanHostError, match='hash differs'):
        host.copy_public(source, target, source.stat().st_size, '0' * 64)
    assert target.exists() and target.stat().st_mode & 0o777 == 0o600


def test_insufficient_disk_headroom_stops_before_container_creation(context, monkeypatch):
    journal, engine, binding, source = context
    monkeypatch.setattr(host.shutil, 'disk_usage', lambda path: SimpleNamespace(free=1024**3))
    with pytest.raises(host.RuntimeScanHostError, match='headroom'):
        host.dispatch(journal, binding, source)
    assert not any(call[0] == 'create' for call in engine.calls)


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'public', 'oversized'])
def test_retained_report_inode_and_byte_bounds_are_real(context, kind):
    journal, _, binding, source = context
    result = host.dispatch(journal, binding, source)
    directory = journal.directory / 'runtime-scan-jobs' / result['job_id']
    report = directory / 'work/output/report.json'
    if kind == 'symlink':
        saved = report.with_name('outside-report.json')
        report.rename(saved)
        report.symlink_to(saved)
    elif kind == 'hardlink':
        os.link(report, report.with_name('second-link'))
    elif kind == 'public':
        report.chmod(0o644)
    with pytest.raises(host.RuntimeScanHostError):
        host.read_worker(report, directory, 1 if kind == 'oversized' else worker.MAX_REPORT)


def test_renamed_output_mount_cannot_be_treated_as_retained_evidence(context, monkeypatch):
    journal, _, binding, source = context
    host.dispatch(journal, binding, source)
    monkeypatch.setattr(native, 'storage_mount', lambda directory: False)
    with pytest.raises(host.RuntimeScanHostError, match='filesystem identity'):
        host.recover(journal, binding)


def test_preparation_only_crash_reconciles_cleanup_not_execution(context):
    journal, engine, binding, _ = context
    journal.begin_runtime_scan(binding)
    result = host.reconcile(journal, binding)
    assert result['cleanup_verified'] and result['scan_completed'] is False
    assert result['runtime_approved'] is False and engine.removed == []
    with pytest.raises(isolation.IsolationError, match='already consumed'):
        journal.begin_runtime_scan(binding)


def test_ambiguous_create_reconciliation_cannot_infer_not_attempted(context):
    journal, engine, binding, source = context
    engine.uncertain = True
    with pytest.raises(host.RuntimeScanHostError, match='cleanup remains uncertain'):
        host.dispatch(journal, binding, source)
    with pytest.raises(host.RuntimeScanHostError, match='exact native termination'):
        host.reconcile(journal, binding)
    assert journal.db.execute("SELECT COUNT(*) FROM events WHERE state='scan_create_attempted'").fetchone()[0] == 1


def test_expired_authority_stops_before_host_or_source_access(context, monkeypatch):
    journal, engine, binding, source = context
    monkeypatch.setattr(time, 'time', lambda: 1792430673.0)
    def forbidden(*args, **kwargs):
        pytest.fail('expired dispatch touched native host or source')
    monkeypatch.setattr(native, 'require_host_limits', forbidden)
    monkeypatch.setattr(host, 'input_plan', forbidden)
    monkeypatch.setattr(isolation, 'ensure_toolchain_image', forbidden)
    with pytest.raises(host.RuntimeScanHostError, match='hosting authority'):
        host.dispatch(journal, {**binding, 'deadline': int(time.time()) + 180}, source)
    assert engine.calls == [] and journal.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0
