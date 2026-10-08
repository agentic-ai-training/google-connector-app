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
    if adoption_receipt(journal, identity) is not None:
        raise ToolchainLoadError('retained adoption is observation-only; no load dispatch')
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
                  '"user":{{json .Config.User}},"entrypoint":{{json (index .Config "Entrypoint")}},'
                  '"volumes":{{json (index .Config "Volumes")}},"healthcheck":{{json (index .Config "Healthcheck")}},'
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


def adoption_receipt(journal, identity):
    """Typed adoption evidence is not a fabricated new daemon/worker receipt."""
    if not journal.db.execute("SELECT 1 FROM sqlite_master WHERE name='toolchain_adoptions'").fetchone():
        return None
    rows = journal.db.execute('SELECT * FROM toolchain_adoptions LIMIT 9').fetchall()
    known = {row['id'] for row in journal.toolchain_imports()}
    if len(rows) > 8:
        raise ToolchainLoadError('adoption inventory exceeds bound')
    found = None
    for row in rows:
        body = row['plan']
        if (row['import_id'] not in known or not isinstance(body, str) or not 0 < len(body.encode()) <= 8192
                or custody.checksum(body.encode()) != row['plan_sha256']):
            raise ToolchainLoadError('adoption audit fingerprint differs')
        value = json.loads(body)
        if not isinstance(value, dict) or custody.canonical(value).decode() != body:
            raise ToolchainLoadError('adoption audit is not canonical')
        if row['import_id'] == identity:
            found = value
    return found


def completion_receipt(journal, identity):
    adopted = adoption_receipt(journal, identity)
    return adopted if adopted is not None else custody.document(directory(journal, identity) / 'result.json')


