"""Real private custody/SQLite; mocked Docker/native hooks, no claim of live proof."""
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from app.coding import isolated_validation as isolation, toolchain_proof as proof
from app.coding import toolchain_loader as loader, toolchain_staging as custody
from scripts.probe_local_offline_toolchains import fixture_archive
from tests.coding.test_toolchain_loader import host as host_fixture, prepared_worker, image_value


host = host_fixture
REAL_RUN = subprocess.run


def loaded(host, monkeypatch):
    token = prepared_worker(host, monkeypatch)
    monkeypatch.setattr(loader.subprocess, 'run', lambda *a, **kw: SimpleNamespace(
        returncode=0, stdout=json.dumps(image_value(host)).encode()))
    loader.run_worker(host.journal, host.identity, token, host.binding)
    monkeypatch.setattr(proof.native, 'require_host_limits', lambda: None)
    monkeypatch.setattr(proof.native, 'require_native_cleanup', lambda cid: None)


def execute(host, monkeypatch, *, valid=True, preparation_failed=False):
    calls = []

    def run(source, profile, timeout, policy, journal, job, **hooks):
        calls.append(profile)
        assert next(row for row in journal.toolchain_imports() if row['id'] == host.identity)['phase'] == 'proof_pending'
        assert timeout == 180 and hooks['proof_cgroup'] == proof.native.CGROUP_PARENT
        assert hooks['readiness'] is proof.readiness
        assert hooks['native_cleanup'] is proof.native.require_native_cleanup
        case = journal.db.execute('SELECT case_name FROM toolchain_proof_cases WHERE job_id=?',
                                  (job['id'],)).fetchone()[0]
        positive = case.endswith('_pass')
        journal.record(job, 'cleaned', 'c' * 64)
        return {'success': positive if valid else not positive, 'exit_code': 0 if positive else 1,
                'output_truncated': False, 'cleanup_verified': True,
                'stdout': '' if positive else proof.FAILURE_MARKER, 'stderr': '',
                'native_enforcement': {'mocked_only': True},
                'executed_commands': [list(isolation.PREPARE_COMMANDS.get(profile, ())
                                           or ('false',)
                                           if preparation_failed else isolation.PROFILES[profile][1])]}

    monkeypatch.setattr(isolation, '_execute_archive', run)
    return calls


@pytest.mark.parametrize('role', ['react', 'spring'])
@pytest.mark.parametrize('negative', [False, True])
def test_pins_cover_actual_positive_and_negative_operator_fixtures(role, negative):
    source, _ = fixture_archive(role, negative=negative)
    for case in proof.CASES[role]:
        if case.endswith('_fail') is negative:
            profile, fingerprint = proof.fixture(role, case, source)
            assert profile in isolation.PROFILES and fingerprint == custody.checksum(source)
            with pytest.raises(proof.ToolchainProofError, match='actual fixture'):
                proof.fixture(role, case, source[:-1] + b'X')


def test_three_cases_are_durable_idempotent_and_do_not_approve_role(host, monkeypatch):
    loaded(host, monkeypatch)
    calls = execute(host, monkeypatch)
    for case in proof.CASES[host.role]:
        source, _ = fixture_archive(host.role, negative=case.endswith('_fail'))
        result = proof.run(host.journal, host.identity, case, source, host.binding)
        assert result['passed'] is True and result['image_approved'] is False
        assert result['cleanup_verified'] is True and result['replayed_observation'] is False
        assert proof.run(host.journal, host.identity, case, source, host.binding)['replayed_observation'] is True
    assert len(calls) == 3
    observed = proof.observe(host.journal, host.identity, host.binding)
    assert observed['all_fixed_cases_passed'] is True and observed['image_approved'] is False
    assert observed['phase'] == 'proof_pending'
    with pytest.raises(isolation.IsolationError, match='toolchain import'):
        host.journal.require_available()


def test_observation_of_unproven_import_does_not_create_proof_table_or_run_loader(host, monkeypatch):
    monkeypatch.setattr(loader, 'reconcile', lambda *args: pytest.fail('observation cannot run loader'))
    assert proof.observe(host.journal, host.identity, host.binding)['all_fixed_cases_passed'] is False
    assert host.journal.db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='toolchain_proof_cases'").fetchone() is None


