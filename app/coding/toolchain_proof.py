"""Operator-only fixed-fixture gVisor proofs; never workload or publication authority.

All source bytes must match independent reviewed pins. The import keeps occupying
the ordinary host slot. Durable per-case jobs/results survive client loss; missing
results never become passing tests. No code selected by a model enters this path.
"""
import hashlib
import json
import re
import time

from app.coding import isolated_validation as isolation, isolated_packaging as native
from app.coding import toolchain_loader as loader, toolchain_staging as custody


TIMEOUT = 180
FAILURE_MARKER = 'gca-reviewed-deliberate-test-failure'
# Exact sandbox-only tmpfs metadata, not a host temporary file.
GUEST_TMP = '/tmp'  # nosec B108
FIXTURES = {
    'react': {
        'pass': (51200, '16b1eca2c9b57ecafda6f4bc33cc9b6eab48e1bf055b34c851dbb90d912f21f0'),
        'fail': (51200, '1ae8ec620c9b3e98844f7c705701f9335e97271341102f7e24a05ac2b69edd57'),
    },
    'spring': {
        'pass': (10240, '6f6d5541ac1409745589933173b1b890b648b916544f312fb99c979c90aa45a8'),
        'fail': (10240, 'b65e64b88b33a497766387d5a20c8a83bd6ac2a95e640a5ae95dde0fcb3e946f'),
    },
}
CASES = {'react': ('react_test_pass', 'react_build_pass', 'react_test_fail'),
         'spring': ('spring_test_pass', 'spring_package_pass', 'spring_test_fail')}
SQL = '''CREATE TABLE IF NOT EXISTS toolchain_proof_cases(
    import_id TEXT NOT NULL,case_name TEXT NOT NULL,attempt INTEGER NOT NULL,
    job_id TEXT NOT NULL UNIQUE,source_sha256 TEXT NOT NULL,binding_sha256 TEXT NOT NULL,
    result TEXT,PRIMARY KEY(import_id,case_name,attempt))'''
RESULT_SQL = '''CREATE TABLE IF NOT EXISTS toolchain_proof_results(
    job_id TEXT PRIMARY KEY,result TEXT NOT NULL)'''
MAX_RETAINED_RESULT = 2 * 1024 * 1024
MAX_DIAGNOSTIC_OUTPUT = 4096


class ToolchainProofError(ValueError):
    pass


def fixture(role, case, body):
    if role not in CASES or case not in CASES[role] or not isinstance(body, bytes):
        raise ToolchainProofError('fixed operator fixture case differs')
    variant = case.rsplit('_', 1)[1]
    size, expected = FIXTURES[role][variant]
    if len(body) != size or hashlib.sha256(body).hexdigest() != expected:
        raise ToolchainProofError('actual fixture bytes differ from reviewed source')
    profile = case.rsplit('_', 1)[0]
    isolation.check_archive(body, profile)
    return profile, expected


def schema(journal, *, create=True):
    if not create and not journal.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='toolchain_proof_cases'").fetchone():
        return False
    if create:
        with journal.db:
            journal.db.execute(SQL)
            journal.db.execute(RESULT_SQL)
    rows = journal.db.execute('SELECT import_id,case_name,attempt FROM toolchain_proof_cases LIMIT 49').fetchall()
    known = {row['id']: row['intent']['role'] for row in journal.toolchain_imports()}
    if len(rows) > 48 or any(row['import_id'] not in known or row['case_name'] not in CASES[known[row['import_id']]]
                            or type(row['attempt']) is not int or row['attempt'] not in (1, 2) for row in rows):
        raise ToolchainProofError('proof inventory or attempt bound differs')
    result_inventory(journal)
    return True


