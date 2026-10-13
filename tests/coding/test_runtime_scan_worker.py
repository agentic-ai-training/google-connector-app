from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from uuid import uuid4

import pytest

from app.coding import runtime_scan_worker as worker


def binding(stage='inventory'):
    return {'version': 1, 'scan_id': str(uuid4()), 'role': 'nginx_static_v1', 'stage': stage,
            'image_id': 'sha256:6b1f85a08c199d29d5b6d71ab9c27bd5b3b393492e01216a15758ff69c4be8b8',
            'manifest_sha256': worker.ROLES['nginx_static_v1'][0], 'scanner_sha256': worker.SCANNER_SHA,
            'signature_receipt_sha256': '712dc1f6a1d34e65d2c54390ba25da4942220e6bdbe9bf5ea8786b9927cfa112',
            'database_catalog_sha256': '79734a08f6dccc4623e6ef7d7bc523a05d4071d57370bde7a5f2cfd4c5086fd1',
            'inventory_sha256': None if stage == 'inventory' else 'd' * 64,
            'controller_sha256': 'e' * 64, 'stop_unix': 1792430672, 'deadline': int(time.time()) + 180}


def readiness(value):
    return {'version': 2, 'runtime': 'runsc', 'native_profile': 'runsc_release20260921_directfs_v2',
            'binding_hash': hashlib.sha256(worker.canonical(value)).hexdigest(), 'container_id': 'f' * 64,
            'native_pid': 123, 'native_start_ticks': 456,
            'native_cgroup': '/gcapackaging.slice/docker-' + 'f' * 64 + '.scope',
            'memory_max': 3 * 1024**3, 'swap_max': 0, 'cpu_quota': 100000, 'cpu_period': 100000,
            'tasks_max': 64, 'aggregate_memory_max': 3 * 1024**3, 'aggregate_swap_max': 0,
            'aggregate_cpu_quota': 100000, 'aggregate_cpu_period': 100000, 'aggregate_tasks_max': 128,
            'native_seccomp': 2, 'native_cap_eff': 0x8001F, 'native_no_new_privs': 1,
            'helper_pids': [123, 456], 'observed_at_unix': int(time.time()), 'expires_at_unix': value['deadline']}


def inventory():
    return {'bomFormat': 'CycloneDX', 'specVersion': '1.6',
            'metadata': {'tools': {'components': [{'name': 'Trivy', 'version': '0.75.0'}]}},
            'components': [{'type': 'library', 'name': 'busybox', 'version': '1.2', 'purl': 'pkg:apk/busybox@1.2'}]}


@pytest.fixture
def files(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, 'INPUT', tmp_path)
    cache = tmp_path / 'private-cache'
    monkeypatch.setattr(worker, 'CACHE', cache)
    scanner = b'synthetic-scanner-data-never-executed'
    monkeypatch.setattr(worker, 'SCANNER_SHA', hashlib.sha256(scanner).hexdigest())
    monkeypatch.setattr(worker, 'SCANNER_BYTES', len(scanner))
    bodies = {'scanner.bin': scanner, 'config.yaml': b'{}\n', 'ignorefile': b'',
              'database/metadata.json': b'{"Version":1}', 'database/trivy-java.db': b'synthetic-db'}
    descriptors = []
    for body in (b'config', b'layer'):
        checksum = hashlib.sha256(body).hexdigest()
        name = 'image/blobs/sha256/' + checksum
        bodies[name] = body
        descriptors.append({'digest': 'sha256:' + checksum, 'size': len(body)})
    manifest = worker.canonical({'schemaVersion': 2, 'config': descriptors[0], 'layers': [descriptors[1]]})
    checksum = hashlib.sha256(manifest).hexdigest()
    monkeypatch.setattr(worker, 'ROLES', {'nginx_static_v1': (checksum, 'alpine')})
    bodies.update({'image/blobs/sha256/' + checksum: manifest,
                   'image/oci-layout': b'{"imageLayoutVersion":"1.0.0"}',
                   'image/index.json': worker.canonical({'schemaVersion': 2, 'manifests': [
                       {'digest': 'sha256:' + checksum, 'size': len(manifest)}]})})
    for name, body in bodies.items():
        path = worker.data_path(name, 'inventory')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        path.chmod(0o600)
    @contextmanager
    def synthetic_stream(path):
        # UID/mount enforcement is not native proof on macOS. Actual regular
        # file bytes/size/digest and OCI lineage are exercised here.
        path = Path(path)
        assert path.is_relative_to(tmp_path) and '..' not in path.parts and not path.is_symlink()
        with path.open('rb') as stream:
            yield stream
    monkeypatch.setattr(worker, 'protected_stream', synthetic_stream)
    return {name: {'sha256': hashlib.sha256(body).hexdigest(), 'bytes': len(body)}
            for name, body in bodies.items()}


