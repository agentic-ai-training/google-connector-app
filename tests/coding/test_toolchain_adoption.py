"""Protected SQLite/custody and real process death; mocked Docker/systemd only."""
import json
import os
from types import SimpleNamespace

import pytest

from app.coding import toolchain_loader as loader, toolchain_staging as custody
from app.coding import toolchain_publication as publication, isolated_validation as isolation
from scripts.transfer_reviewed_toolchain import adoption_evidence
from tests.coding.test_toolchain_loader import host as host_fixture, running_unit
from tests.coding.test_operator_readback_repair import broken as broken_fixture, execute
from tests.coding.test_toolchain_proof import execute as execute_fixture
from app.coding import toolchain_proof as proof
from scripts.probe_local_offline_toolchains import fixture_archive
from tests.coding.test_operator_image_restore import target as restore_target_fixture
from tests.coding.test_operator_image_restore import completed_restoration as completed_restoration_fixture


host, broken = host_fixture, broken_fixture
target, completed_restoration = restore_target_fixture, completed_restoration_fixture
REAL_IMAGE = loader.image


@pytest.fixture
def handover(broken, monkeypatch):
    return new_handover(broken, monkeypatch)


def new_handover(broken, monkeypatch):
    execute(broken, close=True)
    source = loader.item(broken.journal, broken.identity)
    new_binding = {**broken.binding, 'bundle_sha256': '1' * 64}
    new = broken.journal.begin_toolchain_import({**source['intent'], 'bundle_sha256': '1' * 64})
    custody.reuse_custody(broken.journal, new['id'], broken.identity)
    monkeypatch.setattr(loader, 'image', REAL_IMAGE)
    def unit(identity):
        if identity == broken.identity:
            return {**running_unit(broken), 'MainPID': '0', 'SubState': 'exited'}
        return {'LoadState': 'not-found', 'ActiveState': 'inactive', 'SubState': 'dead',
                'Result': 'success', 'MainPID': '0', 'InvocationID': '', 'ExecMainStatus': '0'}
    monkeypatch.setattr(loader, 'unit', unit)
    return SimpleNamespace(**broken.__dict__, new_identity=new['id'], new_binding=new_binding)


def adopt(h):
    return loader.adopt(h.journal, h.new_identity, h.identity, h.new_binding)


def test_source_handover_creates_distinct_audit_without_load_or_fabricated_worker(handover):
    h = handover
    old = loader.completion_receipt(h.journal, h.identity)
    result = adopt(h)
    assert result['load_verified'] and result['phase'] == 'loaded'
    assert result['load_origin'] == 'completed_load_adoption'
    assert result['docker_load_invoked'] is False and result['image_approved'] is False
    assert result['source_intent_id'] == h.identity and result['intent_id'] == h.new_identity
    assert loader.completion_receipt(h.journal, h.identity) == old
    assert loader.item(h.journal, h.identity)['phase'] == 'rejected'
    assert not (h.state / 'toolchain-loads' / h.new_identity).exists()
    assert all('load' not in argv for argv in h.calls)
    assert loader.reconcile(h.journal, h.new_identity, h.new_binding) == result
    assert [row['phase'] for row in loader.item(h.journal, h.new_identity)['observations']] == \
        ['prepared', 'archive_verified', 'adopting', 'loaded']
    with pytest.raises(isolation.IsolationError, match='toolchain import'):
        h.journal.require_available()


def test_adopted_load_can_be_rejected_without_fake_result_json(handover):
    h = handover
    adopt(h)
    result = publication.reject(h.journal, h.new_identity, h.new_binding, h.policy_path, h.registry_path)
    assert result['image_approved'] is False and result['configuration_published'] is False
    h.journal.require_available()
    assert result['load_sha256'] == custody.checksum(custody.canonical(loader.completion_receipt(h.journal, h.new_identity)))


def test_handover_preserves_verified_restoration_causal_chain(completed_restoration, monkeypatch):
    h = new_handover(completed_restoration, monkeypatch)
    assert adopt(h)['load_verified']
    assert loader.adoption_receipt(h.journal, h.new_identity)['source_auxiliary_sha256'] != custody.checksum(b'{}')


