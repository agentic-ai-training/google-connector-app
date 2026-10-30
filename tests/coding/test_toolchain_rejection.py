"""Real private SQLite/custody; Docker/systemd/native proofs are mocked, not live."""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from app.coding import isolated_validation as isolation, toolchain_publication as publication
from app.coding import toolchain_loader as loader, toolchain_staging as custody
from tests.coding.test_toolchain_publication import prepared as prepared_fixture
from tests.coding.test_toolchain_loader import host as host_fixture
from tests.coding.test_toolchain_import_journal import intent


prepared = prepared_fixture
host = host_fixture
REAL_RUN = subprocess.run


def reject(host):
    return publication.reject(host.journal, host.identity, host.binding, host.policy_path, host.registry_path)


def test_rejection_retains_everything_releases_no_approval_and_replays(prepared, monkeypatch):
    config = prepared.policy_path.read_bytes(), prepared.registry_path.read_bytes()
    archive = prepared.state / 'toolchain-imports' / prepared.identity / 'archive.tar'
    before = archive.read_bytes()
    result = reject(prepared)
    assert result['image_approved'] is False and result['configuration_published'] is False
    assert result['custody_retained'] is True and result['image_retained'] is True
    assert archive.read_bytes() == before
    assert (prepared.policy_path.read_bytes(), prepared.registry_path.read_bytes()) == config
    prepared.journal.require_available()
    assert loader.item(prepared.journal, prepared.identity)['phase'] == 'rejected'
    monkeypatch.setattr(loader, 'reconcile', lambda *args: pytest.fail('a rejected observation must not reload'))
    assert reject(prepared) == {**result, 'replayed_observation': True}
    with pytest.raises(publication.ToolchainPublicationError):
        publication.prepare(prepared.journal, prepared.identity, prepared.binding,
                            prepared.policy_path, prepared.registry_path)
    with pytest.raises(loader.ToolchainLoadError):
        loader.prepare(prepared.journal, prepared.identity, prepared.binding)


@pytest.mark.parametrize('fault', ['receipt_missing', 'receipt_bytes', 'history', 'proof', 'job'])
def test_phase_alone_or_changed_receipts_cannot_release_slot(prepared, fault):
    reject(prepared)
    with prepared.journal.db:
        if fault == 'receipt_missing':
            prepared.journal.db.execute('DELETE FROM toolchain_rejections')
        elif fault == 'receipt_bytes':
            prepared.journal.db.execute("UPDATE toolchain_rejections SET receipt='{}'")
        elif fault == 'history':
            row = loader.item(prepared.journal, prepared.identity)
            row['observations'][-1]['evidence_sha256'] = 'a' * 64
            prepared.journal.db.execute('UPDATE toolchain_imports SET observations=? WHERE id=?',
                                        (json.dumps(row['observations']), prepared.identity))
        elif fault == 'proof':
            prepared.journal.db.execute('UPDATE toolchain_proof_cases SET result=NULL WHERE import_id=?',
                                        (prepared.identity,))
        else:
            prepared.journal.db.execute("UPDATE jobs SET state='cleanup_uncertain'")
    with pytest.raises(isolation.IsolationError):
        prepared.journal.require_available()


@pytest.mark.parametrize('fault', ['policy', 'registry', 'load', 'archive', 'native', 'job'])
def test_unconfirmed_or_uncleaned_evidence_cannot_be_rejected(prepared, monkeypatch, fault):
    if fault in {'policy', 'registry'}:
        path = getattr(prepared, fault + '_path')
        path.write_bytes(path.read_bytes() + b' ')
    elif fault == 'load':
        monkeypatch.setattr(loader, 'reconcile', lambda *args: {'load_verified': False})
    elif fault == 'archive':
        path = prepared.state / 'toolchain-imports' / prepared.identity / 'archive.tar'
        path.write_bytes(path.read_bytes()[:-1])
    elif fault == 'native':
        def uncertain(*args):
            raise RuntimeError('native cleanup is not established')
        monkeypatch.setattr(publication.proof.native, 'require_native_cleanup', uncertain)
    else:
        prepared.journal.db.execute("UPDATE jobs SET state='cleanup_uncertain'")
    with pytest.raises((ValueError, RuntimeError)):
        reject(prepared)
    assert loader.item(prepared.journal, prepared.identity)['phase'] == 'proof_pending'
    assert prepared.journal.db.execute("SELECT 1 FROM sqlite_master WHERE name='toolchain_rejections'").fetchone() is None


