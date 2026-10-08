"""Audited restoration of a missing, definitely completed public toolchain image.

Not a retry of the old RPC. A distinct single-use retained unit and write-ahead
claim own the restoration. No source install, policy approval, cleanup or user code.
"""
import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path
import re
import subprocess  # nosec B404
import sys
import time

from app.coding import operator_readback_repair as repair, operator_cleanup_guard as cleanup


MAX_DOCUMENT = 8192
SQL = '''CREATE TABLE IF NOT EXISTS toolchain_restorations(
    import_id TEXT PRIMARY KEY, plan_sha256 TEXT NOT NULL, plan TEXT NOT NULL,
    claim TEXT, result TEXT)'''


class ImageRestoreError(ValueError):
    pass


@contextmanager
def owned_journal(isolation, directory, *, wait_seconds=0):
    """The worker may start before its dispatcher releases the SAME flock."""
    journal = isolation.Journal(directory)
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            journal.__enter__()
            break
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise ImageRestoreError('restoration host ownership wait expired') from None
            time.sleep(0.1)
    try:
        yield journal
    finally:
        journal.__exit__(None, None, None)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def unit_name(identity):
    if not isinstance(identity, str) or not re.fullmatch('[a-f0-9]{64}', identity):
        raise ImageRestoreError('restoration identity differs')
    return 'gca-toolchain-restore-' + identity + '.service'


def show(loader, identity, *, limits=False):
    fields = (*loader.UNIT_LIMITS, 'CPUQuotaPerSecUSec', 'Slice') if limits else loader.UNIT_FIELDS
    result = subprocess.run(  # nosec B603
        [loader.SYSTEMCTL, 'show', unit_name(identity), '--no-pager',
         *('--property=' + field for field in fields)],
        env=loader.CLEAN_ENV, capture_output=True, timeout=10, check=False)
    values = {}
    if len(result.stdout) > 4096:
        raise ImageRestoreError('restoration unit projection exceeds bound')
    for line in result.stdout.decode('ascii').splitlines():
        key, sep, value = line.partition('=')
        if not sep or key not in fields or key in values:
            raise ImageRestoreError('restoration unit projection differs')
        values[key] = value
    if set(values) != set(fields):
        raise ImageRestoreError('restoration unit evidence is incomplete')
    if limits:
        if (result.returncode or any(values[k] != v for k, v in loader.UNIT_LIMITS.items())
                or values['CPUQuotaPerSecUSec'] not in {'1s', '1000000'}
                or values['Slice'] != 'gca-packaging.slice'):
            raise ImageRestoreError('restoration unit enforcement differs')
    elif (values['LoadState'] not in {'loaded', 'not-found'}
          or result.returncode and values['LoadState'] != 'not-found'
          or not re.fullmatch('[0-9]{1,12}', values['MainPID'])
          or values['InvocationID'] and not re.fullmatch('[a-f0-9]{32}', values['InvocationID'])):
        raise ImageRestoreError('restoration unit identity differs')
    return values


def absence(loader, expected):
    """A daemon 'No such image' is absence; transport/template errors are unknown."""
    result = subprocess.run(  # nosec B603
        [loader.DOCKER, '--host=unix:///var/run/docker.sock', 'image', 'inspect',
         '--format', '{{json .Id}}', expected], env=loader.CLEAN_ENV,
        capture_output=True, timeout=15, check=False)
    if (result.returncode == 1 and not result.stdout.strip()
            and result.stderr.strip() == ('Error response from daemon: No such image: ' + expected).encode()):
        return {'image_id': expected, 'missing': True, 'observed_at_unix': int(time.time())}
    if not result.returncode and len(result.stdout) <= 256 and json.loads(result.stdout) == expected:
        return {'image_id': expected, 'missing': False, 'observed_at_unix': int(time.time())}
    raise ImageRestoreError('image absence is not independently confirmed')