def test_exact_file_hashes_and_oci_graph_are_required(files):
    assert worker.verify_files(files, binding()) == sum(item['bytes'] for item in files.values())
    (worker.INPUT / 'scanner.bin').write_bytes(b'x' * worker.SCANNER_BYTES)
    with pytest.raises(worker.ScanWorkerError, match='fingerprint'):
        worker.verify_files(files, binding())


def test_foreign_oci_blob_and_caller_filter_are_rejected(files):
    for name in ('../../escape', 'ignore-policy.rego', 'image/blobs/sha256/' + 'a' * 64):
        changed = {**files, name: {'sha256': hashlib.sha256(b'extra').hexdigest(), 'bytes': 5}}
        if name.startswith('image/'):
            (worker.INPUT / name).write_bytes(b'extra')
        with pytest.raises(worker.ScanWorkerError):
            worker.verify_files(changed, binding())


def test_private_cache_is_single_copy_and_mutation_invalidates_evidence(files):
    assert not (worker.INPUT / 'database').exists()
    path = worker.data_path('database/trivy-java.db', 'inventory')
    assert path.is_relative_to(worker.CACHE)
    worker.verify_files(files, binding())
    path.write_bytes(b'x' * files['database/trivy-java.db']['bytes'])
    with pytest.raises(worker.ScanWorkerError, match='fingerprint'):
        worker.verify_files(files, binding())


def test_oci_index_cannot_select_a_different_manifest(files):
    path = worker.INPUT / 'image/index.json'
    value = json.loads(path.read_bytes())
    value['manifests'][0]['digest'] = 'sha256:' + 'b' * 64
    body = worker.canonical(value)
    path.write_bytes(body)
    changed = {**files, 'image/index.json': {'sha256': hashlib.sha256(body).hexdigest(), 'bytes': len(body)}}
    with pytest.raises(worker.ScanWorkerError, match='different image'):
        worker.verify_files(changed, binding())


def test_exact_copy_is_exclusive_and_checks_content(files, tmp_path):
    destination = tmp_path / 'copy'
    worker.copy_checked('scanner.bin', destination, files['scanner.bin'])
    assert hashlib.sha256(destination.read_bytes()).hexdigest() == worker.SCANNER_SHA
    assert not destination.stat().st_mode & 0o177
    with pytest.raises(worker.ScanWorkerError, match='fresh private file'):
        worker.copy_checked('scanner.bin', destination, files['scanner.bin'])
    with pytest.raises(worker.ScanWorkerError, match='fingerprint'):
        worker.copy_checked('scanner.bin', tmp_path / 'bad-copy', {**files['scanner.bin'], 'sha256': '0' * 64})


def assessment():
    return {'SchemaVersion': 2, 'Metadata': {'OS': {'Family': 'alpine'}},
            'Results': [{'Class': 'os-pkgs', 'Type': 'alpine',
                         'Packages': [{'Name': 'busybox', 'Version': '1.2'}],
                         'Vulnerabilities': [{'VulnerabilityID': 'CVE-test', 'PkgName': 'busybox',
                                              'InstalledVersion': '1.2', 'Severity': 'HIGH'}]}]}


