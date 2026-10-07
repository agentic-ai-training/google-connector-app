"""Trusted fixed-image load lifecycle; no generated code, approval or publication.

The unit owns execution independently of an SSH client. A durable claim precedes
the Docker RPC. Timeout, nonzero exit, killed process or image presence alone is
never evidence that a daemon operation ended. Such cases keep the host reserved.
"""
import json
import os
from pathlib import Path
import re
import subprocess  # nosec B404
import time

from app.coding import toolchain_staging as custody
from app.coding.toolchain_spec import REVIEWED_ARCHIVES, BASES, ROLES


DOCKER = '/usr/bin/docker'
SYSTEMD_RUN = '/usr/bin/systemd-run'
SYSTEMCTL = '/usr/bin/systemctl'
GATEWAY = '/usr/local/libexec/gca/operator_toolchain_host.py'
CLEAN_ENV = {'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent'}
UNIT_FIELDS = ('LoadState', 'ActiveState', 'SubState', 'Result', 'MainPID',
               'InvocationID', 'ExecMainStatus')
BOOT_ID = r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}'
UNIT_LIMITS = {'MemoryMax': '1073741824', 'MemorySwapMax': '0', 'TasksMax': '32',
               'NoNewPrivileges': 'yes', 'CapabilityBoundingSet': '', 'ProtectSystem': 'strict',
               'ProtectHome': 'yes', 'PrivateTmp': 'yes'}


class ToolchainLoadError(ValueError):
    pass


def item(journal, identity):
    if not isinstance(identity, str) or not re.fullmatch(r'[a-f0-9]{64}', identity):
        raise ToolchainLoadError('load identity is invalid')
    row = next((row for row in journal.toolchain_imports() if row['id'] == identity), None)
    if row is None:
        raise ToolchainLoadError('load intent is unknown')
    return row


def directory(journal, identity, *, create=False):
    item(journal, identity)
    root = journal.directory / 'toolchain-loads'
    if create:
        root.mkdir(mode=0o700, exist_ok=True)
        custody.sync(root.parent)
    custody.protected(root, private=True)
    known = {row['id'] for row in journal.toolchain_imports()}
    entries = list(root.iterdir())
    if len(entries) > 8 or any(path.name not in known or not path.is_dir() for path in entries):
        raise ToolchainLoadError('load directory inventory differs')
    result = root / identity
    if create:
        result.mkdir(mode=0o700, exist_ok=True)
        custody.sync(root)
    custody.protected(result, private=True)
    allowed = {name + suffix for name in ('dispatch', 'claim', 'result') for suffix in ('.json', '.pending')}
    paths = list(result.iterdir())
    if len(paths) > 6 or any(path.name not in allowed for path in paths):
        raise ToolchainLoadError('load control inventory differs')
    for path in paths:
        custody.protected(path, private=True)
        if not path.is_file() or path.stat().st_size > custody.MAX_CONTROL_BYTES:
            raise ToolchainLoadError('load control inode or bound differs')
    return result


def unit_name(identity):
    if not isinstance(identity, str) or not re.fullmatch(r'[a-f0-9]{64}', identity):
        raise ToolchainLoadError('unit identity is invalid')
    return 'gca-toolchain-' + identity + '.service'


def unit(identity):
    result = subprocess.run(  # nosec B603
        [SYSTEMCTL, 'show', unit_name(identity), '--no-pager',
         *('--property=' + name for name in UNIT_FIELDS)], env=CLEAN_ENV,
        capture_output=True, timeout=10, check=False)
    if len(result.stdout) > 4096:
        raise ToolchainLoadError('unit observation exceeds bound')
    values = {}
    for line in result.stdout.decode('ascii').splitlines():
        key, separator, value = line.partition('=')
        if not separator or key not in UNIT_FIELDS or key in values:
            raise ToolchainLoadError('unit observation is invalid')
        values[key] = value
    if set(values) != set(UNIT_FIELDS) or (result.returncode and values['LoadState'] != 'not-found'):
        raise ToolchainLoadError('unit observation is unconfirmed')
    if (values['LoadState'] not in {'loaded', 'not-found'}
            or not re.fullmatch(r'[0-9]{1,12}', values['MainPID'])
            or values['InvocationID'] and not re.fullmatch(r'[a-f0-9]{32}', values['InvocationID'])):
        raise ToolchainLoadError('unit identity differs')
    return values