def saved(journal, identity):
    if not journal.db.execute("SELECT 1 FROM sqlite_master WHERE name='toolchain_restorations'").fetchone():
        return None
    rows = journal.db.execute('SELECT * FROM toolchain_restorations LIMIT 9').fetchall()
    known = {row['id'] for row in journal.toolchain_imports()}
    if len(rows) > 8:
        raise ImageRestoreError('restoration inventory exceeds bound')
    found = None
    for row in rows:
        if row['import_id'] not in known:
            raise ImageRestoreError('unknown restoration import')
        for field in ('plan', 'claim', 'result'):
            value = row[field]
            if value is not None and (not isinstance(value, str) or not 1 <= len(value.encode()) <= MAX_DOCUMENT
                                      or canonical(json.loads(value)).decode() != value):
                raise ImageRestoreError('restoration evidence is not bounded canonical JSON')
        if row['plan'] is None or hashlib.sha256(row['plan'].encode()).hexdigest() != row['plan_sha256']:
            raise ImageRestoreError('restoration plan fingerprint differs')
        if row['result'] is not None and row['claim'] is None:
            raise ImageRestoreError('restoration result has no prior claim')
        if row['import_id'] == identity:
            found = {'plan': json.loads(row['plan']), 'plan_sha256': row['plan_sha256'],
                     'claim': json.loads(row['claim']) if row['claim'] is not None else None,
                     'result': json.loads(row['result']) if row['result'] is not None else None}
    return found


def original_evidence(journal, identity, binding, loader):
    row = loader.verify_binding(journal, identity, binding)
    if row['intent']['version'] != 2 or row['phase'] not in {'load_uncertain', 'loaded'}:
        raise ImageRestoreError('restoration requires a completed reserved source-bound import')
    if journal.db.execute("SELECT 1 FROM jobs WHERE state<>'cleaned' LIMIT 1").fetchone():
        raise ImageRestoreError('unfinished host job blocks restoration')
    if journal.db.execute("SELECT 1 FROM sqlite_master WHERE name='toolchain_proof_cases'").fetchone():
        if journal.db.execute('SELECT 1 FROM toolchain_proof_cases WHERE import_id=? LIMIT 1', (identity,)).fetchone():
            raise ImageRestoreError('fixture history requires separate restoration review')
    source = repair.installed_source(loader.custody)
    validator, _ = repair.adapter(source, loader, validate_only=True)
    observed = validator.reconcile(journal, identity, binding)
    if observed.get('daemon_completion_acknowledged') is not True or observed['unit'].get('MainPID') != '0':
        raise ImageRestoreError('original Docker RPC has no definite completed receipt')
    control = loader.directory(journal, identity)
    if any(control.glob('*.pending')):
        raise ImageRestoreError('unsettled original load control blocks restoration')
    receipt = loader.custody.document(control / 'result.json')
    old_receipt = receipt
    recorded_repair = repair.saved_plan(journal, identity)
    if recorded_repair is not None:
        plan = recorded_repair[0]
        if receipt != plan['after']:
            raise ImageRestoreError('original load repair evidence differs')
        old_receipt = plan['before']
    return row, {'binding': binding, 'original_loader_sha256': repair.OLD_LOADER,
                 'image_id': row['intent']['image_id'],
                 'original_load_sha256': digest(old_receipt),
                 'original_claim_sha256': digest(loader.custody.document(control / 'claim.json')),
                 'original_dispatch_sha256': digest(loader.custody.document(control / 'dispatch.json')),
                 'custody_sha256': digest(loader.custody.verify_retained_custody(journal, identity))}