def test_handover_rejects_self_consistent_restoration_change_not_bound_by_repair(completed_restoration, monkeypatch):
    h = new_handover(completed_restoration, monkeypatch)
    row = h.journal.db.execute('SELECT * FROM toolchain_restorations').fetchone()
    claim, result = json.loads(row['claim']), json.loads(row['result'])
    claim['process']['start_ticks'] += 1
    result['claim_sha256'] = custody.checksum(custody.canonical(claim))
    with h.journal.db:
        h.journal.db.execute('UPDATE toolchain_restorations SET claim=?,result=?',
                            (custody.canonical(claim).decode(), custody.canonical(result).decode()))
    with pytest.raises(loader.ToolchainLoadError, match='causal fingerprint'):
        adopt(h)
    assert loader.adoption_receipt(h.journal, h.new_identity) is None


def test_two_source_handovers_form_acyclic_lineage_without_loading_again(handover):
    h = handover
    adopt(h)
    publication.reject(h.journal, h.new_identity, h.new_binding, h.policy_path, h.registry_path)
    previous = loader.item(h.journal, h.new_identity)
    binding = {**h.new_binding, 'bundle_sha256': '2' * 64}
    row = h.journal.begin_toolchain_import({**previous['intent'], 'bundle_sha256': '2' * 64})
    custody.reuse_custody(h.journal, row['id'], h.new_identity)
    result = loader.adopt(h.journal, row['id'], h.new_identity, binding)
    assert result['load_verified'] and result['source_intent_id'] == h.new_identity
    assert loader.reconcile(h.journal, row['id'], binding) == result
    assert all('load' not in argv for argv in h.calls)


def test_adoption_still_requires_all_three_native_proofs_before_publication(handover, monkeypatch):
    h = handover
    adopt(h)
    with pytest.raises(publication.ToolchainPublicationError, match='completed fixture phase'):
        publication.prepare(h.journal, h.new_identity, h.new_binding, h.policy_path, h.registry_path)
    current = SimpleNamespace(**{**h.__dict__, 'identity': h.new_identity, 'binding': h.new_binding})
    monkeypatch.setattr(proof.native, 'require_host_limits', lambda: None)
    monkeypatch.setattr(proof.native, 'require_native_cleanup', lambda cid: None)
    calls = execute_fixture(current, monkeypatch)
    for case in proof.CASES[h.role]:
        source, _ = fixture_archive(h.role, negative=case.endswith('_fail'))
        assert proof.run(h.journal, h.new_identity, case, source, h.new_binding)['passed']
    assert len(calls) == 3
    assert proof.observe(h.journal, h.new_identity, h.new_binding)['all_fixed_cases_passed']
    assert loader.adoption_receipt(h.journal, h.new_identity)['image_approved'] is False
    prepared = publication.prepare(h.journal, h.new_identity, h.new_binding, h.policy_path, h.registry_path)
    result = publication.publish(h.journal, h.new_identity, prepared['plan_sha256'], h.new_binding['bundle_sha256'],
                                 h.policy_path, h.registry_path)
    assert result['image_approved'] is True and result['configuration_published'] is True
    h.journal.require_available()


@pytest.mark.parametrize('fault', ['source_archive', 'new_archive', 'rejection_hash', 'source_receipt',
                                  'source_live', 'new_unit', 'new_control', 'missing_image', 'binding', 'job', 'expiry'])
def test_adoption_refuses_changed_unknown_or_inflight_authority_before_audit(handover, monkeypatch, fault):
    h = handover
    if fault in {'source_archive', 'new_archive'}:
        identity = h.identity if fault == 'source_archive' else h.new_identity
        (h.state / 'toolchain-imports' / identity / 'archive.tar').write_bytes(b'changed')
    elif fault == 'rejection_hash':
        with h.journal.db:
            h.journal.db.execute('UPDATE toolchain_rejections SET receipt_sha256=?', ('0' * 64,))
    elif fault == 'source_receipt':
        path = loader.directory(h.journal, h.identity) / 'result.json'
        value = custody.document(path)
        custody.write_document(path, {**value, 'daemon_completion_acknowledged': False})
    elif fault in {'source_live', 'new_unit'}:
        real = loader.unit
        def unit(identity):
            value = real(identity)
            if identity == (h.identity if fault == 'source_live' else h.new_identity):
                value = {**value, 'LoadState': 'loaded', 'MainPID': '123'}
            return value
        monkeypatch.setattr(loader, 'unit', unit)
    elif fault == 'new_control':
        (h.state / 'toolchain-loads' / h.new_identity).mkdir(mode=0o700)
    elif fault == 'missing_image':
        def missing(*args):
            raise loader.ToolchainLoadError('exact current image absent')
        monkeypatch.setattr(loader, 'image', missing)
    elif fault == 'binding':
        h.new_binding['policy_sha256'] = '0' * 64
    elif fault == 'expiry':
        monkeypatch.setattr(loader.time, 'time', lambda: loader.item(h.journal, h.new_identity)['intent']['stop_unix'] + 1)
    else:
        with h.journal.db:
            h.journal.db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',
                                ('a' * 32, 'unfinished', h.image_id, 'python_compile', 2000000000,
                                 'cleanup_uncertain', None, 1, 'validation'))
    with pytest.raises((ValueError, RuntimeError)):
        adopt(h)
    assert loader.adoption_receipt(h.journal, h.new_identity) is None
    assert loader.item(h.journal, h.new_identity)['phase'] == 'archive_verified'


