"""Fixed operator publication with immutable preparation and forward recovery.

Only explicit publication consumes the reviewed plan under the same host journal.
Preparation never clears proof_pending or approves an image. Partial file updates
keep publishing reserved; no ordinary workload can run until both are verified.
"""
import json
import os
import re
import time

from app.coding import isolated_validation as isolation, toolchain_loader as loader
from app.coding import toolchain_proof as proof, toolchain_staging as custody
from app.coding.toolchain_spec import unique_object


MAX_PLAN = 196608
SQL = '''CREATE TABLE IF NOT EXISTS toolchain_publication_plans(
    import_id TEXT PRIMARY KEY,plan_sha256 TEXT NOT NULL,plan TEXT NOT NULL)'''


class ToolchainPublicationError(ValueError):
    pass


def read_configuration(path):
    custody.protected(path, private=True)
    if not path.is_file() or not 1 <= path.stat().st_size <= 32768:
        raise ToolchainPublicationError('publication configuration bound differs')
    body = path.read_bytes()
    value = json.loads(body, object_pairs_hook=unique_object)
    if not isinstance(value, dict):
        raise ToolchainPublicationError('publication configuration is invalid')
    return body, value


def prepare(journal, identity, binding, policy_path, registry_path):
    """Paths are constants from the protected gateway, never request fields."""
    row = loader.verify_binding(journal, identity, binding)
    if row['phase'] != 'proof_pending' or int(time.time()) + 30 >= row['intent']['stop_unix']:
        raise ToolchainPublicationError('unexpired completed fixture phase required')
    if journal.db.execute("SELECT 1 FROM jobs WHERE state<>'cleaned' LIMIT 1").fetchone():
        raise ToolchainPublicationError('unfinished execution blocks publication preparation')
    old_policy, policy = read_configuration(policy_path)
    old_registry, registry = read_configuration(registry_path)
    if (custody.checksum(old_policy) != binding['policy_sha256']
            or custody.checksum(old_registry) != binding['registry_sha256']
            or policy.get('runtime') != 'runsc' or type(policy.get('version')) is not int
            or policy['version'] not in (2, 3) or policy.get('state_directory') != str(journal.directory)
            or not isinstance(policy.get('images'), dict)):
        raise ToolchainPublicationError('original publication configuration differs')
    isolation.validate_toolchain_registry(registry)
    observed = proof.observe(journal, identity, binding)
    if observed['all_fixed_cases_passed'] is not True:
        raise ToolchainPublicationError('all independently retained fixed cases required')
    loaded = loader.reconcile(journal, identity, binding)
    if loaded.get('load_verified') is not True:
        raise ToolchainPublicationError('positive load completion and current image readback required')
    retained = custody.verify_retained_custody(journal, identity)
    role, image = row['intent']['role'], row['intent']['image_id']
    record = {'origin': 'reviewed_operator_archive', 'role': role, 'import_id': identity,
              'archive_path': str(journal.directory / 'toolchain-imports' / identity / 'archive.tar'),
              **{key: retained['archive'][key] for key in ('archive_sha256', 'archive_bytes',
                 'image_index_digest', 'platform_manifest_digest', 'config_digest')},
              'recipe_sha256': row['intent']['recipe_sha256'], 'bundle_sha256': binding['bundle_sha256'],
              'prior_policy_sha256': binding['policy_sha256'], 'prior_registry_sha256': binding['registry_sha256'],
              'proofs': {case: custody.checksum(custody.canonical(result))
                         for case, result in observed['cases'].items()}, 'stop_unix': row['intent']['stop_unix']}
    if image in registry['images'] and registry['images'][image] != record:
        raise ToolchainPublicationError('existing image provenance cannot be replaced')
    profiles = ('react_test', 'react_build') if role == 'react' else ('spring_test', 'spring_package')
    if any(name in policy['images'] and policy['images'][name] != image for name in profiles):
        raise ToolchainPublicationError('existing profile approval cannot be overwritten')
    new_registry = {'version': 2, 'images': {**registry['images'], image: record}}
    isolation.validate_toolchain_registry(new_registry)
    new_policy = {**policy, 'images': {**policy['images'], **dict.fromkeys(profiles, image)}}
    documents = {'policy_old': old_policy.decode(), 'registry_old': old_registry.decode(),
                 'policy_new': custody.canonical(new_policy).decode(),
                 'registry_new': custody.canonical(new_registry).decode()}
    if any(len(body.encode()) > 32768 for body in documents.values()):
        raise ToolchainPublicationError('publication output configuration exceeds bound')
    plan = {'version': 1, 'intent_id': identity, 'binding': binding, 'documents': documents,
            'hashes': {name: custody.checksum(body.encode()) for name, body in documents.items()},
            'custody_sha256': custody.checksum(custody.canonical(retained)),
            'proofs_sha256': custody.checksum(custody.canonical(observed)),
            'load_sha256': custody.checksum(custody.canonical(loaded))}
    # Unit telemetry may change after completion. It is evidence, not a second
    # execution grant; the immutable manifest binds its first retained observation.
    stable = {key: value for key, value in plan.items() if key != 'load_sha256'}
    body = custody.canonical(plan).decode()
    if len(body.encode()) > MAX_PLAN:
        raise ToolchainPublicationError('publication plan exceeds bound')
    with journal.db:
        journal.db.execute(SQL)
    inventory = journal.db.execute('SELECT import_id,plan_sha256,length(plan) AS size '
                                   'FROM toolchain_publication_plans LIMIT 9').fetchall()
    known = {entry['id'] for entry in journal.toolchain_imports()}
    if (len(inventory) > 8 or any(entry['import_id'] not in known or type(entry['size']) is not int
                                or not 1 <= entry['size'] <= MAX_PLAN
                                or not isinstance(entry['plan_sha256'], str)
                                or not re.fullmatch(r'[a-f0-9]{64}', entry['plan_sha256']) for entry in inventory)):
        raise ToolchainPublicationError('publication inventory or evidence bound differs')
    previous = journal.db.execute('SELECT * FROM toolchain_publication_plans WHERE import_id=?',
                                  (identity,)).fetchone()
    if previous:
        saved = json.loads(previous['plan'], object_pairs_hook=unique_object)
        if (not isinstance(saved, dict) or custody.checksum(previous['plan'].encode()) != previous['plan_sha256']
                or {key: value for key, value in saved.items() if key != 'load_sha256'} != stable):
            raise ToolchainPublicationError('retained publication plan differs')
        fingerprint = previous['plan_sha256']
    else:
        if journal.db.execute('SELECT count(*) FROM toolchain_publication_plans').fetchone()[0] >= 8:
            raise ToolchainPublicationError('publication history requires operator review')
        fingerprint = custody.checksum(body.encode())
        with journal.db:
            journal.db.execute('INSERT INTO toolchain_publication_plans VALUES(?,?,?)',
                               (identity, fingerprint, body))
    return {'intent_id': identity, 'phase': 'prepared', 'plan_sha256': fingerprint,
            'policy_sha256': plan['hashes']['policy_new'], 'registry_sha256': plan['hashes']['registry_new'],
            'image_approved': False, 'configuration_published': False}


