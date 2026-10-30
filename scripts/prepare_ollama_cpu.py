"""Prepare a pinned PUBLIC CPU-only Ollama capacity candidate, never private data.

This is a build-time tool, not an application model downloader or an install script.
The existing production/local Ollama service is deliberately not changed by it.
"""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request

VERSION = '0.40.13'
ARCHIVE_SIZE = 1_525_346_571
ARCHIVE_SHA = 'c9490e0f520be21271d969e6703f012268b1d39f8b70dd58854e5bfef7a75bb6'
ARCHIVE_URL = f'https://github.com/ollama/ollama/releases/download/v{VERSION}/ollama-linux-amd64.tar.zst'
MODEL_MANIFEST_SHA = '0a109f422b47e3a30ba2b10eca18548e944e8a23073ee3f3e947efcf3c45e59f'
REGISTRY = 'https://registry.ollama.ai/v2/library/nomic-embed-text/'
PUBLIC_HOSTS = {'github.com', 'release-assets.githubusercontent.com', 'registry.ollama.ai',
                'dd20bb891979d25aebc8bec07b2b3bbc.r2.cloudflarestorage.com'}


class ArtifactError(RuntimeError):
    pass


def public_url(url):
    value = urllib.parse.urlsplit(url)
    if (value.scheme != 'https' or value.hostname not in PUBLIC_HOSTS
            or value.username or value.password or value.port not in (None, 443)):
        raise ArtifactError('pinned_public_https_source_required')
    return url


class PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url, destination, size, digest):
    """Exclusive, size/hash/deadline-bounded download; no ambient proxies/auth."""
    if type(size) is not int or not 0 < size <= ARCHIVE_SIZE or not re.fullmatch('[a-f0-9]{64}', digest):
        raise ArtifactError('exact_public_artifact_contract_required')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), PublicRedirect())
    request = urllib.request.Request(public_url(url), headers={'User-Agent': 'gca-public-capacity/1'})
    destination = Path(destination)
    count, checksum, created = 0, hashlib.sha256(), False
    deadline = time.monotonic() + 600
    try:
        with opener.open(request, timeout=30) as response:
            public_url(response.url)
            if response.status != 200 or response.headers.get('Content-Encoding', 'identity') != 'identity':
                raise ArtifactError('unencoded_public_artifact_required')
            if response.headers.get('Content-Length') not in (None, str(size)):
                raise ArtifactError('artifact_content_length_mismatch')
            fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            created = True
            with os.fdopen(fd, 'wb') as output:
                while True:
                    data = response.read(min(1024*1024, size-count+1))
                    if not data:
                        break
                    count += len(data)
                    if count > size or time.monotonic() > deadline:
                        raise ArtifactError('public_artifact_size_or_deadline_exceeded')
                    checksum.update(data)
                    output.write(data)
        if count != size or checksum.hexdigest() != digest:
            raise ArtifactError('public_artifact_digest_mismatch')
    except BaseException:
        if created:
            destination.unlink(missing_ok=True)
        raise


def archive_path(name):
    if name.startswith('./'):
        name = name[2:]
    parts = PurePosixPath(name).parts
    if (not parts or name.startswith('/') or '\\' in name or len(name) > 500
            or any(part in {'.', '..'} for part in parts)):
        raise ArtifactError('unsafe_archive_path')
    return PurePosixPath(*parts)