@pytest.mark.parametrize('phase', ['prepared', 'archive_verified', 'load_started', 'load_uncertain'])
def test_ambiguous_load_never_becomes_rejected_by_expiry_or_image_presence(tmp_path, phase, monkeypatch):
    with isolation.Journal(tmp_path) as journal:
        row = journal.begin_toolchain_import(intent())
        for next_phase in ['archive_verified', 'load_started', 'load_uncertain'][:
                ['prepared', 'archive_verified', 'load_started', 'load_uncertain'].index(phase)]:
            row = journal.record_toolchain_import(row['id'], row['phase'], next_phase, 'f' * 64)
        monkeypatch.setattr(isolation.time, 'time', lambda: row['intent']['stop_unix'] + 1)
        with pytest.raises(isolation.IsolationError, match='not permitted'):
            journal.record_toolchain_import(row['id'], phase, 'rejected', 'f' * 64)
        with pytest.raises(isolation.IsolationError, match='toolchain import'):
            journal.require_available()


@pytest.mark.parametrize('boundary', ['receipt', 'terminal'])
def test_process_death_recovers_exact_rejection_without_second_operation(prepared, boundary):
    code = '''import json,os,pathlib,sys
from app.coding import isolated_validation as isolation,toolchain_publication as publication
from app.coding import toolchain_loader as loader,toolchain_staging as custody
from app.coding import toolchain_proof as proof
state,identity,binding,policy,registry,catalog,boundary=json.loads(sys.argv[1])
isolation.VALIDATION_POLICY=pathlib.Path(policy)
isolation.TOOLCHAIN_REGISTRY=pathlib.Path(registry)
loader.REVIEWED_ARCHIVES=catalog
loader.reconcile=lambda *a: {'load_verified':True}
proof.native.require_native_cleanup=lambda *a: None
real=isolation.Journal.record_toolchain_import
def crash(self,*args):
 if args[2]=='rejected':
  if boundary=='terminal': real(self,*args)
  os._exit(27)
 return real(self,*args)
isolation.Journal.record_toolchain_import=crash
with isolation.Journal(pathlib.Path(state)) as journal:
 publication.reject(journal,identity,binding,pathlib.Path(policy),pathlib.Path(registry))
'''
    args = [str(prepared.state), prepared.identity, prepared.binding, str(prepared.policy_path),
            str(prepared.registry_path), loader.REVIEWED_ARCHIVES, boundary]
    prepared.journal.__exit__(None, None, None)
    result = REAL_RUN([sys.executable, '-c', code, json.dumps(args)], capture_output=True, timeout=15, check=False)
    assert result.returncode == 27, result.stderr
    prepared.journal.__enter__()
    result = reject(prepared)
    assert result['replayed_observation'] is (boundary == 'terminal')
    prepared.journal.require_available()
    assert prepared.journal.db.execute('SELECT count(*) FROM toolchain_rejections').fetchone()[0] == 1


def test_new_reviewed_source_gets_distinct_intent_and_reuses_bytes_without_hardlink(prepared):
    reject(prepared)
    previous = loader.item(prepared.journal, prepared.identity)
    request = {**previous['intent'], 'version': 2, 'bundle_sha256': 'b' * 64}
    current = prepared.journal.begin_toolchain_import(request)
    assert current['id'] != prepared.identity
    result = custody.reuse_custody(prepared.journal, current['id'], prepared.identity)
    old_path = prepared.state / 'toolchain-imports' / prepared.identity / 'archive.tar'
    new_path = prepared.state / 'toolchain-imports' / current['id'] / 'archive.tar'
    assert old_path.read_bytes() == new_path.read_bytes()
    assert old_path.stat().st_ino != new_path.stat().st_ino
    assert old_path.stat().st_nlink == new_path.stat().st_nlink == 1
    assert result['archive']['image_loaded'] is False
    assert custody.reuse_custody(prepared.journal, current['id'], prepared.identity) == result
    with pytest.raises(loader.ToolchainLoadError, match='configuration'):
        loader.verify_binding(prepared.journal, current['id'], prepared.binding)
    assert loader.verify_binding(prepared.journal, current['id'],
                                 {**prepared.binding, 'bundle_sha256': 'b' * 64})['id'] == current['id']


@pytest.mark.parametrize('version,changes', [(1, {'bundle_sha256': 'a' * 64}), (2, {}),
                                          (2, {'bundle_sha256': 'invalid'}), (True, {})])
def test_versioned_intent_rejects_missing_extra_or_invalid_source_identity(version, changes):
    with pytest.raises(isolation.IsolationError):
        isolation.Journal.import_intent(intent(version=version, **changes))


def test_different_bundles_never_share_import_identity():
    old = intent()
    first = isolation.Journal.import_intent({**old, 'version': 2, 'bundle_sha256': 'a' * 64})[1]
    second = isolation.Journal.import_intent({**old, 'version': 2, 'bundle_sha256': 'b' * 64})[1]
    assert len({first, second, isolation.Journal.import_intent(old)[1]}) == 3


def test_rejected_receipt_remains_historical_after_other_configuration_changes(prepared):
    reject(prepared)
    # A later separately approved configuration must not invalidate an earlier
    # rejection or resurrect it as an approved image. Publication has its own gate.
    prepared.policy_path.write_bytes(prepared.policy_path.read_bytes() + b' ')
    prepared.registry_path.write_bytes(prepared.registry_path.read_bytes() + b' ')
    prepared.journal.require_available()
    assert reject(prepared)['image_approved'] is False


