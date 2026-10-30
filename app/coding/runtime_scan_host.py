"""Fixed operator scan launcher; not a public/model-selected execution tool.

The authenticated controller must verify target/source/configuration before using
this module. Public input pins, private staging, native readiness, durable claims,
actual output readback and cleanup are enforced here. No runtime approval follows.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess  # nosec B404
import time

from app.coding import isolated_packaging as native, isolated_validation as isolation
from app.coding import oci_packager as oci, runtime_base_acquisition as cas
from app.coding import runtime_scan_worker as worker, scanner_custody as custody, scanner_database as database


WORKER_SHA = 'cb6c2004911726d3983e560c6fccdcc8760fdb49ef5af087d78b6234c1732445'
BASE_ROOT = Path('/var/lib/gca-runtime-candidates-v1')
BINARY_ROOT = Path('/var/lib/gca-runtime-scanner-v1') / custody.BINARY_SHA256
DB_ROOT = Path('/var/lib/gca-scanner-databases-v1') / '79734a08f6dccc4623e6ef7d7bc523a05d4071d57370bde7a5f2cfd4c5086fd1'
MAX_JOBS = 8
TMPFS = 'rw,nosuid,nodev,exec,size=256m,mode=700,uid=65532,gid=65532'
# Container-private mount, not a host temporary path or shared file location.
GUEST_TMP = '/tmp'  # nosec B108
DATA_PINS = {
    'inventory': ('java', 'java-db', 'trivy-java.db', 1552633856,
                  '8718fff42046c2485b31440313fd49fa487f81a03d339a7655f670f6447153f5',
                  '84aa81e5b46749162c15d18526d31e7cb9e7a54b8ec430bc930e6b647c16e8a4'),
    'assessment': ('vulnerability', 'db', 'trivy.db', 1607704576,
                   '217ebd26bb6d7a72aa42a1c2daed8f512148a106855e4add884b878bbc1bd9c2',
                   '2e9a80ca1f354478d8956c6ced231194f8f134e53735a2ff5f097b7bfe1df0b0'),
}


class RuntimeScanHostError(RuntimeError):
    pass


def digest(body):
    return hashlib.sha256(body).hexdigest()


def exact_job(journal, binding):
    observation = journal.observe_runtime_scan(binding['scan_id'], binding['role'], binding['stage'])
    if observation['state'] == 'unknown':
        raise RuntimeScanHostError('scan identity has no durable dispatch')
    row = journal.db.execute('SELECT s.binding,j.* FROM runtime_scan_jobs s JOIN jobs j ON j.id=s.job_id '
                             'WHERE s.scan_key=?', (observation['scan_key'],)).fetchone()
    if row is None or row['binding'] != oci.canonical(binding).decode():
        raise RuntimeScanHostError('retained scan binding differs')
    return dict(row), observation


def jobs_root(journal):
    root = journal.directory / 'runtime-scan-jobs'
    if not root.exists():
        root.mkdir(mode=0o700)
        native._sync(journal.directory)
    native._protected(root, private=True, directory=True)
    children = list(root.iterdir())
    packaging = journal.directory / 'packaging-jobs'
    if packaging.exists():
        native._protected(packaging, private=True, directory=True)
    other_count = len(list(packaging.iterdir())) if packaging.exists() else 0
    if len(children) + other_count >= MAX_JOBS:
        raise RuntimeScanHostError('scan staging capacity exhausted')
    for child in children:
        native._protected(child, private=True, directory=True)
        row = journal.db.execute('SELECT profile,purpose FROM jobs WHERE id=?', (child.name,)).fetchone()
        if row is None or row['purpose'] != 'validation' or row['profile'] not in isolation.RUNTIME_SCAN_PROFILES:
            raise RuntimeScanHostError('untracked scan staging directory')
    return root


def copy_public(source, destination, size, checksum):
    # Hash and copy the same protected descriptor, with fixed expected bytes.
    with database.protected_stream(source, worker.MAX_DATA) as stream:
        if os.fstat(stream.fileno()).st_size != size:
            raise RuntimeScanHostError('public scan input size differs')
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        measured, count = hashlib.sha256(), 0
        try:
            with os.fdopen(fd, 'wb', closefd=False) as target:
                while body := stream.read(1024**2):
                    count += len(body)
                    if count > size:
                        raise RuntimeScanHostError('public scan input grew')
                    target.write(body)
                    measured.update(body)
                target.flush()
                os.fsync(fd)
            if count != size or measured.hexdigest() != checksum:
                raise RuntimeScanHostError('public scan input hash differs')
            os.fchown(fd, 65532, 65532)
        finally:
            os.close(fd)


def input_plan(journal, binding):
    role, folder, name, size, checksum, metadata_checksum = DATA_PINS[binding['stage']]
    if binding['manifest_sha256'] != cas.pin(binding['role'])['manifest_digest'][7:]:
        raise RuntimeScanHostError('runtime manifest is outside the reviewed catalog')
    facts = database.observe_database(DB_ROOT, role, now=datetime.now(timezone.utc))
    if (facts['database_digest'] != 'sha256:' + checksum or facts['metadata_digest'] != 'sha256:' + metadata_checksum
            or digest(oci.canonical(database.CATALOG)) != binding['database_catalog_sha256']):
        raise RuntimeScanHostError('database content differs from closed scan pins')
    observed = custody.observe(BINARY_ROOT)
    if observed != {'complete': True, 'offset': custody.BINARY_BYTES, 'prefix_sha256': custody.BINARY_SHA256}:
        raise RuntimeScanHostError('signature-bound scanner custody differs')
    paths = {'scanner.bin': (BINARY_ROOT / 'trivy.bin', custody.BINARY_BYTES, custody.BINARY_SHA256),
             'database/' + name: (DB_ROOT / role / name, size, checksum)}
    metadata_body = cas.read_file(DB_ROOT / role / 'metadata.json', (DB_ROOT / role / 'metadata.json').stat().st_size)
    paths['database/metadata.json'] = (DB_ROOT / role / 'metadata.json', len(metadata_body), metadata_checksum)
    if binding['stage'] == 'inventory':
        cas.observe_candidate(BASE_ROOT, role=binding['role'])
        root = BASE_ROOT / binding['role']
        manifest = cas.read_file(root / 'blobs/sha256' / binding['manifest_sha256'],
                                 (root / 'blobs/sha256' / binding['manifest_sha256']).stat().st_size)
        graph = oci._metadata(manifest)
        paths['image/blobs/sha256/' + binding['manifest_sha256']] = (
            root / 'blobs/sha256' / binding['manifest_sha256'], len(manifest), binding['manifest_sha256'])
        for item in [graph['config'], *graph['layers']]:
            checksum = item['digest'][7:]
            paths['image/blobs/sha256/' + checksum] = (root / 'blobs/sha256' / checksum, item['size'], checksum)
        for name in ('oci-layout', 'index.json'):
            path = root / name
            body = cas.read_file(path, path.stat().st_size)
            paths['image/' + name] = (path, len(body), digest(body))
    else:
        key = journal.runtime_scan_key(binding['scan_id'], binding['role'], 'inventory')
        row = journal.db.execute('SELECT binding FROM runtime_scan_jobs WHERE scan_key=?', (key,)).fetchone()
        if row is None:
            raise RuntimeScanHostError('assessment has no inventory parent')
        parent = json.loads(row[0])
        retained = recover(journal, parent)
        if retained['report_sha256'] != binding['inventory_sha256']:
            raise RuntimeScanHostError('assessment inventory bytes differ')
        path = journal.directory / 'runtime-scan-jobs' / retained['job_id'] / 'work/output/report.json'
        paths['inventory.cdx.json'] = (path, retained['report_bytes'], retained['report_sha256'])
    return paths


def prepare(journal, job, binding, paths, worker_body):
    if not isinstance(worker_body, bytes) or digest(worker_body) != WORKER_SHA:
        raise RuntimeScanHostError('reviewed scanner worker source differs')
    total = sum(item[1] for item in paths.values()) + len(worker_body) + 3
    if total + 96 * 1024**2 > native.VOLUME_BYTES or shutil.disk_usage(journal.directory).free < 4 * 1024**3:
        raise RuntimeScanHostError('scan staging has insufficient combined headroom')
    directory = jobs_root(journal) / job['id']
    directory.mkdir(mode=0o700)
    native._sync(directory.parent)
    native._write(directory / 'binding.json', oci.canonical(binding))
    native._sync(directory)
    work = native.storage(directory, create=True)
    inputs, cache, output = (work / name for name in ('input', 'cache', 'output'))
    for path in (inputs, cache, output):
        native._worker_directory(path)
    files = {}
    for name, (source, size, checksum) in paths.items():
        destination = (cache / DATA_PINS[binding['stage']][1] / name.removeprefix('database/')) if name.startswith('database/') else inputs / name
        missing = []
        parent = destination.parent
        while parent not in (inputs, cache) and not parent.exists():
            missing.append(parent)
            parent = parent.parent
        for parent in reversed(missing):
            native._worker_directory(parent)
        if name == 'inventory.cdx.json':
            body = read_worker(source, source.parents[2], worker.MAX_REPORT)
            if len(body) != size or digest(body) != checksum:
                raise RuntimeScanHostError('retained parent changed during staging')
            native._write(destination, body, worker=True)
        else:
            copy_public(source, destination, size, checksum)
        files[name] = {'bytes': size, 'sha256': checksum}
    for name, body in (('config.yaml', b'{}\n'), ('ignorefile', b'')):
        native._write(inputs / name, body, worker=True)
        files[name] = {'bytes': len(body), 'sha256': digest(body)}
    native._write(inputs / 'worker.py', worker_body, worker=True)
    frame = oci.canonical({'version': 1, 'binding': binding, 'files': files})
    if len(frame) > 16384:
        raise RuntimeScanHostError('scan worker frame exceeds bound')
    native._write(inputs / 'request.json', frame, worker=True)
    for path in (inputs, cache, output, work, directory):
        native._sync(path)
    return directory, [(inputs, '/gca/input', True), (cache, '/gca/cache', False), (output, '/gca/output', False)]


def create_arguments(journal, job, mounts):
    args = ['create', '--name', job['name'], '--pull=never', '--runtime=runsc',
            '--label=gca.purpose=validation', '--label=gca.scope=' + journal.scope, '--label=gca.job=' + job['id'],
            '--network=none', '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges:true',
            '--user=65532:65532', '--ipc=private', '--cgroupns=private', '--memory=3g', '--memory-swap=3g',
            '--cpus=1', '--pids-limit=64', '--cgroup-parent=gcapackaging.slice', '--ulimit=nofile=64:64',
            '--log-driver=none', '--tmpfs', GUEST_TMP + ':' + TMPFS, '--entrypoint=/usr/bin/env']
    for source, destination, readonly in mounts:
        args.extend(['--mount', 'type=bind,src=' + str(native._path(str(source))) + ',dst=' + destination
                     + (',readonly' if readonly else '')])
    return args + [job['image'], '-i', 'PATH=/usr/local/bin:/usr/bin:/bin', 'HOME=/nonexistent',
                   'LANG=C.UTF-8', 'PYTHONDONTWRITEBYTECODE=1', 'PYTHONUNBUFFERED=1',
                   'python3', '-I', '-B', '/gca/input/worker.py']


def verify_container(journal, job, cid, mounts):
    journal.verify_identity(job, cid)
    spec = json.loads(isolation.docker('inspect', cid))[0]
    host = spec['HostConfig']
    expected = {(str(source), destination, not readonly) for source, destination, readonly in mounts}
    actual = {(x.get('Source'), x.get('Destination'), x.get('RW')) for x in spec['Mounts'] if x.get('Type') == 'bind'}
    cmd = create_arguments(journal, job, mounts)
    if (actual != expected or len(spec['Mounts']) not in {len(expected), len(expected) + 1}
            or len([x for x in spec['Mounts'] if x.get('Type') == 'bind']) != len(expected)
            or any(x.get('Type') not in {'bind', 'tmpfs'} or x.get('Type') == 'tmpfs' and x.get('Destination') != GUEST_TMP
                   or x.get('Type') == 'bind' and x.get('Propagation') != 'rprivate' for x in spec['Mounts'])
            or host.get('Runtime') != 'runsc' or host.get('NetworkMode') != 'none'
            or host.get('Privileged') is not False or host.get('Devices') or host.get('DeviceRequests')
            or host.get('PidMode') != '' or host.get('IpcMode') != 'private' or host.get('UTSMode') != ''
            or host.get('CgroupnsMode') != 'private' or host.get('PortBindings') or host.get('GroupAdd')
            or host.get('PublishAllPorts') is not False or host.get('ReadonlyRootfs') is not True
            or host.get('CapAdd') or host.get('CapDrop') != ['ALL']
            or host.get('SecurityOpt') != ['no-new-privileges:true'] or host.get('Tmpfs') != {GUEST_TMP: TMPFS}
            or host.get('Memory') != 3 * 1024**3 or host.get('MemorySwap') != 3 * 1024**3
            or host.get('NanoCpus') != 1_000_000_000 or host.get('PidsLimit') != 64
            or host.get('CgroupParent') != 'gcapackaging.slice'
            or host.get('Ulimits') != [{'Name': 'nofile', 'Hard': 64, 'Soft': 64}]
            or host.get('LogConfig', {}).get('Type') != 'none'
            or host.get('RestartPolicy', {}).get('Name') not in {'', 'no'}
            or host.get('RestartPolicy', {}).get('MaximumRetryCount') != 0
            or spec['Config'].get('Healthcheck') is not None or spec['Config'].get('User') != '65532:65532'
            or spec['Config'].get('OpenStdin') is not False or spec['Config'].get('Tty') is not False
            or spec['Config'].get('Entrypoint') != ['/usr/bin/env']
            or spec['Config'].get('Cmd') != cmd[cmd.index(job['image']) + 1:]):
        raise RuntimeScanHostError('scan container enforcement differs')
    return spec


def publish_readiness(journal, job, cid, mounts, binding, directory, child):
    end = min(time.monotonic() + 10, time.monotonic() + max(0, binding['deadline'] - time.time()))
    while time.monotonic() < end and child.poll() is None:
        spec = verify_container(journal, job, cid, mounts)
        if spec.get('State', {}).get('Running') is True:
            try:
                proof = native.verify_native_worker(spec, binding, deadline_field='deadline')
            except native.NativeObservationChanged:
                time.sleep(0.1)
                continue
            worker.verify_readiness(binding, proof)
            again = verify_container(journal, job, cid, mounts)
            if (again.get('State', {}).get('Running') is not True or again['State'].get('Pid') != proof['native_pid']
                    or proof['container_id'] != cid):
                raise RuntimeScanHostError('scan native identity changed before publication')
            body = oci.canonical(proof)
            native._write(directory / 'native-proof.json', body)
            inputs = directory / 'work/input'
            native._write(inputs / 'host-proof.pending', body, worker=True)
            os.rename(inputs / 'host-proof.pending', inputs / 'host-proof.json')
            native._sync(inputs)
            native._sync(directory)
            return
        time.sleep(0.1)
    raise RuntimeScanHostError('scan native readiness did not arrive')


def read_worker(path, directory, maximum):
    if not path.is_relative_to(directory / 'work') or '..' in path.parts:
        raise RuntimeScanHostError('scan output path is not fixed')
    for entry in (path, *path.parents):
        if entry == directory:
            break
        info = entry.lstat()
        if entry.is_symlink() or info.st_uid not in {0, os.geteuid(), 65532} or info.st_mode & 0o077:
            raise RuntimeScanHostError('scan output ownership differs')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o177
                or info.st_uid not in {0, os.geteuid(), 65532} or not 0 < info.st_size <= maximum):
            raise RuntimeScanHostError('scan output inode or bound differs')
        body = stream.read(maximum + 1)
    if len(body) > maximum or len(body) != info.st_size:
        raise RuntimeScanHostError('scan output grew')
    return body


def recover(journal, binding):
    job, observation = exact_job(journal, binding)
    if observation['cleanup_verified'] is not True:
        raise RuntimeScanHostError('scan native cleanup remains uncertain')
    directory = journal.directory / 'runtime-scan-jobs' / job['id']
    native._protected(directory, private=True, directory=True)
    if not native.storage_mount(directory):
        raise RuntimeScanHostError('scan output filesystem identity differs')
    body = read_worker(directory / 'work/output/report.json', directory, worker.MAX_REPORT)
    result_body = read_worker(directory / 'work/output/worker-result.json', directory, 16384)
    result = worker.metadata(result_body, 16384)
    proof_path = native._protected(directory / 'native-proof.json', private=True)
    if proof_path.stat().st_size > 16384:
        raise RuntimeScanHostError('scan native proof exceeds bound')
    proof_body = proof_path.read_bytes()
    proof = worker.metadata(proof_body, 16384)
    guest_proof = read_worker(directory / 'work/input/host-proof.json', directory, 16384)
    if (proof_body != guest_proof or proof['binding_hash'] != digest(oci.canonical(binding))
            or proof['container_id'] != job['container_id'] or proof['expires_at_unix'] != binding['deadline']):
        raise RuntimeScanHostError('scan retained native proof lineage differs')
    expected = {'version', 'binding_hash', 'report_sha256', 'report_bytes', 'scanner_exit_code', 'scanner_duration_ms',
                'scanner_executed', 'native_cleanup_verified', 'scan_completed', 'metrics', 'runtime_approved'}
    measured = worker.report_metrics(body, binding)
    if (set(result) != expected or type(result['version']) is not int or result['version'] != 1
            or result['binding_hash'] != proof['binding_hash']
            or result['report_sha256'] != digest(body) or type(result['report_bytes']) is not int or result['report_bytes'] != len(body)
            or type(result['scanner_exit_code']) is not int or result['scanner_exit_code'] != 0
            or type(result['scanner_duration_ms']) is not int or not 0 <= result['scanner_duration_ms'] <= 270000
            or result['scanner_executed'] is not True or result['metrics'] != measured
            or any(result[key] is not False for key in ('native_cleanup_verified', 'scan_completed', 'runtime_approved'))):
        raise RuntimeScanHostError('scan retained result differs from actual output')
    key = observation['scan_key']
    record = (key, digest(body), len(body), digest(proof_body))
    old = journal.db.execute('SELECT * FROM runtime_scan_outputs WHERE scan_key=?', (key,)).fetchone()
    if old is not None and tuple(old) != record:
        raise RuntimeScanHostError('scan output publication conflicts with durable evidence')
    with journal.db:
        journal.db.execute('INSERT OR IGNORE INTO runtime_scan_outputs VALUES(?,?,?,?)', record)
    return {**result, 'job_id': job['id'], 'native_cleanup_verified': True,
            'native_proof_sha256': digest(proof_body), 'report_custody_verified': True,
            'scan_completed': False, 'runtime_approved': False}


def reconcile(journal, binding):
    """Recover cleanup, never dispatch or convert a failed scan into success.

    The write-ahead create event distinguishes a preparation-only crash from an
    ambiguous external create. Both retain the consumed business-key tombstone.
    """
    native.require_host_limits()
    job, observation = exact_job(journal, binding)
    if observation['cleanup_verified'] is not True:
        attempted = journal.db.execute("SELECT 1 FROM events WHERE job_id=? AND state='scan_create_attempted'",
                                       (job['id'],)).fetchone() is not None
        try:
            journal.cleanup(job, create_confirmed=bool(job['container_id']), create_not_attempted=not attempted)
        except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired):
            journal.record(job, 'cleanup_uncertain')
            raise RuntimeScanHostError('scan reconciliation requires exact native termination') from None
    return {**journal.observe_runtime_scan(binding['scan_id'], binding['role'], binding['stage']),
            'scan_completed': False, 'runtime_approved': False}


def dispatch(journal, binding, worker_body):
    # Reject expired spending authority BEFORE native admission, image lookup or
    # multi-GiB source reads. Full closed-schema validation remains in the journal.
    now = int(time.time())
    if (not isinstance(binding, dict) or type(binding.get('stop_unix')) is not int
            or binding['stop_unix'] != 1792430672 or type(binding.get('deadline')) is not int
            or not now < binding['deadline'] <= min(now + 270, binding['stop_unix'] - 1)):
        raise RuntimeScanHostError('scan hosting authority is expired or invalid')
    native.require_host_limits()
    journal.require_available()
    isolation.ensure_toolchain_image(binding['image_id'])
    paths = input_plan(journal, binding)
    job = journal.begin_runtime_scan(binding)
    create_attempted = create_confirmed = False
    signal.alarm(max(1, binding['deadline'] - int(time.time())))
    try:
        directory, mounts = prepare(journal, job, binding, paths, worker_body)
        if time.time() >= binding['deadline']:
            raise RuntimeScanHostError('scan authority expired before create')
        with journal.db:
            journal.db.execute("INSERT INTO events(job_id,state,observed_at) VALUES(?,'scan_create_attempted',?)",
                               (job['id'], int(time.time())))
        create_attempted = True
        cid = isolation.docker(*create_arguments(journal, job, mounts)).decode().strip()
        if not re.fullmatch('[a-f0-9]{64}', cid):
            raise RuntimeScanHostError('scan create outcome is uncertain')
        create_confirmed = True
        journal.record(job, 'running', cid)
        verify_container(journal, job, cid, mounts)
        response = isolation.execute_bounded(['start', '-a', cid], max(1, binding['deadline'] - int(time.time())),
            output_limit=16384, on_started=lambda child: publish_readiness(journal, job, cid, mounts, binding, directory, child))
    finally:
        signal.alarm(0)
        try:
            journal.cleanup(job, create_confirmed=create_confirmed, create_not_attempted=not create_attempted)
        except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired):
            journal.record(job, 'cleanup_uncertain')
            raise RuntimeScanHostError('scan native cleanup remains uncertain') from None
    if response['success'] is not True or response['output_truncated'] is not False:
        raise RuntimeScanHostError('scan worker did not return bounded successful evidence')
    result = recover(journal, binding)
    retained = {key: value for key, value in result.items()
                if key not in {'job_id', 'native_proof_sha256', 'report_custody_verified'}}
    retained['native_cleanup_verified'] = False
    if worker.metadata(response['stdout'].encode(), 16384) != retained:
        raise RuntimeScanHostError('scan stream differs from retained output')
    return result