def verify_binding(journal, identity, binding):
    row = item(journal, identity)
    if (not isinstance(binding, dict) or set(binding) != {'bundle_sha256', 'policy_sha256', 'registry_sha256'}
            or any(not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value)
                   for value in binding.values())
            or any(binding[key] != row['intent'][key] for key in ('policy_sha256', 'registry_sha256'))
            or row['intent']['version'] == 2 and binding['bundle_sha256'] != row['intent']['bundle_sha256']
            or any(row['intent'].get(key) != value for key, value in REVIEWED_ARCHIVES[row['intent']['role']].items())):
        raise ToolchainLoadError('reserved load catalog or configuration differs')
    return row


def enforcement(identity):
    result = subprocess.run(  # nosec B603
        [SYSTEMCTL, 'show', unit_name(identity), '--no-pager',
         *('--property=' + name for name in (*UNIT_LIMITS, 'CPUQuotaPerSecUSec'))],
        env=CLEAN_ENV, capture_output=True, timeout=10, check=False)
    if result.returncode or len(result.stdout) > 4096:
        raise ToolchainLoadError('unit limits are unconfirmed')
    values = {}
    for line in result.stdout.decode('ascii').splitlines():
        key, separator, value = line.partition('=')
        if not separator or key in values:
            raise ToolchainLoadError('unit limit projection differs')
        values[key] = value
    if (set(values) != set(UNIT_LIMITS) | {'CPUQuotaPerSecUSec'}
            or any(values[name] != expected for name, expected in UNIT_LIMITS.items())
            or values['CPUQuotaPerSecUSec'] not in {'1s', '1000000'}):
        raise ToolchainLoadError('unit resource/security limits differ')
    return values


def prepare(journal, identity, binding):
    row = verify_binding(journal, identity, binding)
    if row['phase'] not in {'cancelled', 'archive_verified'}:
        raise ToolchainLoadError('load preparation requires never-started verified custody')
    if row['intent']['stop_unix'] <= int(time.time()):
        raise ToolchainLoadError('load allocation expired')
    receipt = custody.verify_retained_custody(journal, identity)
    custody_hash = custody.checksum(custody.canonical(receipt))
    path = directory(journal, identity, create=True) / 'dispatch.json'
    dispatch = {'version': 1, 'intent_id': identity, 'unit': unit_name(identity),
                'binding': binding, 'custody_sha256': custody_hash,
                'stop_unix': row['intent']['stop_unix']}
    if path.exists() and custody.document(path) != dispatch:
        raise ToolchainLoadError('retained load dispatch differs')
    custody.write_document(path, dispatch)
    if row['phase'] == 'cancelled':
        journal.record_toolchain_import(identity, 'cancelled', 'archive_verified', custody_hash)
    return dispatch