@pytest.mark.parametrize('fault', ['hash', 'new_binding', 'source_identity', 'metadata', 'repair_history'])
def test_retained_adoption_never_overwrites_changed_evidence(handover, fault):
    h = handover
    adopt(h)
    before = h.journal.db.execute('SELECT * FROM toolchain_adoptions').fetchone()
    if fault == 'repair_history':
        with h.journal.db:
            h.journal.db.execute('UPDATE toolchain_readback_repairs SET plan_sha256=?', ('0' * 64,))
    else:
        value = json.loads(before['plan'])
        if fault == 'new_binding':
            value['binding']['bundle_sha256'] = '0' * 64
        elif fault == 'source_identity':
            value['source_intent_id'] = h.new_identity
        elif fault == 'metadata':
            value['image']['id'] = 'sha256:' + '0' * 64
        body = custody.canonical(value).decode()
        with h.journal.db:
            h.journal.db.execute('UPDATE toolchain_adoptions SET plan=?,plan_sha256=?',
                                (body, '0' * 64 if fault == 'hash' else custody.checksum(body.encode())))
    after = tuple(h.journal.db.execute('SELECT * FROM toolchain_adoptions').fetchone())
    with pytest.raises((ValueError, RuntimeError)):
        loader.reconcile(h.journal, h.new_identity, h.new_binding)
    assert tuple(h.journal.db.execute('SELECT * FROM toolchain_adoptions').fetchone()) == after


@pytest.mark.parametrize('boundary', ['audit', 'adopting', 'loaded'])
def test_real_process_death_recovers_adoption_without_another_rpc(handover, boundary):
    h = handover
    original = h.journal.record_toolchain_import
    h.journal.__exit__(None, None, None)
    pid = os.fork()
    if pid == 0:
        try:
            h.journal.__enter__()
            def interrupted(identity, before, after, evidence):
                if boundary == 'audit' and after == 'adopting':
                    os._exit(41)
                result = original(identity, before, after, evidence)
                if after == boundary:
                    os._exit(41)
                return result
            h.journal.record_toolchain_import = interrupted
            adopt(h)
        except BaseException:
            os._exit(99)
        os._exit(0)
    _, status = os.waitpid(pid, 0)
    h.journal.__enter__()
    assert os.waitstatus_to_exitcode(status) == 41
    assert adopt(h)['load_verified']
    assert all('load' not in argv for argv in h.calls)
    with pytest.raises(loader.ToolchainLoadError, match='adoption'):
        loader.prepare(h.journal, h.new_identity, h.new_binding)


@pytest.mark.parametrize('fault', [None, 'load', 'origin', 'source', 'approval', 'hash'])
def test_client_distinguishes_adoption_from_a_new_load(fault):
    result = {'image_approved': False, 'production_policy_changed': False,
              'load': {'intent_id': 'a' * 64, 'source_intent_id': 'b' * 64, 'load_origin': 'completed_load_adoption',
                       'load_verified': True, 'phase': 'loaded', 'adoption_plan_sha256': 'c' * 64,
                       'docker_load_invoked': False, 'image_approved': False}}
    if fault:
        result['load'][{'load': 'docker_load_invoked', 'origin': 'load_origin', 'source': 'source_intent_id',
                        'approval': 'image_approved', 'hash': 'adoption_plan_sha256'}[fault]] = True
        with pytest.raises(RuntimeError):
            adoption_evidence(result, 'a' * 64, 'b' * 64)
    else:
        adoption_evidence(result, 'a' * 64, 'b' * 64)
