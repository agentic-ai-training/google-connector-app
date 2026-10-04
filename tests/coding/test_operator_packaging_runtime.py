"""Real filesystem/SQLite transitions; kernel ownership observations simulated."""
import hashlib
import json
from pathlib import Path
import sqlite3
import stat

import pytest

from scripts.prepare_packaging_runtime import REMOTE_SOURCE


@pytest.fixture
def operator(tmp_path):
    namespace = {'__name__': 'tested_optional_runtime'}
    exec(compile(REMOTE_SOURCE, 'tested_optional_runtime', 'exec'), namespace)
    (tmp_path / 'bin').mkdir()
    paths = [tmp_path / 'bin' / name for name in ('runsc', 'sentry', 'parking')]
    for path in paths:
        path.write_bytes(b'fixed-test-binary-' + path.name.encode())
        path.chmod(0o755)
    namespace['BINARY'] = paths[-1]
    namespace['PINS'] = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    namespace['POLICY'] = tmp_path / 'policy.json'
    namespace['POLICY'].write_text(json.dumps({'state_directory': str(tmp_path)}))
    namespace['GROUP'] = tmp_path / 'group'
    namespace['GROUP'].mkdir()
    (namespace['GROUP'] / 'cgroup.procs').write_text('')
    (tmp_path / 'slot.lock').touch()
    with sqlite3.connect(tmp_path / 'jobs.sqlite3') as connection:
        connection.execute('CREATE TABLE jobs(state TEXT)')
    namespace['safe'] = lambda path: path  # No claim of real Linux/root/cgroup proof.
    return namespace, tmp_path


def probe(operator):
    return operator[0]['operate']({'operation': 'probe'})


def transition(operator, operation='exclude', snapshot_hash=None):
    return operator[0]['operate']({'operation': operation,
                                  'expected_snapshot_hash': snapshot_hash or probe(operator)['snapshot_hash']})


def test_default_probe_never_changes_mode_or_creates_intent(operator):
    result = probe(operator)
    assert result['optional_sidecar_excluded'] is False
    assert result['state']['intent'] is None
    assert not (operator[1] / 'optional-sidecar.json').exists()


def test_exclusion_readback_replay_and_restoration_preserve_exact_bytes(operator):
    ns, _ = operator
    before = {name: Path(name).read_bytes() for name in ns['PINS']}
    result = transition(operator)
    assert result['optional_sidecar_excluded'] is True
    assert result['state']['intent']['phase'] == 'committed'
    assert stat.S_IMODE(ns['BINARY'].stat().st_mode) == 0o644
    assert probe(operator)['snapshot_hash'] == result['snapshot_hash']
    assert transition(operator)['snapshot_hash'] == result['snapshot_hash']
    assert transition(operator, 'restore')['optional_sidecar_excluded'] is False
    assert before == {name: Path(name).read_bytes() for name in ns['PINS']}
    assert all(result[key] is False for key in
               ('production_packaging_activated', 'cloud_resource_created', 'docker_restarted'))


def test_stale_snapshot_never_changes_binary(operator):
    with pytest.raises(operator[0]['RuntimeErrorEvidence'], match='snapshot changed'):
        transition(operator, snapshot_hash='0' * 64)
    assert probe(operator)['state']['intent'] is None


@pytest.mark.parametrize('path_index', [0, 1, 2])
def test_foreign_runtime_digest_never_reaches_transition(operator, path_index):
    path = Path(list(operator[0]['PINS'])[path_index])
    path.write_bytes(b'unreviewed-runtime')
    with pytest.raises(operator[0]['RuntimeErrorEvidence'], match='package differs'):
        probe(operator)


@pytest.mark.parametrize('kind', ['journal', 'group', 'foreign_child', 'foreign_mode'])
def test_unreconciled_or_foreign_state_is_not_modified(operator, kind):
    ns, state = operator
    if kind == 'journal':
        with sqlite3.connect(state / 'jobs.sqlite3') as connection:
            connection.execute("INSERT INTO jobs VALUES('cleanup_uncertain')")
    elif kind == 'group':
        (ns['GROUP'] / 'cgroup.procs').write_text('4242')
    elif kind == 'foreign_child':
        child = ns['GROUP'] / 'foreign'
        child.mkdir()
        (child / 'cgroup.procs').write_text('')
    else:
        ns['BINARY'].chmod(0o744)
    with pytest.raises(ns['RuntimeErrorEvidence']):
        probe(operator)
    assert not (state / 'optional-sidecar.json').exists()


def test_extra_authority_is_not_accepted(operator):
    with pytest.raises(operator[0]['RuntimeErrorEvidence'], match='envelope'):
        operator[0]['operate']({'operation': 'probe', 'path': '/unreviewed'})


def test_interruption_after_chmod_requires_fresh_readback_and_same_target(operator):
    ns, _ = operator
    sync = ns['sync']

    def interrupted(path):
        if path == ns['BINARY'].parent:
            raise RuntimeError('simulated lost response after fixed chmod')
        return sync(path)

    ns['sync'] = interrupted
    with pytest.raises(RuntimeError, match='lost response'):
        transition(operator)
    state = probe(operator)
    assert state['optional_sidecar_excluded'] is True
    assert state['state']['intent']['phase'] == 'prepared'
    with pytest.raises(ns['RuntimeErrorEvidence'], match='same-target'):
        transition(operator, 'restore', state['snapshot_hash'])
    ns['sync'] = sync
    result = transition(operator, 'exclude', state['snapshot_hash'])
    assert result['state']['intent']['phase'] == 'committed'


def test_binary_replacement_between_snapshot_and_chmod_is_rejected(operator):
    ns, _ = operator
    original = ns['persist']

    def replace(path, intent):
        original(path, intent)
        if intent['phase'] == 'prepared':
            replacement = ns['BINARY'].with_name('replacement')
            replacement.write_bytes(ns['BINARY'].read_bytes())
            replacement.chmod(0o755)
            replacement.replace(ns['BINARY'])

    ns['persist'] = replace
    with pytest.raises(ns['RuntimeErrorEvidence'], match='inode changed'):
        transition(operator)
    assert probe(operator)['state']['intent']['phase'] == 'prepared'
