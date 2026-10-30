"""Bounded streaming inspection of operator-produced OCI Docker-save archives.

No extraction, image loading, execution, network, registry publication or approval.
The expected image ID and role come from trusted operator configuration, not a model.
"""
import hashlib
import json
from pathlib import PurePosixPath
import re
import tarfile
import time

from app.coding.oci_packager import DIGEST, _metadata, ImagePackagingError
from app.coding import toolchain_spec as spec


MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_ENTRIES = 128
MAX_METADATA_BYTES = 1024 * 1024
MAX_RETAINED_METADATA = 16 * MAX_METADATA_BYTES
INDEX = 'application/vnd.oci.image.index.v1+json'
MANIFEST = 'application/vnd.oci.image.manifest.v1+json'


class ToolchainArchiveError(ValueError):
    pass


class BoundedReader:
    def __init__(self, source, deadline):
        self.source, self.deadline = source, deadline
        self.count = 0
        self.digest = hashlib.sha256()
        self.header = bytearray()
        self.payload_remaining = 0
        self.ended = False
        self.end_padding = 0

    def check_framing(self, body):
        """Reject extended headers BEFORE tarfile can allocate their claimed body.

        Fixed OCI filenames fit classic headers. Blob bodies are skipped in O(1)
        per read; only a single 512-byte header is retained by this framing guard.
        """
        offset = 0
        while offset < len(body):
            if self.ended:
                if any(body[offset:]):
                    raise ToolchainArchiveError('nonzero trailing toolchain archive data')
                self.end_padding += len(body) - offset
                return
            if self.payload_remaining:
                consumed = min(self.payload_remaining, len(body) - offset)
                self.payload_remaining -= consumed
                offset += consumed
                continue
            consumed = min(512 - len(self.header), len(body) - offset)
            self.header.extend(body[offset:offset + consumed])
            offset += consumed
            if len(self.header) < 512:
                continue
            if not any(self.header):
                self.ended = True
                self.end_padding = 512
                self.header.clear()
                continue
            try:
                member = tarfile.TarInfo.frombuf(bytes(self.header), 'utf-8', 'strict')
            except (tarfile.HeaderError, UnicodeError):
                raise ToolchainArchiveError('invalid toolchain tar header') from None
            self.header.clear()
            if member.type not in {tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE}:
                raise ToolchainArchiveError('extended/link/device toolchain headers are forbidden')
            if member.size < 0 or member.size > MAX_ARCHIVE_BYTES:
                raise ToolchainArchiveError('toolchain header size exceeds bound')
            self.payload_remaining = ((member.size + 511) // 512) * 512

    def read(self, size=-1):
        if time.monotonic() >= self.deadline:
            raise ToolchainArchiveError('toolchain archive deadline exceeded')
        if size < 0 or size > MAX_METADATA_BYTES:
            size = MAX_METADATA_BYTES
        body = self.source.read(min(size, MAX_ARCHIVE_BYTES - self.count + 1))
        if not isinstance(body, bytes):
            raise ToolchainArchiveError('binary toolchain stream required')
        self.count += len(body)
        if self.count > MAX_ARCHIVE_BYTES:
            raise ToolchainArchiveError('toolchain archive byte limit exceeded')
        self.check_framing(body)
        self.digest.update(body)
        return body


def metadata(body):
    try:
        return _metadata(body)
    except ImagePackagingError:
        raise ToolchainArchiveError('invalid toolchain metadata') from None


def inspect_archive(source, *, role, image_id, timeout_seconds=120):
    if role not in spec.ROLES or not isinstance(image_id, str) or not DIGEST.fullmatch(image_id):
        raise ToolchainArchiveError('trusted role and immutable image identity required')
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 300:
        raise ToolchainArchiveError('bounded toolchain inspection timeout required')
    reader = BoundedReader(source, time.monotonic() + timeout_seconds)
    blobs, small, named, seen = {}, {}, {}, set()
    retained = 0
    try:
        with tarfile.open(fileobj=reader, mode='r|') as archive:
            for item in archive:
                name = item.name.rstrip('/') if item.isdir() else item.name
                path = PurePosixPath(name)
                if (name in seen or len(seen) >= MAX_ENTRIES or path.is_absolute()
                        or path.as_posix() != name or '..' in path.parts or '.' in name.split('/')
                        or item.sparse is not None or item.linkname or item.mode & 0o7000
                        or item.size < 0 or item.size > MAX_ARCHIVE_BYTES
                        or not (item.isfile() or item.isdir())):
                    raise ToolchainArchiveError('unsafe or excessive toolchain archive members')
                seen.add(name)
                if item.isdir():
                    if name not in {'blobs', 'blobs/sha256'} or item.size:
                        raise ToolchainArchiveError('unexpected toolchain archive directory')
                    continue
                blob = re.fullmatch(r'blobs/sha256/([a-f0-9]{64})', name)
                if not blob and name not in {'index.json', 'oci-layout', 'manifest.json'}:
                    raise ToolchainArchiveError('unexpected toolchain archive file')
                stream = archive.extractfile(item)
                digest = hashlib.sha256()
                body = bytearray()
                size = 0
                keep = item.size <= MAX_METADATA_BYTES
                while chunk := stream.read(MAX_METADATA_BYTES):
                    size += len(chunk)
                    digest.update(chunk)
                    if keep:
                        body.extend(chunk)
                if size != item.size:
                    raise ToolchainArchiveError('truncated toolchain blob')
                if keep:
                    retained += len(body)
                    if retained > MAX_RETAINED_METADATA:
                        raise ToolchainArchiveError('toolchain metadata memory bound exceeded')
                if blob:
                    if digest.hexdigest() != blob[1]:
                        raise ToolchainArchiveError('toolchain blob hash mismatch')
                    checksum = 'sha256:' + blob[1]
                    blobs[checksum] = size
                    if keep:
                        small[checksum] = bytes(body)
                else:
                    if not keep:
                        raise ToolchainArchiveError('toolchain metadata exceeds bound')
                    named[name] = bytes(body)
            while tail := archive.fileobj.read(MAX_METADATA_BYTES):
                if any(tail):
                    raise ToolchainArchiveError('nonzero trailing toolchain archive data')
    except (tarfile.TarError, EOFError, OSError):
        raise ToolchainArchiveError('malformed toolchain archive') from None
    if not reader.ended or reader.end_padding < 1024 or reader.header or reader.payload_remaining:
        raise ToolchainArchiveError('complete tar end markers required')
    if set(named) != {'index.json', 'oci-layout', 'manifest.json'} or not blobs:
        raise ToolchainArchiveError('complete OCI Docker-save archive required')
    if metadata(named['oci-layout']) != {'imageLayoutVersion': '1.0.0'}:
        raise ToolchainArchiveError('unsupported OCI layout')

    def descriptor(value):
        if (not isinstance(value, dict) or not isinstance(value.get('digest'), str)
                or not DIGEST.fullmatch(value['digest']) or type(value.get('size')) is not int
                or value['size'] <= 0 or blobs.get(value['digest']) != value['size']):
            raise ToolchainArchiveError('toolchain descriptor does not bind its blob')
        return value['digest']

    index = metadata(named['index.json'])
    roots = index.get('manifests')
    if (type(index.get('schemaVersion')) is not int or index.get('schemaVersion') != 2 or index.get('mediaType') != INDEX
            or not isinstance(roots, list) or len(roots) != 1 or descriptor(roots[0]) != image_id):
        raise ToolchainArchiveError('archive does not bind the expected image index')
    root = metadata(small.get(image_id, b''))
    platforms = root.get('manifests')
    if (type(root.get('schemaVersion')) is not int or root.get('schemaVersion') != 2
            or root.get('mediaType') != INDEX or not isinstance(platforms, list)):
        raise ToolchainArchiveError('reviewed multi-platform image index required')
    selected = [item for item in platforms if isinstance(item, dict)
                and item.get('platform') == {'architecture': 'amd64', 'os': 'linux'}]
    if len(selected) != 1 or not 1 <= len(platforms) <= 8:
        raise ToolchainArchiveError('exactly one reviewed Linux AMD64 platform required')
    reachable = {image_id}
    for item in platforms:
        child_digest = descriptor(item)
        child = metadata(small.get(child_digest, b''))
        if (type(child.get('schemaVersion')) is not int or child.get('schemaVersion') != 2
                or child.get('mediaType') != MANIFEST):
            raise ToolchainArchiveError('unexpected nested image manifest')
        layers = child.get('layers')
        if not isinstance(layers, list) or not 1 <= len(layers) <= 64:
            raise ToolchainArchiveError('toolchain layer count exceeded')
        reachable.update([child_digest, descriptor(child.get('config'))])
        reachable.update(descriptor(layer) for layer in layers)
    if reachable != set(blobs):
        raise ToolchainArchiveError('unreferenced toolchain archive blob')
    manifest_digest = selected[0]['digest']
    manifest = metadata(small[manifest_digest])
    config_digest = manifest['config']['digest']
    config = metadata(small.get(config_digest, b''))
    runtime = config.get('config', {})
    if not isinstance(runtime, dict) or not isinstance(runtime.get('Labels') or {}, dict):
        raise ToolchainArchiveError('toolchain runtime configuration is invalid')
    labels = runtime.get('Labels') or {}
    if (config.get('architecture') != 'amd64' or config.get('os') != 'linux'
            or not isinstance(runtime, dict) or runtime.get('User') != '65532:65532'
            or runtime.get('Volumes') or runtime.get('Entrypoint') or runtime.get('Healthcheck')
            or labels.get('org.gca.toolchain.role') != spec.ROLES[role]
            or labels.get('org.gca.toolchain.base') != spec.BASES[role].split('@')[1]
            or labels.get('org.gca.toolchain.operator-only') != 'true'):
        raise ToolchainArchiveError('toolchain role/configuration differs')
    try:
        compatibility = json.loads(named['manifest.json'], object_pairs_hook=spec.unique_object)
    except (ValueError, UnicodeError, RecursionError):
        raise ToolchainArchiveError('invalid Docker compatibility metadata') from None
    if (not isinstance(compatibility, list) or len(compatibility) != 1
            or not isinstance(compatibility[0], dict)
            or compatibility[0].get('Config') != 'blobs/sha256/' + config_digest.split(':')[1]
            or compatibility[0].get('Layers') != ['blobs/sha256/' + item['digest'].split(':')[1]
                                                 for item in manifest['layers']]
            or compatibility[0].get('RepoTags') not in (None, [])):
        raise ToolchainArchiveError('tagless Docker compatibility metadata differs')
    return {'archive_sha256': reader.digest.hexdigest(), 'archive_bytes': reader.count,
            'image_index_digest': image_id, 'platform_manifest_digest': manifest_digest,
            'config_digest': config_digest, 'role': spec.ROLES[role],
            'blob_count': len(blobs), 'image_loaded': False, 'image_approved': False}