def extract_cpu(stream, root):
    """Select executable + CPU root libraries; never unpack GPU directories.

Only regular files and same-directory links ending at a selected regular file
are accepted. No extractall, devices, archive-controlled permissions or traversal.
"""
    root = Path(root)
    root.mkdir(mode=0o700)  # Fresh build-owned destination only.
    selected, links, total, archived, seen = [], [], 0, 0, set()
    with tarfile.open(fileobj=stream, mode='r|') as archive:
        for index, member in enumerate(archive):
            if index >= 20_000 or member.size < 0:
                raise ArtifactError('bounded_release_archive_required')
            archived += member.size
            if archived > 30_000_000_000:
                raise ArtifactError('bounded_decompressed_release_required')
            if member.isdir() and member.name in {'.', './'}:
                continue
            path = archive_path(member.name)
            keep = str(path) == 'bin/ollama' or (len(path.parts) == 3 and path.parts[:2] == ('lib', 'ollama'))
            if not keep or member.isdir():
                continue
            if str(path) in seen:
                raise ArtifactError('duplicate_cpu_artifact_path')
            seen.add(str(path))
            destination = root.joinpath(*path.parts)
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if member.issym() or member.islnk():
                target = archive_path(member.linkname)
                if member.issym() and len(target.parts) == 1:
                    target = path.parent/target
                if target.parent != path.parent:
                    raise ArtifactError('cpu_link_must_remain_in_same_directory')
                links.append((path, target))
                continue
            if not member.isfile() or not 0 < member.size < 700_000_000:
                raise ArtifactError('bounded_regular_cpu_file_required')
            total += member.size
            if total > 1_500_000_000:
                raise ArtifactError('cpu_payload_size_exceeded')
            source = archive.extractfile(member)
            if source is None:
                raise ArtifactError('cpu_file_content_required')
            remaining = member.size
            with destination.open('xb') as output:
                while remaining:
                    block = source.read(min(1024*1024, remaining))
                    if not block:
                        raise ArtifactError('truncated_cpu_file')
                    output.write(block)
                    remaining -= len(block)
            destination.chmod(0o755 if member.mode & 0o111 else 0o644)
            selected.append(str(path))
    pending = list(links)
    for _ in range(len(links)+1):
        next_pending = []
        for path, target in pending:
            linked = root.joinpath(*target.parts)
            if str(target) not in seen or not linked.is_file():
                next_pending.append((path, target))
                continue
            root.joinpath(*path.parts).symlink_to(target.name)
        if len(next_pending) == len(pending):
            break
        pending = next_pending
    if pending or not {'bin/ollama', 'lib/ollama/llama-server'} <= set(selected):
        raise ArtifactError('complete_cpu_runtime_and_resolved_links_required')
    return {'runtime_version': VERSION, 'archive_sha256': ARCHIVE_SHA,
            'regular_files': len(selected), 'cpu_payload_bytes': total, 'gpu_directories_included': False}


def prepare_model(root):
    root = Path(root)
    root.mkdir(mode=0o700)
    # The immutable manifest's exact digest is independently pinned, not trusted
    # merely because an HTTPS server currently calls it "latest".
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), PublicRedirect())
    request = urllib.request.Request(REGISTRY+'manifests/latest', headers={
        'Accept': 'application/vnd.docker.distribution.manifest.v2+json'})
    with opener.open(request, timeout=30) as response:
        public_url(response.url)
        raw = response.read(65537)
    if len(raw) > 65536 or hashlib.sha256(raw).hexdigest() != MODEL_MANIFEST_SHA:
        raise ArtifactError('pinned_nomic_manifest_required')
    value = json.loads(raw)
    entries = [value['config'], *value['layers']]
    if len(entries) != 4 or sum(entry['size'] for entry in entries) != 274_302_450:
        raise ArtifactError('pinned_model_blob_contract_required')
    blobs = root/'blobs'
    blobs.mkdir()
    for entry in entries:
        digest = entry['digest']
        if not re.fullmatch('sha256:[a-f0-9]{64}', digest):
            raise ArtifactError('sha256_model_blob_required')
        fetch(REGISTRY+'blobs/'+digest, blobs/digest.replace(':', '-'), entry['size'], digest[7:])
    manifest = root/'manifests/registry.ollama.ai/library/nomic-embed-text/latest'
    manifest.parent.mkdir(parents=True)
    with manifest.open('xb') as output:
        output.write(raw)
    return {'model': 'nomic-embed-text', 'manifest_sha256': MODEL_MANIFEST_SHA,
            'blob_bytes': sum(entry['size'] for entry in entries)}


def main():
    # Only invoked inside the dedicated disposable build stage. No .env reader.
    import zstandard
    with tempfile.TemporaryDirectory(prefix='gca-public-artifact-') as owned:
        archive = Path(owned)/'release.tar.zst'
        fetch(ARCHIVE_URL, archive, ARCHIVE_SIZE, ARCHIVE_SHA)
        with archive.open('rb') as compressed:
            with zstandard.ZstdDecompressor(max_window_size=2**28).stream_reader(compressed) as stream:
                runtime = extract_cpu(stream, '/cpu')
    model = prepare_model('/model')
    print(json.dumps({'public_cpu_candidate': runtime, 'public_model': model}), flush=True)


if __name__ == '__main__':
    main()