def result_inventory(journal):
    """Historical receipts may predate raw-result retention; never invent them."""
    if not journal.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='toolchain_proof_results'").fetchone():
        return False
    rows = journal.db.execute('SELECT job_id,length(CAST(result AS BLOB)) AS size '
                              'FROM toolchain_proof_results LIMIT 49').fetchall()
    if len(rows) > 48 or any(type(row['size']) is not int or not 1 <= row['size'] <= MAX_RETAINED_RESULT
                            or not journal.db.execute('SELECT 1 FROM toolchain_proof_cases WHERE job_id=?',
                                                      (row['job_id'],)).fetchone() for row in rows):
        raise ToolchainProofError('retained diagnostic inventory or bound differs')
    return True


def result_document(value):
    """Only fixed public-fixture execution output; no customer source or secrets."""
    if (not isinstance(value, dict) or any(type(value.get(key)) is not bool
                                         for key in ('success', 'output_truncated', 'cleanup_verified'))
            or type(value.get('exit_code')) is not int
            or any(not isinstance(value.get(key), str) for key in ('stdout', 'stderr'))
            or sum(len(value[key].encode()) for key in ('stdout', 'stderr')) > isolation.MAX_OUTPUT_BYTES
            or not isinstance(value.get('native_enforcement'), dict)
            or not isinstance(value.get('executed_commands'), list)
            or not 1 <= len(value['executed_commands']) <= 2
            or any(not isinstance(command, list) or not command
                   or any(not isinstance(part, str) or len(part) > 4096 for part in command)
                   for command in value['executed_commands'])):
        raise ToolchainProofError('fixed fixture diagnostic result differs')
    body = custody.canonical(value)
    if not 1 <= len(body) <= MAX_RETAINED_RESULT:
        raise ToolchainProofError('fixed fixture diagnostic result exceeds bound')
    return body


def retained_result(journal, record, receipt):
    if not result_inventory(journal):
        return None
    raw = journal.db.execute('SELECT result FROM toolchain_proof_results WHERE job_id=?',
                             (record['job_id'],)).fetchone()
    if raw is None:
        return None
    body = raw['result'].encode()
    value = json.loads(body, object_pairs_hook=custody.unique_object)
    if (result_document(value) != body or custody.checksum(body) != receipt['result_sha256']
            or value['exit_code'] != receipt['exit_code'] or value['cleanup_verified'] is not True
            or custody.checksum(custody.canonical(value['native_enforcement']))
            != receipt['native_enforcement_sha256']):
        raise ToolchainProofError('retained diagnostic result/receipt lineage differs')
    return value


def diagnostic_tail(text, budget):
    """Budget serialized bytes, including Unicode/control escaping, not characters."""
    low, high = 0, min(len(text), budget)
    while low < high:
        middle = (low + high + 1) // 2
        if len(custody.canonical(text[-middle:])) <= budget:
            low = middle
        else:
            high = middle - 1
    return text[-low:] if low else ''


def verify_container(journal, job, container_id):
    journal.verify_identity(job, container_id)
    spec = json.loads(isolation.docker('inspect', container_id))[0]
    host, config = spec['HostConfig'], spec['Config']
    file_limit = isolation.OPEN_FILE_LIMITS.get(job.get('profile'), 256)
    if (spec.get('Id') != container_id or spec.get('State', {}).get('Running') is not True
            or any(mount.get('Type') != 'tmpfs' or mount.get('Destination') not in {'/workspace', GUEST_TMP}
                   or mount.get('RW') is not True for mount in spec['Mounts'])
            or host.get('Runtime') != 'runsc' or host.get('NetworkMode') != 'none'
            or host.get('Privileged') is not False or host.get('ReadonlyRootfs') is not True
            or host.get('Devices') or host.get('DeviceRequests') or host.get('CapAdd')
            or host.get('CapDrop') != ['ALL'] or host.get('SecurityOpt') != ['no-new-privileges:true']
            or host.get('Memory') != 1024**3 or host.get('MemorySwap') != 1024**3
            or host.get('NanoCpus') != 1_000_000_000 or host.get('PidsLimit') != 64
            or host.get('CgroupParent') != native.CGROUP_PARENT or host.get('PidMode') != ''
            or host.get('IpcMode') != 'private' or host.get('CgroupnsMode') != 'private'
            or host.get('UTSMode') != '' or host.get('PortBindings') or host.get('GroupAdd')
            or host.get('PublishAllPorts') is not False or config.get('User') != '65532:65532'
            or config.get('Healthcheck') or config.get('Entrypoint') != ['/bin/sh']
            or config.get('OpenStdin') is not False or config.get('Tty') is not False
            or host.get('LogConfig', {}).get('Type') != 'none'
            or host.get('RestartPolicy', {}).get('Name') not in {'', 'no'}
            or host.get('RestartPolicy', {}).get('MaximumRetryCount') != 0
            or host.get('Ulimits') != [{'Name': 'nofile', 'Hard': file_limit, 'Soft': file_limit}]
            or host.get('Tmpfs') != {
                '/workspace': 'rw,nosuid,nodev,exec,size=256m,mode=1777',
                GUEST_TMP: 'rw,nosuid,nodev,exec,size=64m,mode=1777'}):
        raise ToolchainProofError('fixture sandbox enforcement differs')
    return spec