def verify_plan(record, identity, evidence, controller_hash):
    plan = record['plan']
    fields = {'version', 'intent_id', 'unit', 'controller_sha256', 'stop_unix', 'image_absence', *evidence}
    if (not isinstance(plan, dict) or set(plan) != fields or type(plan['version']) is not int
            or plan['version'] != 1 or plan['intent_id'] != identity or plan['unit'] != unit_name(identity)
            or plan['controller_sha256'] != controller_hash or plan['stop_unix'] != cleanup.STOP_UNIX
            or any(plan[k] != value for k, value in evidence.items())
            or not isinstance(plan['image_absence'], dict)
            or set(plan['image_absence']) != {'image_id', 'missing', 'observed_at_unix'}
            or plan['image_absence']['missing'] is not True
            or plan['image_absence']['image_id'] != evidence['image_id']
            or type(plan['image_absence']['observed_at_unix']) is not int
            or not 0 < plan['image_absence']['observed_at_unix'] < plan['stop_unix']):
        raise ImageRestoreError('retained restoration authority differs')


def prepare(journal, identity, binding, loader, controller_hash):
    row, evidence = original_evidence(journal, identity, binding, loader)
    previous = saved(journal, identity)
    if previous is not None:
        verify_plan(previous, identity, evidence, controller_hash)
        return previous
    if int(time.time()) + 300 >= row['intent']['stop_unix']:
        raise ImageRestoreError('insufficient fixed hosting lifetime for restoration')
    missing = absence(loader, row['intent']['image_id'])
    if not missing['missing']:
        raise ImageRestoreError('present image does not authorize another load')
    if cleanup.settings('read') != cleanup.AFTER:
        raise ImageRestoreError('cleanup retention guard is not verified')
    if show(loader, identity)['LoadState'] != 'not-found':
        raise ImageRestoreError('unknown prior restoration unit requires review')
    plan = {'version': 1, 'intent_id': identity, 'unit': unit_name(identity),
            'controller_sha256': controller_hash, 'stop_unix': row['intent']['stop_unix'],
            'image_absence': missing, **evidence}
    body = canonical(plan)
    if len(body) > MAX_DOCUMENT:
        raise ImageRestoreError('restoration plan exceeds bound')
    with journal.db:
        journal.db.execute(SQL)
        journal.db.execute('INSERT INTO toolchain_restorations VALUES(?,?,?,NULL,NULL)',
                           (identity, hashlib.sha256(body).hexdigest(), body.decode()))
    return saved(journal, identity)


def dispatch(journal, identity, binding, loader, controller_hash, source):
    record = prepare(journal, identity, binding, loader, controller_hash)
    if hashlib.sha256(source.encode()).hexdigest() != controller_hash:
        raise ImageRestoreError('restoration worker source differs')
    unit = show(loader, identity)
    if record['claim'] is not None or unit['LoadState'] != 'not-found':
        return {'dispatch_repeated': False, 'dispatch_acknowledged': False, 'unit': unit}
    if int(time.time()) + 300 >= record['plan']['stop_unix']:
        raise ImageRestoreError('restoration allocation expired before dispatch')
    argv = [loader.SYSTEMD_RUN, '--no-block', '--quiet', '--unit=' + unit_name(identity),
            '--property=RemainAfterExit=yes', '--property=RuntimeMaxSec=300', '--property=TimeoutStopSec=10',
            '--property=Slice=gca-packaging.slice', '--property=MemoryMax=1G', '--property=MemorySwapMax=0',
            '--property=CPUQuota=100%', '--property=TasksMax=32', '--property=NoNewPrivileges=yes',
            '--property=CapabilityBoundingSet=', '--property=ProtectSystem=strict', '--property=ProtectHome=yes',
            '--property=PrivateTmp=yes', '--property=LimitCORE=0', '--property=RestrictAddressFamilies=AF_UNIX AF_INET',
            '--property=IPAddressDeny=any', '--property=IPAddressAllow=169.254.169.254/32',
            '--property=ReadWritePaths=' + str(journal.directory),
            '--property=StandardOutput=null', '--property=StandardError=null',
            '/usr/bin/env', '-i', 'PATH=/usr/bin:/bin', 'HOME=/nonexistent',
            '/usr/bin/python3', '-I', '-B', '-c', source, '--restore-worker', identity, record['plan_sha256']]
    try:
        result = subprocess.run(argv, env=loader.CLEAN_ENV, stdout=subprocess.DEVNULL,  # nosec B603
                                stderr=subprocess.DEVNULL, timeout=10, check=False)
        acknowledged = result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        acknowledged = False
    return {'dispatch_repeated': False, 'dispatch_acknowledged': acknowledged, 'unit_name': unit_name(identity)}