def dispatch(journal, identity, binding):
    """Prepare fixed outbox then request one retained unit; never wait on Docker."""
    row = verify_binding(journal, identity, binding)
    if row['phase'] in {'load_started', 'load_uncertain', 'loaded', 'proof_pending'}:
        return {'phase': row['phase'], 'dispatch_repeated': False, 'unit': unit(identity)}
    request = prepare(journal, identity, binding)
    control = directory(journal, identity)
    if any((control / name).exists() for name in ('claim.json', 'claim.pending')):
        return {'phase': row['phase'], 'dispatch_repeated': False,
                'reconciliation_required': True, 'image_approved': False}
    if int(time.time()) + 300 >= row['intent']['stop_unix']:
        raise ToolchainLoadError('insufficient fixed hosting lifetime for load unit')
    observed = unit(identity)
    if observed['LoadState'] != 'not-found':
        return {'phase': 'archive_verified', 'dispatch_repeated': False, 'unit': observed}
    token = custody.checksum(custody.canonical(request))
    # The retained unique unit name prevents concurrent replay; the worker's
    # durable claim independently prevents a second Docker RPC if a unit is lost.
    argv = [SYSTEMD_RUN, '--no-block', '--quiet', '--unit=' + request['unit'],
         '--property=RemainAfterExit=yes', '--property=RuntimeMaxSec=300',
         '--property=TimeoutStopSec=10', '--property=MemoryMax=1G',
         '--property=MemorySwapMax=0', '--property=CPUQuota=100%', '--property=TasksMax=32',
         '--property=NoNewPrivileges=yes', '--property=CapabilityBoundingSet=',
         '--property=ProtectSystem=strict', '--property=ProtectHome=yes',
         '--property=PrivateTmp=yes', '--property=LimitCORE=0',
         '--property=RestrictAddressFamilies=AF_UNIX AF_INET',
         '--property=IPAddressDeny=any', '--property=IPAddressAllow=169.254.169.254/32',
         '--property=ReadWritePaths=' + str(journal.directory),
         '--property=StandardOutput=null', '--property=StandardError=null',
         '/usr/bin/env', '-i', 'PATH=/usr/bin:/bin', 'HOME=/nonexistent',
         '/usr/bin/python3', '-I', '-B', GATEWAY, '--load-worker', identity, token]
    try:
        result = subprocess.run(  # nosec B603
            argv, env=CLEAN_ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=10, check=False)
        acknowledged = result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        acknowledged = False
    # Even a nonzero/timeout is ambiguous unit creation; inspect the same unit.
    return {'phase': 'archive_verified', 'dispatch_acknowledged': acknowledged,
            'dispatch_repeated': False, 'unit_name': request['unit'], 'image_approved': False}


def process_identity():
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    body = Path('/proc/self/stat').read_text()
    fields = body[body.rfind(')') + 2:].split()
    if (not re.fullmatch(BOOT_ID, boot) or len(fields) < 20
            or not fields[19].isdigit()):
        raise ToolchainLoadError('load worker process identity differs')
    return {'boot_id': boot, 'pid': os.getpid(), 'start_ticks': int(fields[19])}


def image(role, expected):
    # Only projected public configuration; never a full Docker inspect containing
    # arbitrary Env values. Immutable index/platform/config identities are distinct.
    projection = ('{"id":{{json .Id}},"os":{{json .Os}},"arch":{{json .Architecture}},'
                  '"user":{{json .Config.User}},"entrypoint":{{json .Config.Entrypoint}},'
                  '"volumes":{{json .Config.Volumes}},"healthcheck":{{json (index .Config "Healthcheck")}},'
                  '"role":{{json (index .Config.Labels "org.gca.toolchain.role")}},'
                  '"base":{{json (index .Config.Labels "org.gca.toolchain.base")}},'
                  '"operator":{{json (index .Config.Labels "org.gca.toolchain.operator-only")}}}')
    result = subprocess.run(  # nosec B603
        [DOCKER, '--host=unix:///var/run/docker.sock', 'image', 'inspect', '--format', projection, expected],
        capture_output=True, env=CLEAN_ENV, timeout=15, check=False)
    if result.returncode or len(result.stdout) > 8192:
        raise ToolchainLoadError('immutable image readback is unconfirmed')
    value = json.loads(result.stdout)
    if (not isinstance(value, dict) or set(value) != {'id', 'os', 'arch', 'user', 'entrypoint', 'volumes',
                                                    'healthcheck', 'role', 'base', 'operator'}
            or value['id'] != expected or value['os'] != 'linux' or value['arch'] != 'amd64'
            or value['user'] != '65532:65532' or value['entrypoint'] or value['volumes'] or value['healthcheck']
            or value['role'] != ROLES[role] or value['base'] != BASES[role].split('@')[1]
            or value['operator'] != 'true'):
        raise ToolchainLoadError('loaded image differs from reviewed public recipe')
    return value