def readiness(journal, job, container_id):
    deadline = min(time.monotonic() + 10, time.monotonic() + max(0, job['deadline'] - time.time()))
    binding = {'purpose': 'operator_toolchain_fixture', 'validation_job_id': job['id'],
               'image_id': job['image'], 'execution_deadline_unix': job['deadline']}
    while time.monotonic() < deadline:
        spec = verify_container(journal, job, container_id)
        try:
            evidence = native.verify_native_worker(spec, binding)
            break
        except native.NativeObservationChanged:
            time.sleep(0.1)
    else:
        raise ToolchainProofError('stable native fixture enforcement unavailable')
    if evidence['memory_max'] != 1024**3 or evidence['tasks_max'] != 64:
        raise ToolchainProofError('effective fixture memory/task ceiling differs')
    # Read-only fixed guest observation before any fixture code is extracted.
    body = isolation.docker('exec', container_id, '/bin/cat', '/proc/self/status')
    if len(body) > 16384:
        raise ToolchainProofError('guest privilege observation exceeds bound')
    status = dict(line.split(':', 1) for line in body.decode('ascii').splitlines() if ':' in line)
    try:
        if (any(int(status[key].strip(), 16) != 0 for key in ('CapEff', 'CapPrm', 'CapBnd'))
                or status['NoNewPrivs'].strip() != '1'
                or any(list(map(int, status[key].split())) != [65532] * 4 for key in ('Uid', 'Gid'))):
            raise ToolchainProofError('guest UID/capabilities/privilege filter differs')
    except (KeyError, ValueError) as error:
        raise ToolchainProofError('guest privilege evidence invalid') from error
    limits = isolation.docker('exec', container_id, '/bin/sh', '-ec', 'ulimit -Sn; ulimit -Hn')
    expected = str(isolation.OPEN_FILE_LIMITS.get(job.get('profile'), 256))
    if len(limits) > 128 or limits.decode('ascii').split() != [expected, expected]:
        raise ToolchainProofError('guest open-file limits differ')
    return {**evidence, 'guest_uid': 65532, 'guest_cap_eff': 0, 'guest_no_new_privs': 1,
            'guest_open_files_soft': int(expected), 'guest_open_files_hard': int(expected)}


