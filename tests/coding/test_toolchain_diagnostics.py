"""Private SQLite result custody and read-only diagnostics; native calls are mocked."""
import json
import sqlite3

import pytest

from app.coding import toolchain_proof as proof, toolchain_staging as custody
from app.coding import isolated_validation as isolation, toolchain_loader as loader
from scripts.probe_local_offline_toolchains import fixture_archive
from tests.coding.test_toolchain_loader import host as host_fixture
from tests.coding.test_toolchain_proof import loaded, execute


host = host_fixture


def run_case(host, monkeypatch, **options):
    loaded(host, monkeypatch)
    calls = execute(host, monkeypatch, **options)
    case = proof.CASES[host.role][0]
    source, _ = fixture_archive(host.role)
    receipt = proof.run(host.journal, host.identity, case, source, host.binding)
    return case, receipt, calls


def test_full_result_is_bound_and_diagnostic_read_never_executes_or_changes_proof_projection(host, monkeypatch):
    case, receipt, calls = run_case(host, monkeypatch)
    before = proof.observe(host.journal, host.identity, host.binding)
    raw = host.journal.db.execute('SELECT result FROM toolchain_proof_results').fetchone()[0]
    assert custody.checksum(raw.encode()) == receipt['result_sha256']
    monkeypatch.setattr(isolation, '_execute_archive', lambda *a, **k: pytest.fail('diagnostics cannot execute'))
    monkeypatch.setattr(loader, 'reconcile', lambda *a, **k: pytest.fail('diagnostics cannot load'))
    result = proof.diagnostics(host.journal, host.identity, host.binding)
    item = result['cases'][case]
    assert result['execution_invoked'] is False and result['image_approved'] is False
    assert item['raw_result_available'] and item['cleanup_verified'] and item['passed']
    assert item['result_sha256'] == receipt['result_sha256'] and item['attempt'] == 1
    assert proof.observe(host.journal, host.identity, host.binding) == before
    assert len(calls) == 1


def test_failed_result_is_retained_without_turning_failure_into_success(host, monkeypatch):
    case, receipt, _ = run_case(host, monkeypatch, valid=False)
    item = proof.diagnostics(host.journal, host.identity, host.binding)['cases'][case]
    assert receipt['passed'] is False and item['passed'] is False and item['success'] is False
    assert proof.observe(host.journal, host.identity, host.binding)['all_fixed_cases_passed'] is False


@pytest.mark.parametrize('legacy', ['no_table', 'missing_row', 'missing_receipt'])
def test_old_or_interrupted_output_is_explicitly_unavailable_not_reconstructed(host, monkeypatch, legacy):
    case, _, _ = run_case(host, monkeypatch)
    with host.journal.db:
        if legacy == 'no_table':
            host.journal.db.execute('DROP TABLE toolchain_proof_results')
        elif legacy == 'missing_row':
            host.journal.db.execute('DELETE FROM toolchain_proof_results')
        else:
            host.journal.db.execute('UPDATE toolchain_proof_cases SET result=NULL')
    item = proof.diagnostics(host.journal, host.identity, host.binding)['cases'][case]
    assert item['raw_result_available'] is False
    assert item['receipt_available'] is (legacy != 'missing_receipt')
    assert 'stdout' not in item and 'passed' not in item


@pytest.mark.parametrize('field,value', [('stdout', 'changed'), ('exit_code', 9),
                                        ('cleanup_verified', False), ('native_enforcement', {})])
def test_tampered_full_output_invalidates_diagnostics_and_passing_observation(host, monkeypatch, field, value):
    run_case(host, monkeypatch)
    row = host.journal.db.execute('SELECT * FROM toolchain_proof_results').fetchone()
    result = json.loads(row['result']); result[field] = value
    with host.journal.db:
        host.journal.db.execute('UPDATE toolchain_proof_results SET result=? WHERE job_id=?',
                               (custody.canonical(result).decode(), row['job_id']))
    for observer in (proof.diagnostics, proof.observe):
        with pytest.raises(proof.ToolchainProofError, match='diagnostic'):
            observer(host.journal, host.identity, host.binding)


def test_receipt_and_full_output_commit_atomically(host, monkeypatch):
    loaded(host, monkeypatch); execute(host, monkeypatch)
    proof.schema(host.journal)
    host.journal.db.execute("CREATE TRIGGER deny_receipt BEFORE UPDATE OF result ON toolchain_proof_cases "
                            "BEGIN SELECT RAISE(ABORT,'injected receipt failure'); END")
    case = proof.CASES[host.role][0]; source, _ = fixture_archive(host.role)
    with pytest.raises(sqlite3.IntegrityError, match='injected'):
        proof.run(host.journal, host.identity, case, source, host.binding)
    assert host.journal.db.execute('SELECT count(*) FROM toolchain_proof_results').fetchone()[0] == 0
    assert host.journal.db.execute('SELECT result FROM toolchain_proof_cases').fetchone()[0] is None
    assert host.journal.db.execute('SELECT state FROM jobs').fetchone()[0] == 'cleaned'


@pytest.mark.parametrize('fault', ['orphan', 'oversize'])
def test_bad_result_inventory_blocks_execution_before_another_case(host, monkeypatch, fault):
    case, _, calls = run_case(host, monkeypatch)
    with host.journal.db:
        if fault == 'orphan':
            host.journal.db.execute('INSERT INTO toolchain_proof_results VALUES(?,?)', ('a' * 32, '{}'))
        else:
            host.journal.db.execute('UPDATE toolchain_proof_results SET result=?',
                                   ('x' * (proof.MAX_RETAINED_RESULT + 1),))
    source, _ = fixture_archive(host.role)
    with pytest.raises(proof.ToolchainProofError, match='diagnostic inventory'):
        proof.run(host.journal, host.identity, proof.CASES[host.role][1], source, host.binding)
    assert len(calls) == 1


@pytest.mark.parametrize('text', ['x' * 8000, '\u2603' * 8000, '\U0001f642' * 8000, '\x00' * 8000])
def test_unicode_and_control_output_is_bounded_by_serialized_bytes_and_keeps_tail(text):
    value = proof.diagnostic_tail(text + 'cause', 128)
    assert len(custody.canonical(value)) <= 128 and value.endswith('cause')


def test_empty_diagnostics_does_not_create_tables(host, monkeypatch):
    monkeypatch.setattr(loader, 'reconcile', lambda *a: pytest.fail('no execution'))
    result = proof.diagnostics(host.journal, host.identity, host.binding)
    assert result['cases'] == {} and result['execution_invoked'] is False
    assert host.journal.db.execute("SELECT 1 FROM sqlite_master WHERE name='toolchain_proof_results'").fetchone() is None
