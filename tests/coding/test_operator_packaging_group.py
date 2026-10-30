"""Real file/journal transactions; systemd and cgroup observations are simulated."""
import json
from pathlib import Path
import sqlite3

import pytest

from scripts.prepare_packaging_group import REMOTE_SOURCE


@pytest.fixture
def operator(tmp_path):
    namespace = {'__name__': 'tested_operator_group'}
    exec(compile(REMOTE_SOURCE, 'tested_operator_group', 'exec'), namespace)
    namespace['UNIT'] = tmp_path / 'gcapackaging.slice'
    namespace['GROUP'] = tmp_path / 'group'
    namespace['POLICY'] = tmp_path / 'policy.json'
    namespace['POLICY'].write_text(json.dumps({'state_directory': str(tmp_path)}))
    (tmp_path / 'slot.lock').touch()
    with sqlite3.connect(tmp_path / 'jobs.sqlite3') as connection:
        connection.execute('CREATE TABLE jobs(state TEXT)')
    # Temporary paths and cgroups are not real root-owned kernel files.
    namespace['safe'] = lambda path: path
    calls = []

    def systemctl(*args):
        calls.append(args)
        assert args in {('daemon-reload',), ('start', 'gcapackaging.slice')}
        if args[0] == 'start':
            namespace['GROUP'].mkdir(exist_ok=True)
            for name, value in {'cpu.max': '100000 100000', 'memory.max': '3221225472',
                                'memory.swap.max': '0', 'pids.max': '128', 'cgroup.procs': ''}.items():
                (namespace['GROUP'] / name).write_text(value)
            controller = namespace['GROUP'] / 'controller'
            if controller.exists():
                (controller / 'cgroup.procs').write_text('')

    namespace['systemctl'] = systemctl
    namespace['sync'] = lambda path: None
    # mkdir creates cgroup files in the kernel; replicate only that operation.
    original_mkdir = Path.mkdir

    return namespace, calls, tmp_path, original_mkdir


def probe(operator):
    return operator[0]['operate']({'operation': 'probe'})


def apply(operator, monkeypatch, snapshot_hash=None):
    ns, _, _, mkdir = operator

    def simulated_mkdir(path, *args, **kwargs):
        mkdir(path, *args, **kwargs)
        if path == ns['GROUP'] / 'controller':
            (path / 'cgroup.procs').write_text('')

    monkeypatch.setattr(Path, 'mkdir', simulated_mkdir)
    return ns['operate']({'operation': 'apply', 'expected_snapshot_hash': snapshot_hash or probe(operator)['snapshot_hash']})


def test_probe_is_readonly_and_does_not_assert_missing_limits(operator):
    result = probe(operator)
    assert result['kernel_limits_verified'] is False
    assert result['state'] == {'unit_hash': None, 'kernel': {}}
    assert operator[1] == [] and not operator[0]['UNIT'].exists()


def test_fixed_group_apply_and_independent_replay(operator, monkeypatch):
    result = apply(operator, monkeypatch)
    assert result['kernel_limits_verified'] is True
    assert result['production_packaging_activated'] is False
    assert result['cloud_resource_created'] is False
    assert operator[1] == [('daemon-reload',), ('start', 'gcapackaging.slice')]
    assert operator[0]['UNIT'].read_bytes() == operator[0]['BODY']
    assert operator[0]['UNIT'].stat().st_mode & 0o777 == 0o600
    assert probe(operator)['snapshot_hash'] == result['snapshot_hash']
    assert apply(operator, monkeypatch)['snapshot_hash'] == result['snapshot_hash']


def test_changed_snapshot_cannot_write_or_start(operator, monkeypatch):
    with pytest.raises(operator[0]['GroupError'], match='snapshot changed'):
        apply(operator, monkeypatch, '0' * 64)
    assert not operator[0]['UNIT'].exists() and not operator[1]


def test_unresolved_journal_prevents_setup(operator, monkeypatch):
    with sqlite3.connect(operator[2] / 'jobs.sqlite3') as connection:
        connection.execute("INSERT INTO jobs VALUES('cleanup_uncertain')")
    with pytest.raises(operator[0]['GroupError'], match='unreconciled'):
        apply(operator, monkeypatch)
    assert not operator[0]['UNIT'].exists() and not operator[1]


def test_foreign_unit_is_not_replaced(operator):
    operator[0]['UNIT'].write_bytes(b'foreign-operator-policy')
    with pytest.raises(operator[0]['GroupError'], match='foreign packaging unit'):
        probe(operator)
    assert operator[0]['UNIT'].read_bytes() == b'foreign-operator-policy'


@pytest.mark.parametrize('occupant', ['aggregate', 'controller', 'foreign_child'])
def test_occupied_group_is_never_modified(operator, monkeypatch, occupant):
    apply(operator, monkeypatch)
    group = operator[0]['GROUP']
    if occupant == 'foreign_child':
        (group / 'foreign').mkdir()
    else:
        (group / ('controller/cgroup.procs' if occupant == 'controller' else 'cgroup.procs')).write_text('4242')
    with pytest.raises(operator[0]['GroupError'], match='occupied|contains native processes'):
        probe(operator)


@pytest.mark.parametrize('values', [{}, {'memory.max': 'max'}, {'cpu.max': 'max 100000'}])
def test_missing_or_unbounded_kernel_values_are_not_readiness(operator, values):
    assert operator[0]['ready'](values) is False


def test_extra_operator_authority_is_not_accepted(operator):
    with pytest.raises(operator[0]['GroupError'], match='envelope'):
        operator[0]['operate']({'operation': 'probe', 'shell': 'arbitrary'})


def test_install_interruption_is_visible_and_can_roll_forward(operator, monkeypatch):
    ns = operator[0]
    start = ns['systemctl']

    def interrupted(*args):
        raise ns['GroupError']('synthetic systemd response lost')

    ns['systemctl'] = interrupted
    with pytest.raises(ns['GroupError'], match='response lost'):
        apply(operator, monkeypatch)
    result = probe(operator)
    assert result['state']['unit_hash'] is not None and result['kernel_limits_verified'] is False
    ns['systemctl'] = start
    assert apply(operator, monkeypatch, result['snapshot_hash'])['kernel_limits_verified'] is True
