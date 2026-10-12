"""Fixed operator-only legacy Python restoration; never a model-selected tool.

One source/config-bound singleton reservation, one systemd dispatch and one
write-ahead worker claim. An uncertain daemon RPC is never retried automatically.
The existing approval is preserved: restoration grants no new image authority.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess  # nosec B404
import sys
import time

from app.coding import operator_cleanup_guard as cleanup, operator_image_restore as shared


IMAGE = 'sha256:6b1f85a08c199d29d5b6d71ab9c27bd5b3b393492e01216a15758ff69c4be8b8'
ARCHIVE_SHA = '36723f7fff07fb1712479bdbf5b09a31d67f38b0a45af4648e1c00b1e80d3778'
ARCHIVE_BYTES = 46237696
MAX_DOCUMENT = 8192
SQL = '''CREATE TABLE legacy_toolchain_restorations(
    image_id TEXT PRIMARY KEY, plan_sha256 TEXT NOT NULL, plan TEXT NOT NULL,
    dispatch TEXT, claim TEXT, result TEXT, reconciliation TEXT)'''


class LegacyRestoreError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def saved(journal, isolation):
    if not journal.db.execute("SELECT 1 FROM sqlite_master WHERE name='legacy_toolchain_restorations'").fetchone():
        return None
    isolation.legacy_restoration_pending(journal.db)
    row = journal.db.execute('SELECT * FROM legacy_toolchain_restorations').fetchone()
    if row['image_id'] != IMAGE:
        raise LegacyRestoreError('legacy recovery target differs')
    return {key: json.loads(row[key]) if row[key] is not None else None
            for key in ('plan', 'dispatch', 'claim', 'result', 'reconciliation')}


def binding(gateway, expected, *, require_lifetime=True):
    if (not isinstance(expected, dict) or set(expected) != {'bundle_sha256', 'policy_sha256',
                                                          'registry_sha256', 'controller_sha256'}
            or any(not isinstance(v, str) or not re.fullmatch('[a-f0-9]{64}', v) for v in expected.values())
            or not callable(getattr(gateway['isolation'], 'legacy_restoration_pending', None))):
        raise LegacyRestoreError('legacy recovery requires exact authority and an installed host barrier')
    gateway['target_identity']()
    if gateway['snapshot']() != expected['bundle_sha256']:
        raise LegacyRestoreError('legacy recovery source changed')
    policy, ph, rh = gateway['configuration']()
    if (ph != expected['policy_sha256'] or rh != expected['registry_sha256']
            or policy.get('images', {}).get('python_compile') != IMAGE
            or require_lifetime and int(time.time()) + 300 >= cleanup.STOP_UNIX):
        raise LegacyRestoreError('legacy recovery approval or lifetime changed')
    return policy


def registry_record(gateway):
    document, _ = gateway['publication'].read_configuration(gateway['isolation'].TOOLCHAIN_REGISTRY)
    gateway['isolation'].validate_toolchain_registry(document)
    record = document['images'].get(IMAGE)
    if (not isinstance(record, dict) or set(record) != {'archive_path', 'archive_sha256',
                                                       'archive_bytes', 'repository_digest'}
            or record['archive_sha256'] != ARCHIVE_SHA or record['archive_bytes'] != ARCHIVE_BYTES
            or not isinstance(record['archive_path'], str)
            or Path(record['archive_path']).name != IMAGE.removeprefix('sha256:') + '.tar'):
        raise LegacyRestoreError('legacy approved archive differs')
    return record


@contextmanager
def archive_stream(gateway, record, expected_inode=None):
    """Hash and load the same protected open inode; no path reopening race."""
    path = Path(record['archive_path'])
    if not path.is_absolute():
        raise LegacyRestoreError('legacy archive path is not absolute')
    gateway['custody'].protected(path, private=True)
    before = path.lstat()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        current = os.fstat(stream.fileno())
        fields = ('st_dev', 'st_ino', 'st_mode', 'st_uid', 'st_nlink', 'st_size', 'st_mtime_ns')
        inode = {name: getattr(current, name) for name in fields}
        if (inode != {name: getattr(before, name) for name in fields}
                or not stat.S_ISREG(current.st_mode) or current.st_nlink != 1
                or current.st_mode & 0o077 or current.st_uid not in {0, os.geteuid()}
                or current.st_size != ARCHIVE_BYTES or expected_inode is not None and inode != expected_inode):
            raise LegacyRestoreError('legacy archive inode changed')
        checksum = hashlib.sha256()
        size = 0
        while body := stream.read(1024 * 1024):
            size += len(body)
            if size > ARCHIVE_BYTES:
                raise LegacyRestoreError('legacy archive grew beyond bound')
            checksum.update(body)
        after = os.fstat(stream.fileno())
        if (size != ARCHIVE_BYTES or checksum.hexdigest() != ARCHIVE_SHA
                or inode != {name: getattr(after, name) for name in fields}):
            raise LegacyRestoreError('legacy archive digest or inode differs')
        stream.seek(0)
        yield stream, inode


def image(loader):
    template = ('{"Id":{{json .Id}},"Os":{{json .Os}},'
                '"Architecture":{{json .Architecture}}}')
    result = subprocess.run(  # nosec B603
        [loader.DOCKER, '--host=unix:///var/run/docker.sock', 'image', 'inspect', '--format', template, IMAGE],
        env=loader.CLEAN_ENV, capture_output=True, timeout=15, check=False)
    if result.returncode or not 1 <= len(result.stdout) <= 512:
        raise LegacyRestoreError('legacy exact image readback is unconfirmed')
    value = json.loads(result.stdout)
    if value != {'Id': IMAGE, 'Os': 'linux', 'Architecture': 'amd64'}:
        raise LegacyRestoreError('legacy image metadata differs')
    return value


def verify_plan(record, authority):
    plan = record['plan']
    if (not isinstance(plan, dict) or set(plan) != {'version', 'identity', 'unit', 'image_id',
            'authority', 'stop_unix', 'archive', 'archive_inode', 'image_absence'}
            or plan['image_id'] != IMAGE or plan['authority'] != authority
            or plan['stop_unix'] != cleanup.STOP_UNIX
            or plan['unit'] != shared.unit_name(plan['identity'])
            or plan['identity'] != digest({'version': 1, 'image_id': IMAGE, 'authority': authority})
            or plan['archive']['archive_sha256'] != ARCHIVE_SHA
            or plan['archive']['archive_bytes'] != ARCHIVE_BYTES
            or not isinstance(plan['archive_inode'], dict)
            or set(plan['archive_inode']) != {'st_dev', 'st_ino', 'st_mode', 'st_uid',
                                            'st_nlink', 'st_size', 'st_mtime_ns'}
            or any(type(v) is not int or v < 0 for v in plan['archive_inode'].values())
            or not isinstance(plan['image_absence'], dict)
            or plan['image_absence'].get('image_id') != IMAGE
            or plan['image_absence'].get('missing') is not True):
        raise LegacyRestoreError('legacy retained plan authority differs')
    return plan


def prepare(journal, gateway, authority):
    existing = saved(journal, gateway['isolation'])
    if existing is not None:
        verify_plan(existing, authority)
        return existing
    journal.require_available()
    identity = digest({'version': 1, 'image_id': IMAGE, 'authority': authority})
    loader = gateway['loader']
    missing = shared.absence(loader, IMAGE)
    if not missing['missing'] or shared.show(loader, identity)['LoadState'] != 'not-found':
        raise LegacyRestoreError('legacy restoration is not a fresh missing-image reservation')
    if cleanup.settings('read') != cleanup.AFTER:
        raise LegacyRestoreError('legacy cleanup guard differs')
    archive = registry_record(gateway)
    with archive_stream(gateway, archive) as (_, inode):
        plan = {'version': 1, 'identity': identity, 'unit': shared.unit_name(identity),
                'image_id': IMAGE, 'authority': authority, 'stop_unix': cleanup.STOP_UNIX,
                'archive': archive, 'archive_inode': inode, 'image_absence': missing}
    if len(canonical(plan)) > MAX_DOCUMENT:
        raise LegacyRestoreError('legacy plan exceeds bound')
    with journal.db:
        journal.db.execute(SQL)
        journal.db.execute('INSERT INTO legacy_toolchain_restorations VALUES(?,?,?,NULL,NULL,NULL,NULL)',
                           (IMAGE, digest(plan), canonical(plan).decode()))
    return saved(journal, gateway['isolation'])


def put(journal, field, value, prerequisite):
    statements = {
        ('dispatch', 'plan'): 'UPDATE legacy_toolchain_restorations SET dispatch=? '
                             'WHERE image_id=? AND dispatch IS NULL AND plan IS NOT NULL',
        ('claim', 'dispatch'): 'UPDATE legacy_toolchain_restorations SET claim=? '
                              'WHERE image_id=? AND claim IS NULL AND dispatch IS NOT NULL',
        ('result', 'claim'): 'UPDATE legacy_toolchain_restorations SET result=? '
                            'WHERE image_id=? AND result IS NULL AND claim IS NOT NULL',
        ('reconciliation', 'plan'): 'UPDATE legacy_toolchain_restorations SET reconciliation=? '
                                   'WHERE image_id=? AND reconciliation IS NULL AND plan IS NOT NULL',
    }
    if (field, prerequisite) not in statements:
        raise LegacyRestoreError('legacy receipt field is not approved')
    body = canonical(value)
    if len(body) > MAX_DOCUMENT:
        raise LegacyRestoreError('legacy receipt exceeds bound')
    with journal.db:
        count = journal.db.execute(statements[field, prerequisite], (body.decode(), IMAGE)).rowcount
        if count != 1:
            raise LegacyRestoreError('legacy receipt already consumed or owner differs')


def dispatch(journal, gateway, authority, source):
    record = prepare(journal, gateway, authority)
    plan = verify_plan(record, authority)
    loader, identity = gateway['loader'], plan['identity']
    if record['dispatch'] is not None or record['reconciliation'] is not None:
        return {'dispatch_repeated': False, 'dispatch_acknowledged': False, 'identity': identity}
    if hashlib.sha256(source.encode()).hexdigest() != authority['controller_sha256']:
        raise LegacyRestoreError('legacy worker source differs')
    if shared.show(loader, identity)['LoadState'] != 'not-found':
        raise LegacyRestoreError('legacy unit already exists without dispatch authority')
    allocation = cleanup.STOP_UNIX - int(time.time())
    if allocation <= 300:
        raise LegacyRestoreError('legacy dispatch lifetime exhausted')
    decision = {'version': 1, 'identity': identity, 'plan_sha256': digest(plan),
                'unit': plan['unit'], 'dispatched_at_unix': int(time.time())}
    put(journal, 'dispatch', decision, 'plan')
    argv = [loader.SYSTEMD_RUN, '--no-block', '--quiet', '--unit=' + plan['unit'],
            '--property=RemainAfterExit=yes', '--property=RuntimeMaxSec=300', '--property=TimeoutStopSec=10',
            '--property=Slice=gca-packaging.slice', '--property=MemoryMax=1G', '--property=MemorySwapMax=0',
            '--property=CPUQuota=100%', '--property=TasksMax=32', '--property=NoNewPrivileges=yes',
            '--property=CapabilityBoundingSet=', '--property=ProtectSystem=strict', '--property=ProtectHome=yes',
            '--property=PrivateTmp=yes', '--property=LimitCORE=0', '--property=RestrictAddressFamilies=AF_UNIX AF_INET',
            '--property=IPAddressDeny=any', '--property=IPAddressAllow=169.254.169.254/32',
            '--property=ReadWritePaths=' + str(journal.directory),
            '--property=StandardOutput=null', '--property=StandardError=null',
            '/usr/bin/env', '-i', 'PATH=/usr/bin:/bin', 'HOME=/nonexistent',
            '/usr/bin/python3', '-I', '-B', '-c', source, '--legacy-worker', digest(plan)]
    try:
        result = subprocess.run(argv, env=loader.CLEAN_ENV, stdout=subprocess.DEVNULL,  # nosec B603
                                stderr=subprocess.DEVNULL, timeout=10, check=False)
        acknowledged = result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        acknowledged = False
    return {'identity': identity, 'dispatch_repeated': False, 'dispatch_acknowledged': acknowledged}


def worker(journal, gateway, authority, token):
    record = saved(journal, gateway['isolation'])
    if record is None:
        raise LegacyRestoreError('legacy worker has no prepared reservation')
    plan = verify_plan(record, authority)
    loader, identity = gateway['loader'], plan['identity']
    if token != digest(plan) or record['dispatch'] is None or record['claim'] is not None:
        raise LegacyRestoreError('legacy worker authority already consumed or differs')
    unit, process = shared.show(loader, identity), loader.process_identity()
    if (unit['LoadState'] != 'loaded' or unit['ActiveState'] not in {'active', 'activating'}
            or unit['MainPID'] != str(process['pid']) or not unit['InvocationID']):
        raise LegacyRestoreError('legacy worker process identity differs')
    claim = {'version': 1, 'identity': identity, 'dispatch_sha256': digest(record['dispatch']),
             'invocation_id': unit['InvocationID'], 'process': process,
             'enforcement': shared.show(loader, identity, limits=True)}
    put(journal, 'claim', claim, 'dispatch')
    invoked, acknowledged, actual, failure = False, False, None, None
    stage = 'cleanup_guard'
    try:
        if cleanup.settings('read') != cleanup.AFTER:
            raise LegacyRestoreError('legacy cleanup guard changed')
        stage = 'image_absence'
        if not shared.absence(loader, IMAGE)['missing']:
            raise LegacyRestoreError('legacy image reappeared; no load permitted')
        stage = 'disk_headroom'
        space = os.statvfs(journal.directory)
        if space.f_bavail * space.f_frsize < max(512 * 1024**2, 3 * ARCHIVE_BYTES):
            raise LegacyRestoreError('legacy restoration disk headroom insufficient')
        stage = 'archive_custody'
        if registry_record(gateway) != plan['archive']:
            raise LegacyRestoreError('legacy archive registry changed')
        with archive_stream(gateway, plan['archive'], plan['archive_inode']) as (stream, _):
            stage = 'allocation'
            remaining = min(240, cleanup.STOP_UNIX - int(time.time()) - 20)
            if remaining < 1:
                raise LegacyRestoreError('legacy RPC lifetime exhausted')
            stage = 'load_rpc'
            invoked = True
            result = subprocess.run(  # nosec B603
                [loader.DOCKER, '--host=unix:///var/run/docker.sock', 'image', 'load',
                 '--input', '/proc/self/fd/' + str(stream.fileno())],
                pass_fds=(stream.fileno(),), env=loader.CLEAN_ENV, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=remaining, check=False)
            if result.returncode:
                raise LegacyRestoreError('legacy daemon did not acknowledge normal completion')
            acknowledged = True
        stage = 'image_readback'
        actual = image(loader)
    except (OSError, ValueError, subprocess.SubprocessError):
        failure = stage
    result = {'version': 1, 'identity': identity, 'claim_sha256': digest(claim),
              'docker_load_invoked': invoked, 'daemon_completion_acknowledged': acknowledged,
              'image': actual, 'failure_stage': failure}
    put(journal, 'result', result, 'claim')
    return result


def observe(journal, gateway, authority, *, reconcile=False, cancel=False):
    record = saved(journal, gateway['isolation'])
    if record is None:
        return {'prepared': False, 'docker_load_invoked': False}
    plan = verify_plan(record, authority)
    unit = shared.show(gateway['loader'], plan['identity'])
    if reconcile or cancel:
        if record['reconciliation'] is None:
            if cancel:
                if (any(record[key] is not None for key in ('dispatch', 'claim', 'result'))
                        or unit['LoadState'] != 'not-found' or unit['MainPID'] != '0'):
                    raise LegacyRestoreError('dispatched legacy recovery cannot be cancelled')
            else:
                result, claim = record['result'], record['claim']
                if (result is None or claim is None or result['failure_stage'] is not None
                        or result['daemon_completion_acknowledged'] is not True
                        or result['docker_load_invoked'] is not True
                        or result['image'] != image(gateway['loader'])
                        or unit['LoadState'] != 'loaded' or unit['MainPID'] != '0'
                        or unit['Result'] != 'success' or unit['ExecMainStatus'] != '0'
                        or unit['InvocationID'] != claim['invocation_id']):
                    raise LegacyRestoreError('legacy completion is not independently verified')
            terminal = {'version': 1, 'identity': plan['identity'], 'plan_sha256': digest(plan),
                        **{key + '_sha256': digest(record[key]) if record[key] else None
                           for key in ('dispatch', 'claim', 'result')},
                        'phase': 'cancelled' if cancel else 'completed', 'unit': unit}
            put(journal, 'reconciliation', terminal, 'plan')
            record = saved(journal, gateway['isolation'])
        if record['reconciliation']['phase'] != ('cancelled' if cancel else 'completed'):
            raise LegacyRestoreError('legacy reconciliation outcome differs')
    return {'prepared': True, 'identity': plan['identity'], 'plan_sha256': digest(plan),
            'dispatch_present': record['dispatch'] is not None, 'claim_present': record['claim'] is not None,
            'result_present': record['result'] is not None, 'unit': unit,
            'result_sha256': digest(record['result']) if record['result'] else None,
            'failure_stage': record['result']['failure_stage'] if record['result'] else None,
            'phase': record['reconciliation']['phase'] if record['reconciliation'] else 'reserved',
            'image_approval_changed': False, 'allocation_changed': False}


def main(gateway):
    if sys.platform != 'linux' or os.geteuid() != 0:
        raise LegacyRestoreError('legacy restoration requires fixed Linux operator root')
    args = Path('/proc/self/cmdline').read_bytes().split(b'\0')
    source = args[args.index(b'-c') + 1].decode()
    controller_hash = hashlib.sha256(source.encode()).hexdigest()
    is_worker = len(sys.argv) == 3 and sys.argv[1] == '--legacy-worker'
    if is_worker:
        # The reservation contains authority; source/hash are rechecked below.
        policy, _, _ = gateway['configuration']()
        operation, token, request = 'worker', sys.argv[2], None
    else:
        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise LegacyRestoreError('duplicate legacy request field')
                value[key] = item
            return value
        body = sys.stdin.buffer.read(MAX_DOCUMENT + 1)
        request = json.loads(body, object_pairs_hook=unique)
        if (len(body) > MAX_DOCUMENT or not isinstance(request, dict)
                or set(request) != {'version', 'operation', 'authority'}
                or type(request['version']) is not int or request['version'] != 1
                or request['operation'] not in {'prepare', 'dispatch', 'observe', 'reconcile', 'cancel'}):
            raise LegacyRestoreError('legacy request envelope differs')
        operation, token = request['operation'], None
        policy = binding(gateway, request['authority'], require_lifetime=operation in {'prepare', 'dispatch'})
    with shared.owned_journal(gateway['isolation'], Path(policy['state_directory']),
                              wait_seconds=20 if is_worker else 0) as journal:
        if is_worker:
            record = saved(journal, gateway['isolation'])
            if record is None:
                raise LegacyRestoreError('legacy worker has no prior reservation')
            authority = record['plan']['authority']
        else:
            authority = request['authority']
        if (not isinstance(authority, dict) or set(authority) != {'bundle_sha256', 'policy_sha256',
                                                               'registry_sha256', 'controller_sha256'}
                or any(not isinstance(v, str) or not re.fullmatch('[a-f0-9]{64}', v) for v in authority.values())
                or authority['controller_sha256'] != controller_hash):
            raise LegacyRestoreError('legacy controller source authority differs')
        if binding(gateway, authority, require_lifetime=operation in {'prepare', 'dispatch', 'worker'}) != policy:
            raise LegacyRestoreError('legacy configuration changed before ownership')
        if operation == 'prepare':
            record = prepare(journal, gateway, authority)
            result = {'prepared': True, 'identity': record['plan']['identity'],
                      'plan_sha256': digest(record['plan']), 'docker_load_invoked': False}
        elif operation == 'dispatch':
            result = dispatch(journal, gateway, authority, source)
        elif operation == 'worker':
            result = worker(journal, gateway, authority, token)
        else:
            result = observe(journal, gateway, authority, reconcile=operation == 'reconcile',
                             cancel=operation == 'cancel')
    print(json.dumps({'operation': operation, **result}, sort_keys=True))