def replace_configuration(path, old_hash, new_body):
    """Exact forward-only file replacement. Never truncate unrelated staging bytes."""
    old, _ = read_configuration(path)
    pending = path.with_name(path.name + '.gca-publication.pending')
    target = new_body.encode()
    if old == target:
        if pending.exists() or pending.is_symlink():
            raise ToolchainPublicationError('unexpected publication staging requires review')
        return
    if custody.checksum(old) != old_hash:
        raise ToolchainPublicationError('publication target changed outside retained plan')
    if pending.exists() or pending.is_symlink():
        custody.protected(pending, private=True)
        if not pending.is_file() or pending.stat().st_size > len(target):
            raise ToolchainPublicationError('publication staging bound differs')
        prefix = pending.read_bytes()
        if not target.startswith(prefix):
            raise ToolchainPublicationError('publication staging differs from retained plan')
        fd = os.open(pending, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
    else:
        prefix = b''
        fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(target[len(prefix):])
        stream.flush()
        os.fsync(stream.fileno())
    if read_configuration(path)[0] != old:
        raise ToolchainPublicationError('publication configuration changed before replacement')
    os.replace(pending, path)
    custody.sync(path.parent)
    if read_configuration(path)[0] != target:
        raise ToolchainPublicationError('publication replacement readback differs')


def publish(journal, identity, fingerprint, bundle_hash, policy_path, registry_path):
    """Explicit fixed-target operator action; cannot accept a different candidate."""
    row = loader.item(journal, identity)
    if row['phase'] not in {'proof_pending', 'publishing', 'enrolled'}:
        raise ToolchainPublicationError('publication requires completed fixture maintenance')
    plan, retained = journal.publication_plan(row)
    if retained != fingerprint or plan['binding']['bundle_sha256'] != bundle_hash:
        raise ToolchainPublicationError('publication plan or reviewed controller changed')
    if row['phase'] == 'enrolled':
        journal.verify_publications()
        return {**isolation.publication_receipt(identity, fingerprint, plan['hashes']), 'replayed_observation': True}
    if int(time.time()) + 30 >= row['intent']['stop_unix']:
        raise ToolchainPublicationError('publication cannot extend fixed hosting lifetime')
    if journal.db.execute("SELECT 1 FROM jobs WHERE state<>'cleaned' LIMIT 1").fetchone():
        raise ToolchainPublicationError('unfinished execution blocks publication')
    # Verify both current paths are one of this transaction's exact states before
    # reserving or writing either. No hidden rollback of unrelated configuration.
    for name, path in (('policy', policy_path), ('registry', registry_path)):
        actual, _ = read_configuration(path)
        if actual.decode() not in {plan['documents'][name + '_old'], plan['documents'][name + '_new']}:
            raise ToolchainPublicationError('publication current files differ from retained plan')
    observed = proof.observe(journal, identity, plan['binding'])
    if (observed['all_fixed_cases_passed'] is not True
            or custody.checksum(custody.canonical({**observed, 'phase': 'proof_pending'})) != plan['proofs_sha256']):
        raise ToolchainPublicationError('publication fixed-case receipts changed')
    if loader.reconcile(journal, identity, plan['binding']).get('load_verified') is not True:
        raise ToolchainPublicationError('publication positive load evidence is unconfirmed')
    if (custody.checksum(custody.canonical(custody.verify_retained_custody(journal, identity)))
            != plan['custody_sha256']):
        raise ToolchainPublicationError('publication retained archive custody changed')
    if row['phase'] == 'proof_pending':
        journal.record_toolchain_import(identity, 'proof_pending', 'publishing', fingerprint)
    replace_configuration(registry_path, plan['hashes']['registry_old'], plan['documents']['registry_new'])
    replace_configuration(policy_path, plan['hashes']['policy_old'], plan['documents']['policy_new'])
    for name, path in (('policy', policy_path), ('registry', registry_path)):
        if custody.checksum(read_configuration(path)[0]) != plan['hashes'][name + '_new']:
            raise ToolchainPublicationError('complete publication readback differs')
    receipt = isolation.publication_receipt(identity, fingerprint, plan['hashes'])
    journal.record_toolchain_import(identity, 'publishing', 'enrolled', custody.checksum(custody.canonical(receipt)))
    journal.verify_publications()
    return {**receipt, 'replayed_observation': False}


def reject(journal, identity, binding, policy_path, registry_path):
    """Explicit operator abandonment, not approval, rollback or image deletion.

    A positive completed Docker receipt AND current immutable image readback are
    mandatory. Uncertain loads and partial publication can never enter this path.
    Receipt commit precedes terminal CAS so a lost response is safely observable.
    """
    row = loader.item(journal, identity)
    if row['phase'] == 'rejected':
        journal.verify_rejections()
        saved = journal.db.execute('SELECT receipt FROM toolchain_rejections WHERE import_id=?',
                                   (identity,)).fetchone()
        return {**json.loads(saved['receipt']), 'replayed_observation': True}
    row = loader.verify_binding(journal, identity, binding)
    if row['phase'] not in {'loaded', 'proof_pending'}:
        raise ToolchainPublicationError('only definitely completed unpublished loads may be rejected')
    if journal.db.execute("SELECT 1 FROM jobs WHERE state<>'cleaned' LIMIT 1").fetchone():
        raise ToolchainPublicationError('unfinished execution blocks rejection')
    for name, path in (('policy', policy_path), ('registry', registry_path)):
        if custody.checksum(read_configuration(path)[0]) != binding[name + '_sha256']:
            raise ToolchainPublicationError('rejection cannot conceal configuration drift')
    if loader.reconcile(journal, identity, binding).get('load_verified') is not True:
        raise ToolchainPublicationError('rejection requires positive load completion and image readback')
    retained = custody.verify_retained_custody(journal, identity)
    fingerprint = custody.checksum(custody.canonical(binding))
    if proof.schema(journal, create=False):
        for case in journal.db.execute('SELECT * FROM toolchain_proof_cases WHERE import_id=?', (identity,)):
            if case['binding_sha256'] != fingerprint:
                raise ToolchainPublicationError('rejection proof source binding differs')
            proof._verified(journal, case, fingerprint)
            job = journal.db.execute('SELECT * FROM jobs WHERE id=?', (case['job_id'],)).fetchone()
            if job is None or job['state'] != 'cleaned':
                raise ToolchainPublicationError('rejection needs identity-safe fixture cleanup')
            if job['container_id']:
                proof.native.require_native_cleanup(job['container_id'])
    receipt = {'version': 1, 'intent_id': identity, 'binding': binding,
               'custody_sha256': custody.checksum(custody.canonical(retained)),
               'load_sha256': custody.checksum(custody.canonical(
                   loader.completion_receipt(journal, identity) if hasattr(loader, 'completion_receipt') else
                   custody.document(loader.directory(journal, identity) / 'result.json'))),
               'proofs_sha256': journal.rejection_proofs(identity),
               'image_approved': False, 'configuration_published': False,
               'custody_retained': True, 'image_retained': True}
    body = custody.canonical(receipt).decode()
    if len(body.encode()) > 8192:
        raise ToolchainPublicationError('rejection receipt exceeds bound')
    with journal.db:
        journal.db.execute('CREATE TABLE IF NOT EXISTS toolchain_rejections('
                           'import_id TEXT PRIMARY KEY,receipt_sha256 TEXT NOT NULL,receipt TEXT NOT NULL)')
    rows = journal.db.execute('SELECT * FROM toolchain_rejections LIMIT 9').fetchall()
    known = {item['id'] for item in journal.toolchain_imports()}
    if (len(rows) > 8 or any(item['import_id'] not in known or not isinstance(item['receipt'], str)
                           or not 1 <= len(item['receipt'].encode()) <= 8192
                           or custody.checksum(item['receipt'].encode()) != item['receipt_sha256'] for item in rows)):
        raise ToolchainPublicationError('rejection inventory or fingerprint differs')
    saved = next((item for item in rows if item['import_id'] == identity), None)
    if saved is not None and saved['receipt'] != body:
        raise ToolchainPublicationError('retained rejection receipt changed; reconcile instead of overwriting')
    checksum = custody.checksum(body.encode())
    if saved is None:
        with journal.db:
            journal.db.execute('INSERT INTO toolchain_rejections VALUES(?,?,?)', (identity, checksum, body))
    journal.record_toolchain_import(identity, row['phase'], 'rejected', checksum)
    journal.verify_rejections()
    return {**receipt, 'replayed_observation': False}
