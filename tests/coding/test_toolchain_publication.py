"""Protected SQLite/configuration evidence; systemd, Docker and native hooks mocked."""
import io
import json
from pathlib import Path
import subprocess
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from app.coding import toolchain_publication as publication, toolchain_staging as custody
from app.coding import isolated_validation as isolation, toolchain_proof as proof
from tests.coding.test_toolchain_loader import host as host_fixture
from tests.coding.test_toolchain_proof import loaded, execute
from scripts.probe_local_offline_toolchains import fixture_archive


host = host_fixture
REAL_RUN = subprocess.run


@pytest.fixture
def prepared(host, monkeypatch):
    policy_path, registry_path = host.state / 'policy.json', host.state / 'registry.json'
    python = 'sha256:' + 'a' * 64
    policy = {'version': 2, 'runtime': 'runsc', 'state_directory': str(host.state),
              'images': {'python_compile': python}}
    legacy = {'archive_path': str(host.state / ('a' * 64 + '.tar')), 'archive_sha256': 'a' * 64,
              'archive_bytes': 100, 'repository_digest': 'docker.io/example/python@' + python}
    registry = {'version': 1, 'images': {python: legacy}}
    policy_path.write_bytes(custody.canonical(policy))
    registry_path.write_bytes(custody.canonical(registry))
    policy_path.chmod(0o600)
    registry_path.chmod(0o600)
    monkeypatch.setattr(isolation, 'VALIDATION_POLICY', policy_path)
    monkeypatch.setattr(isolation, 'TOOLCHAIN_REGISTRY', registry_path)
    original = next(row for row in host.journal.toolchain_imports() if row['id'] == host.identity)
    intent = {**original['intent'], 'policy_sha256': custody.checksum(policy_path.read_bytes()),
              'registry_sha256': custody.checksum(registry_path.read_bytes())}
    body = (host.state / 'toolchain-imports' / host.identity / 'archive.tar').read_bytes()
    row = host.journal.begin_toolchain_import(intent)
    receipt = custody.stage_archive(host.journal, row['id'], io.BytesIO(body))
    host.journal.record_toolchain_import(row['id'], 'archive_verified', 'cancelled', 'a' * 64)
    current = SimpleNamespace(**{**host.__dict__, 'identity': row['id'], 'receipt': receipt,
                                'binding': {**host.binding, 'policy_sha256': intent['policy_sha256'],
                                            'registry_sha256': intent['registry_sha256']}})
    loaded(current, monkeypatch)
    execute(current, monkeypatch)
    for case in proof.CASES[current.role]:
        source, _ = fixture_archive(current.role, negative=case.endswith('_fail'))
        proof.run(current.journal, current.identity, case, source, current.binding)
    return SimpleNamespace(**current.__dict__, policy_path=policy_path, registry_path=registry_path,
                           python=python, legacy=legacy)


def prepare(host):
    return publication.prepare(host.journal, host.identity, host.binding, host.policy_path, host.registry_path)


def plan(host):
    row = host.journal.db.execute('SELECT * FROM toolchain_publication_plans').fetchone()
    assert custody.checksum(row['plan'].encode()) == row['plan_sha256']
    return json.loads(row['plan'])


def test_preparation_preserves_old_configuration_and_never_releases_or_approves(prepared):
    before = prepared.policy_path.read_bytes(), prepared.registry_path.read_bytes()
    result = prepare(prepared)
    assert result['image_approved'] is False and result['configuration_published'] is False
    assert (prepared.policy_path.read_bytes(), prepared.registry_path.read_bytes()) == before
    saved = plan(prepared)
    new_registry = json.loads(saved['documents']['registry_new'])
    assert isolation.validate_toolchain_registry(new_registry) == new_registry
    assert new_registry['images'][prepared.python] == prepared.legacy
    record = new_registry['images'][prepared.image_id]
    assert record['origin'] == 'reviewed_operator_archive' and 'repository_digest' not in record
    assert record['image_index_digest'] == prepared.image_id
    assert Path(record['archive_path']).read_bytes()
    assert set(record['proofs']) == set(proof.CASES[prepared.role])
    new_policy = json.loads(saved['documents']['policy_new'])
    assert new_policy['images']['python_compile'] == prepared.python
    for case in proof.CASES[prepared.role]:
        assert new_policy['images'][case.rsplit('_', 1)[0]] == prepared.image_id
    assert prepare(prepared) == result
    assert prepared.journal.db.execute('SELECT count(*) FROM toolchain_publication_plans').fetchone()[0] == 1
    with pytest.raises(isolation.IsolationError, match='toolchain import'):
        prepared.journal.require_available()