def run_worker(journal, identity, token, binding):
    """Called by the fixed retained unit with the SAME host slot already owned."""
    row = verify_binding(journal, identity, binding)
    control = directory(journal, identity)
    request = custody.document(control / 'dispatch.json')
    expected = {'version': 1, 'intent_id': identity, 'unit': unit_name(identity), 'binding': binding,
                'custody_sha256': request.get('custody_sha256'), 'stop_unix': row['intent']['stop_unix']}
    if request != expected or token != custody.checksum(custody.canonical(request)):
        raise ToolchainLoadError('worker differs from durable dispatch')
    if row['phase'] != 'archive_verified' or any((control / name).exists() for name in ('claim.json', 'claim.pending')):
        raise ToolchainLoadError('worker already claimed; observe without replay')
    receipt = custody.verify_retained_custody(journal, identity)
    if custody.checksum(custody.canonical(receipt)) != request['custody_sha256']:
        raise ToolchainLoadError('worker custody changed since dispatch')
    observed = unit(identity)
    process = process_identity()
    if (observed['LoadState'] != 'loaded' or observed['ActiveState'] not in {'active', 'activating'}
            or observed['MainPID'] != str(process['pid']) or not observed['InvocationID']):
        raise ToolchainLoadError('worker is not the exact retained unit process')
    limits = enforcement(identity)
    claim = {'version': 1, 'dispatch_sha256': token, 'process': process,
             'invocation_id': observed['InvocationID'], 'enforcement': limits}
    custody.write_document(control / 'claim.json', claim)
    journal.record_toolchain_import(identity, 'archive_verified', 'load_started',
                                    custody.checksum(custody.canonical(claim)))
    archive = journal.directory / 'toolchain-imports' / identity / 'archive.tar'
    daemon_acknowledged = False
    try:
        # The RPC is admitted only after a durable claim. It never executes layer
        # contents, changes tags, pulls network images or runs generated source.
        remaining = min(240, row['intent']['stop_unix'] - int(time.time()) - 20)
        if remaining < 1:
            raise ToolchainLoadError('fixed hosting lifetime expired before RPC')
        result = subprocess.run(  # nosec B603
            [DOCKER, '--host=unix:///var/run/docker.sock', 'image', 'load', '--input', str(archive)],
            env=CLEAN_ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=remaining, check=False)
        if result.returncode:
            raise ToolchainLoadError('load lacks a normal daemon completion acknowledgement')
        daemon_acknowledged = True
        actual = image(row['intent']['role'], row['intent']['image_id'])
    except (OSError, ValueError, subprocess.SubprocessError):
        uncertain = {'version': 1, 'intent_id': identity, 'claim_sha256': custody.checksum(custody.canonical(claim)),
                     'daemon_completion_acknowledged': daemon_acknowledged,
                     'image_readback_verified': False, 'image': None, 'image_approved': False}
        custody.write_document(control / 'result.json', uncertain)
        journal.record_toolchain_import(identity, 'load_started', 'load_uncertain',
                                        custody.checksum(custody.canonical(uncertain)))
        return uncertain
    terminal = {'version': 1, 'intent_id': identity, 'claim_sha256': custody.checksum(custody.canonical(claim)),
                'daemon_completion_acknowledged': True, 'image_readback_verified': True,
                'image': actual, 'image_approved': False}
    custody.write_document(control / 'result.json', terminal)
    journal.record_toolchain_import(identity, 'load_started', 'loaded', custody.checksum(custody.canonical(terminal)))
    return terminal


