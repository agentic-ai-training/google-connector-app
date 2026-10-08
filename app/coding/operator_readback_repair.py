"""Operator-only c312172 optional-field repair; no source install or Docker load.

An exact reviewed verifier delta runs transiently over the protected installed
dependency closure. Original execution bindings remain immutable. A bounded audit
is committed before receipt/CAS changes. This is never a model-selectable tool.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import sys
import types


ROOT = Path('/usr/local/libexec/gca')
OLD_SNAPSHOT = '512d6bfc80bab82c64902b98a41e00c95fd3e203b0f1d2b2347617330c6700f4'
OLD_LOADER = 'a2e75912722edae60eb4ef28b4fe77951a683f1f513286420538cd4d2d95101f'
MAX_PLAN = 8192
SQL = '''CREATE TABLE IF NOT EXISTS toolchain_readback_repairs(
    import_id TEXT PRIMARY KEY,plan_sha256 TEXT NOT NULL,plan TEXT NOT NULL)'''
REPLACEMENTS = (('.Config.Entrypoint', '(index .Config "Entrypoint")'),
                ('.Config.Volumes', '(index .Config "Volumes")'))


class ReadbackRepairError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def digest(body):
    return hashlib.sha256(body).hexdigest()


def corrected_source(original):
    if not isinstance(original, bytes) or digest(original) != OLD_LOADER:
        raise ReadbackRepairError('repair accepts only the exact reviewed old verifier')
    value = original.decode()
    for before, after in REPLACEMENTS:
        if value.count(before) != 1:
            raise ReadbackRepairError('reviewed optional-field correction differs')
        value = value.replace(before, after)
    compile(value, 'reviewed_optional_field_verifier', 'exec')
    return value.encode()


def adapter(original, installed, *, validate_only=False):
    body = corrected_source(original)
    if validate_only:
        marker = b'    # Unit/process absence or a present image alone is NOT sufficient. Only a\n'
        if body.count(marker) != 1:
            raise ReadbackRepairError('protected receipt validator boundary differs')
        body = body.replace(marker, b"    return {'phase': row['phase'], 'unit': observed, "
                            b"'daemon_completion_acknowledged': True}\n" + marker)
    module = types.ModuleType('gca_reviewed_readback_adapter')
    exec(compile(body, 'reviewed_optional_field_verifier', 'exec'), module.__dict__)  # nosec B102
    if module.REVIEWED_ARCHIVES != installed.REVIEWED_ARCHIVES:
        raise ReadbackRepairError('adapter catalog differs from protected dependency')
    return module, digest(body)


def saved_plan(journal, identity):
    if not journal.db.execute("SELECT 1 FROM sqlite_master WHERE name='toolchain_readback_repairs'").fetchone():
        return None
    rows = journal.db.execute('SELECT * FROM toolchain_readback_repairs LIMIT 9').fetchall()
    known = {row['id'] for row in journal.toolchain_imports()}
    if len(rows) > 8:
        raise ReadbackRepairError('readback repair inventory exceeds bound')
    found = None
    for row in rows:
        if (row['import_id'] not in known or not isinstance(row['plan'], str)
                or not 1 <= len(row['plan'].encode()) <= MAX_PLAN
                or digest(row['plan'].encode()) != row['plan_sha256']):
            raise ReadbackRepairError('readback audit fingerprint differs')
        value = json.loads(row['plan'])
        if canonical(value).decode() != row['plan']:
            raise ReadbackRepairError('readback audit is not canonical')
        if row['import_id'] == identity:
            found = (value, row['plan_sha256'])
    return found


def repair(journal, identity, binding, original, installed, publisher, policy, registry, controller_hash,
           *, close=False):
    """Only the operator wrapper supplies protected dependencies and fixed paths."""
    row = installed.verify_binding(journal, identity, binding)
    if (row['intent']['version'] != 2 or row['phase'] not in {'load_uncertain', 'loaded', 'rejected'}
            or row['intent']['bundle_sha256'] != binding['bundle_sha256']
            or not isinstance(controller_hash, str) or not re.fullmatch(r'[a-f0-9]{64}', controller_hash)):
        raise ReadbackRepairError('exact completed-load repair authority required')
    if journal.db.execute("SELECT 1 FROM jobs WHERE state<>'cleaned' LIMIT 1").fetchone():
        raise ReadbackRepairError('unfinished job blocks readback repair')
    if journal.db.execute("SELECT 1 FROM sqlite_master WHERE name='toolchain_proof_cases'").fetchone():
        if journal.db.execute('SELECT 1 FROM toolchain_proof_cases WHERE import_id=? LIMIT 1', (identity,)).fetchone():
            raise ReadbackRepairError('fixture execution requires a separately reviewed source transition')
    corrected, verifier_hash = adapter(original, installed)
    validator, validator_hash = adapter(original, installed, validate_only=True)
    custody = installed.custody
    for name, path in (('policy', policy), ('registry', registry)):
        if digest(publisher.read_configuration(path)[0]) != binding[name + '_sha256']:
            raise ReadbackRepairError('repair cannot conceal configuration drift')
    directory = installed.directory(journal, identity)
    receipt = custody.document(directory / 'result.json')
    previous = saved_plan(journal, identity)
    if previous is None:
        if any(directory.glob('*.pending')):
            raise ReadbackRepairError('unsettled load control prevents fresh repair preparation')
        if (row['phase'] != 'load_uncertain' or receipt.get('daemon_completion_acknowledged') is not True
                or receipt.get('image_readback_verified') is not False or receipt.get('image') is not None
                or receipt.get('image_approved') is not False):
            raise ReadbackRepairError('positive original completion with failed readback required')
        # Existing reconciliation validates the whole original claim, receipt and
        # unit lineage without writing when this original projection cannot pass.
        observed = validator.reconcile(journal, identity, binding)
        if observed.get('daemon_completion_acknowledged') is not True:
            raise ReadbackRepairError('original failed-readback evidence differs')
        if observed['unit'].get('MainPID') != '0':
            raise ReadbackRepairError('live load worker cannot be repaired')
        actual = corrected.image(row['intent']['role'], row['intent']['image_id'])
        after = {**receipt, 'image_readback_verified': True, 'image': actual}
        plan = {'version': 1, 'intent_id': identity, 'binding': binding,
                'original_loader_sha256': digest(original), 'corrected_loader_sha256': verifier_hash,
                'receipt_validator_sha256': validator_hash,
                'controller_sha256': controller_hash,
                'dispatch_sha256': digest(canonical(custody.document(directory / 'dispatch.json'))),
                'claim_sha256': digest(canonical(custody.document(directory / 'claim.json'))),
                'custody_sha256': digest(canonical(custody.verify_retained_custody(journal, identity))),
                'before': receipt, 'after': after}
        body = canonical(plan)
        if len(body) > MAX_PLAN:
            raise ReadbackRepairError('readback audit exceeds bound')
        with journal.db:
            journal.db.execute(SQL)
            journal.db.execute('INSERT INTO toolchain_readback_repairs VALUES(?,?,?)',
                               (identity, digest(body), body.decode()))
        previous = (plan, digest(body))
    plan, fingerprint = previous
    if (not isinstance(plan, dict) or set(plan) != {'version', 'intent_id', 'binding', 'original_loader_sha256',
                                                  'corrected_loader_sha256', 'receipt_validator_sha256',
                                                  'controller_sha256', 'dispatch_sha256',
                                                  'claim_sha256', 'custody_sha256', 'before', 'after'}
            or type(plan['version']) is not int or plan['version'] != 1 or plan['intent_id'] != identity
            or plan['binding'] != binding or plan['original_loader_sha256'] != digest(original)
            or plan['corrected_loader_sha256'] != verifier_hash or plan['controller_sha256'] != controller_hash
            or plan['receipt_validator_sha256'] != validator_hash
            or receipt not in (plan['before'], plan['after'])
            or plan['after'] != {**plan['before'], 'image_readback_verified': True,
                                'image': corrected.image(row['intent']['role'], row['intent']['image_id'])}
            or plan['dispatch_sha256'] != digest(canonical(custody.document(directory / 'dispatch.json')))
            or plan['claim_sha256'] != digest(canonical(custody.document(directory / 'claim.json')))
            or plan['custody_sha256'] != digest(canonical(custody.verify_retained_custody(journal, identity)))):
        raise ReadbackRepairError('retained repair/source/load evidence changed')
    for pending in directory.glob('*.pending'):
        if pending.name != 'result.pending' or receipt != plan['before']:
            raise ReadbackRepairError('unsettled load control differs from retained repair')
        with custody.file_stream(pending) as stream:
            prefix = stream.read(MAX_PLAN + 1)
        if not canonical(plan['after']).startswith(prefix):
            raise ReadbackRepairError('partial receipt differs from retained repair')
    if row['phase'] == 'rejected':
        journal.verify_rejections()
        return {'intent_id': identity, 'phase': 'rejected', 'repair_plan_sha256': fingerprint,
                'image_approved': False, 'configuration_changed': False, 'docker_load_invoked': False}
    verified = corrected.reconcile(journal, identity, binding)
    if verified.get('load_verified') is not True or custody.document(directory / 'result.json') != plan['after']:
        raise ReadbackRepairError('exact completed load is not reconciled')
    if close:
        old = publisher.loader
        try:
            publisher.loader = corrected
            publisher.reject(journal, identity, binding, policy, registry)
        finally:
            publisher.loader = old
    return {'intent_id': identity, 'phase': installed.item(journal, identity)['phase'],
            'repair_plan_sha256': fingerprint, 'corrected_loader_sha256': verifier_hash,
            'image_approved': False, 'configuration_changed': False, 'docker_load_invoked': False}


def main():
    if sys.platform != 'linux' or os.geteuid() != 0:
        raise ReadbackRepairError('fixed Linux operator repair required')
    body = sys.stdin.buffer.read(MAX_PLAN + 1)
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ReadbackRepairError('duplicate repair request field')
            result[key] = value
        return result
    request = json.loads(body, object_pairs_hook=unique)
    fields = {'version', 'operation', 'intent_id', 'expected_snapshot_hash', 'controller_sha256'}
    if (len(body) > MAX_PLAN or not isinstance(request, dict) or set(request) != fields
            or type(request['version']) is not int or request['version'] != 1
            or request['operation'] not in {'repair_readback', 'reject_repaired'}
            or any(not isinstance(request[key], str) or not re.fullmatch(r'[a-f0-9]{64}', request[key])
                   for key in ('intent_id', 'expected_snapshot_hash', 'controller_sha256'))):
        raise ReadbackRepairError('bounded repair request differs')
    # The -c source is independently bound, not a client-provided passed flag.
    argv = Path('/proc/self/cmdline').read_bytes().split(b'\0')
    index = argv.index(b'-c')
    if digest(argv[index + 1]) != request['controller_sha256']:
        raise ReadbackRepairError('reviewed repair controller differs')
    gateway = runpy.run_path(str(ROOT / 'operator_toolchain_host.py'))
    if gateway['snapshot']() != request['expected_snapshot_hash'] or request['expected_snapshot_hash'] != OLD_SNAPSHOT:
        raise ReadbackRepairError('repair requires the exact installed c312172 closure')
    gateway['target_identity']()
    policy, policy_hash, registry_hash = gateway['configuration']()
    with gateway['isolation'].Journal(Path(policy['state_directory'])) as journal:
        if gateway['snapshot']() != OLD_SNAPSHOT:
            raise ReadbackRepairError('operator source changed before slot ownership')
        row = gateway['loader'].item(journal, request['intent_id'])
        if (row['intent']['host_instance_id'] != gateway['TARGET_INSTANCE']
                or row['intent']['stop_unix'] != gateway['STOP_UNIX']):
            raise ReadbackRepairError('repair cannot change the target or hosting deadline')
        binding = {'bundle_sha256': OLD_SNAPSHOT, 'policy_sha256': policy_hash, 'registry_sha256': registry_hash}
        source = gateway['custody'].file_stream(ROOT / 'toolchain_loader.py')
        with source as stream:
            original = stream.read(100001)
        result = repair(journal, request['intent_id'], binding, original, gateway['loader'],
                        gateway['publication'], gateway['POLICY'], gateway['REGISTRY'],
                        request['controller_sha256'], close=request['operation'] == 'reject_repaired')
    print(json.dumps({'operation': request['operation'], **result}, sort_keys=True))


if __name__ == '__main__':
    main()