def worker(journal, identity, token, binding, loader, controller_hash):
    record = saved(journal, identity)
    row, evidence = original_evidence(journal, identity, binding, loader)
    if record is None or record['plan_sha256'] != token:
        raise ImageRestoreError('restoration worker has no exact prior intent')
    verify_plan(record, identity, evidence, controller_hash)
    if record['claim'] is not None:
        raise ImageRestoreError('restoration already claimed; observe without replay')
    observed = show(loader, identity)
    process = loader.process_identity()
    if (observed['LoadState'] != 'loaded' or observed['ActiveState'] not in {'active', 'activating'}
            or observed['MainPID'] != str(process['pid']) or not observed['InvocationID']):
        raise ImageRestoreError('restoration worker unit identity differs')
    limits = show(loader, identity, limits=True)
    claim = {'version': 1, 'plan_sha256': token, 'unit': unit_name(identity), 'process': process,
             'invocation_id': observed['InvocationID'], 'enforcement': limits}
    with journal.db:
        changed = journal.db.execute('UPDATE toolchain_restorations SET claim=? WHERE import_id=? AND claim IS NULL',
                                     (canonical(claim).decode(), identity)).rowcount
        if changed != 1:
            raise ImageRestoreError('restoration claim owner differs')
    ack, invoked, actual, failure = False, False, None, None
    stage = 'cleanup_guard'
    try:
        if cleanup.settings('read') != cleanup.AFTER:
            raise ImageRestoreError('cleanup retention guard changed before restoration')
        stage = 'image_absence'
        if not absence(loader, row['intent']['image_id'])['missing']:
            raise ImageRestoreError('image reappeared before restoration; no load attempted')
        stage = 'allocation'
        remaining = min(240, row['intent']['stop_unix'] - int(time.time()) - 20)
        if remaining < 1:
            raise ImageRestoreError('restoration allocation expired before RPC')
        stage = 'disk_headroom'
        space = os.statvfs(journal.directory)
        if space.f_bavail * space.f_frsize < max(512 * 1024 * 1024, 3 * row['intent']['archive_bytes']):
            raise ImageRestoreError('restoration free disk headroom is insufficient')
        archive = journal.directory / 'toolchain-imports' / identity / 'archive.tar'
        invoked = True
        stage = 'load_rpc'
        result = subprocess.run(  # nosec B603
            [loader.DOCKER, '--host=unix:///var/run/docker.sock', 'image', 'load', '--input', str(archive)],
            env=loader.CLEAN_ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=remaining, check=False)
        if result.returncode:
            raise ImageRestoreError('restoration lacks normal daemon completion')
        ack = True
        stage = 'image_readback'
        observer, _ = repair.adapter(repair.installed_source(loader.custody), loader)
        actual = observer.image(row['intent']['role'], row['intent']['image_id'])
    except (OSError, ValueError, subprocess.SubprocessError):
        failure = stage
    receipt = {'version': 1, 'intent_id': identity, 'plan_sha256': token, 'claim_sha256': digest(claim),
               'docker_load_invoked': invoked, 'daemon_completion_acknowledged': ack,
               'image_readback_verified': actual is not None, 'image': actual,
               'failure_stage': failure, 'image_approved': False}
    record_result(journal, identity, receipt)
    return receipt