def _verified(journal, row, binding_hash):
    if (row['binding_sha256'] != binding_hash or row['result'] is None
            or len(row['result']) > 8192):
        return None
    value = json.loads(row['result'])
    role = loader.item(journal, row['import_id'])['intent']['role']
    job = journal.db.execute('SELECT * FROM jobs WHERE id=?', (row['job_id'],)).fetchone()
    profile, variant = row['case_name'].rsplit('_', 1)
    expected = FIXTURES[role][variant][1]
    fields = {'version', 'intent_id', 'case', 'job_id', 'source_sha256', 'binding_sha256',
              'exit_code', 'passed', 'cleanup_verified', 'native_enforcement_sha256',
              'result_sha256', 'observed_at_unix'}
    if (not isinstance(value, dict) or set(value) != fields
            or type(value['version']) is not int or value['version'] != 1
            or value['intent_id'] != row['import_id'] or value['case'] != row['case_name']
            or value['job_id'] != row['job_id'] or value['binding_sha256'] != binding_hash
            or value['source_sha256'] != row['source_sha256'] or value['source_sha256'] != expected
            or job is None or job['state'] != 'cleaned' or job['profile'] != profile
            or job['image'] != loader.item(journal, row['import_id'])['intent']['image_id']
            or value['cleanup_verified'] is not True or type(value['passed']) is not bool
            or type(value['exit_code']) is not int or type(value['observed_at_unix']) is not int
            or value['observed_at_unix'] < 1
            or value['passed'] and value['exit_code'] != (0 if variant == 'pass' else 1)
            or any(not isinstance(value[key], str) or not re.fullmatch(r'[a-f0-9]{64}', value[key])
                   for key in ('native_enforcement_sha256', 'result_sha256'))):
        raise ToolchainProofError('retained fixture result lineage differs')
    retained_result(journal, row, value)
    if job['container_id']:
        native.require_native_cleanup(job['container_id'])
    return value


def run(journal, identity, case, source, binding):
    row = loader.verify_binding(journal, identity, binding)
    profile, source_hash = fixture(row['intent']['role'], case, source)
    if row['phase'] not in {'loaded', 'proof_pending'}:
        raise ToolchainProofError('reserved import does not permit fixture proof')
    if int(time.time()) + TIMEOUT + 90 >= row['intent']['stop_unix']:
        raise ToolchainProofError('fixed hosting lifetime insufficient for fixture proof')
    verified_load = loader.reconcile(journal, identity, binding)
    if verified_load.get('load_verified') is not True:
        raise ToolchainProofError('positive daemon completion and image readback required')
    if row['phase'] == 'loaded':
        journal.record_toolchain_import(identity, 'loaded', 'proof_pending',
                                        custody.checksum(custody.canonical(verified_load)))
    if loader.item(journal, identity)['phase'] != 'proof_pending':
        raise ToolchainProofError('reserved import does not permit fixture proof')
    schema(journal)
    binding_hash = custody.checksum(custody.canonical(binding))
    rows = journal.db.execute('SELECT * FROM toolchain_proof_cases WHERE import_id=? AND case_name=? '
                             'ORDER BY attempt', (identity, case)).fetchall()
    for previous in rows:
        result = _verified(journal, previous, binding_hash)
        if result and result['passed']:
            return {**result, 'replayed_observation': True, 'image_approved': False}
    # Reconciliation may clean a lost sandbox but never invent its missing result.
    for previous in journal.db.execute('SELECT j.* FROM toolchain_proof_cases p JOIN jobs j '
                                      'ON p.job_id=j.id WHERE p.import_id=?', (identity,)):
        if previous['state'] != 'cleaned':
            raise ToolchainProofError('earlier fixture needs identity-safe cleanup before retry')
        if previous['container_id']:
            native.require_native_cleanup(previous['container_id'])
    native.require_host_limits()
    job = journal.begin_toolchain_proof(identity, profile, row['intent']['image_id'], TIMEOUT,
                                        case, source_hash, binding_hash)
    policy = {'images': {profile: row['intent']['image_id']}}
    result = isolation._execute_archive(source, profile, TIMEOUT, policy, journal, job,
                                        readiness=readiness, native_cleanup=native.require_native_cleanup,
                                        proof_cgroup=native.CGROUP_PARENT)
    if result.get('cleanup_verified') is not True or not isinstance(result.get('native_enforcement'), dict):
        raise ToolchainProofError('fixture enforcement and native cleanup evidence required')
    started_expected = result.get('executed_commands', [])[-1:] == [list(isolation.PROFILES[profile][1])]
    positive = case.endswith('_pass')
    passed = (result['success'] is positive and result['exit_code'] == (0 if positive else 1)
              and result['output_truncated'] is False and result['cleanup_verified'] is True
              and started_expected and (positive or FAILURE_MARKER in result['stdout'] + result['stderr']))
    receipt = {'version': 1, 'intent_id': identity, 'case': case, 'job_id': job['id'],
               'source_sha256': source_hash, 'binding_sha256': binding_hash,
               'exit_code': result['exit_code'], 'passed': passed, 'cleanup_verified': True,
               'native_enforcement_sha256': custody.checksum(custody.canonical(result['native_enforcement'])),
               'result_sha256': custody.checksum(custody.canonical(result)), 'observed_at_unix': int(time.time())}
    body = custody.canonical(receipt).decode()
    if len(body) > 8192:
        raise ToolchainProofError('fixture receipt exceeds bound')
    with journal.db:
        journal.db.execute('INSERT INTO toolchain_proof_results VALUES(?,?)',
                           (job['id'], result_document(result).decode()))
        journal.db.execute('UPDATE toolchain_proof_cases SET result=? WHERE job_id=? AND result IS NULL',
                           (body, job['id']))
    return {**receipt, 'image_approved': False, 'replayed_observation': False}