def auxiliary_history(journal, identity, result):
    """Retain repaired/restored causal history separately from original completion."""
    history, repaired, restored = {}, None, None
    queries = {'toolchain_readback_repairs': 'SELECT * FROM toolchain_readback_repairs LIMIT 9',
               'toolchain_restorations': 'SELECT * FROM toolchain_restorations LIMIT 9'}
    for table, query in queries.items():
        if not journal.db.execute('SELECT 1 FROM sqlite_master WHERE name=?', (table,)).fetchone():
            continue
        rows = journal.db.execute(query).fetchall()
        known = {row['id'] for row in journal.toolchain_imports()}
        if len(rows) > 8 or any(row['import_id'] not in known for row in rows):
            raise ToolchainLoadError('auxiliary completion inventory differs')
        selected = next((row for row in rows if row['import_id'] == identity), None)
        if selected is None:
            continue
        names = ('plan', 'claim', 'result') if table == 'toolchain_restorations' else ('plan',)
        docs = {}
        for name in names:
            body = selected[name]
            if not isinstance(body, str) or not 0 < len(body.encode()) <= 8192:
                raise ToolchainLoadError('auxiliary completion is incomplete')
            value = json.loads(body)
            if not isinstance(value, dict) or custody.canonical(value).decode() != body:
                raise ToolchainLoadError('auxiliary completion is not canonical')
            docs[name] = value
        if custody.checksum(selected['plan'].encode()) != selected['plan_sha256']:
            raise ToolchainLoadError('auxiliary plan fingerprint differs')
        if table == 'toolchain_readback_repairs':
            repaired = docs['plan']
            control = directory(journal, identity)
            source = item(journal, identity)['intent']
            if (repaired.get('after') != result or repaired.get('intent_id') != identity
                    or repaired.get('binding') != {key: source[key] for key in
                                                   ('bundle_sha256', 'policy_sha256', 'registry_sha256')}
                    or repaired.get('claim_sha256') != custody.checksum(custody.canonical(custody.document(control/'claim.json')))
                    or repaired.get('dispatch_sha256') != custody.checksum(custody.canonical(custody.document(control/'dispatch.json')))
                    or repaired.get('custody_sha256') != custody.checksum(custody.canonical(
                        custody.verify_retained_custody(journal, identity)))):
                raise ToolchainLoadError('repaired source completion differs')
        if table == 'toolchain_restorations':
            plan, claim, receipt = (docs[name] for name in names)
            if (receipt.get('intent_id') != identity or receipt.get('image') != result.get('image')
                    or receipt.get('image_approved') is not False or receipt.get('failure_stage') is not None
                    or any(receipt.get(key) is not True for key in ('docker_load_invoked',
                           'daemon_completion_acknowledged', 'image_readback_verified'))
                    or claim.get('plan_sha256') != selected['plan_sha256']
                    or receipt.get('plan_sha256') != selected['plan_sha256']
                    or receipt.get('claim_sha256') != custody.checksum(custody.canonical(claim))
                    or plan.get('intent_id') != identity):
                raise ToolchainLoadError('restored source completion differs')
            restored = {'plan_sha256': selected['plan_sha256'],
                        'claim_sha256': custody.checksum(custody.canonical(claim)),
                        'result_sha256': custody.checksum(custody.canonical(receipt)),
                        'controller_sha256': plan.get('controller_sha256')}
            if (not isinstance(claim.get('invocation_id'), str)
                    or not re.fullmatch(r'[a-f0-9]{32}', claim['invocation_id'])):
                raise ToolchainLoadError('restoration terminal identity differs')
            observed = subprocess.run(  # nosec B603
                [SYSTEMCTL, 'show', 'gca-toolchain-restore-' + identity + '.service', '--no-pager',
                 '--property=LoadState', '--property=MainPID', '--property=InvocationID'],
                env=CLEAN_ENV, capture_output=True, timeout=10, check=False)
            if len(observed.stdout) > 4096:
                raise ToolchainLoadError('restoration terminal observation exceeds bound')
            values = {}
            for line in observed.stdout.decode('ascii').splitlines():
                key, sep, value = line.partition('=')
                if not sep or key in values:
                    raise ToolchainLoadError('restoration terminal observation differs')
                values[key] = value
            if (set(values) != {'LoadState', 'MainPID', 'InvocationID'} or values['MainPID'] != '0'
                    or values['LoadState'] not in {'loaded', 'not-found'}
                    or observed.returncode and values['LoadState'] != 'not-found'
                    or values['LoadState'] == 'loaded' and values['InvocationID'] != claim['invocation_id']):
                raise ToolchainLoadError('restoration worker is not terminal')
        history[table] = {name: custody.checksum(custody.canonical(value)) for name, value in docs.items()}
    expected = custody.checksum(custody.canonical(restored)) if restored is not None else None
    if repaired is not None and repaired.get('restoration_evidence_sha256') != expected:
        raise ToolchainLoadError('repair and restoration causal fingerprint differs')
    return custody.checksum(custody.canonical(history))


def adoption_plan(journal, identity, source_identity, binding, *, seen=()):
    row = verify_binding(journal, identity, binding)
    source = item(journal, source_identity)
    if (source_identity == identity or source_identity in seen or len(seen) >= 8
            or row['intent']['version'] != 2 or source['intent']['version'] != 2
            or source['phase'] != 'rejected' or source['intent']['bundle_sha256'] == binding['bundle_sha256']
            or any(source['intent'][key] != row['intent'][key] for key in
                   ('role', 'host_instance_id', 'stop_unix', 'policy_sha256', 'registry_sha256',
                    *REVIEWED_ARCHIVES[row['intent']['role']]))
            or journal.db.execute("SELECT 1 FROM jobs WHERE state<>'cleaned' LIMIT 1").fetchone()):
        raise ToolchainLoadError('adoption requires same-content terminal source and new reviewed binding')
    journal.verify_rejections()
    source_binding = {key: source['intent'][key] for key in binding}
    completed = completion_receipt(journal, source_identity)
    if adoption_receipt(journal, source_identity) is None and (
            completed.get('daemon_completion_acknowledged') is not True
            or completed.get('image_readback_verified') is not True or completed.get('image_approved') is not False):
        raise ToolchainLoadError('source has no positive immutable completion receipt')
    verified = reconcile(journal, source_identity, source_binding, _seen=(*seen, identity))
    if verified.get('load_verified') is not True or verified['unit']['MainPID'] != '0':
        raise ToolchainLoadError('source completion or terminal worker is unconfirmed')
    rejection = journal.db.execute('SELECT * FROM toolchain_rejections WHERE import_id=?', (source_identity,)).fetchone()
    source_custody = custody.verify_retained_custody(journal, source_identity)
    if json.loads(rejection['receipt'])['load_sha256'] != custody.checksum(custody.canonical(completed)):
        raise ToolchainLoadError('source rejection does not bind exact completion')
    retained = custody.verify_retained_custody(journal, identity)
    actual = image(row['intent']['role'], row['intent']['image_id'])
    return {'version': 1, 'origin': 'completed_load_adoption', 'intent_id': identity,
            'source_intent_id': source_identity, 'binding': binding, 'source_binding': source_binding,
            'source_completion_sha256': custody.checksum(custody.canonical(completed)),
            'source_rejection_sha256': rejection['receipt_sha256'],
            'source_custody_sha256': custody.checksum(custody.canonical(source_custody)),
            'source_auxiliary_sha256': auxiliary_history(journal, source_identity, completed),
            'custody_sha256': custody.checksum(custody.canonical(retained)),
            'image': actual, 'docker_load_invoked': False, 'image_approved': False}


