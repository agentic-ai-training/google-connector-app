"""Real private CAS files and hash/range checks; no registry, approval or host mutation."""
from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import urllib.request

import pytest

from app.coding import oci_packager as oci, runtime_base_acquisition as acquisition
from tests.coding.test_oci_packager import base
from scripts import acquire_runtime_base_candidates as cli


@pytest.fixture
def target(monkeypatch):
    measured = base()
    manifest = json.loads(measured.manifest)
    index = oci.canonical({'schemaVersion': 2, 'mediaType': sorted(acquisition.INDEX_MEDIA)[0], 'manifests': [{
        'mediaType': manifest['mediaType'], 'digest': oci.digest(measured.manifest), 'size': len(measured.manifest),
        'platform': {'os': 'linux', 'architecture': 'amd64'},
    }]})
    catalog = {'repository': 'library/nginx', 'discovery_tag': 'test-not-an-approval',
               'index_digest': oci.digest(index), 'manifest_digest': oci.digest(measured.manifest),
               'config_digest': oci.digest(measured.config), 'layer_count': len(measured.layers),
               'layer_bytes': sum(map(len, measured.layers))}
    monkeypatch.setattr(acquisition, 'CATALOG', {'nginx_static_v1': catalog})
    content = {oci.digest(body): body for body in (measured.config, *measured.layers)}
    class Registry:
        def __init__(self):
            self.ranges, self.requests = [], 0
            self.status, self.wrong_range, self.corrupt, self.interrupt = None, False, False, False
        def manifest(self, checksum):
            return {catalog['index_digest']: index, catalog['manifest_digest']: measured.manifest}[checksum]
        @contextmanager
        def blob(self, checksum, *, offset=0):
            self.requests += 1
            self.ranges.append((checksum, offset))
            body = content[checksum]
            headers = {'Content-Range': 'bytes ' + str(offset) + '-' + str(len(body) - 1) + '/' + str(len(body))}
            if self.wrong_range:
                headers['Content-Range'] = 'bytes 0-1/2'
            response = io.BytesIO((b'x' * len(body) if self.corrupt else body)[offset:])
            response.status = self.status or (206 if offset else 200)
            response.headers = headers
            if self.interrupt:
                original = response.read
                calls = 0
                def fail(size):
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        return original(min(size, 7))
                    raise TimeoutError('synthetic transport interruption')
                response.read = fail
            yield response
    with tempfile.TemporaryDirectory(prefix='.gca-runtime-acquisition-', dir=Path.home()) as directory:
        root = Path(directory)
        root.chmod(0o700)
        yield SimpleNamespace(root=root, measured=measured, manifest=manifest, catalog=catalog,
                              registry=Registry(), content=content)


def test_candidate_acquires_verified_layout_without_granting_approval(target):
    result = acquisition.acquire_candidate(target.root, role='nginx_static_v1', registry=target.registry)
    assert result['rootfs_verified']
    for key in ('runtime_approved', 'vulnerability_scan_verified', 'docker_loaded', 'application_behavior_verified'):
        assert result[key] is False
    directory = target.root / 'nginx_static_v1'
    assert json.loads((directory / 'receipt.json').read_bytes()) == result
    assert not (target.root / 'runtime-policy.json').exists()
    previous = target.registry.requests
    assert acquisition.acquire_candidate(target.root, role='nginx_static_v1', registry=target.registry) == result
    assert target.registry.requests == previous  # retained blob readback does not redownload
    assert all(path.stat().st_mode & 0o077 == 0 for path in directory.rglob('*'))


def test_partial_continues_only_exact_range_and_verifies_full_hash(target):
    descriptor = target.manifest['config']
    path = target.root / descriptor['digest'].split(':')[1]
    pending = path.with_name(path.name + '.partial')
    body = target.content[descriptor['digest']]
    pending.write_bytes(body[:7]);pending.chmod(0o600)
    acquisition.acquire_blob(path, descriptor, target.registry)
    assert path.read_bytes() == body and not pending.exists()
    assert target.registry.ranges == [(descriptor['digest'], 7)]


@pytest.mark.parametrize('fault', ['wrong_range', 'status'])
def test_partial_is_never_appended_to_an_unproven_response(target, fault):
    descriptor = target.manifest['config'];path = target.root / descriptor['digest'].split(':')[1]
    pending = path.with_name(path.name + '.partial')
    body = target.content[descriptor['digest']][:7]
    pending.write_bytes(body);pending.chmod(0o600)
    setattr(target.registry, fault, True if fault == 'wrong_range' else 200)
    with pytest.raises(acquisition.RuntimeAcquisitionError, match='range continuation'):
        acquisition.acquire_blob(path, descriptor, target.registry)
    assert pending.read_bytes() == body and not path.exists()


def test_interrupted_download_retains_bounded_partial_and_can_resume(target):
    descriptor = target.manifest['config'];path = target.root / descriptor['digest'].split(':')[1]
    target.registry.interrupt = True
    with pytest.raises(TimeoutError):
        acquisition.acquire_blob(path, descriptor, target.registry)
    pending = path.with_name(path.name + '.partial')
    assert pending.stat().st_size == 7 and not path.exists()
    target.registry.interrupt = False
    acquisition.acquire_blob(path, descriptor, target.registry)
    assert oci.digest(path.read_bytes()) == descriptor['digest']


def test_completed_partial_is_reconciled_without_network(target):
    descriptor = target.manifest['config'];path = target.root / descriptor['digest'].split(':')[1]
    pending = path.with_name(path.name + '.partial')
    pending.write_bytes(target.content[descriptor['digest']]);pending.chmod(0o600)
    acquisition.acquire_blob(path, descriptor, target.registry)
    assert target.registry.requests == 0 and path.exists() and not pending.exists()


