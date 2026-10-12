"""Operator-only acquisition of exact public OCI bases; no runtime approval.

Pins come from source-reviewed catalog entries, never model text. This module
downloads content-addressed bytes into a protected candidate store and rechecks
platform/config/rootfs. It never extracts layers, loads Docker, or publishes a
production runtime policy. Partial blobs resume only with exact HTTP range proof.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
import urllib.error
import urllib.parse
import urllib.request

from app.coding import oci_packager as oci


CATALOG = {
    'nginx_static_v1': {
        'repository': 'library/nginx', 'discovery_tag': '1.30.5-alpine',
        'index_digest': 'sha256:0985e772fb9f729e6fa0980da05fca5d9c468e870eed43071545afa9d2e27d94',
        'manifest_digest': 'sha256:8f84ed99befc3891b8f329c5c202785278a2cfb7c25107d57fb2a134a3117433',
        'config_digest': 'sha256:43d9d8c1f8968f09df8c1aa6c136ecc617e62d64fe4c0b24c97de4eb210bd973',
        'layer_bytes': 26073522, 'layer_count': 8,
    },
    'temurin_java17_v1': {
        'repository': 'library/eclipse-temurin', 'discovery_tag': '17-jre-noble',
        'index_digest': 'sha256:900322f4cc1b9da9d730ae4bb56d8a1dabbaa9cfa32809fc8b542b600370a045',
        'manifest_digest': 'sha256:af3852c35f8339ab7beba6f17b7fc15c4aca4d72979c5f09776c9ff74385058f',
        'config_digest': 'sha256:3b65da7b2a66cb9cddacdb02dc26b21881a353fa2aa29a7f578ac9eb5df89893',
        'layer_bytes': 96662597, 'layer_count': 5,
    },
}
INDEX_MEDIA = {'application/vnd.oci.image.index.v1+json',
               'application/vnd.docker.distribution.manifest.list.v2+json'}
MANIFEST_MEDIA = {oci.OCI_MANIFEST, oci.DOCKER_MANIFEST}
AUTH_HOST, REGISTRY_HOST = 'auth.docker.io', 'registry-1.docker.io'
CDN_HOSTS = {'production.cloudflare.docker.com', 'production.cloudfront.docker.com'}


class RuntimeAcquisitionError(ValueError):
    pass


def pin(role):
    if not isinstance(role, str) or role not in CATALOG:
        raise RuntimeAcquisitionError('runtime role is not in the reviewed public catalog')
    return CATALOG[role].copy()


def secure_url(url):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None, 443)
            or parsed.hostname not in {AUTH_HOST, REGISTRY_HOST, *CDN_HOSTS} or parsed.fragment):
        raise RuntimeAcquisitionError('runtime acquisition endpoint is not allowlisted HTTPS')
    return parsed


class PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        secure_url(newurl)
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and urllib.parse.urlsplit(newurl).hostname != REGISTRY_HOST:
            redirected.remove_header('Authorization')
        return redirected


class PublicRegistry:
    def __init__(self, role, *, timeout_seconds=240):
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 300:
            raise RuntimeAcquisitionError('runtime acquisition timeout exceeds bound')
        self.catalog = pin(role)
        self.deadline = time.monotonic() + timeout_seconds
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), PublicRedirect())
        query = urllib.parse.urlencode({'service': 'registry.docker.io',
                                       'scope': 'repository:' + self.catalog['repository'] + ':pull'})
        with self.open('https://' + AUTH_HOST + '/token?' + query, {}) as response:
            body = response.read(32769)
            if len(body) > 32768:
                raise RuntimeAcquisitionError('public registry token response exceeds bound')
            value = oci._metadata(body)
        self.token = value.get('token')
        if (not isinstance(self.token, str) or not 1 <= len(self.token) <= 16384
                or not re.fullmatch('[A-Za-z0-9._-]+', self.token)):
            raise RuntimeAcquisitionError('public registry pull token is invalid')

    @contextmanager
    def open(self, url, headers):
        secure_url(url)
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeAcquisitionError('runtime acquisition deadline exhausted')
        try:
            with self.opener.open(urllib.request.Request(url, headers=headers), timeout=min(20, remaining)) as response:
                secure_url(response.url)
                yield response
        except (urllib.error.URLError, TimeoutError, OSError):
            raise RuntimeAcquisitionError('public registry transport is unconfirmed') from None

    def endpoint(self, kind, checksum):
        if kind not in {'manifests', 'blobs'} or not isinstance(checksum, str) or not oci.DIGEST.fullmatch(checksum):
            raise RuntimeAcquisitionError('immutable runtime descriptor required')
        return 'https://' + REGISTRY_HOST + '/v2/' + self.catalog['repository'] + '/' + kind + '/' + checksum

    def manifest(self, checksum):
        headers = {'Authorization': 'Bearer ' + self.token,
                   'Accept': ','.join(sorted(INDEX_MEDIA | MANIFEST_MEDIA))}
        with self.open(self.endpoint('manifests', checksum), headers) as response:
            body = response.read(oci.MAX_METADATA_BYTES + 1)
            if (response.status != 200 or len(body) > oci.MAX_METADATA_BYTES
                    or response.headers.get('Docker-Content-Digest') != checksum or oci.digest(body) != checksum):
                raise RuntimeAcquisitionError('runtime manifest fingerprint differs')
        return body

    @contextmanager
    def blob(self, checksum, *, offset=0):
        headers = {'Authorization': 'Bearer ' + self.token}
        if offset:
            headers['Range'] = 'bytes=' + str(offset) + '-'
        with self.open(self.endpoint('blobs', checksum), headers) as response:
            yield response


def manifest_graph(role, registry):
    expected = pin(role)
    index_body = registry.manifest(expected['index_digest'])
    if oci.digest(index_body) != expected['index_digest']:
        raise RuntimeAcquisitionError('runtime source index fingerprint differs')
    index = oci._metadata(index_body)
    roots = index.get('manifests')
    if (type(index.get('schemaVersion')) is not int or index['schemaVersion'] != 2
            or index.get('mediaType') not in INDEX_MEDIA or not isinstance(roots, list)
            or not 1 <= len(roots) <= 64):
        raise RuntimeAcquisitionError('runtime source index is invalid')
    selected = [entry for entry in roots if isinstance(entry, dict)
                and entry.get('platform') == {'os': 'linux', 'architecture': 'amd64'}]
    if len(selected) != 1 or selected[0].get('digest') != expected['manifest_digest']:
        raise RuntimeAcquisitionError('runtime AMD64 selection differs from the reviewed pin')
    manifest_body = registry.manifest(expected['manifest_digest'])
    if oci.digest(manifest_body) != expected['manifest_digest']:
        raise RuntimeAcquisitionError('runtime platform manifest fingerprint differs')
    manifest = oci._metadata(manifest_body)
    config, layers = manifest.get('config'), manifest.get('layers')
    if (set(manifest) != {'schemaVersion', 'mediaType', 'config', 'layers'}
            or type(manifest['schemaVersion']) is not int or manifest['schemaVersion'] != 2
            or manifest['mediaType'] not in MANIFEST_MEDIA
            or selected[0].get('mediaType') != manifest['mediaType']
            or type(selected[0].get('size')) is not int or selected[0]['size'] != len(manifest_body)
            or not isinstance(config, dict) or config.get('digest') != expected['config_digest']
            or not isinstance(layers, list) or len(layers) != expected['layer_count']):
        raise RuntimeAcquisitionError('runtime manifest/config/layer selection differs')
    for item in (config, *layers):
        if (not isinstance(item, dict) or set(item) != {'mediaType', 'digest', 'size'}
                or not isinstance(item['digest'], str) or not oci.DIGEST.fullmatch(item['digest'])
                or type(item['size']) is not int or not 1 <= item['size'] <= oci.MAX_BASE_BYTES):
            raise RuntimeAcquisitionError('runtime blob descriptor is invalid')
    if (config['size'] > oci.MAX_METADATA_BYTES or config['mediaType'] not in {oci.OCI_CONFIG, oci.DOCKER_CONFIG}
            or sum(item['size'] for item in layers) != expected['layer_bytes']
            or expected['layer_bytes'] > oci.MAX_BASE_BYTES
            or any(item['mediaType'] not in {oci.OCI_LAYER, oci.DOCKER_LAYER} for item in layers)):
        raise RuntimeAcquisitionError('runtime content bounds or media types differ')
    return index_body, manifest_body, manifest


def protected(path):
    for current in (path, *path.parents):
        info = current.lstat()
        if (current.is_symlink() or info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o022
                or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode))
                or stat.S_ISREG(info.st_mode) and info.st_nlink != 1):
            raise RuntimeAcquisitionError('runtime candidate path is not operator protected')
    if path.stat().st_mode & 0o077:
        raise RuntimeAcquisitionError('runtime candidate inode is not private')
    return path


def sync(path):
    fd = os.open(path, os.O_DIRECTORY | os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_file(path, size):
    protected(path)
    if type(size) is not int or not 1 <= size <= oci.MAX_BASE_BYTES:
        raise RuntimeAcquisitionError('runtime file read exceeds bound')
    before = path.lstat()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != size
                or info.st_mode & 0o077 or info.st_uid not in {0, os.geteuid()}
                or (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink)
                != (before.st_dev, before.st_ino, before.st_mode, before.st_uid, before.st_nlink)):
            raise RuntimeAcquisitionError('runtime candidate file size or inode differs')
        body = stream.read(size + 1)
    if len(body) != size:
        raise RuntimeAcquisitionError('runtime candidate file changed during read')
    return body


def write_exact(path, body):
    """Immutable known bytes; interrupted complete writes may be reconciled."""
    if path.exists() or path.is_symlink():
        if read_file(path, len(body)) != body:
            raise RuntimeAcquisitionError('runtime candidate file content changed')
        return
    temporary = path.with_name(path.name + '.partial')
    if temporary.exists() or temporary.is_symlink():
        if read_file(temporary, len(body)) != body:
            raise RuntimeAcquisitionError('runtime metadata partial needs explicit review')
    else:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
    os.rename(temporary, path)
    sync(path.parent)


def acquire_blob(path, descriptor, registry):
    checksum, size = descriptor['digest'], descriptor['size']
    if path.exists() or path.is_symlink():
        if oci.digest(read_file(path, size)) != checksum:
            raise RuntimeAcquisitionError('retained runtime blob digest differs')
        return
    temporary = path.with_name(path.name + '.partial')
    if temporary.exists() or temporary.is_symlink():
        protected(temporary)
    fd = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'r+b') as stream:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid not in {0, os.geteuid()}
                or info.st_mode & 0o077 or not 0 <= info.st_size <= size):
            raise RuntimeAcquisitionError('runtime partial inode or size differs')
        offset = info.st_size
        checksum_state = hashlib.sha256()
        while body := stream.read(1024 * 1024):
            checksum_state.update(body)
        count = offset
        try:
            if offset < size:
                with registry.blob(checksum, offset=offset) as response:
                    if (response.status != (206 if offset else 200)
                            or offset and response.headers.get('Content-Range') !=
                            'bytes ' + str(offset) + '-' + str(size - 1) + '/' + str(size)):
                        raise RuntimeAcquisitionError('runtime partial lacks exact range continuation')
                    while body := response.read(min(1024 * 1024, size - count + 1)):
                        count += len(body)
                        if count > size:
                            raise RuntimeAcquisitionError('runtime blob exceeded descriptor bound')
                        stream.write(body)
                        checksum_state.update(body)
            if count != size or 'sha256:' + checksum_state.hexdigest() != checksum:
                raise RuntimeAcquisitionError('runtime downloaded blob fingerprint differs')
        finally:
            stream.flush()
            os.fsync(fd)
    os.rename(temporary, path)
    sync(path.parent)


def acquire_candidate(root, *, role, registry):
    """Caller owns the host flock and timeout/cgroup; no approval is emitted."""
    protected(root)
    expected = pin(role)
    index_body, manifest_body, manifest = manifest_graph(role, registry)
    directory = root / role
    directory.mkdir(mode=0o700, exist_ok=True)
    protected(directory)
    blobs = directory / 'blobs'
    blobs.mkdir(mode=0o700, exist_ok=True)
    protected(blobs)
    checksums = blobs / 'sha256'
    checksums.mkdir(mode=0o700, exist_ok=True)
    protected(checksums)
    write_exact(directory / 'source-index.json', index_body)
    write_exact(checksums / expected['manifest_digest'].split(':')[1], manifest_body)
    for descriptor in (manifest['config'], *manifest['layers']):
        acquire_blob(checksums / descriptor['digest'].split(':')[1], descriptor, registry)
    inspect_candidate_blobs(role, directory, manifest_body, manifest)
    write_exact(directory / 'oci-layout', oci.canonical({'imageLayoutVersion': '1.0.0'}))
    write_exact(directory / 'index.json', oci.canonical({'schemaVersion': 2, 'manifests': [{
        'digest': expected['manifest_digest'], 'size': len(manifest_body), 'mediaType': manifest['mediaType'],
    }]}))
    receipt = candidate_receipt(role)
    write_exact(directory / 'receipt.json', oci.canonical(receipt))
    return observe_candidate(root, role=role)


def candidate_receipt(role):
    expected = pin(role)
    return {'version': 1, 'role': role, 'catalog_sha256': hashlib.sha256(oci.canonical(expected)).hexdigest(),
               'index_digest': expected['index_digest'], 'manifest_digest': expected['manifest_digest'],
               'config_digest': expected['config_digest'], 'layer_bytes': expected['layer_bytes'],
               'layer_count': expected['layer_count'], 'rootfs_verified': True,
               'runtime_approved': False, 'vulnerability_scan_verified': False,
               'docker_loaded': False, 'application_behavior_verified': False}


def inspect_candidate_blobs(role, directory, manifest_body, manifest):
    expected = pin(role)
    checksums = directory / 'blobs/sha256'
    config = read_file(checksums / expected['config_digest'].split(':')[1], manifest['config']['size'])
    bodies = tuple(read_file(checksums / item['digest'].split(':')[1], item['size']) for item in manifest['layers'])
    measured = oci.ApprovedRuntimeBase(role, expected['manifest_digest'], manifest_body, config, bodies)
    measured.inspect()  # structural/diff-ID inspection, NOT an operator approval


def observe_candidate(root, *, role):
    """Read-only rehash, including rootfs; a saved receipt is never sufficient."""
    protected(root)
    expected = pin(role)
    directory = root / role
    protected(directory)
    class FileRegistry:
        def manifest(self, checksum):
            if checksum == expected['index_digest']:
                path = directory / 'source-index.json'
            elif checksum == expected['manifest_digest']:
                path = directory / 'blobs/sha256' / checksum.split(':')[1]
            else:
                raise RuntimeAcquisitionError('candidate metadata is outside the reviewed graph')
            protected(path)
            size = path.stat().st_size
            if not 1 <= size <= oci.MAX_METADATA_BYTES:
                raise RuntimeAcquisitionError('candidate metadata exceeds bound')
            return read_file(path, size)
    _, body, manifest = manifest_graph(role, FileRegistry())
    inspect_candidate_blobs(role, directory, body, manifest)
    metadata = {'oci-layout': oci.canonical({'imageLayoutVersion': '1.0.0'}),
                'index.json': oci.canonical({'schemaVersion': 2, 'manifests': [{
                    'digest': expected['manifest_digest'], 'size': len(body), 'mediaType': manifest['mediaType'],
                }]}), 'receipt.json': oci.canonical(candidate_receipt(role))}
    for name, expected_body in metadata.items():
        if read_file(directory / name, len(expected_body)) != expected_body:
            raise RuntimeAcquisitionError('candidate layout/receipt content changed')
    return candidate_receipt(role)