def adopt(journal, identity, source_identity, binding, *, _seen=()):
    """Source handover, never load replay or policy approval; durable plan before CAS."""
    row = verify_binding(journal, identity, binding)
    previous = adoption_receipt(journal, identity)
    if previous is None:
        if row['phase'] != 'archive_verified' or row['intent']['stop_unix'] <= int(time.time()):
            raise ToolchainLoadError('adoption requires unexpired never-loaded verified custody')
        control = journal.directory / 'toolchain-loads' / identity
        if control.exists() or control.is_symlink() or unit(identity)['LoadState'] != 'not-found':
            raise ToolchainLoadError('new adoption already has load control or unit authority')
    plan = adoption_plan(journal, identity, source_identity, binding, seen=_seen)
    if previous is not None and previous != plan:
        raise ToolchainLoadError('retained adoption lineage changed')
    body = custody.canonical(plan)
    if len(body) > 8192:
        raise ToolchainLoadError('adoption plan exceeds bound')
    fingerprint = custody.checksum(body)
    if previous is None:
        with journal.db:
            journal.db.execute('CREATE TABLE IF NOT EXISTS toolchain_adoptions('
                               'import_id TEXT PRIMARY KEY,plan_sha256 TEXT NOT NULL,plan TEXT NOT NULL)')
            journal.db.execute('INSERT INTO toolchain_adoptions VALUES(?,?,?)', (identity, fingerprint, body.decode()))
    if row['phase'] == 'archive_verified':
        row = journal.record_toolchain_import(identity, 'archive_verified', 'adopting', fingerprint)
    if row['phase'] == 'adopting':
        row = journal.record_toolchain_import(identity, 'adopting', 'loaded', fingerprint)
    if (row['phase'] not in {'loaded', 'proof_pending', 'publishing', 'enrolled', 'rejected'}
            or not any(entry['phase'] == 'adopting' and entry['evidence_sha256'] == fingerprint
                       for entry in row['observations'])):
        raise ToolchainLoadError('adoption phase lineage differs')
    return {'intent_id': identity, 'phase': row['phase'], 'unit': unit(source_identity), 'load_verified': True,
            'load_origin': 'completed_load_adoption', 'source_intent_id': source_identity,
            'adoption_plan_sha256': fingerprint, 'docker_load_invoked': False, 'image_approved': False}


def reconcile(journal, identity, binding, *, _seen=()):
    """No Docker load. A positive protected terminal receipt is required."""
    row = verify_binding(journal, identity, binding)
    adopted = adoption_receipt(journal, identity)
    if adopted is not None:
        return adopt(journal, identity, adopted.get('source_intent_id'), binding, _seen=_seen)
    observed = unit(identity)
    if row['phase'] not in {'load_started', 'load_uncertain', 'loaded', 'proof_pending', 'publishing', 'enrolled', 'rejected'}:
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
