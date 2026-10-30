"""Streaming custody fault tests; tiny fake layers never prove runtime behavior."""
import hashlib
import io
import json
import tarfile

import pytest

from app.coding import toolchain_archive as scan
from app.coding import toolchain_spec as spec
from app.coding.oci_packager import canonical, digest, OCI_CONFIG, OCI_LAYER


def fixture(*, role='react', user='65532:65532', config_override=None, compatibility=None, extras=None):
    config = {'architecture': 'amd64', 'os': 'linux', 'config': {'User': user, 'Labels': {
        'org.gca.toolchain.role': spec.ROLES[role],
        'org.gca.toolchain.base': spec.BASES[role].split('@')[1],
        'org.gca.toolchain.operator-only': 'true'}},
        'rootfs': {'type': 'layers', 'diff_ids': [digest(b'fixed-layer')]}}
    if config_override is not None:
        config['config'] = config_override
    config_body, layer_body = canonical(config), b'fixed-layer'

    def descriptor(body, media):
        return {'digest': digest(body), 'size': len(body), 'mediaType': media}

    manifest_body = canonical({'schemaVersion': 2, 'mediaType': scan.MANIFEST,
                               'config': descriptor(config_body, OCI_CONFIG),
                               'layers': [descriptor(layer_body, OCI_LAYER)]})
    platform = descriptor(manifest_body, scan.MANIFEST)
    platform['platform'] = {'architecture': 'amd64', 'os': 'linux'}
    root_body = canonical({'schemaVersion': 2, 'mediaType': scan.INDEX, 'manifests': [platform]})
    roots = canonical({'schemaVersion': 2, 'mediaType': scan.INDEX,
                       'manifests': [descriptor(root_body, scan.INDEX)]})
    blobs = [config_body, layer_body, manifest_body, root_body]
    files = {'blobs/sha256/' + digest(body).split(':')[1]: body for body in blobs}
    config_path = 'blobs/sha256/' + digest(config_body).split(':')[1]
    layer_path = 'blobs/sha256/' + digest(layer_body).split(':')[1]
    files.update({'index.json': roots, 'oci-layout': canonical({'imageLayoutVersion': '1.0.0'}),
                  'manifest.json': json.dumps(compatibility if compatibility is not None else
                                              [{'Config': config_path, 'Layers': [layer_path],
                                                'RepoTags': None}]).encode()})
    files.update(extras or {})
    return files, digest(root_body)


def archive(files, extra_member=None):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w') as out:
        for name, body in sorted(files.items()):
            item = tarfile.TarInfo(name)
            item.size = len(body)
            out.addfile(item, io.BytesIO(body))
        if extra_member is not None:
            out.addfile(extra_member)
    return stream.getvalue()


@pytest.mark.parametrize('role', ['react', 'spring'])
def test_tagless_stream_hash_and_distinct_digest_namespaces_are_bound_without_execution(role):
    files, image = fixture(role=role)
    body = archive(files)
    result = scan.inspect_archive(io.BytesIO(body), role=role, image_id=image)
    assert result['archive_sha256'] == hashlib.sha256(body).hexdigest()
    assert result['archive_bytes'] == len(body)
    assert len({result[k] for k in ('image_index_digest', 'platform_manifest_digest', 'config_digest')}) == 3
    assert result['image_loaded'] is result['image_approved'] is False


@pytest.mark.parametrize('kind', ['wrong_root', 'wrong_blob', 'extra_blob', 'unknown_file',
                                 'link', 'duplicate', 'traversal', 'trailing', 'truncated'])
def test_archive_custody_tampering_cannot_reach_image_loading(kind):
    files, image = fixture()
    extra = None
    if kind == 'wrong_root':
        image = 'sha256:' + 'a' * 64
    elif kind == 'wrong_blob':
        blob = next(name for name in files if name.startswith('blobs/'))
        files[blob] += b'changed'
    elif kind == 'extra_blob':
        files['blobs/sha256/' + digest(b'unreferenced').split(':')[1]] = b'unreferenced'
    elif kind == 'unknown_file':
        files['unexpected'] = b'unsafe'
    elif kind in ('link', 'duplicate', 'traversal'):
        extra = tarfile.TarInfo('index.json' if kind == 'duplicate' else '../private' if kind == 'traversal' else 'link')
        if kind == 'link':
            extra.type, extra.linkname = tarfile.SYMTYPE, '/private'
    body = archive(files, extra)
    if kind == 'trailing':
        body += b'nonzero-trailing'
    elif kind == 'truncated':
        body = body[:700]
    with pytest.raises(scan.ToolchainArchiveError):
        scan.inspect_archive(io.BytesIO(body), role='react', image_id=image)