def record_result(journal, identity, receipt):
    with journal.db:
        changed = journal.db.execute('UPDATE toolchain_restorations SET result=? '
                                     'WHERE import_id=? AND result IS NULL AND claim IS NOT NULL',
                                     (canonical(receipt).decode(), identity)).rowcount
        if changed != 1:
            raise ImageRestoreError('restoration completion owner differs')


def observe(journal, identity, binding, loader, controller_hash):
    record = saved(journal, identity)
    if record is None:
        return {'restoration_prepared': False, 'image_approved': False}
    row, evidence = original_evidence(journal, identity, binding, loader)
    verify_plan(record, identity, evidence, controller_hash)
    observed = show(loader, identity)
    claim, result = record['claim'], record['result']
    if claim is not None:
        if (not isinstance(claim, dict) or set(claim) != {'version', 'plan_sha256', 'unit', 'process',
                                                        'invocation_id', 'enforcement'}
                or type(claim['version']) is not int or claim['version'] != 1
                or claim['plan_sha256'] != record['plan_sha256'] or claim['unit'] != unit_name(identity)
                or not isinstance(claim['process'], dict) or set(claim['process']) != {'boot_id', 'pid', 'start_ticks'}
                or not isinstance(claim['process']['boot_id'], str)
                or not re.fullmatch(loader.BOOT_ID, claim['process']['boot_id'])
                or type(claim['process']['pid']) is not int or claim['process']['pid'] <= 1
                or claim['process']['pid'] >= 2**31
                or type(claim['process']['start_ticks']) is not int or claim['process']['start_ticks'] <= 0
                or claim['process']['start_ticks'] >= 2**64
                or not isinstance(claim['invocation_id'], str)
                or not re.fullmatch('[a-f0-9]{32}', claim['invocation_id'])
                or claim['enforcement'] not in ({**loader.UNIT_LIMITS, 'CPUQuotaPerSecUSec': quota,
                                               'Slice': 'gca-packaging.slice'} for quota in ('1s', '1000000'))
                or observed['LoadState'] == 'loaded' and observed['InvocationID'] != claim['invocation_id']):
            raise ImageRestoreError('retained restoration claim differs')
    verified = False
    if result is not None:
        fields = {'version', 'intent_id', 'plan_sha256', 'claim_sha256', 'docker_load_invoked',
                  'daemon_completion_acknowledged', 'image_readback_verified', 'image', 'failure_stage', 'image_approved'}
        if (not isinstance(result, dict) or set(result) != fields or result['version'] != 1
                or type(result['version']) is not int or result['intent_id'] != identity
                or result['plan_sha256'] != record['plan_sha256'] or result['claim_sha256'] != digest(claim)
                or any(type(result[k]) is not bool for k in ('docker_load_invoked', 'daemon_completion_acknowledged',
                                                           'image_readback_verified', 'image_approved'))
                or result['image_approved'] is not False
                or result['failure_stage'] not in {None, 'cleanup_guard', 'image_absence', 'allocation',
                                                   'disk_headroom', 'load_rpc', 'image_readback'}
                or not result['image_readback_verified'] and result['image'] is not None
                or result['image_readback_verified'] and not result['daemon_completion_acknowledged']
                or result['daemon_completion_acknowledged'] and not result['docker_load_invoked']
                or result['failure_stage'] is None and not result['image_readback_verified']
                or result['failure_stage'] is not None and result['image_readback_verified']
                or result['failure_stage'] == 'image_readback' and not result['daemon_completion_acknowledged']
                or result['failure_stage'] == 'load_rpc' and result['daemon_completion_acknowledged']
                or result['failure_stage'] in {'cleanup_guard', 'image_absence', 'allocation', 'disk_headroom'}
                and result['docker_load_invoked']):
            raise ImageRestoreError('restoration completion receipt differs')
        if result['daemon_completion_acknowledged'] and result['image_readback_verified']:
            observer, _ = repair.adapter(repair.installed_source(loader.custody), loader)
            if (not result['docker_load_invoked'] or result['failure_stage'] is not None
                    or result['image'] != observer.image(row['intent']['role'], row['intent']['image_id'])):
                raise ImageRestoreError('restoration current image evidence differs')
            verified = observed['MainPID'] == '0'
    return {'intent_id': identity, 'plan_sha256': record['plan_sha256'], 'restoration_prepared': True,
            'claim_present': claim is not None, 'result_present': result is not None,
            'restoration_verified': verified, 'unit': observed,
            'daemon_completion_acknowledged': result['daemon_completion_acknowledged'] if result else False,
            'image_approved': False, 'original_import_phase': row['phase'],
            'result_sha256': digest(result) if result is not None else None}


