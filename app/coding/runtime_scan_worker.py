"""Fixed keyless scanner worker; host readiness/cleanup remain independent.

No application imports, model-selected commands, keys, shell, registry lookup or
Docker socket. A trusted launcher supplies the read-only per-job input/proof mount.
Inventory is not a vulnerability assessment; neither grants runtime approval.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import selectors
import signal
import socket
import stat
import subprocess  # nosec B404
import sys
import time
import uuid


INPUT = Path('/gca/input')
OUTPUT = Path('/gca/output')
CACHE = Path('/gca/cache')  # Host stages one private copy, not a second multi-GiB copy.
# A fresh exclusive directory on the independently checked per-job executable tmpfs.
TMP_EXEC = Path('/tmp/gca-trivy')  # nosec B108
MAX_REPORT = 4 * 1024**2
MAX_DATA = 2 * 1024**3
MAX_LOG = 256000
SCANNER_BYTES = 172765344
SCANNER_SHA = '93f9da8e4ba5e0c1c76d8234ed2494cf9afb0a96fd21953e424bb795f3299b8e'
ROLES = {
    'nginx_static_v1': ('8f84ed99befc3891b8f329c5c202785278a2cfb7c25107d57fb2a134a3117433', 'alpine'),
    'temurin_java17_v1': ('af3852c35f8339ab7beba6f17b7fc15c4aca4d72979c5f09776c9ff74385058f', 'ubuntu'),
}
SAFE_ENV = {'PATH', 'HOME', 'CI', 'LANG', 'LC_ALL', 'HOSTNAME', 'PYTHONDONTWRITEBYTECODE',
            'PYTHONUNBUFFERED', 'PYTHONUTF8'}


class ScanWorkerError(RuntimeError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ScanWorkerError('duplicate scanner metadata field')
        result[key] = value
    return result


def metadata(body, maximum=MAX_REPORT):
    if not isinstance(body, bytes) or not 1 <= len(body) <= maximum:
        raise ScanWorkerError('scanner metadata exceeds bound')
    try:
        value = json.loads(body, object_pairs_hook=unique_object,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise ScanWorkerError('scanner metadata is invalid') from None
    if not isinstance(value, dict):
        raise ScanWorkerError('scanner metadata must be an object')
    return value


def protected_stream(path):
    # The fixed host gives this UID private files on an independently verified
    # read-only input mount. Do not resolve away links before inspecting them.
    path = Path(path)
    root = INPUT if path.is_relative_to(INPUT) else CACHE if path.is_relative_to(CACHE) else None
    if root is None or '..' in path.parts:
        raise ScanWorkerError('scanner input path is not fixed')
    for entry in (path, *path.parents):
        if entry == root.parent:
            break
        info = entry.lstat()
        if entry.is_symlink() or info.st_uid != 65532 or info.st_mode & 0o022:
            raise ScanWorkerError('scanner input ownership differs')
    before = path.lstat()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o177
                or info.st_uid != 65532 or not 0 <= info.st_size <= MAX_DATA
                or (info.st_dev, info.st_ino, info.st_mode, info.st_size)
                != (before.st_dev, before.st_ino, before.st_mode, before.st_size)):
            raise ScanWorkerError('scanner input inode differs')
        return os.fdopen(fd, 'rb')
    except BaseException:
        os.close(fd)
        raise


def read_small(path, maximum):
    with protected_stream(path) as stream:
        if os.fstat(stream.fileno()).st_size > maximum:
            raise ScanWorkerError('scanner small input exceeds bound')
        body = stream.read(maximum + 1)
    if len(body) > maximum:
        raise ScanWorkerError('scanner small input changed')
    return body


def verify_files(files, binding):
    if not isinstance(files, dict) or not 5 <= len(files) <= 72:
        raise ScanWorkerError('scanner file inventory is invalid')
    stage = binding['stage']
    required = {'scanner.bin', 'config.yaml', 'ignorefile', 'database/metadata.json',
                'database/' + ('trivy-java.db' if stage == 'inventory' else 'trivy.db')}
    if stage == 'inventory':
        required |= {'image/oci-layout', 'image/index.json',
                     'image/blobs/sha256/' + binding['manifest_sha256']}
    else:
        required.add('inventory.cdx.json')
    if not required <= set(files):
        raise ScanWorkerError('scanner required input is missing')
    total = 0
    for name, item in files.items():
        if (not isinstance(name, str) or name not in required and not
                (stage == 'inventory' and re.fullmatch(r'image/blobs/sha256/[a-f0-9]{64}', name))
                or not isinstance(item, dict) or set(item) != {'sha256', 'bytes'}
                or not isinstance(item['sha256'], str) or not re.fullmatch('[a-f0-9]{64}', item['sha256'])
                or type(item['bytes']) is not int or not 0 <= item['bytes'] <= MAX_DATA):
            raise ScanWorkerError('scanner input descriptor differs')
        total += item['bytes']
        if total > 2040 * 1024**2:
            raise ScanWorkerError('scanner input volume bound exceeded')
        checksum = hashlib.sha256()
        with protected_stream(data_path(name, stage)) as stream:
            if os.fstat(stream.fileno()).st_size != item['bytes']:
                raise ScanWorkerError('scanner input size differs')
            while body := stream.read(1024**2):
                checksum.update(body)
                if stream.tell() > item['bytes']:
                    raise ScanWorkerError('scanner input grew beyond its bound')
        if checksum.hexdigest() != item['sha256']:
            raise ScanWorkerError('scanner input fingerprint differs')
    if files['scanner.bin'] != {'sha256': SCANNER_SHA, 'bytes': SCANNER_BYTES}:
        raise ScanWorkerError('scanner executable pin differs')
    if files['config.yaml']['sha256'] != hashlib.sha256(b'{}\n').hexdigest() or files['config.yaml']['bytes'] != 3:
        raise ScanWorkerError('scanner config is not the fixed empty configuration')
    if files['ignorefile'] != {'sha256': hashlib.sha256(b'').hexdigest(), 'bytes': 0}:
        raise ScanWorkerError('scanner exclusions are not empty')
    if stage == 'assessment' and files['inventory.cdx.json']['sha256'] != binding['inventory_sha256']:
        raise ScanWorkerError('scanner inventory lineage differs')
    if stage == 'inventory':
        manifest = metadata(read_small(INPUT / ('image/blobs/sha256/' + binding['manifest_sha256']), 1024**2))
        if manifest.get('schemaVersion') != 2 or not isinstance(manifest.get('layers'), list) or not 1 <= len(manifest['layers']) <= 64:
            raise ScanWorkerError('scanner OCI manifest is unsupported')
        names = set(required)
        for descriptor in [manifest.get('config'), *manifest['layers']]:
            if (not isinstance(descriptor, dict) or not isinstance(descriptor.get('digest'), str)
                    or not re.fullmatch('sha256:[a-f0-9]{64}', descriptor['digest'])
                    or type(descriptor.get('size')) is not int):
                raise ScanWorkerError('scanner OCI descriptor differs')
            name = 'image/blobs/sha256/' + descriptor['digest'][7:]
            if files.get(name) != {'sha256': descriptor['digest'][7:], 'bytes': descriptor['size']}:
                raise ScanWorkerError('scanner OCI blob lineage differs')
            names.add(name)
        if set(files) != names:
            raise ScanWorkerError('scanner OCI layout has unbound blobs')
        if files['image/blobs/sha256/' + binding['manifest_sha256']]['sha256'] != binding['manifest_sha256']:
            raise ScanWorkerError('scanner OCI manifest digest differs')
        index = metadata(read_small(INPUT / 'image/index.json', 16384), 16384)
        descriptors = index.get('manifests')
        if (index.get('schemaVersion') != 2 or not isinstance(descriptors, list) or len(descriptors) != 1
                or descriptors[0].get('digest') != 'sha256:' + binding['manifest_sha256']
                or descriptors[0].get('size') != files['image/blobs/sha256/' + binding['manifest_sha256']]['bytes']):
            raise ScanWorkerError('scanner OCI index selects a different image')
    return total


def data_path(name, stage):
    if name.startswith('database/'):
        return CACHE / ('java-db' if stage == 'inventory' else 'db') / name.removeprefix('database/')
    return INPUT / name


def verify_readiness(binding, proof):
    from_fields = {'version', 'scan_id', 'role', 'stage', 'image_id', 'manifest_sha256',
                   'scanner_sha256', 'signature_receipt_sha256', 'database_catalog_sha256',
                   'inventory_sha256', 'controller_sha256', 'stop_unix', 'deadline'}
    now = int(time.time())
    if (not isinstance(binding, dict) or set(binding) != from_fields
            or type(binding['version']) is not int or binding['version'] != 1
            or not isinstance(binding['role'], str) or binding['role'] not in ROLES
            or binding['stage'] not in ('inventory', 'assessment')
            or binding['manifest_sha256'] != ROLES[binding['role']][0]
            or binding['scanner_sha256'] != SCANNER_SHA
            or binding['image_id'] != 'sha256:6b1f85a08c199d29d5b6d71ab9c27bd5b3b393492e01216a15758ff69c4be8b8'
            or binding['signature_receipt_sha256'] != '712dc1f6a1d34e65d2c54390ba25da4942220e6bdbe9bf5ea8786b9927cfa112'
            or binding['database_catalog_sha256'] != '79734a08f6dccc4623e6ef7d7bc523a05d4071d57370bde7a5f2cfd4c5086fd1'
            or (binding['stage'] == 'inventory' and binding['inventory_sha256'] is not None)
            or (binding['stage'] == 'assessment' and (not isinstance(binding['inventory_sha256'], str)
                or not re.fullmatch('[a-f0-9]{64}', binding['inventory_sha256'])))
            or not isinstance(binding['controller_sha256'], str) or not re.fullmatch('[a-f0-9]{64}', binding['controller_sha256'])
            or type(binding['deadline']) is not int or not now < binding['deadline'] <= now + 270
            or binding['stop_unix'] != 1792430672 or binding['deadline'] >= binding['stop_unix']):
        raise ScanWorkerError('scanner binding is not the fixed current job')
    try:
        if str(uuid.UUID(binding['scan_id'])) != binding['scan_id']:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise ScanWorkerError('scanner logical identity is invalid') from None
    numbers = ('memory_max', 'swap_max', 'cpu_quota', 'cpu_period', 'tasks_max',
               'aggregate_memory_max', 'aggregate_swap_max', 'aggregate_cpu_quota',
               'aggregate_cpu_period', 'aggregate_tasks_max', 'native_pid', 'native_start_ticks',
               'native_seccomp', 'native_cap_eff', 'native_no_new_privs', 'observed_at_unix', 'expires_at_unix')
    if (not isinstance(proof, dict) or any(type(proof.get(key)) is not int for key in numbers)
            or set(proof) != set(numbers) | {'version', 'runtime', 'native_profile', 'binding_hash',
                                            'container_id', 'native_cgroup', 'helper_pids'}
            or proof.get('version') != 2 or proof.get('runtime') != 'runsc'
            or proof.get('native_profile') != 'runsc_release20260921_directfs_v2'
            or proof.get('binding_hash') != hashlib.sha256(canonical(binding)).hexdigest()
            or not isinstance(proof.get('container_id'), str) or not re.fullmatch('[a-f0-9]{64}', proof['container_id'])
            or proof.get('native_cgroup') != '/gcapackaging.slice/docker-' + proof['container_id'] + '.scope'
            or not 0 < proof['memory_max'] <= 3 * 1024**3 or proof['swap_max'] != 0
            or not 0 < proof['cpu_quota'] <= proof['cpu_period'] or not 0 < proof['tasks_max'] <= 64
            or not 0 < proof['aggregate_memory_max'] <= 3 * 1024**3 or proof['aggregate_swap_max'] != 0
            or not 0 < proof['aggregate_cpu_quota'] <= proof['aggregate_cpu_period']
            or not 0 < proof['aggregate_tasks_max'] <= 128 or proof['native_pid'] <= 1
            or proof['native_start_ticks'] <= 0 or proof['native_seccomp'] != 2
            or proof['native_cap_eff'] != 0x8001F or proof['native_no_new_privs'] != 1
            or not isinstance(proof['helper_pids'], list) or not 1 <= len(proof['helper_pids']) <= 128
            or any(type(pid) is not int or pid <= 1 for pid in proof['helper_pids'])
            or len(proof['helper_pids']) != len(set(proof['helper_pids']))
            or not now - 15 <= proof['observed_at_unix'] <= now
            or proof['expires_at_unix'] != binding['deadline']):
        raise ScanWorkerError('scanner native readiness is not bound/current/enforced')


def observation(path):
    with path.open() as stream:
        body = stream.read(65537)
    if len(body) > 65536:
        raise ScanWorkerError('scanner guest observation exceeds bound')
    return body


def guest_guard():
    if sys.platform != 'linux' or os.geteuid() != 65532 or set(os.environ) - SAFE_ENV:
        raise ScanWorkerError('scanner requires keyless unprivileged Linux')
    status = dict(line.split(':', 1) for line in observation(Path('/proc/self/status')).splitlines() if ':' in line)
    if int(status['CapEff'].strip(), 16) != 0 or status['NoNewPrivs'].strip() != '1':
        raise ScanWorkerError('scanner guest privilege boundary differs')
    interfaces = [line.split(':')[0].strip() for line in observation(Path('/proc/net/dev')).splitlines() if ':' in line]
    if interfaces != ['lo'] or [name for _, name in socket.if_nameindex()] != ['lo']:
        raise ScanWorkerError('scanner guest network is not isolated')
    mounts = [line.split() for line in observation(Path('/proc/self/mountinfo')).splitlines()]
    for path, option in (('/', 'ro'), (str(INPUT), 'ro'), (str(CACHE), 'rw')):
        selected = [item for item in mounts if len(item) > 6 and item[4] == path]
        if len(selected) != 1 or option not in selected[0][5].split(','):
            raise ScanWorkerError('scanner read-only boundary differs')
    temporary = [item for item in mounts if len(item) > 6 and item[4] == str(TMP_EXEC.parent)]
    if (len(temporary) != 1 or not {'rw', 'nosuid', 'nodev'} <= set(temporary[0][5].split(','))
            or 'noexec' in temporary[0][5].split(',') or temporary[0][temporary[0].index('-') + 1] != 'tmpfs'):
        raise ScanWorkerError('scanner executable staging is not the sandbox tmpfs')


def arguments(stage):
    common = [str(TMP_EXEC / 'trivy'), '--cache-dir', str(CACHE),
              '--config', str(INPUT / 'config.yaml'), '--timeout', '180s']
    flags = ['--offline-scan', '--skip-db-update', '--skip-java-db-update',
             '--skip-version-check', '--skip-vex-repo-update', '--disable-telemetry',
             '--ignorefile', str(INPUT / 'ignorefile'), '--pkg-types', 'os,library',
             '--no-progress', '--output', str(OUTPUT / 'report.json')]
    if stage == 'inventory':
        return common + ['image'] + flags + ['--skip-check-update', '--parallel', '1',
               '--format', 'cyclonedx', '--input', str(INPUT / 'image')]
    if stage == 'assessment':
        return common + ['sbom'] + flags + ['--scanners', 'vuln', '--list-all-pkgs',
               '--format', 'json', str(INPUT / 'inventory.cdx.json')]
    raise ScanWorkerError('unknown scanner stage')


def report_metrics(body, binding):
    report = metadata(body)
    if binding['stage'] == 'inventory':
        components = report.get('components')
        generator = (report.get('metadata') or {}).get('tools')
        tools = generator if isinstance(generator, list) else generator.get('components', []) if isinstance(generator, dict) else []
        if (report.get('bomFormat') != 'CycloneDX' or report.get('specVersion') not in ('1.4', '1.5', '1.6', '1.7')
                or not isinstance(components, list) or not 1 <= len(components) <= 50000
                or not any(isinstance(x, dict) and str(x.get('name', '')).lower() == 'trivy'
                           and x.get('version') == '0.75.0' for x in tools)):
            raise ScanWorkerError('scanner inventory is empty, unsupported or not from the pinned tool')
        packages = [x for x in components if isinstance(x, dict) and x.get('type') == 'library']
        if any(not isinstance(x.get('purl'), str) or not x['purl'].startswith('pkg:')
               or not isinstance(x.get('name'), str) or not x['name']
               or not isinstance(x.get('version'), str) or not x['version'] for x in packages):
            raise ScanWorkerError('scanner inventory package identity is malformed')
        if not packages:
            raise ScanWorkerError('scanner inventory has no identifiable packages')
        return {'stage': 'inventory', 'components': len(components), 'packages': len(packages),
                'vulnerability_assessment_completed': False, 'runtime_approved': False}
    results = report.get('Results')
    family = ((report.get('Metadata') or {}).get('OS') or {}).get('Family')
    if report.get('SchemaVersion') != 2 or not isinstance(results, list) or not results or family != ROLES[binding['role']][1]:
        raise ScanWorkerError('scanner assessment lacks the expected OS/package coverage')
    packages = findings = os_packages = 0
    counts = {level: 0 for level in ('UNKNOWN', 'LOW', 'MEDIUM', 'HIGH', 'CRITICAL')}
    for result in results:
        if not isinstance(result, dict) or result.get('Class') not in ('os-pkgs', 'lang-pkgs'):
            raise ScanWorkerError('scanner result coverage is unsupported')
        entries, vulnerabilities = result.get('Packages') or [], result.get('Vulnerabilities') or []
        if not isinstance(entries, list) or not isinstance(vulnerabilities, list):
            raise ScanWorkerError('scanner package/finding inventory is malformed')
        packages += len(entries)
        if any(not isinstance(x, dict) or not isinstance(x.get('Name'), str) or not x['Name']
               or not isinstance(x.get('Version'), str) or not x['Version'] for x in entries):
            raise ScanWorkerError('scanner assessed package identity is malformed')
        if result['Class'] == 'os-pkgs' and result.get('Type') == family:
            os_packages += len(entries)
        for item in vulnerabilities:
            if (not isinstance(item, dict) or not isinstance(item.get('VulnerabilityID'), str) or not item['VulnerabilityID']
                    or not isinstance(item.get('PkgName'), str) or not item['PkgName']
                    or item.get('Severity') not in counts):
                raise ScanWorkerError('scanner finding is malformed')
            findings += 1
            counts[item['Severity']] += 1
    if not packages or not os_packages:
        raise ScanWorkerError('scanner assessment has no package coverage')
    return {'stage': 'assessment', 'packages': packages, 'findings': findings, 'severity_counts': counts,
            'inventory_based_assessment_completed': True, 'runtime_approved': False,
            'vendor_binary_coverage_requires_review': binding['role'] == 'temurin_java17_v1'}


def database_freshness(body, stage, now):
    value = metadata(body, 16384)
    if (set(value) - {'Version', 'UpdatedAt', 'NextUpdate', 'DownloadedAt'}
            or type(value.get('Version')) is not int or value['Version'] != (1 if stage == 'inventory' else 2)):
        raise ScanWorkerError('scanner database schema differs')
    try:
        updated = datetime.fromisoformat(value['UpdatedAt'].replace('Z', '+00:00'))
        next_update = datetime.fromisoformat(value['NextUpdate'].replace('Z', '+00:00'))
        if updated.tzinfo is None or next_update.tzinfo is None or now.tzinfo is None:
            raise ValueError()
        if not (now - timedelta(hours=24) <= updated <= now + timedelta(minutes=5)
                and updated < next_update <= updated + timedelta(hours=72) and now < next_update):
            raise ValueError()
        if 'DownloadedAt' in value:
            downloaded = datetime.fromisoformat(value['DownloadedAt'].replace('Z', '+00:00'))
            if downloaded.tzinfo is None or downloaded > now + timedelta(minutes=5):
                raise ValueError()
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ScanWorkerError('scanner database freshness expired or invalid') from None
    return value


def copy_checked(name, destination, descriptor, *, executable=False):
    # Destinations are fixed by the reviewed worker, never from the input JSON.
    if destination.is_symlink() or destination.exists():
        raise ScanWorkerError('scanner destination is not a fresh private file')
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    checksum = hashlib.sha256()
    count = 0
    try:
        with os.fdopen(fd, 'wb', closefd=False) as target, protected_stream(INPUT / name) as source:
            while chunk := source.read(1024**2):
                count += len(chunk)
                if count > descriptor['bytes']:
                    raise ScanWorkerError('scanner source changed while copying')
                target.write(chunk)
                checksum.update(chunk)
            target.flush()
            os.fsync(fd)
        if count != descriptor['bytes'] or checksum.hexdigest() != descriptor['sha256']:
            raise ScanWorkerError('scanner copy fingerprint differs')
        if executable:
            os.fchmod(fd, 0o700)  # Only the sandbox-internal /tmp copy, never host data.
    finally:
        os.close(fd)


def run_bounded(arguments, deadline):
    """No shell/ambient env. Bound BOTH output streams and the entire child group."""
    environment = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': '/nonexistent',
                   'LANG': 'C.UTF-8', 'GOMAXPROCS': '1', 'TMPDIR': str(OUTPUT / 'tmp')}
    process = subprocess.Popen(arguments, cwd=OUTPUT, env=environment,  # nosec B603
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True)
    logs = {'stdout': bytearray(), 'stderr': bytearray()}
    try:
        with selectors.DefaultSelector() as selector:
            for name in logs:
                stream = getattr(process, name)
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            while selector.get_map():
                if time.monotonic() >= deadline:
                    raise ScanWorkerError('scanner child deadline exceeded')
                for key, _ in selector.select(min(0.2, max(0, deadline - time.monotonic()))):
                    body = os.read(key.fileobj.fileno(), min(65536, MAX_LOG - len(logs[key.data]) + 1))
                    if not body:
                        selector.unregister(key.fileobj)
                        continue
                    logs[key.data].extend(body)
                    if len(logs[key.data]) > MAX_LOG:
                        raise ScanWorkerError('scanner child output exceeded bound')
            return process.wait(timeout=max(0.001, deadline - time.monotonic())), logs
    except BaseException:
        # Retain bounded diagnostic prefixes even for timeout/output overflow;
        # these are explicitly incomplete logs, never a success or full transcript.
        for name, body in logs.items():
            write_output(name + '.partial.bin', body[:MAX_LOG])
        raise
    finally:
        # A child can close its pipes while descendants still run. Kill the exact
        # session even on ordinary completion; host-native cleanup is separate.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
        for name in logs:
            getattr(process, name).close()


def write_output(name, body):
    fd = os.open(OUTPUT / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())


def read_report():
    path = OUTPUT / 'report.json'
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 65532 or info.st_nlink != 1
                or info.st_mode & 0o177
                or not 1 <= info.st_size <= MAX_REPORT):
            raise ScanWorkerError('scanner output report is absent, unsafe or oversized')
        body = stream.read(MAX_REPORT + 1)
    if len(body) > MAX_REPORT:
        raise ScanWorkerError('scanner report changed during read')
    return body


def apply_process_limits():
    # The trusted container already has nofile=64:64. Never try to raise its
    # hard limit from the unprivileged guest; that would stop before scanning.
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_DATA, MAX_DATA))
    resource.setrlimit(resource.RLIMIT_CPU, (220, 221))


def main():
    guest_guard()
    request = metadata(read_small(INPUT / 'request.json', 16384), 16384)
    if set(request) != {'version', 'binding', 'files'} or type(request['version']) is not int or request['version'] != 1:
        raise ScanWorkerError('scanner request is not fixed')
    deadline = time.monotonic() + 12
    while not (INPUT / 'host-proof.json').exists():
        if time.monotonic() >= deadline:
            raise ScanWorkerError('scanner host readiness did not arrive')
        time.sleep(0.1)
    binding = request['binding']
    verify_readiness(binding, metadata(read_small(INPUT / 'host-proof.json', 16384), 16384))
    verify_files(request['files'], binding)
    database_path = data_path('database/metadata.json', binding['stage'])
    database_body = read_small(database_path, 16384)
    database_freshness(database_body, binding['stage'], datetime.now(timezone.utc))
    if OUTPUT.is_symlink() or OUTPUT.stat().st_uid != 65532 or OUTPUT.stat().st_mode & 0o077:
        raise ScanWorkerError('scanner output is not private')
    apply_process_limits()
    # Execution happens only on this sandbox-internal tmpfs. The host's retained
    # binary and private data filesystem remain non-executable.
    binary_root = TMP_EXEC
    binary_root.mkdir(mode=0o700)
    copy_checked('scanner.bin', binary_root / 'trivy', request['files']['scanner.bin'], executable=True)
    (OUTPUT / 'tmp').mkdir(mode=0o700)
    started = time.monotonic()
    remaining = binding['deadline'] - time.time() - 5
    if remaining <= 0:
        raise ScanWorkerError('scanner stage authority expired during preparation')
    exit_code, logs = run_bounded(arguments(binding['stage']), started + min(185, remaining))
    for name, body in logs.items():
        write_output(name + '.bin', body)
    if exit_code != 0:
        raise ScanWorkerError('scanner returned a nonzero exit; retained logs require diagnosis')
    body = read_report()
    measured = report_metrics(body, binding)
    # Rehash the actual private cache after the child exits. Mutable cache bytes
    # cannot silently become evidence for the immutable original database pin.
    verify_files(request['files'], binding)
    database_freshness(database_body, binding['stage'], datetime.now(timezone.utc))
    if time.time() >= binding['deadline']:
        raise ScanWorkerError('scanner stage authority expired before result publication')
    result = {'version': 1, 'binding_hash': hashlib.sha256(canonical(binding)).hexdigest(),
              'report_sha256': hashlib.sha256(body).hexdigest(), 'report_bytes': len(body),
              'scanner_exit_code': exit_code, 'scanner_duration_ms': int((time.monotonic() - started) * 1000),
              'scanner_executed': True, 'native_cleanup_verified': False, 'scan_completed': False,
              'metrics': measured, 'runtime_approved': False}
    write_output('worker-result.json', canonical(result))
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'worker_stopped': True, 'error_class': type(error).__name__,
                          'detail': str(error) if isinstance(error, ScanWorkerError) else 'scanner worker failed',
                          'runtime_approved': False}, sort_keys=True))
        raise SystemExit(1)