@pytest.mark.parametrize('fault', ['policy', 'registry', 'missing_case', 'unfinished_job', 'expired'])
def test_incomplete_or_changed_authority_cannot_prepare_candidate(prepared, monkeypatch, fault):
    if fault in {'policy', 'registry'}:
        path = getattr(prepared, fault + '_path')
        path.write_bytes(path.read_bytes() + b' ')
    if fault == 'missing_case':
        with prepared.journal.db:
            prepared.journal.db.execute('DELETE FROM toolchain_proof_cases WHERE import_id=? AND case_name=?',
                                        (prepared.identity, proof.CASES[prepared.role][0]))
    if fault == 'unfinished_job':
        job = prepared.journal.db.execute('SELECT * FROM jobs LIMIT 1').fetchone()
        prepared.journal.record(dict(job), 'cleanup_uncertain')
    if fault == 'expired':
        row = next(r for r in prepared.journal.toolchain_imports() if r['id'] == prepared.identity)
        monkeypatch.setattr(publication.time, 'time', lambda: row['intent']['stop_unix'])
    with pytest.raises(publication.ToolchainPublicationError):
        prepare(prepared)
    assert prepared.journal.db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='toolchain_publication_plans'").fetchone() is None


def test_corrupted_retained_plan_cannot_be_repaired_by_overwriting_history(prepared):
    prepare(prepared)
    with prepared.journal.db:
        prepared.journal.db.execute("UPDATE toolchain_publication_plans SET plan='{}'")
    with pytest.raises(publication.ToolchainPublicationError, match='retained publication'):
        prepare(prepared)
    assert prepared.journal.db.execute('SELECT plan FROM toolchain_publication_plans').fetchone()[0] == '{}'


@pytest.mark.parametrize('field,value', [('origin', 'repository'), ('role', {}), ('import_id', 'fake'),
                                        ('stop_unix', True), ('proofs', {}), ('config_digest', 'sha256:wrong')])
def test_typed_operator_provenance_rejects_forged_fields(prepared, field, value):
    prepare(prepared)
    registry = json.loads(plan(prepared)['documents']['registry_new'])
    registry['images'][prepared.image_id][field] = value
    with pytest.raises(isolation.IsolationError, match='operator provenance'):
        isolation.validate_toolchain_registry(registry)


def test_missing_operator_image_cannot_trigger_hidden_docker_load(prepared, monkeypatch):
    prepare(prepared)
    registry = prepared.state / 'future-registry.json'
    registry.write_text(plan(prepared)['documents']['registry_new'])
    registry.chmod(0o600)
    monkeypatch.setattr(isolation, 'TOOLCHAIN_REGISTRY', registry)
    calls = []

    def missing(*args, **kwargs):
        calls.append(args)
        raise isolation.IsolationError('missing image')

    monkeypatch.setattr(isolation, 'docker', missing)
    with pytest.raises(isolation.IsolationError, match='explicit maintenance'):
        isolation.ensure_toolchain_image(prepared.image_id)
    assert len(calls) == 1 and calls[0][:2] == ('image', 'inspect')


