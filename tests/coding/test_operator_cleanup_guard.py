"""Cleanup configuration CAS/audit tests; no Docker, API or deletion calls."""
from types import SimpleNamespace

import pytest

from app.coding import operator_cleanup_guard as guard, toolchain_staging as custody
from tests.coding.test_toolchain_loader import host as host_fixture


host = host_fixture


@pytest.fixture
def backend(host):
    value = SimpleNamespace(state=guard.BEFORE.copy(), writes=0)
    request = {'controller_sha256': 'a' * 64, 'expected_snapshot_hash': 'b' * 64,
               'expected_settings_sha256': guard.digest(guard.BEFORE)}
    def read():
        return value.state.copy()
    def write():
        assert custody.document(host.state / 'coolify-cleanup-guard-plan.json')['after'] == guard.AFTER
        value.writes += 1
        value.state = guard.AFTER.copy()
        return read()
    value.apply = lambda: guard.apply(host.journal, custody, request, read(), read, write)
    return value


def test_guard_changes_only_forced_cleanup_and_replay_performs_no_new_write(backend):
    result = backend.apply()
    assert result['settings'] == guard.AFTER and backend.writes == 1
    assert backend.apply() == result and backend.writes == 1
    assert {k for k in guard.BEFORE if guard.BEFORE[k] != guard.AFTER[k]} == {'force_docker_cleanup'}
    assert all(result[k] is False for k in ('cleanup_invoked', 'images_restored', 'allocation_changed',
                                          'policy_changed', 'volumes_deleted', 'networks_deleted'))


@pytest.mark.parametrize('boundary', ['after_update', 'receipt'])
def test_lost_response_recovers_from_durable_plan_without_overwriting_foreign_state(backend, host, monkeypatch, boundary):
    real = custody.write_document
    def crash(path, value):
        if path.name == 'coolify-cleanup-guard-receipt.json':
            if boundary == 'receipt':
                real(path, value)
            raise RuntimeError('simulated observation loss')
        real(path, value)
    monkeypatch.setattr(custody, 'write_document', crash)
    with pytest.raises(RuntimeError):
        backend.apply()
    assert backend.state == guard.AFTER
    assert (host.state / 'coolify-cleanup-guard-plan.json').exists()
    monkeypatch.setattr(custody, 'write_document', real)
    assert backend.apply()['settings'] == guard.AFTER


@pytest.mark.parametrize('change', ['volumes', 'networks', 'threshold', 'foreign_state', 'audit', 'receipt', 'job'])
def test_guard_never_overrides_unreviewed_cleanup_configuration(backend, host, change):
    if change in {'audit', 'receipt'}:
        backend.apply()
        path = host.state / ('coolify-cleanup-guard-' + ('plan' if change == 'audit' else 'receipt') + '.json')
        custody.write_document(path, {'foreign': True})
    elif change == 'job':
        with host.journal.db:
            host.journal.db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',
                                    ('a' * 32, 'unfinished', host.image_id, 'python_compile',
                                     2000000000, 'cleanup_uncertain', None, 1, 'validation'))
    else:
        backend.state[{'volumes': 'delete_unused_volumes', 'networks': 'delete_unused_networks',
                       'threshold': 'docker_cleanup_threshold', 'foreign_state': 'force_docker_cleanup'}[change]] = (
                           90 if change == 'threshold' else True if change != 'foreign_state' else False)
    writes = backend.writes
    with pytest.raises(guard.CleanupGuardError):
        backend.apply()
    assert backend.writes == writes


def test_fixed_backend_never_contains_cleanup_or_image_loading_command():
    assert 'force_docker_cleanup' in guard.PHP
    assert '->update([\'force_docker_cleanup\'=>false])' in guard.PHP
    assert 'prune' not in guard.PHP and 'docker image' not in guard.PHP