def test_cleaned_failed_fixture_can_be_retained_and_rejected_without_approval(prepared):
    row = prepared.journal.db.execute('SELECT * FROM toolchain_proof_cases WHERE import_id=? LIMIT 1',
                                      (prepared.identity,)).fetchone()
    result = json.loads(row['result'])
    result.update(passed=False, exit_code=2)
    # Construct a coherent synthetic failure fixture, not a forged pass receipt.
    raw = json.loads(prepared.journal.db.execute('SELECT result FROM toolchain_proof_results WHERE job_id=?',
                                               (row['job_id'],)).fetchone()[0])
    raw.update(success=False, exit_code=2)
    raw_body = custody.canonical(raw)
    result['result_sha256'] = custody.checksum(raw_body)
    with prepared.journal.db:
        prepared.journal.db.execute('UPDATE toolchain_proof_results SET result=? WHERE job_id=?',
                                    (raw_body.decode(), row['job_id']))
        prepared.journal.db.execute('UPDATE toolchain_proof_cases SET result=? WHERE job_id=?',
                                    (custody.canonical(result).decode(), row['job_id']))
    assert publication.proof.observe(prepared.journal, prepared.identity, prepared.binding)['all_fixed_cases_passed'] is False
    assert reject(prepared)['image_approved'] is False
    assert json.loads(prepared.journal.db.execute('SELECT result FROM toolchain_proof_cases WHERE job_id=?',
                                                  (row['job_id'],)).fetchone()[0]) == result
    prepared.journal.require_available()


@pytest.mark.parametrize('phase', ['publishing', 'enrolled'])
def test_rejection_never_rolls_back_partial_or_completed_publication(prepared, phase):
    result = publication.prepare(prepared.journal, prepared.identity, prepared.binding,
                                 prepared.policy_path, prepared.registry_path)
    if phase == 'publishing':
        prepared.journal.record_toolchain_import(prepared.identity, 'proof_pending', 'publishing', result['plan_sha256'])
    else:
        publication.publish(prepared.journal, prepared.identity, result['plan_sha256'],
                            prepared.binding['bundle_sha256'], prepared.policy_path, prepared.registry_path)
    before = prepared.policy_path.read_bytes(), prepared.registry_path.read_bytes()
    with pytest.raises(publication.ToolchainPublicationError, match='unpublished'):
        reject(prepared)
    assert (prepared.policy_path.read_bytes(), prepared.registry_path.read_bytes()) == before


@pytest.mark.parametrize('corrupt', [False, True])
def test_installer_reads_rejection_receipt_under_owned_slot_without_execution(prepared, monkeypatch, corrupt):
    from app.coding import operator_bundle as bundle
    reject(prepared)
    root = prepared.state / 'operator-source'
    root.mkdir(mode=0o700)
    source = root / 'isolated_validation.py'
    source.write_bytes(Path(isolation.__file__).read_bytes())
    source.chmod(0o755)
    monkeypatch.setattr(bundle, 'ROOT', root)
    monkeypatch.setattr(bundle, 'POLICY', prepared.policy_path)
    monkeypatch.setattr(bundle, 'REGISTRY', prepared.registry_path)
    if corrupt:
        with prepared.journal.db:
            prepared.journal.db.execute('DELETE FROM toolchain_rejections')
    with sqlite3.connect('file:' + str(prepared.state / 'jobs.sqlite3') + '?mode=ro', uri=True) as connection:
        if corrupt:
            with pytest.raises(RuntimeError, match='[Rr]ejection receipt'):
                bundle.verify_enrolled(connection, prepared.state)
        else:
            bundle.verify_enrolled(connection, prepared.state)
            assert connection.execute('SELECT count(*) FROM toolchain_rejections').fetchone()[0] == 1


@pytest.mark.parametrize('fault', ['source_unknown', 'same_identity', 'image', 'source_active', 'archive'])
def test_custody_reuse_rejects_nonterminal_different_or_corrupt_source(host, fault):
    old = loader.item(host.journal, host.identity)
    values = {**old['intent'], 'version': 2, 'bundle_sha256': 'b' * 64}
    if fault == 'image':
        values['image_id'] = 'sha256:' + 'a' * 64
    current = host.journal.begin_toolchain_import(values)
    source_id = host.identity
    if fault == 'source_unknown':
        source_id = '0' * 64
    elif fault == 'same_identity':
        source_id = current['id']
    elif fault == 'source_active':
        # A prepared destination cannot itself act as retained source.
        source_id, current = current['id'], old
    elif fault == 'archive':
        path = host.state / 'toolchain-imports' / host.identity / 'archive.tar'
        path.write_bytes(path.read_bytes()[:-1])
    with pytest.raises(custody.ToolchainStagingError):
        custody.reuse_custody(host.journal, current['id'], source_id)