def test_process_death_after_plan_commit_preserves_exact_preparation_without_publication(prepared):
    root = str(Path(__file__).resolve().parents[2])
    values = repr({'identity': prepared.identity, 'binding': prepared.binding,
                   'state': str(prepared.state), 'policy': str(prepared.policy_path),
                   'registry': str(prepared.registry_path)})
    before = prepared.policy_path.read_bytes(), prepared.registry_path.read_bytes()
    prepared.journal.__exit__()
    code = '\n'.join([
        'import sys,os,json', 'from pathlib import Path', 'sys.path.insert(0,' + repr(root) + ')',
        'from app.coding import toolchain_publication as p,toolchain_loader as l,toolchain_proof as f',
        'from app.coding.isolated_validation import Journal', 'v=' + values,
        'with Journal(Path(v["state"])) as j:',
        '    intent=json.loads(j.db.execute("SELECT intent FROM toolchain_imports WHERE id=?",'
        '(v["identity"],)).fetchone()[0])',
        '    l.REVIEWED_ARCHIVES={intent["role"]:{k:intent[k] for k in '
        '("image_id","archive_sha256","archive_bytes","recipe_sha256")}}',
        '    l.reconcile=lambda *a:{"load_verified":True}',
        '    f.native.require_native_cleanup=lambda *a:None',
        '    p.prepare(j,v["identity"],v["binding"],Path(v["policy"]),Path(v["registry"]))',
        '    os._exit(97)',
    ])
    result = REAL_RUN([sys.executable, '-I', '-B', '-c', code], capture_output=True,
                      timeout=15, env={'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent'}, check=False)
    prepared.journal.__enter__()
    assert result.returncode == 97
    retained = plan(prepared)
    observed = prepare(prepared)
    assert observed['plan_sha256'] == custody.checksum(custody.canonical(retained))
    assert observed['configuration_published'] is False
    assert (prepared.policy_path.read_bytes(), prepared.registry_path.read_bytes()) == before


def publish(host, fingerprint=None):
    fingerprint = prepare(host)['plan_sha256'] if fingerprint is None else fingerprint
    return publication.publish(host.journal, host.identity, fingerprint, host.binding['bundle_sha256'],
                               host.policy_path, host.registry_path)


def test_explicit_publication_releases_only_verified_pair_and_replay_does_not_write(prepared, monkeypatch):
    fingerprint = prepare(prepared)['plan_sha256']
    result = publish(prepared, fingerprint)
    assert result['image_approved'] is True and result['configuration_published'] is True
    assert result['replayed_observation'] is False
    prepared.journal.require_available()
    monkeypatch.setattr(publication, 'replace_configuration', lambda *args: pytest.fail('replay cannot write'))
    assert publish(prepared, fingerprint)['replayed_observation'] is True
    with pytest.raises(publication.ToolchainPublicationError, match='controller changed'):
        publish(prepared, '0' * 64)


@pytest.mark.parametrize('fault', ['policy', 'registry', 'receipt'])
def test_changed_published_configuration_or_receipt_closes_admission(prepared, fault):
    fingerprint = prepare(prepared)['plan_sha256']
    publish(prepared, fingerprint)
    if fault in {'policy', 'registry'}:
        path = getattr(prepared, fault + '_path')
        path.write_bytes(path.read_bytes() + b' ')
    else:
        row = next(row for row in prepared.journal.toolchain_imports() if row['id'] == prepared.identity)
        events = row['observations']
        events[-1]['evidence_sha256'] = '0' * 64
        with prepared.journal.db:
            prepared.journal.db.execute('UPDATE toolchain_imports SET observations=? WHERE id=?',
                                        (json.dumps(events), prepared.identity))
    with pytest.raises(isolation.IsolationError):
        prepared.journal.require_available()


def test_terminal_phase_without_actual_pair_cannot_release_admission(prepared):
    fingerprint = prepare(prepared)['plan_sha256']
    retained = plan(prepared)
    prepared.journal.record_toolchain_import(prepared.identity, 'proof_pending', 'publishing', fingerprint)
    receipt = isolation.publication_receipt(prepared.identity, fingerprint, retained['hashes'])
    prepared.journal.record_toolchain_import(prepared.identity, 'publishing', 'enrolled',
                                           custody.checksum(custody.canonical(receipt)))
    with pytest.raises(isolation.IsolationError):
        prepared.journal.require_available()