@pytest.mark.parametrize('fault', ['corrupt', 'hardlink', 'symlink', 'public', 'oversize'])
def test_bad_partial_or_download_is_not_promoted(target, fault):
    descriptor = target.manifest['config'];path = target.root / descriptor['digest'].split(':')[1]
    pending = path.with_name(path.name + '.partial')
    if fault == 'corrupt':
        target.registry.corrupt = True
    else:
        pending.write_bytes(b'prefix');pending.chmod(0o600)
        if fault == 'hardlink':
            os.link(pending, target.root / 'second')
        elif fault == 'symlink':
            other = target.root / 'original';pending.rename(other);pending.symlink_to(other)
        elif fault == 'public':
            pending.chmod(0o644)
        else:
            pending.write_bytes(b'x' * (descriptor['size'] + 1))
    with pytest.raises(acquisition.RuntimeAcquisitionError):
        acquisition.acquire_blob(path, descriptor, target.registry)
    assert not path.exists()


def test_changed_retained_cas_is_not_overwritten(target):
    acquisition.acquire_candidate(target.root, role='nginx_static_v1', registry=target.registry)
    descriptor = target.manifest['config']
    path = target.root / 'nginx_static_v1/blobs/sha256' / descriptor['digest'].split(':')[1]
    path.write_bytes(b'x' * descriptor['size'])
    with pytest.raises(acquisition.RuntimeAcquisitionError, match='digest differs'):
        acquisition.acquire_candidate(target.root, role='nginx_static_v1', registry=target.registry)
    assert path.read_bytes() == b'x' * descriptor['size']


@pytest.mark.parametrize('fault', ['blob', 'receipt', 'index', 'layout', 'source_index'])
def test_readonly_observer_rehashes_every_authoritative_candidate_component(target, fault):
    acquisition.acquire_candidate(target.root, role='nginx_static_v1', registry=target.registry)
    directory = target.root / 'nginx_static_v1'
    paths = {'blob': directory / 'blobs/sha256' / target.manifest['config']['digest'].split(':')[1],
             'receipt': directory / 'receipt.json', 'index': directory / 'index.json',
             'layout': directory / 'oci-layout', 'source_index': directory / 'source-index.json'}
    path = paths[fault];original = path.read_bytes();path.write_bytes(b'x' * len(original))
    requests = target.registry.requests
    with pytest.raises((acquisition.RuntimeAcquisitionError, oci.ImagePackagingError)):
        acquisition.observe_candidate(target.root, role='nginx_static_v1')
    assert path.read_bytes() == b'x' * len(original) and target.registry.requests == requests


def test_completed_candidate_observation_is_readonly_and_not_approval(target):
    expected = acquisition.acquire_candidate(target.root, role='nginx_static_v1', registry=target.registry)
    before = {path: (path.stat().st_mtime_ns, path.read_bytes()) for path in target.root.rglob('*') if path.is_file()}
    assert acquisition.observe_candidate(target.root, role='nginx_static_v1') == expected
    assert before == {path: (path.stat().st_mtime_ns, path.read_bytes()) for path in before}


def test_invented_runtime_role_is_rejected_without_network(target):
    with pytest.raises(acquisition.RuntimeAcquisitionError, match='reviewed public catalog'):
        acquisition.acquire_candidate(target.root, role='model-selected-image', registry=target.registry)
    assert target.registry.requests == 0


@pytest.mark.parametrize('url', ['http://registry-1.docker.io/x', 'https://127.0.0.1/x',
                                 'https://169.254.169.254/x', 'https://evil.example/x',
                                 'https://user:pass@registry-1.docker.io/x', 'https://registry-1.docker.io:444/x'])
def test_public_registry_never_accepts_private_or_unknown_endpoint(url):
    with pytest.raises(acquisition.RuntimeAcquisitionError, match='allowlisted HTTPS'):
        acquisition.secure_url(url)


def test_public_cdn_redirect_does_not_forward_registry_bearer():
    request = urllib.request.Request('https://registry-1.docker.io/v2/library/nginx/blobs/sha256:' + 'a' * 64,
                                     headers={'Authorization': 'Bearer synthetic-public-pull-token'})
    redirected = acquisition.PublicRedirect().redirect_request(
        request, None, 307, 'redirect', {}, 'https://production.cloudfront.docker.com/public-blob')
    assert not redirected.has_header('Authorization')


def test_runtime_candidate_cli_is_source_bounded_and_rejects_false_approval(target):
    source = cli.program();compile(source, 'reviewed-runtime-client', 'exec')
    assert len(source.encode()) <= 100000
    with pytest.raises(RuntimeError, match='unconfirmed'):
        cli.verify({'operation': 'probe'}, {'operation': 'probe', 'runtime_approved': True,
                                         'docker_loaded': False, 'journal_available': True})


def test_historical_runtime_controller_refuses_unknown_or_changed_blob(monkeypatch):
    with pytest.raises(RuntimeError, match='not reviewed'):
        cli.frozen_program('f' * 64)
    expected = next(iter(cli.REVIEWED_PROGRAMS))
    def wrong(argv, **kwargs):
        assert argv[-1] == 'refs/gca/runtime-acquisitions/' + expected
        return SimpleNamespace(returncode=0, stdout=b'print("not reviewed")')
    monkeypatch.setattr(cli.subprocess, 'run', wrong)
    with pytest.raises(RuntimeError, match='custody differs'):
        cli.frozen_program(expected)