def observe(journal, identity, binding):
    row = loader.verify_binding(journal, identity, binding)
    if not schema(journal, create=False):
        return {'intent_id': identity, 'phase': row['phase'],
                'cases': {case: None for case in CASES[row['intent']['role']]},
                'all_fixed_cases_passed': False, 'image_approved': False}
    fingerprint = custody.checksum(custody.canonical(binding))
    results = {}
    for case in CASES[row['intent']['role']]:
        rows = journal.db.execute('SELECT * FROM toolchain_proof_cases WHERE import_id=? AND case_name=? '
                                 'ORDER BY attempt', (identity, case)).fetchall()
        verified = [_verified(journal, result, fingerprint) for result in rows]
        results[case] = next((result for result in verified if result and result['passed']), None)
    return {'intent_id': identity, 'phase': row['phase'], 'cases': results,
            'all_fixed_cases_passed': all(results.values()), 'image_approved': False}


def diagnostics(journal, identity, binding):
    """Read-only bounded latest-attempt evidence; cannot rerun or approve a case.

    Keep the original observe/publication projection unchanged so older published
    evidence hashes remain valid. Full results and proof receipts commit atomically.
    """
    row = loader.verify_binding(journal, identity, binding)
    cases = {}
    if schema(journal, create=False):
        fingerprint = custody.checksum(custody.canonical(binding))
        for case in CASES[row['intent']['role']]:
            record = journal.db.execute('SELECT * FROM toolchain_proof_cases WHERE import_id=? AND case_name=? '
                                       'ORDER BY attempt DESC LIMIT 1', (identity, case)).fetchone()
            if record is None:
                continue
            receipt = _verified(journal, record, fingerprint)
            value = retained_result(journal, record, receipt) if receipt else None
            if receipt is None or value is None:
                cases[case] = {'job_id': record['job_id'], 'attempt': record['attempt'],
                               'raw_result_available': False, 'receipt_available': receipt is not None}
                continue
            stderr = diagnostic_tail(value['stderr'], MAX_DIAGNOSTIC_OUTPUT // 2)
            stdout = diagnostic_tail(value['stdout'], MAX_DIAGNOSTIC_OUTPUT - len(custody.canonical(stderr)))
            cases[case] = {'job_id': record['job_id'], 'attempt': record['attempt'],
                           'raw_result_available': True, 'receipt_available': True,
                           'result_sha256': receipt['result_sha256'], 'passed': receipt['passed'],
                           'exit_code': value['exit_code'], 'success': value['success'],
                           'executed_commands': value['executed_commands'],
                           'stdout': stdout, 'stderr': stderr,
                           'diagnostic_excerpt_truncated': len(stdout) + len(stderr)
                           < len(value['stdout']) + len(value['stderr']),
                           'output_truncated_at_execution': value['output_truncated'],
                           'cleanup_verified': True}
    return {'version': 1, 'intent_id': identity, 'phase': row['phase'], 'cases': cases,
            'execution_invoked': False, 'image_approved': False}