@pytest.mark.parametrize('boundary', ['reserved', 'registry', 'policy'])
def test_interrupted_publication_keeps_slot_and_resumes_only_original_pair(prepared, monkeypatch, boundary):
    fingerprint = prepare(prepared)['plan_sha256']
    original = publication.replace_configuration
    calls = []

    def interrupted(path, *args):
        calls.append(path)
        if boundary == 'reserved':
            raise RuntimeError('interruption after durable reservation')
        original(path, *args)
        if (boundary == 'registry' and path == prepared.registry_path
                or boundary == 'policy' and path == prepared.policy_path):
            raise RuntimeError('interruption after durable replacement')

    monkeypatch.setattr(publication, 'replace_configuration', interrupted)
    with pytest.raises(RuntimeError, match='interruption'):
        publish(prepared, fingerprint)
    with pytest.raises(isolation.IsolationError, match='toolchain import'):
        prepared.journal.require_available()
    with pytest.raises(publication.ToolchainPublicationError, match='controller changed'):
        publish(prepared, '0' * 64)
    monkeypatch.setattr(publication, 'replace_configuration', original)
    assert publish(prepared, fingerprint)['configuration_published'] is True
    prepared.journal.require_available()


@pytest.mark.parametrize('foreign', [False, True])
def test_partial_configuration_staging_is_exact_prefix_only(prepared, foreign):
    fingerprint = prepare(prepared)['plan_sha256']
    retained = plan(prepared)
    pending = prepared.registry_path.with_name(prepared.registry_path.name + '.gca-publication.pending')
    pending.write_bytes(b'foreign' if foreign else retained['documents']['registry_new'].encode()[:37])
    pending.chmod(0o600)
    if foreign:
        with pytest.raises(publication.ToolchainPublicationError, match='staging differs'):
            publish(prepared, fingerprint)
        assert pending.read_bytes() == b'foreign'
        with pytest.raises(isolation.IsolationError, match='toolchain import'):
            prepared.journal.require_available()
    else:
        assert publish(prepared, fingerprint)['configuration_published'] is True
        prepared.journal.require_available()


@pytest.mark.parametrize('boundary', ['reserved', 'registry', 'policy', 'complete'])
def test_real_process_death_keeps_publication_or_exact_completed_evidence(prepared, boundary):
    fingerprint = prepare(prepared)['plan_sha256']
    root = str(Path(__file__).resolve().parents[2])
    values = repr({'identity': prepared.identity, 'binding': prepared.binding, 'fingerprint': fingerprint,
                   'state': str(prepared.state), 'policy': str(prepared.policy_path),
                   'registry': str(prepared.registry_path), 'boundary': boundary})
    prepared.journal.__exit__()
    code = '\n'.join([
        'import sys,os,json', 'from pathlib import Path', 'sys.path.insert(0,' + repr(root) + ')',
        'from app.coding import toolchain_publication as p,toolchain_loader as l,toolchain_proof as f',
        'from app.coding import isolated_validation as i', 'v=' + values,
        'i.VALIDATION_POLICY=Path(v["policy"]);i.TOOLCHAIN_REGISTRY=Path(v["registry"])',
        'with i.Journal(Path(v["state"])) as j:',
        '    intent=json.loads(j.db.execute("SELECT intent FROM toolchain_imports WHERE id=?",'
        '(v["identity"],)).fetchone()[0])',
        '    l.REVIEWED_ARCHIVES={intent["role"]:{k:intent[k] for k in '
        '("image_id","archive_sha256","archive_bytes","recipe_sha256")}}',
        '    l.reconcile=lambda *a:{"load_verified":True}', '    f.native.require_native_cleanup=lambda *a:None',
        '    original=p.replace_configuration', '    def interrupted(path,*args):',
        '        if v["boundary"]=="reserved":os._exit(97)', '        original(path,*args)',
        '        if path==Path(v.get(v["boundary"],"/unreachable")):os._exit(97)',
        '    p.replace_configuration=interrupted',
        '    p.publish(j,v["identity"],v["fingerprint"],v["binding"]["bundle_sha256"],'
        'Path(v["policy"]),Path(v["registry"]))', '    os._exit(97)',
    ])
    result = REAL_RUN([sys.executable, '-I', '-B', '-c', code], capture_output=True,
                      timeout=15, env={'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent'}, check=False)
    prepared.journal.__enter__()
    assert result.returncode == 97
    if boundary != 'complete':
        with pytest.raises(isolation.IsolationError, match='toolchain import'):
            prepared.journal.require_available()
    result = publish(prepared, fingerprint)
    assert result['configuration_published'] is True
    assert result['replayed_observation'] is (boundary == 'complete')
    prepared.journal.require_available()