@pytest.mark.parametrize('fault', ['case', 'source', 'phase'])
def test_no_generated_source_or_ordinary_dispatch_enters_proof_slot(host, monkeypatch, fault):
    case = proof.CASES[host.role][0]
    source, _ = fixture_archive(host.role)
    calls = execute(host, monkeypatch)
    if fault != 'phase':
        loaded(host, monkeypatch)
    if fault == 'case':
        case = 'python_unit_pass'
    if fault == 'source':
        source += b'generated-source'
    with pytest.raises((proof.ToolchainProofError, loader.ToolchainLoadError)):
        proof.run(host.journal, host.identity, case, source, host.binding)
    assert calls == []
    assert loader.item(host.journal, host.identity)['phase'] != 'proof_pending'


@pytest.mark.parametrize('bad', ['wrong_result', 'preparation_failure'])
def test_false_results_do_not_pass_and_attempts_are_bounded(host, monkeypatch, bad):
    loaded(host, monkeypatch)
    calls = execute(host, monkeypatch, valid=bad != 'wrong_result', preparation_failed=bad == 'preparation_failure')
    case = proof.CASES[host.role][-1]
    source, _ = fixture_archive(host.role, negative=True)
    for _ in range(2):
        assert proof.run(host.journal, host.identity, case, source, host.binding)['passed'] is False
    with pytest.raises(isolation.IsolationError, match='attempt bound'):
        proof.run(host.journal, host.identity, case, source, host.binding)
    assert len(calls) == 2
    assert proof.observe(host.journal, host.identity, host.binding)['all_fixed_cases_passed'] is False


def test_uncertain_native_cleanup_overrides_result_and_blocks_next_case(host, monkeypatch):
    loaded(host, monkeypatch)
    case = proof.CASES[host.role][0]
    source, _ = fixture_archive(host.role)

    def uncertain(*args, **kwargs):
        job = args[5]
        host.journal.record(job, 'cleanup_uncertain', 'c' * 64)
        raise isolation.IsolationError('native termination unconfirmed')

    monkeypatch.setattr(isolation, '_execute_archive', uncertain)
    with pytest.raises(isolation.IsolationError, match='native termination'):
        proof.run(host.journal, host.identity, case, source, host.binding)
    assert proof.observe(host.journal, host.identity, host.binding)['cases'][case] is None
    with pytest.raises(proof.ToolchainProofError, match='earlier fixture'):
        proof.run(host.journal, host.identity, case, source, host.binding)


@pytest.mark.parametrize('fault', ['cleanup_verified', 'native_enforcement'])
def test_missing_enforcement_or_cleanup_evidence_never_creates_receipt(host, monkeypatch, fault):
    loaded(host, monkeypatch)
    execute(host, monkeypatch)
    real = isolation._execute_archive

    def incomplete(*args, **kwargs):
        result = real(*args, **kwargs)
        result[fault] = False if fault == 'cleanup_verified' else None
        return result

    monkeypatch.setattr(isolation, '_execute_archive', incomplete)
    case = proof.CASES[host.role][0]
    source, _ = fixture_archive(host.role)
    with pytest.raises(proof.ToolchainProofError, match='enforcement and native cleanup'):
        proof.run(host.journal, host.identity, case, source, host.binding)
    assert proof.observe(host.journal, host.identity, host.binding)['cases'][case] is None


@pytest.mark.parametrize('field,value', [('source_sha256', '0' * 64), ('job_id', '0' * 32),
                                        ('version', True), ('exit_code', 7), ('cleanup_verified', False)])
def test_altered_receipts_cannot_be_observed_as_passes(host, monkeypatch, field, value):
    loaded(host, monkeypatch)
    execute(host, monkeypatch)
    case = proof.CASES[host.role][0]
    source, _ = fixture_archive(host.role)
    proof.run(host.journal, host.identity, case, source, host.binding)
    row = host.journal.db.execute('SELECT * FROM toolchain_proof_cases').fetchone()
    result = json.loads(row['result'])
    result[field] = value
    with host.journal.db:
        host.journal.db.execute('UPDATE toolchain_proof_cases SET result=? WHERE job_id=?',
                                (json.dumps(result), row['job_id']))
    with pytest.raises(proof.ToolchainProofError, match='lineage'):
        proof.observe(host.journal, host.identity, host.binding)