def self_source():
    argv = Path('/proc/self/cmdline').read_bytes().split(b'\0')
    return argv[argv.index(b'-c') + 1].decode()


def main(gateway):
    if sys.platform != 'linux' or os.geteuid() != 0:
        raise ImageRestoreError('fixed Linux operator restoration required')
    source = self_source()
    controller_hash = hashlib.sha256(source.encode()).hexdigest()
    worker_args = len(sys.argv) == 4 and sys.argv[1] == '--restore-worker'
    if worker_args:
        identity, token = sys.argv[2:]
        operation = 'worker'
    else:
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ImageRestoreError('duplicate restoration request field')
                result[key] = value
            return result
        body = sys.stdin.buffer.read(MAX_DOCUMENT + 1)
        request = json.loads(body, object_pairs_hook=unique)
        if (len(body) > MAX_DOCUMENT or not isinstance(request, dict)
                or set(request) != {'version', 'operation', 'intent_id', 'expected_snapshot_hash', 'controller_sha256'}
                or type(request['version']) is not int or request['version'] != 1
                or request['operation'] not in {'prepare', 'dispatch', 'observe'}
                or request['controller_sha256'] != controller_hash
                or request['expected_snapshot_hash'] != repair.OLD_SNAPSHOT):
            raise ImageRestoreError('restoration request/source authority differs')
        identity, operation, token = request['intent_id'], request['operation'], None
    unit_name(identity)
    if worker_args and not re.fullmatch('[a-f0-9]{64}', token):
        raise ImageRestoreError('restoration worker token differs')
    gateway['target_identity']()
    if gateway['snapshot']() != repair.OLD_SNAPSHOT:
        raise ImageRestoreError('restoration requires the exact retained source closure')
    policy, ph, rh = gateway['configuration']()
    with owned_journal(gateway['isolation'], Path(policy['state_directory']),
                       wait_seconds=20 if worker_args else 0) as journal:
        if gateway['snapshot']() != repair.OLD_SNAPSHOT or gateway['configuration']() != (policy, ph, rh):
            raise ImageRestoreError('restoration configuration changed before ownership')
        row = gateway['loader'].item(journal, identity)
        if (row['intent']['host_instance_id'] != gateway['TARGET_INSTANCE']
                or row['intent']['stop_unix'] != cleanup.STOP_UNIX):
            raise ImageRestoreError('restoration cannot change host or allocation')
        binding = {'bundle_sha256': repair.OLD_SNAPSHOT, 'policy_sha256': ph, 'registry_sha256': rh}
        if operation == 'prepare':
            record = prepare(journal, identity, binding, gateway['loader'], controller_hash)
            result = {'plan_sha256': record['plan_sha256'], 'restoration_prepared': True,
                      'image_approved': False, 'docker_load_invoked': False}
        elif operation == 'dispatch':
            result = dispatch(journal, identity, binding, gateway['loader'], controller_hash, source)
        elif operation == 'worker':
            result = worker(journal, identity, token, binding, gateway['loader'], controller_hash)
        else:
            result = observe(journal, identity, binding, gateway['loader'], controller_hash)
    print(json.dumps({'operation': operation, 'intent_id': identity, **result}, sort_keys=True))
