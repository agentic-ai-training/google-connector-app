"""Real protected custody/SQLite; mocked systemd/Docker never executes OCI layers."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

import pytest

from app.coding import isolated_validation as isolation, toolchain_staging as custody
from app.coding import toolchain_loader as loader
from app.coding.toolchain_spec import BASES, ROLES
from app.coding.toolchain_archive import ToolchainArchiveError
from tests.coding.test_toolchain_archive import fixture, archive


@pytest.fixture(params=('react', 'spring'))
def host(request, monkeypatch):
    with tempfile.TemporaryDirectory(prefix='.gca-load-test-', dir=Path.home()) as name:
        state = Path(name)
        files, image_id = fixture(role=request.param)
        body = archive(files)
        catalog = {'image_id': image_id, 'archive_sha256': custody.checksum(body),
                   'archive_bytes': len(body), 'recipe_sha256': 'c' * 64}
        monkeypatch.setattr(loader, 'REVIEWED_ARCHIVES', {request.param: catalog})
        intent = {'version': 1, 'role': request.param, 'host_instance_id': '822115653245959101',
                  'policy_sha256': 'd' * 64, 'registry_sha256': 'e' * 64,
                  'stop_unix': int(time.time()) + 3600, **catalog}
        binding = {'bundle_sha256': 'f' * 64, 'policy_sha256': 'd' * 64, 'registry_sha256': 'e' * 64}
        with isolation.Journal(state) as journal:
            row = journal.begin_toolchain_import(intent)
            receipt = custody.stage_archive(journal, row['id'], io.BytesIO(body))
            journal.record_toolchain_import(row['id'], 'archive_verified', 'cancelled', 'a' * 64)
            yield SimpleNamespace(journal=journal, identity=row['id'], receipt=receipt, binding=binding,
                                  role=request.param, image_id=image_id, state=state)


def running_unit(host):
    return {'LoadState': 'loaded', 'ActiveState': 'active', 'SubState': 'running',
            'Result': 'success', 'MainPID': '123', 'InvocationID': 'a' * 32, 'ExecMainStatus': '0'}


def image_value(host):
    return {'id': host.image_id, 'os': 'linux', 'arch': 'amd64', 'user': '65532:65532',
            'entrypoint': None, 'volumes': None, 'healthcheck': None, 'role': ROLES[host.role],
            'base': BASES[host.role].split('@')[1], 'operator': 'true'}


def prepared_worker(host, monkeypatch):
    request = loader.prepare(host.journal, host.identity, host.binding)
    monkeypatch.setattr(loader, 'unit', lambda identity: running_unit(host))
    monkeypatch.setattr(loader, 'enforcement', lambda identity: {**loader.UNIT_LIMITS, 'CPUQuotaPerSecUSec': '1s'})
    monkeypatch.setattr(loader, 'process_identity', lambda: {'boot_id': '11111111-1111-1111-1111-111111111111',
                                                           'pid': 123, 'start_ticks': 99})
    return custody.checksum(custody.canonical(request))


def test_explicit_reopen_requires_actual_matching_custody_and_keeps_host_reserved(host):
    before = (host.state / 'toolchain-imports' / host.identity / 'archive.tar').read_bytes()
    request = loader.prepare(host.journal, host.identity, host.binding)
    assert request['custody_sha256'] == custody.checksum(custody.canonical(host.receipt))
    assert loader.item(host.journal, host.identity)['phase'] == 'archive_verified'
    assert (host.state / 'toolchain-imports' / host.identity / 'archive.tar').read_bytes() == before
    with pytest.raises(isolation.IsolationError, match='toolchain import'):
        host.journal.require_available()
    assert loader.prepare(host.journal, host.identity, host.binding) == request


@pytest.mark.parametrize('change', ['archive', 'receipt', 'binding', 'catalog'])
def test_reopen_cannot_turn_cancelled_or_tampered_bytes_into_new_authority(host, change, monkeypatch):
    binding = dict(host.binding)
    if change == 'archive':
        path = host.state / 'toolchain-imports' / host.identity / 'archive.tar'
        path.write_bytes(b'corrupted')
    elif change == 'receipt':
        path = host.state / 'toolchain-imports' / host.identity / 'custody.json'
        custody.write_document(path, {**host.receipt, 'version': 2})
    elif change == 'binding':
        binding['registry_sha256'] = '1' * 64
    else:
        monkeypatch.setattr(loader, 'REVIEWED_ARCHIVES', {host.role: {'archive_sha256': '1' * 64}})
    with pytest.raises((loader.ToolchainLoadError, custody.ToolchainStagingError, ToolchainArchiveError)):
        loader.prepare(host.journal, host.identity, binding)
    assert loader.item(host.journal, host.identity)['phase'] == 'cancelled'


def test_worker_claim_precedes_rpc_and_normal_ack_plus_readback_is_required(host, monkeypatch):
    token = prepared_worker(host, monkeypatch)
    calls = []

    def run(argv, **options):
        calls.append(argv)
        assert options['env'] == loader.CLEAN_ENV
        assert loader.item(host.journal, host.identity)['phase'] == 'load_started'
        assert (loader.directory(host.journal, host.identity) / 'claim.json').exists()
        if 'load' in argv:
            assert argv == [loader.DOCKER, '--host=unix:///var/run/docker.sock', 'image', 'load', '--input',
                            str(host.state / 'toolchain-imports' / host.identity / 'archive.tar')]
            return SimpleNamespace(returncode=0)
        assert '--format' in argv and argv[-1] == host.image_id
        assert '(index .Config "Healthcheck")' in argv[argv.index('--format') + 1]
        return SimpleNamespace(returncode=0, stdout=json.dumps(image_value(host)).encode())

    monkeypatch.setattr(loader.subprocess, 'run', run)
    result = loader.run_worker(host.journal, host.identity, token, host.binding)
    assert result['daemon_completion_acknowledged'] is True and result['image_approved'] is False
    assert loader.item(host.journal, host.identity)['phase'] == 'loaded'
    assert len(calls) == 2
    with pytest.raises(loader.ToolchainLoadError, match='already claimed'):
        loader.run_worker(host.journal, host.identity, token, host.binding)
    assert len(calls) == 2
    with pytest.raises(isolation.IsolationError, match='toolchain import'):
        host.journal.require_available()


@pytest.mark.parametrize('outcome', ['timeout', 'nonzero', 'readback_mismatch'])
def test_ambiguous_load_cannot_cancel_reload_or_infer_success_from_image_presence(host, monkeypatch, outcome):
    token = prepared_worker(host, monkeypatch)
    calls = []

    def run(argv, **options):
        calls.append(argv)
        if outcome == 'timeout':
            raise subprocess.TimeoutExpired(argv, 240)
        if outcome == 'nonzero':
            return SimpleNamespace(returncode=1)
        value = {**image_value(host), 'user': 'root'}
        return SimpleNamespace(returncode=0, stdout=json.dumps(value).encode())

    monkeypatch.setattr(loader.subprocess, 'run', run)
    result = loader.run_worker(host.journal, host.identity, token, host.binding)
    assert result['daemon_completion_acknowledged'] is (outcome == 'readback_mismatch')
    assert loader.item(host.journal, host.identity)['phase'] == 'load_uncertain'
    count = len(calls)
    assert loader.dispatch(host.journal, host.identity, host.binding)['dispatch_repeated'] is False
    assert loader.reconcile(host.journal, host.identity, host.binding)['reconciliation_required'] is True
    assert len(calls) == count + (1 if outcome == 'readback_mismatch' else 0)
    with pytest.raises(isolation.IsolationError, match='not permitted'):
        host.journal.record_toolchain_import(host.identity, 'load_uncertain', 'cancelled', 'a' * 64)


def test_lost_journal_write_after_terminal_receipt_recovers_without_second_rpc(host, monkeypatch):
    token = prepared_worker(host, monkeypatch)
    original = host.journal.record_toolchain_import

    def interrupted(identity, expected, phase, evidence):
        if phase == 'loaded':
            raise InterruptedError('lost final journal update')
        return original(identity, expected, phase, evidence)

    monkeypatch.setattr(host.journal, 'record_toolchain_import', interrupted)
    calls = []

    def run(argv, **options):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=json.dumps(image_value(host)).encode())

    monkeypatch.setattr(loader.subprocess, 'run', run)
    with pytest.raises(InterruptedError):
        loader.run_worker(host.journal, host.identity, token, host.binding)
    assert loader.item(host.journal, host.identity)['phase'] == 'load_started'
    monkeypatch.setattr(host.journal, 'record_toolchain_import', original)
    assert loader.reconcile(host.journal, host.identity, host.binding)['load_verified'] is True
    assert sum('load' in call for call in calls) == 1
    assert loader.item(host.journal, host.identity)['phase'] == 'loaded'


def test_normal_daemon_ack_but_failed_readback_can_reconcile_without_load_replay(host, monkeypatch):
    token = prepared_worker(host, monkeypatch)
    calls = []

    def run(argv, **options):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=json.dumps({**image_value(host), 'user': 'root'}).encode())

    monkeypatch.setattr(loader.subprocess, 'run', run)
    result = loader.run_worker(host.journal, host.identity, token, host.binding)
    assert result['daemon_completion_acknowledged'] is True and result['image_readback_verified'] is False
    monkeypatch.setattr(loader, 'image', lambda role, identity: image_value(host))
    assert loader.reconcile(host.journal, host.identity, host.binding)['load_verified'] is True
    assert sum('load' in call for call in calls) == 1


@pytest.mark.parametrize('change', ['claim_hash', 'invocation'])
def test_terminal_receipt_cannot_override_journal_or_live_unit_lineage(host, monkeypatch, change):
    token = prepared_worker(host, monkeypatch)
    monkeypatch.setattr(loader.subprocess, 'run', lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout=json.dumps(image_value(host)).encode()))
    loader.run_worker(host.journal, host.identity, token, host.binding)
    if change == 'claim_hash':
        path = loader.directory(host.journal, host.identity) / 'claim.json'
        claim = custody.document(path)
        custody.write_document(path, {**claim, 'process': {**claim['process'], 'pid': 999}})
    else:
        monkeypatch.setattr(loader, 'unit', lambda identity: {**running_unit(host), 'InvocationID': 'b' * 32})
    with pytest.raises(loader.ToolchainLoadError, match='lineage'):
        loader.reconcile(host.journal, host.identity, host.binding)


@pytest.mark.parametrize('changed', ['MemoryMax', 'MemorySwapMax', 'TasksMax', 'NoNewPrivileges',
                                    'CapabilityBoundingSet', 'ProtectSystem', 'ProtectHome', 'PrivateTmp',
                                    'CPUQuotaPerSecUSec'])
def test_unit_limit_readback_rejects_each_relaxed_or_missing_property(changed, monkeypatch):
    values = {**loader.UNIT_LIMITS, 'CPUQuotaPerSecUSec': '1s'}
    values[changed] = 'unsafe'
    output = '\n'.join(key + '=' + value for key, value in values.items()).encode()
    monkeypatch.setattr(loader.subprocess, 'run', lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout=output))
    with pytest.raises(loader.ToolchainLoadError, match='resource/security'):
        loader.enforcement('a' * 64)


@pytest.mark.parametrize('filename', ['claim.json', 'claim.pending'])
def test_pre_rpc_claim_without_phase_update_is_never_dispatched_again(host, monkeypatch, filename):
    token = prepared_worker(host, monkeypatch)
    control = loader.directory(host.journal, host.identity)
    custody.write_document(control / 'claim.json', {'dispatch_sha256': token})
    if filename == 'claim.pending':
        (control / 'claim.json').rename(control / 'claim.pending')
    monkeypatch.setattr(loader, 'unit', lambda identity: pytest.fail('Do not dispatch/observe new unit for existing claim'))
    assert loader.dispatch(host.journal, host.identity, host.binding)['reconciliation_required'] is True


def test_unit_dispatch_has_fixed_command_and_resource_limits_and_no_wait(host, monkeypatch):
    missing = {**running_unit(host), 'LoadState': 'not-found', 'MainPID': '0', 'InvocationID': ''}
    monkeypatch.setattr(loader, 'unit', lambda identity: missing)
    calls = []
    monkeypatch.setattr(loader.subprocess, 'run', lambda argv, **options:
                        calls.append((argv, options)) or SimpleNamespace(returncode=0))
    result = loader.dispatch(host.journal, host.identity, host.binding)
    assert result['dispatch_acknowledged'] is True
    argv, options = calls[0]
    assert '--no-block' in argv and '--property=RemainAfterExit=yes' in argv
    assert '--property=RuntimeMaxSec=300' in argv and '--property=TasksMax=32' in argv
    assert '--property=MemoryMax=1G' in argv and '--property=MemorySwapMax=0' in argv
    assert argv[-6:-3] == ['-I', '-B', loader.GATEWAY]
    assert argv[-2] == host.identity and options['env'] == loader.CLEAN_ENV
    assert loader.item(host.journal, host.identity)['phase'] == 'archive_verified'


def test_unit_dispatch_timeout_keeps_outbox_and_slot_without_claiming_docker_started(host, monkeypatch):
    missing = {**running_unit(host), 'LoadState': 'not-found', 'MainPID': '0', 'InvocationID': ''}
    monkeypatch.setattr(loader, 'unit', lambda identity: missing)

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired('fixed systemd dispatch', 10)

    monkeypatch.setattr(loader.subprocess, 'run', timeout)
    result = loader.dispatch(host.journal, host.identity, host.binding)
    assert result['dispatch_acknowledged'] is False
    assert (loader.directory(host.journal, host.identity) / 'dispatch.json').exists()
    assert loader.item(host.journal, host.identity)['phase'] == 'archive_verified'
    with pytest.raises(isolation.IsolationError, match='toolchain import'):
        host.journal.require_available()


@pytest.mark.parametrize('point', ['before_rpc', 'after_terminal_receipt'])
def test_abrupt_worker_process_death_keeps_claim_and_requires_positive_receipt_for_recovery(point, monkeypatch):
    with tempfile.TemporaryDirectory(prefix='.gca-load-death-', dir=Path.home()) as name:
        state = Path(name)
        files, image_id = fixture()
        body = archive(files)
        catalog = {'image_id': image_id, 'archive_sha256': custody.checksum(body),
                   'archive_bytes': len(body), 'recipe_sha256': 'c' * 64}
        monkeypatch.setattr(loader, 'REVIEWED_ARCHIVES', {'react': catalog})
        intent = {'version': 1, 'role': 'react', 'host_instance_id': '822115653245959101',
                  'policy_sha256': 'd' * 64, 'registry_sha256': 'e' * 64,
                  'stop_unix': int(time.time()) + 3600, **catalog}
        binding = {'bundle_sha256': 'f' * 64, 'policy_sha256': 'd' * 64, 'registry_sha256': 'e' * 64}
        value = {'id': image_id, 'os': 'linux', 'arch': 'amd64', 'user': '65532:65532',
                 'entrypoint': None, 'volumes': None, 'healthcheck': None,
                 'role': ROLES['react'], 'base': BASES['react'].split('@')[1], 'operator': 'true'}
        with isolation.Journal(state) as journal:
            row = journal.begin_toolchain_import(intent)
            custody.stage_archive(journal, row['id'], io.BytesIO(body))
            request = loader.prepare(journal, row['id'], binding)
        token = custody.checksum(custody.canonical(request))
        program = '\n'.join([
            'import json,os,pathlib,sys',
            'from types import SimpleNamespace',
            'from app.coding import isolated_validation as i,toolchain_loader as l',
            'catalog=json.loads(sys.argv[1]); binding=json.loads(sys.argv[2]); value=json.loads(sys.argv[3])',
            "l.REVIEWED_ARCHIVES={'react':catalog}",
            "l.process_identity=lambda:{'boot_id':'11111111-1111-1111-1111-111111111111','pid':os.getpid(),'start_ticks':99}",
            "l.unit=lambda identity:{'LoadState':'loaded','ActiveState':'active','SubState':'running',"
            "'Result':'success','MainPID':str(os.getpid()),'InvocationID':'a'*32,'ExecMainStatus':'0'}",
            "l.enforcement=lambda identity:{**l.UNIT_LIMITS,'CPUQuotaPerSecUSec':'1s'}",
            'def run(argv,**options):',
            " if 'load' in argv and sys.argv[7]=='before_rpc': os._exit(90)",
            ' return SimpleNamespace(returncode=0,stdout=json.dumps(value).encode())',
            'l.subprocess.run=run',
            'with i.Journal(pathlib.Path(sys.argv[4])) as journal:',
            ' original=journal.record_toolchain_import',
            ' def record(identity,expected,phase,evidence):',
            "  if phase=='loaded' and sys.argv[7]=='after_terminal_receipt': os._exit(91)",
            '  return original(identity,expected,phase,evidence)',
            ' journal.record_toolchain_import=record',
            ' l.run_worker(journal,sys.argv[5],sys.argv[6],binding)',
        ])
        completed = subprocess.run([sys.executable, '-c', program, json.dumps(catalog), json.dumps(binding),
                                    json.dumps(value), str(state), row['id'], token, point],
                                   env={'PATH': '/usr/bin:/bin', 'HOME': str(Path.home())},
                                   capture_output=True, timeout=20, check=False)
        assert completed.returncode == (90 if point == 'before_rpc' else 91)
        with isolation.Journal(state) as journal:
            assert loader.item(journal, row['id'])['phase'] == 'load_started'
            monkeypatch.setattr(loader, 'unit', lambda identity: {'LoadState': 'not-found'})
            monkeypatch.setattr(loader, 'image', lambda role, identity: value)
            monkeypatch.setattr(loader.subprocess, 'run', lambda *args, **kwargs: pytest.fail('No RPC replay'))
            result = loader.reconcile(journal, row['id'], binding)
            if point == 'before_rpc':
                assert result['reconciliation_required'] is True and result['phase'] == 'load_started'
            else:
                assert result['load_verified'] is True and result['phase'] == 'loaded'
            with pytest.raises(isolation.IsolationError, match='toolchain import'):
                journal.require_available()


@pytest.mark.parametrize('changes', [{'MainPID': '124'}, {'InvocationID': ''}, {'ActiveState': 'inactive'}])
def test_wrong_worker_unit_identity_never_starts_docker_rpc(host, monkeypatch, changes):
    token = prepared_worker(host, monkeypatch)
    monkeypatch.setattr(loader, 'unit', lambda identity: {**running_unit(host), **changes})
    monkeypatch.setattr(loader.subprocess, 'run', lambda *args, **kwargs: pytest.fail('No Docker RPC permitted'))
    with pytest.raises(loader.ToolchainLoadError, match='exact retained unit'):
        loader.run_worker(host.journal, host.identity, token, host.binding)
    assert loader.item(host.journal, host.identity)['phase'] == 'archive_verified'