def test_abrupt_process_death_retains_proof_job_without_inventing_pass(host, monkeypatch):
    loaded(host, monkeypatch)
    case = proof.CASES[host.role][0]
    source, _ = fixture_archive(host.role)
    loader.reconcile(host.journal, host.identity, host.binding)
    host.journal.record_toolchain_import(host.identity, 'loaded', 'proof_pending', 'a' * 64)
    proof.schema(host.journal)
    fingerprint = custody.checksum(custody.canonical(host.binding))
    host.journal.__exit__()
    root = str(Path(__file__).resolve().parents[2])
    program = '\n'.join([
        'import sys,os', 'from pathlib import Path', 'sys.path.insert(0,' + repr(root) + ')',
        'from app.coding.isolated_validation import Journal',
        'with Journal(Path(' + repr(str(host.state)) + ')) as journal:',
        '    journal.begin_toolchain_proof(' + repr(host.identity) + ',' + repr(case.rsplit('_', 1)[0]) + ','
        + repr(host.image_id) + ',180,' + repr(case) + ',' + repr(custody.checksum(source)) + ','
        + repr(fingerprint) + ')', '    os._exit(97)',
    ])
    result = REAL_RUN([sys.executable, '-I', '-B', '-c', program], capture_output=True,
                      timeout=10, env={'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent'}, check=False)
    host.journal.__enter__()
    assert result.returncode == 97
    assert proof.observe(host.journal, host.identity, host.binding)['cases'][case] is None
    calls = execute(host, monkeypatch)
    with pytest.raises(proof.ToolchainProofError, match='earlier fixture'):
        proof.run(host.journal, host.identity, case, source, host.binding)
    assert calls == []
    assert host.journal.db.execute('SELECT state FROM jobs').fetchone()[0] == 'creating'


def sandbox_spec():
    return {'Id': 'c' * 64, 'State': {'Running': True}, 'Mounts': [],
            'Config': {'User': '65532:65532', 'Entrypoint': ['/bin/sh'], 'OpenStdin': False, 'Tty': False},
            'HostConfig': {'Runtime': 'runsc', 'NetworkMode': 'none', 'Privileged': False,
                           'ReadonlyRootfs': True, 'CapDrop': ['ALL'], 'SecurityOpt': ['no-new-privileges:true'],
                           'Memory': 1024**3, 'MemorySwap': 1024**3, 'NanoCpus': 1000000000, 'PidsLimit': 64,
                           'CgroupParent': proof.native.CGROUP_PARENT, 'PidMode': '', 'UTSMode': '',
                           'IpcMode': 'private', 'CgroupnsMode': 'private', 'PublishAllPorts': False,
                           'LogConfig': {'Type': 'none'}, 'RestartPolicy': {'Name': 'no', 'MaximumRetryCount': 0},
                           'Ulimits': [{'Name': 'nofile', 'Hard': 256, 'Soft': 256}],
                           'Tmpfs': {'/workspace': 'rw,nosuid,nodev,exec,size=256m,mode=1777',
                                     '/tmp': 'rw,nosuid,nodev,exec,size=64m,mode=1777'}}}


@pytest.mark.parametrize('field,value', [('Runtime', 'runc'), ('NetworkMode', 'host'), ('Privileged', True),
                                        ('Memory', 2*1024**3), ('PidsLimit', 128), ('CapAdd', ['SYS_ADMIN']),
                                        ('ReadonlyRootfs', False), ('Tmpfs', {}), ('CgroupParent', '')])
def test_weaker_sandbox_metadata_is_rejected_before_fixture_execution(monkeypatch, field, value):
    spec = sandbox_spec()
    spec['HostConfig'][field] = value
    journal = SimpleNamespace(verify_identity=lambda *args: None)
    monkeypatch.setattr(isolation, 'docker', lambda *args: json.dumps([spec]).encode())
    with pytest.raises(proof.ToolchainProofError, match='sandbox enforcement'):
        proof.verify_container(journal, {}, spec['Id'])


def test_native_enforcement_and_guest_privileges_are_both_required(monkeypatch):
    spec = sandbox_spec()
    journal = SimpleNamespace(verify_identity=lambda *args: None)
    status = 'CapEff:\t0000000000000000\nCapPrm:\t0\nCapBnd:\t0\nNoNewPrivs:\t1\nUid:\t65532 65532 65532 65532\nGid:\t65532 65532 65532 65532\n'
    monkeypatch.setattr(isolation, 'docker', lambda *args: json.dumps([spec]).encode() if args[0] == 'inspect'
                        else status.encode())
    monkeypatch.setattr(proof.native, 'verify_native_worker', lambda *args: {'memory_max': 1024**3, 'tasks_max': 64})
    import time
    job = {'id': 'd' * 32, 'image': 'sha256:' + 'e' * 64, 'deadline': int(time.time()) + 100}
    assert proof.readiness(journal, job, spec['Id'])['guest_cap_eff'] == 0
    status = status.replace('NoNewPrivs:\t1', 'NoNewPrivs:\t0')
    with pytest.raises(proof.ToolchainProofError, match='guest privilege'):
        proof.readiness(journal, job, spec['Id'])