@pytest.mark.parametrize('runtime', [[], {'User': '0:0'}, {'User': '65532:65532', 'Labels': 'wrong'}])
def test_invalid_or_privileged_runtime_metadata_fails_closed(runtime):
    files, image = fixture(config_override=runtime)
    with pytest.raises(scan.ToolchainArchiveError):
        scan.inspect_archive(io.BytesIO(archive(files)), role='react', image_id=image)


@pytest.mark.parametrize('compatibility', [[None], [{'Config': 'private', 'Layers': [], 'RepoTags': ['override:latest']}], {}])
def test_compatibility_cannot_override_platform_or_tag_other_images(compatibility):
    files, image = fixture(compatibility=compatibility)
    with pytest.raises(scan.ToolchainArchiveError):
        scan.inspect_archive(io.BytesIO(archive(files)), role='react', image_id=image)


@pytest.mark.parametrize('bound', ['MAX_ARCHIVE_BYTES', 'MAX_ENTRIES', 'MAX_RETAINED_METADATA'])
def test_stream_memory_and_total_archive_bounds(monkeypatch, bound):
    files, image = fixture()
    monkeypatch.setattr(scan, bound, 1)
    with pytest.raises(scan.ToolchainArchiveError):
        scan.inspect_archive(io.BytesIO(archive(files)), role='react', image_id=image)


def test_deadline_is_checked_between_reads_and_never_accepts_an_unbounded_timeout(monkeypatch):
    reader = scan.BoundedReader(io.BytesIO(b'fixed'), 1)
    monkeypatch.setattr(scan.time, 'monotonic', lambda: 2)
    with pytest.raises(scan.ToolchainArchiveError, match='deadline'):
        reader.read(1)
    with pytest.raises(scan.ToolchainArchiveError, match='timeout'):
        scan.inspect_archive(io.BytesIO(), role='react', image_id='sha256:' + 'a' * 64, timeout_seconds=0)


@pytest.mark.parametrize('kind', [tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.GNUTYPE_LONGNAME,
                                 tarfile.GNUTYPE_LONGLINK, tarfile.GNUTYPE_SPARSE])
def test_extended_header_rejected_before_tarfile_can_read_its_claimed_body(kind):
    member = tarfile.TarInfo('untrusted-extension')
    member.type = kind
    member.size = 128 * 1024 * 1024
    # No large body is constructed/read; the first header itself must be rejected.
    body = member.tobuf(format=tarfile.USTAR_FORMAT)
    with pytest.raises(scan.ToolchainArchiveError, match='extended'):
        scan.inspect_archive(io.BytesIO(body), role='react', image_id='sha256:' + 'a' * 64)


def test_headers_and_payload_spans_can_cross_arbitrary_source_read_boundaries():
    files, image = fixture()
    body = archive(files)

    class Fragmented(io.BytesIO):
        def read(self, size=-1):
            return super().read(min(size, 7))

    result = scan.inspect_archive(Fragmented(body), role='react', image_id=image)
    assert result['archive_sha256'] == hashlib.sha256(body).hexdigest()
    assert result['archive_bytes'] == len(body)


@pytest.mark.parametrize('padding', [0, 512])
def test_hash_valid_blobs_do_not_make_truncated_tar_end_markers_complete(padding):
    files, image = fixture()
    body = archive(files)
    with tarfile.open(fileobj=io.BytesIO(body)) as stream:
        last = max(item.offset_data + ((item.size + 511) // 512) * 512 for item in stream)
    with pytest.raises(scan.ToolchainArchiveError, match='end markers'):
        scan.inspect_archive(io.BytesIO(body[:last] + b'\0' * padding), role='react', image_id=image)