def test_two_roles_form_an_exact_configuration_chain_without_replacing_python(prepared, monkeypatch):
    from app.coding import toolchain_loader as loader
    from tests.coding.test_toolchain_archive import fixture, archive
    publish(prepared)
    other = 'spring' if prepared.role == 'react' else 'react'
    files, image_id = fixture(role=other)
    body = archive(files)
    catalog = {'image_id': image_id, 'archive_sha256': custody.checksum(body),
               'archive_bytes': len(body), 'recipe_sha256': 'c' * 64}
    monkeypatch.setattr(loader, 'REVIEWED_ARCHIVES', {**loader.REVIEWED_ARCHIVES, other: catalog})
    prior = next(row for row in prepared.journal.toolchain_imports() if row['id'] == prepared.identity)
    intent = {**prior['intent'], **catalog, 'role': other,
              'policy_sha256': custody.checksum(prepared.policy_path.read_bytes()),
              'registry_sha256': custody.checksum(prepared.registry_path.read_bytes())}
    row = prepared.journal.begin_toolchain_import(intent)
    custody.stage_archive(prepared.journal, row['id'], io.BytesIO(body))
    prepared.journal.record_toolchain_import(row['id'], 'archive_verified', 'cancelled', 'a' * 64)
    current = SimpleNamespace(**{**prepared.__dict__, 'identity': row['id'], 'role': other, 'image_id': image_id,
                                'binding': {**prepared.binding, 'policy_sha256': intent['policy_sha256'],
                                            'registry_sha256': intent['registry_sha256']}})
    loaded(current, monkeypatch)
    execute(current, monkeypatch)
    for case in proof.CASES[other]:
        source, _ = fixture_archive(other, negative=case.endswith('_fail'))
        proof.run(current.journal, current.identity, case, source, current.binding)
    assert publish(current)['configuration_published'] is True
    current.journal.require_available()
    registry = json.loads(current.registry_path.read_bytes())
    assert len(registry['images']) == 3 and registry['images'][current.python] == current.legacy


@pytest.mark.parametrize('corrupt', [False, True])
def test_bundle_reader_verifies_enrolled_history_read_only_under_existing_slot(prepared, monkeypatch, corrupt):
    from app.coding import operator_bundle as bundle
    publish(prepared)
    root = prepared.state / 'operator-source'
    root.mkdir(mode=0o700)
    path = root / 'isolated_validation.py'
    path.write_bytes(Path(isolation.__file__).read_bytes())
    path.chmod(0o755)
    monkeypatch.setattr(bundle, 'ROOT', root)
    monkeypatch.setattr(bundle, 'POLICY', prepared.policy_path)
    monkeypatch.setattr(bundle, 'REGISTRY', prepared.registry_path)
    before = prepared.policy_path.read_bytes(), prepared.registry_path.read_bytes()
    if corrupt:
        prepared.registry_path.write_bytes(before[1] + b' ')
    with sqlite3.connect('file:' + str(prepared.state / 'jobs.sqlite3') + '?mode=ro', uri=True) as connection:
        if corrupt:
            with pytest.raises(RuntimeError):
                bundle.verify_enrolled(connection, prepared.state)
        else:
            bundle.verify_enrolled(connection, prepared.state)
    assert prepared.policy_path.read_bytes() == before[0]
    assert prepared.registry_path.read_bytes() == before[1] + (b' ' if corrupt else b'')