def reconcile(journal, identity, binding):
    """No Docker load. A positive protected terminal receipt is required."""
    row = verify_binding(journal, identity, binding)
    observed = unit(identity)
    if row['phase'] not in {'load_started', 'load_uncertain', 'loaded', 'proof_pending', 'publishing', 'enrolled'}:
        return {'phase': row['phase'], 'unit': observed, 'image_approved': False}
    control = directory(journal, identity)
    receipt_path = control / 'result.json'
    if not receipt_path.exists():
        return {'phase': row['phase'], 'unit': observed, 'image_approved': False,
                'reconciliation_required': True}
    result = custody.document(receipt_path)
    claim = custody.document(control / 'claim.json')
    request = custody.document(control / 'dispatch.json')
    process = claim.get('process', {})
    claim_hash = custody.checksum(custody.canonical(claim))
    if (set(request) != {'version', 'intent_id', 'unit', 'binding', 'custody_sha256', 'stop_unix'}
            or type(request['version']) is not int or request['version'] != 1
            or request['intent_id'] != identity or request['unit'] != unit_name(identity)
            or request['stop_unix'] != row['intent']['stop_unix']
            or set(claim) != {'version', 'dispatch_sha256', 'process', 'invocation_id', 'enforcement'}
            or type(claim['version']) is not int or claim['version'] != 1
            or not isinstance(process, dict) or set(process) != {'boot_id', 'pid', 'start_ticks'}
            or not isinstance(process['boot_id'], str) or not re.fullmatch(BOOT_ID, process['boot_id'])
            or type(process['pid']) is not int or not 1 < process['pid'] < 2**31
            or type(process['start_ticks']) is not int or process['start_ticks'] < 1
            or not isinstance(claim['invocation_id'], str) or not re.fullmatch(r'[a-f0-9]{32}', claim['invocation_id'])
            or claim['enforcement'] != {**UNIT_LIMITS, 'CPUQuotaPerSecUSec': '1s'}
            and claim['enforcement'] != {**UNIT_LIMITS, 'CPUQuotaPerSecUSec': '1000000'}
            or not any(entry['phase'] == 'load_started' and entry['evidence_sha256'] == claim_hash
                       for entry in row['observations'])
            or observed.get('LoadState') == 'loaded' and observed.get('InvocationID') != claim['invocation_id']):
        raise ToolchainLoadError('load claim or retained unit lineage differs')
    if (set(result) != {'version', 'intent_id', 'claim_sha256', 'daemon_completion_acknowledged',
                       'image_readback_verified', 'image', 'image_approved'}
            or result.get('intent_id') != identity or type(result.get('version')) is not int
            or result.get('version') != 1 or type(result.get('daemon_completion_acknowledged')) is not bool
            or type(result.get('image_readback_verified')) is not bool
            or result.get('image_approved') is not False
            or result.get('claim_sha256') != custody.checksum(custody.canonical(claim))
            or claim.get('dispatch_sha256') != custody.checksum(custody.canonical(request))
            or request.get('binding') != binding):
        raise ToolchainLoadError('load reconciliation evidence differs')
    if result.get('daemon_completion_acknowledged') is not True:
        return {'phase': row['phase'], 'unit': observed, 'image_approved': False,
                'reconciliation_required': True}
    # Unit/process absence or a present image alone is NOT sufficient. Only a
    # prior normal completion receipt plus current exact immutable readback works.
    try:
        actual = image(row['intent']['role'], row['intent']['image_id'])
    except (OSError, ValueError, subprocess.SubprocessError):
        return {'phase': row['phase'], 'unit': observed, 'image_approved': False,
                'daemon_completion_acknowledged': True, 'reconciliation_required': True,
                'failure_stage': 'image_readback'}
    if result['image_readback_verified'] and result.get('image') != actual:
        raise ToolchainLoadError('terminal image readback changed')
    if not result['image_readback_verified']:
        if result['image'] is not None:
            raise ToolchainLoadError('unverified image evidence differs')
        result = {**result, 'image_readback_verified': True, 'image': actual}
        custody.write_document(receipt_path, result)
    if row['phase'] in {'load_started', 'load_uncertain'}:
        row = journal.record_toolchain_import(identity, row['phase'], 'loaded',
                                             custody.checksum(custody.canonical(result)))
    return {'phase': row['phase'], 'unit': observed, 'image_approved': False, 'load_verified': True}