def test_stage_commands_have_distinct_supported_flags_and_no_filters():
    image, sbom = worker.arguments('inventory'), worker.arguments('assessment')
    for command in (image, sbom):
        for flag in ('--offline-scan', '--skip-db-update', '--skip-java-db-update', '--disable-telemetry',
                     '--skip-version-check', '--skip-vex-repo-update', '--ignorefile', '--config'):
            assert flag in command
        assert not {'--server', '--ignore-unfixed', '--severity', '--template', '--enable-modules'} & set(command)
    assert '--parallel' in image and '--skip-check-update' in image
    assert '--parallel' not in sbom and '--skip-check-update' not in sbom
    assert sbom[sbom.index('--scanners') + 1] == 'vuln'
    assert '--list-all-pkgs' in sbom
    assert image[image.index('--cache-dir') + 1] == str(worker.CACHE)
    with pytest.raises(worker.ScanWorkerError):
        worker.arguments('shell')


@pytest.mark.parametrize('fault', [
    {'binding_hash': 'a' * 64}, {'memory_max': 4 * 1024**3}, {'swap_max': 1}, {'tasks_max': 65},
    {'cpu_quota': 100001}, {'aggregate_tasks_max': 129}, {'native_no_new_privs': 0},
    {'helper_pids': [123, 123]}, {'observed_at_unix': 0}, {'runtime': 'runc'}, {'extra': True},
])
def test_forged_or_stale_native_envelope_is_rejected(fault):
    value = binding()
    worker.verify_readiness(value, readiness(value))
    with pytest.raises(worker.ScanWorkerError, match='readiness'):
        worker.verify_readiness(value, {**readiness(value), **fault})


def test_wrong_binary_or_role_cannot_use_an_otherwise_matching_proof():
    for field, other in (('scanner_sha256', 'a' * 64), ('role', 'postgres'), ('stage', 'shell'),
                         ('image_id', 'sha256:' + 'b' * 64)):
        value = {**binding(), field: other}
        with pytest.raises(worker.ScanWorkerError):
            worker.verify_readiness(value, readiness(value))


@pytest.mark.parametrize('stage, parent', [('inventory', 'a' * 64), ('assessment', None),
                                         ('assessment', 'not-a-digest'), ('assessment', True)])
def test_readiness_requires_exact_stage_parent_semantics(stage, parent):
    value = {**binding(stage), 'inventory_sha256': parent}
    with pytest.raises(worker.ScanWorkerError, match='binding'):
        worker.verify_readiness(value, readiness(value))


def test_inventory_is_not_a_vulnerability_scan_or_runtime_approval():
    result = worker.report_metrics(worker.canonical(inventory()), binding())
    assert result['packages'] == 1
    assert result['vulnerability_assessment_completed'] is False and result['runtime_approved'] is False
    for document in ({}, {**inventory(), 'components': []},
                     {**inventory(), 'metadata': {'tools': []}},
                     {**inventory(), 'components': [{'type': 'library', 'purl': True}]}):
        with pytest.raises(worker.ScanWorkerError):
            worker.report_metrics(worker.canonical(document), binding())


def test_assessment_counts_all_findings_but_does_not_approve_runtime():
    result = worker.report_metrics(worker.canonical(assessment()), binding('assessment'))
    assert result['severity_counts']['HIGH'] == 1 and result['findings'] == 1
    assert result['inventory_based_assessment_completed'] and result['runtime_approved'] is False
    report = assessment()
    report['Results'][0]['Vulnerabilities'] = []
    assert worker.report_metrics(worker.canonical(report), binding('assessment'))['findings'] == 0
    report['Results'][0]['Packages'] = []
    with pytest.raises(worker.ScanWorkerError, match='coverage'):
        worker.report_metrics(worker.canonical(report), binding('assessment'))


