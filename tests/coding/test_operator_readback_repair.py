"""Real protected receipts/SQLite; mocked systemd/Docker, never live host proof."""
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.coding import operator_readback_repair as repair, toolchain_publication as publisher
from app.coding import toolchain_loader as loader, toolchain_staging as custody, toolchain_spec as spec
from app.coding import isolated_validation as isolation
from scripts.repair_toolchain_readback import verify
from tests.coding.test_toolchain_loader import host as host_fixture, prepared_worker, image_value, running_unit


host = host_fixture
REAL_RUN = subprocess.run


def old_source():
    value = (Path(__file__).parents[1] / 'fixtures' / 'toolchain_loader_c312172.txt').read_bytes()
    assert repair.digest(value) == repair.OLD_LOADER
    return value


@pytest.fixture
def broken(host, monkeypatch):
    policy, registry = host.state / 'policy.json', host.state / 'registry.json'
    policy.write_bytes(custody.canonical({'version': 2, 'runtime': 'runsc', 'state_directory': str(host.state),
                                       'images': {'python_compile': 'sha256:' + 'a' * 64}}))
    python = 'sha256:' + 'a' * 64
    legacy = {'archive_path': str(host.state / ('a' * 64 + '.tar')), 'archive_sha256': 'a' * 64,
              'archive_bytes': 100, 'repository_digest': 'docker.io/example/python@' + python}
    registry.write_bytes(custody.canonical({'version': 1, 'images': {python: legacy}}))
    policy.chmod(0o600)
    registry.chmod(0o600)
    original = loader.item(host.journal, host.identity)
    intent = {**original['intent'], 'version': 2, 'bundle_sha256': host.binding['bundle_sha256'],
              'policy_sha256': custody.checksum(policy.read_bytes()), 'registry_sha256': custody.checksum(registry.read_bytes())}
    body = (host.state / 'toolchain-imports' / host.identity / 'archive.tar').read_bytes()
    row = host.journal.begin_toolchain_import(intent)
    receipt = custody.stage_archive(host.journal, row['id'], io.BytesIO(body))
    value = SimpleNamespace(**{**host.__dict__, 'identity': row['id'], 'receipt': receipt,
                              'binding': {key: intent[key] for key in host.binding}}, policy_path=policy, registry_path=registry)
    token = prepared_worker(value, monkeypatch)
    monkeypatch.setattr(loader.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0))
    def failed(*a):
        raise loader.ToolchainLoadError('original optional-key projection failed')
    monkeypatch.setattr(loader, 'image', failed)
    result = loader.run_worker(value.journal, value.identity, token, value.binding)
    assert result['daemon_completion_acknowledged'] and not result['image_readback_verified']
    calls = []
    def inspect(argv, **kw):
        calls.append(argv)
        if argv[0] == loader.SYSTEMCTL:
            observed = {**running_unit(value), 'MainPID': '0', 'SubState': 'exited'}
            return SimpleNamespace(returncode=0, stdout='\n'.join(k + '=' + v for k, v in observed.items()).encode())
        assert 'load' not in argv
        assert '(index .Config "Entrypoint")' in argv[argv.index('--format') + 1]
        assert '(index .Config "Volumes")' in argv[argv.index('--format') + 1]
        return SimpleNamespace(returncode=0, stdout=json.dumps(image_value(value)).encode())
    monkeypatch.setattr(loader.subprocess, 'run', inspect)
    monkeypatch.setattr(spec, 'REVIEWED_ARCHIVES', loader.REVIEWED_ARCHIVES)
    monkeypatch.setattr(isolation, 'VALIDATION_POLICY', policy)
    monkeypatch.setattr(isolation, 'TOOLCHAIN_REGISTRY', registry)
    return SimpleNamespace(**value.__dict__, calls=calls)


def execute(host, **kwargs):
    return repair.repair(host.journal, host.identity, host.binding, old_source(), loader, publisher,
                         host.policy_path, host.registry_path, '9' * 64, **kwargs)


def test_repair_preserves_binding_and_archive_and_does_not_release_or_approve(broken):
    row = loader.item(broken.journal, broken.identity)
    before = broken.policy_path.read_bytes(), broken.registry_path.read_bytes()
    result = execute(broken)
    assert result['phase'] == 'loaded' and not result['image_approved'] and not result['docker_load_invoked']
    assert loader.item(broken.journal, broken.identity)['intent'] == row['intent']
    assert (broken.policy_path.read_bytes(), broken.registry_path.read_bytes()) == before
    plan, fingerprint = repair.saved_plan(broken.journal, broken.identity)
    assert fingerprint == result['repair_plan_sha256']
    assert plan['before']['image_readback_verified'] is False and plan['after']['image_readback_verified'] is True
    assert execute(broken) == result
    with pytest.raises(isolation.IsolationError, match='toolchain import'):
        broken.journal.require_available()
    assert all('load' not in argv for argv in broken.calls)


def test_only_explicit_close_rejects_and_preserves_audit_and_approves_nothing(broken):
    execute(broken)
    result = execute(broken, close=True)
    assert result['phase'] == 'rejected' and not result['image_approved']
    broken.journal.require_available()
    assert execute(broken, close=True)['repair_plan_sha256'] == result['repair_plan_sha256']
    assert repair.saved_plan(broken.journal, broken.identity)
    assert broken.journal.db.execute('SELECT count(*) FROM toolchain_rejections').fetchone()[0] == 1


@pytest.mark.parametrize('fault', ['daemon', 'claim', 'dispatch', 'unit', 'policy', 'registry', 'job'])
def test_ambiguous_or_changed_evidence_never_creates_repair_plan(broken, monkeypatch, fault):
    directory = loader.directory(broken.journal, broken.identity)
    if fault == 'daemon':
        value = custody.document(directory / 'result.json')
        custody.write_document(directory / 'result.json', {**value, 'daemon_completion_acknowledged': False})
    elif fault == 'claim':
        value = custody.document(directory / 'claim.json')
        custody.write_document(directory / 'claim.json', {**value, 'invocation_id': 'b' * 32})
    elif fault == 'dispatch':
        value = custody.document(directory / 'dispatch.json')
        custody.write_document(directory / 'dispatch.json', {**value, 'binding': {**value['binding'], 'bundle_sha256': 'a' * 64}})
    elif fault == 'unit':
        real = loader.subprocess.run
        def running(argv, **kw):
            value = real(argv, **kw)
            if argv[0] == loader.SYSTEMCTL:
                value.stdout = value.stdout.replace(b'MainPID=0', b'MainPID=123')
            return value
        monkeypatch.setattr(loader.subprocess, 'run', running)
    elif fault in {'policy', 'registry'}:
        path = getattr(broken, fault + '_path')
        path.write_bytes(path.read_bytes() + b' ')
    else:
        with broken.journal.db:
            broken.journal.db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',
                                      ('a' * 32, 'unreconciled-test-job', broken.image_id, 'python_compile',
                                       2000000000, 'cleanup_uncertain', None, 1, 'validation'))
    with pytest.raises((ValueError, RuntimeError)):
        execute(broken)
    assert broken.journal.db.execute("SELECT 1 FROM sqlite_master WHERE name='toolchain_readback_repairs'").fetchone() is None
    assert loader.item(broken.journal, broken.identity)['phase'] == 'load_uncertain'


def test_foreign_code_cannot_become_the_verifier_adapter():
    for body in (old_source() + b'\n', b'pass\n'):
        with pytest.raises(repair.ReadbackRepairError, match='exact reviewed'):
            repair.corrected_source(body)


@pytest.mark.parametrize('mode', [0o600, 0o644])
def test_operator_source_reader_accepts_protected_public_code_without_relaxing_custody(host, monkeypatch, mode):
    monkeypatch.setattr(repair, 'ROOT', host.state)
    path = host.state / 'toolchain_loader.py'
    path.write_bytes(old_source())
    path.chmod(mode)
    assert repair.installed_source(custody) == old_source()
    if mode == 0o644:
        with pytest.raises(custody.ToolchainStagingError, match='not private'):
            custody.file_stream(path)


@pytest.mark.parametrize('fault', ['writable', 'symlink', 'hardlink', 'changed', 'oversize', 'race'])
def test_operator_source_reader_rejects_unprotected_or_changed_source(host, monkeypatch, fault):
    monkeypatch.setattr(repair, 'ROOT', host.state)
    path = host.state / 'toolchain_loader.py'
    path.write_bytes(old_source())
    path.chmod(0o644)
    if fault == 'writable':
        path.chmod(0o664)
    elif fault == 'symlink':
        actual = host.state / 'other.py'
        path.rename(actual)
        path.symlink_to(actual)
    elif fault == 'hardlink':
        os.link(path, host.state / 'other.py')
    elif fault in {'changed', 'oversize'}:
        path.write_bytes(b'x' * 100001 if fault == 'oversize' else old_source() + b'\n')
    else:
        real_open = repair.os.open
        def swap(filename, flags, *args, **kwargs):
            if filename == path:
                replacement = host.state / 'replacement.py'
                replacement.write_bytes(old_source())
                replacement.chmod(0o644)
                os.replace(replacement, path)
            return real_open(filename, flags, *args, **kwargs)
        monkeypatch.setattr(repair.os, 'open', swap)
    with pytest.raises((custody.ToolchainStagingError, repair.ReadbackRepairError)):
        repair.installed_source(custody)


@pytest.mark.parametrize('fault', ['hash', 'plan', 'result', 'controller'])
def test_repair_retry_preserves_existing_evidence_instead_of_overwriting(broken, fault):
    execute(broken)
    if fault in {'hash', 'plan'}:
        with broken.journal.db:
            broken.journal.db.execute('UPDATE toolchain_readback_repairs SET ' +
                                     ('plan_sha256=?' if fault == 'hash' else 'plan=?'),
                                     ('a' * 64 if fault == 'hash' else '{}',))
    elif fault == 'result':
        path = loader.directory(broken.journal, broken.identity) / 'result.json'
        value = custody.document(path)
        custody.write_document(path, {**value, 'image': None})
    args = {'controller_hash': 'b' * 64} if fault == 'controller' else {}
    with pytest.raises(repair.ReadbackRepairError):
        if args:
            repair.repair(broken.journal, broken.identity, broken.binding, old_source(), loader, publisher,
                          broken.policy_path, broken.registry_path, args['controller_hash'])
        else:
            execute(broken)