@pytest.mark.parametrize('fault', ['wrong_os', 'malformed_package', 'wrong_severity', 'unsupported_class', 'no_os_result'])
def test_false_or_incomplete_assessment_is_rejected(fault):
    report = assessment()
    if fault == 'wrong_os':
        report['Metadata']['OS']['Family'] = 'other'
    elif fault == 'malformed_package':
        report['Results'][0]['Packages'] = [{'Name': 'x'}]
    elif fault == 'wrong_severity':
        report['Results'][0]['Vulnerabilities'][0]['Severity'] = 'harmless'
    elif fault == 'unsupported_class':
        report['Results'][0]['Class'] = 'text'
    else:
        report['Results'][0].update(Class='lang-pkgs', Type='jar')
    with pytest.raises(worker.ScanWorkerError):
        worker.report_metrics(worker.canonical(report), binding('assessment'))


def test_vendor_binary_gap_is_explicit_for_java_runtime():
    report = assessment()
    report['Metadata']['OS']['Family'] = 'ubuntu'
    report['Results'][0]['Type'] = 'ubuntu'
    result = worker.report_metrics(worker.canonical(report), {**binding('assessment'), 'role': 'temurin_java17_v1'})
    assert result['vendor_binary_coverage_requires_review'] and not result['runtime_approved']


def test_freshness_is_temporal_not_an_unchecked_receipt():
    now = datetime.now(timezone.utc)
    value = {'Version': 1, 'UpdatedAt': (now - timedelta(hours=1)).isoformat(),
             'NextUpdate': (now + timedelta(hours=23)).isoformat()}
    worker.database_freshness(worker.canonical(value), 'inventory', now)
    for changed in ({'Version': True}, {'Version': 2}, {'UpdatedAt': now.replace(tzinfo=None).isoformat()},
                    {'UpdatedAt': (now - timedelta(hours=25)).isoformat()}, {'NextUpdate': now.isoformat()}):
        with pytest.raises(worker.ScanWorkerError):
            worker.database_freshness(worker.canonical({**value, **changed}), 'inventory', now)


def test_json_bounds_duplicate_fields_and_nan_are_rejected():
    for body in (b'', b'{"a":1,"a":2}', b'{"a":NaN}', b'[]', b'x' * (worker.MAX_REPORT + 1)):
        with pytest.raises(worker.ScanWorkerError):
            worker.metadata(body)


def test_child_has_no_ambient_credentials_and_bounded_output(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, 'OUTPUT', tmp_path)
    monkeypatch.setenv('RUNTIME_API_KEY', 'synthetic-secret-not-a-real-key')
    args = [sys.executable, '-I', '-c', 'import os;print("RUNTIME_API_KEY" in os.environ)']
    code, logs = worker.run_bounded(args, time.monotonic() + 5)
    assert code == 0 and logs['stdout'].strip() == b'False'
    monkeypatch.setattr(worker, 'MAX_LOG', 100)
    with pytest.raises(worker.ScanWorkerError, match='output exceeded'):
        worker.run_bounded([sys.executable, '-I', '-c', 'print("x"*10000)'], time.monotonic() + 5)
    assert (tmp_path / 'stdout.partial.bin').stat().st_size <= 100


def test_child_timeout_is_killed_and_retains_only_partial_diagnostics(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, 'OUTPUT', tmp_path)
    with pytest.raises(worker.ScanWorkerError, match='deadline'):
        worker.run_bounded([sys.executable, '-I', '-c', 'import time;time.sleep(5)'], time.monotonic() + 0.1)
    assert (tmp_path / 'stderr.partial.bin').exists()


def test_scanner_is_never_invoked_on_the_operator_mac():
    if sys.platform == 'linux' and os.geteuid() == 65532:
        pytest.skip('this test specifically checks a non-worker platform')
    with pytest.raises(worker.ScanWorkerError, match='keyless unprivileged Linux'):
        worker.main()


def test_process_limits_do_not_try_to_expand_container_hard_limits(monkeypatch):
    applied = {}
    monkeypatch.setattr(worker.resource, 'setrlimit', lambda which, value: applied.update({which: value}))
    worker.apply_process_limits()
    assert applied[worker.resource.RLIMIT_NOFILE] == (64, 64)
    assert applied[worker.resource.RLIMIT_FSIZE] == (worker.MAX_DATA, worker.MAX_DATA)
    assert applied[worker.resource.RLIMIT_CPU] == (220, 221)