@pytest.mark.parametrize('fault', [None, 'phase', 'approval', 'load', 'configuration', 'hash'])
def test_client_accepts_only_exact_nonapproving_repair_evidence(fault):
    request = {'operation': 'repair_readback', 'intent_id': 'a' * 64}
    result = {'operation': 'repair_readback', 'intent_id': 'a' * 64, 'phase': 'loaded',
              'repair_plan_sha256': 'b' * 64, 'image_approved': False,
              'configuration_changed': False, 'docker_load_invoked': False}
    if fault:
        result[{'phase': 'phase', 'approval': 'image_approved', 'load': 'docker_load_invoked',
                'configuration': 'configuration_changed', 'hash': 'repair_plan_sha256'}[fault]] = True
        with pytest.raises(RuntimeError):
            verify(request, result)
    else:
        assert verify(request, result) == result


@pytest.mark.parametrize('boundary', ['audit', 'receipt', 'loaded', 'rejected'])
def test_real_process_death_recovers_exact_reserved_import_without_another_load(broken, boundary):
    args = {'state': str(broken.state), 'identity': broken.identity, 'binding': broken.binding,
            'policy': str(broken.policy_path), 'registry': str(broken.registry_path),
            'catalog': loader.REVIEWED_ARCHIVES, 'image': image_value(broken), 'old_source': old_source().decode(),
            'boundary': boundary}
    code = '''import json,os,pathlib,sys
from types import SimpleNamespace
from app.coding import operator_readback_repair as r,toolchain_loader as l,toolchain_spec as s
from app.coding import toolchain_publication as p,toolchain_staging as c,isolated_validation as i
v=json.loads(sys.argv[1]);l.REVIEWED_ARCHIVES=s.REVIEWED_ARCHIVES=v['catalog']
i.VALIDATION_POLICY=pathlib.Path(v['policy']);i.TOOLCHAIN_REGISTRY=pathlib.Path(v['registry'])
def inspect(argv,**kw):
 assert 'load' not in argv
 if argv[0]==l.SYSTEMCTL:
  fields={'LoadState':'loaded','ActiveState':'active','SubState':'exited','Result':'success',
          'MainPID':'0','InvocationID':'a'*32,'ExecMainStatus':'0'}
  return SimpleNamespace(returncode=0,stdout='\\n'.join(k+'='+value for k,value in fields.items()).encode())
 return SimpleNamespace(returncode=0,stdout=json.dumps(v['image']).encode())
l.subprocess.run=inspect
real_adapter=r.adapter
def adapter(*a,**kw):
 module,fingerprint=real_adapter(*a,**kw)
 if v['boundary']=='audit' and not kw.get('validate_only'):
  module.reconcile=lambda *a:os._exit(29)
 return module,fingerprint
r.adapter=adapter
real_write=c.write_document
def write(path,value):
 real_write(path,value)
 if v['boundary']=='receipt' and path.name=='result.json':os._exit(29)
c.write_document=write
real_record=i.Journal.record_toolchain_import
def record(self,*a):
 value=real_record(self,*a)
 if v['boundary'] in {'loaded','rejected'} and a[2]==v['boundary']:os._exit(29)
 return value
i.Journal.record_toolchain_import=record
with i.Journal(pathlib.Path(v['state'])) as journal:
 r.repair(journal,v['identity'],v['binding'],v['old_source'].encode(),l,p,
          pathlib.Path(v['policy']),pathlib.Path(v['registry']),'9'*64,close=v['boundary']=='rejected')
'''
    broken.journal.__exit__(None, None, None)
    result = REAL_RUN([sys.executable, '-c', code, json.dumps(args)], capture_output=True, timeout=15, check=False)
    broken.journal.__enter__()
    assert result.returncode == 29, result.stderr
    assert repair.saved_plan(broken.journal, broken.identity)
    current = execute(broken, close=boundary == 'rejected')
    assert current['phase'] == ('rejected' if boundary == 'rejected' else 'loaded')
    assert broken.journal.db.execute('SELECT count(*) FROM toolchain_readback_repairs').fetchone()[0] == 1
    if boundary == 'rejected':
        broken.journal.require_available()
    else:
        with pytest.raises(isolation.IsolationError, match='toolchain import'):
            broken.journal.require_available()


@pytest.mark.parametrize('filename', ['claim.pending', 'dispatch.pending', 'result.pending'])
def test_fresh_repair_never_ignores_unsettled_load_control(broken, filename):
    path = loader.directory(broken.journal, broken.identity) / filename
    path.write_bytes(b'foreign')
    path.chmod(0o600)
    with pytest.raises(repair.ReadbackRepairError, match='unsettled'):
        execute(broken)
    assert path.read_bytes() == b'foreign'
    assert repair.saved_plan(broken.journal, broken.identity) is None
