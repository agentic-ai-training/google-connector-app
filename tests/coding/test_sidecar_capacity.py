"""Keyless sidecar proof contracts. No downloads, Docker or cloud mutations."""
import hashlib
import io
import json
from pathlib import Path
import tarfile

import pytest
import yaml

from scripts import prepare_ollama_cpu as artifacts
from scripts import sidecar_capacity_fixture as sink
from scripts import verify_sidecar_capacity as capacity


def archive(entries):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w') as output:
        for path, value, kind in entries:
            info = tarfile.TarInfo(path)
            info.mode = 0o4755  # Must not propagate setuid.
            if kind == 'regular':
                raw = value.encode()
                info.size = len(raw)
                output.addfile(info, io.BytesIO(raw))
            else:
                info.type = tarfile.SYMTYPE if kind == 'symlink' else tarfile.CHRTYPE
                info.linkname = value
                output.addfile(info)
    stream.seek(0)
    return stream


CPU = [('bin/ollama', 'synthetic-executable', 'regular'),
       ('lib/ollama/llama-server', 'synthetic-runner', 'regular')]


def test_cpu_artifact_selection_excludes_gpu_and_preserves_safe_library_link(tmp_path):
    root = tmp_path/'cpu'
    proof = artifacts.extract_cpu(archive(CPU + [
        ('lib/ollama/cuda_v12/private-gpu.so', 'not-copied', 'regular'),
        ('lib/ollama/libggml.so.1', 'synthetic-cpu', 'regular'),
        ('lib/ollama/libggml.so', 'libggml.so.1', 'symlink')]), root)
    assert proof['regular_files'] == 3 and proof['gpu_directories_included'] is False
    assert not (root/'lib/ollama/cuda_v12').exists()
    assert (root/'lib/ollama/libggml.so').read_text() == 'synthetic-cpu'
    assert (root/'bin/ollama').stat().st_mode & 0o7777 == 0o755


@pytest.mark.parametrize('extra', [
    [('../outside', 'unsafe', 'regular')],
    [('/outside', 'unsafe', 'regular')],
    [('lib/ollama/link', '../../outside', 'symlink')],
    [('lib/ollama/link', '/outside', 'symlink')],
    [('lib/ollama/link', 'missing', 'symlink')],
    [('lib/ollama/link', 'link2', 'symlink'), ('lib/ollama/link2', 'link', 'symlink')],
    [('lib/ollama/device', '', 'device')],
    [('bin/ollama', 'duplicate', 'regular')],
])
def test_cpu_archive_rejects_traversal_devices_missing_links_and_duplicate_paths(tmp_path, extra):
    with pytest.raises(artifacts.ArtifactError):
        artifacts.extract_cpu(archive(CPU+extra), tmp_path/'cpu')
    assert not (tmp_path/'outside').exists()


def test_artifact_destination_must_be_fresh_and_runtime_complete(tmp_path):
    with pytest.raises(FileExistsError):
        artifacts.extract_cpu(archive(CPU), tmp_path)
    with pytest.raises(artifacts.ArtifactError, match='complete_cpu_runtime'):
        artifacts.extract_cpu(archive(CPU[:1]), tmp_path/'incomplete')


def test_tar_root_directory_is_not_confused_with_a_payload_path(tmp_path):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w') as output:
        root = tarfile.TarInfo('./')
        root.type = tarfile.DIRTYPE
        output.addfile(root)
        for name, value, _ in CPU:
            member = tarfile.TarInfo('./'+name)
            member.size = len(value)
            output.addfile(member, io.BytesIO(value.encode()))
    stream.seek(0)
    assert artifacts.extract_cpu(stream, tmp_path/'cpu')['regular_files'] == 2


@pytest.mark.parametrize('url', ['http://github.com/file', 'https://evil.invalid/file',
    'https://github.com:444/file', 'https://user:password@github.com/file', 'file:///etc/passwd'])
def test_only_exact_public_https_download_hosts_allowed(url):
    with pytest.raises(artifacts.ArtifactError):
        artifacts.public_url(url)


def test_public_fetch_verifies_size_hash_exclusive_write_and_cleans_only_owned_partial(tmp_path, monkeypatch):
    class Response(io.BytesIO):
        status = 200
        url = 'https://github.com/public'
        headers = {'Content-Length': '3'}
    class Opener:
        def open(self, *args, **kwargs): return Response(b'abc')
    monkeypatch.setattr(artifacts.urllib.request, 'build_opener', lambda *args: Opener())
    destination = tmp_path/'artifact'
    artifacts.fetch(Response.url, destination, 3, hashlib.sha256(b'abc').hexdigest())
    assert destination.read_bytes() == b'abc'
    with pytest.raises(FileExistsError):
        artifacts.fetch(Response.url, destination, 3, hashlib.sha256(b'abc').hexdigest())
    assert destination.read_bytes() == b'abc'
    broken = tmp_path/'mismatch'
    with pytest.raises(artifacts.ArtifactError, match='digest_mismatch'):
        artifacts.fetch(Response.url, broken, 3, '0'*64)
    assert not broken.exists()


def v(number):
    out = bytearray()
    while number >= 128:
        out.append((number & 127) | 128)
        number >>= 7
    out.append(number)
    return bytes(out)


def field(number, raw):
    if isinstance(raw, str): raw = raw.encode()
    return v((number << 3) | 2)+v(len(raw))+raw


def write(labels):
    return field(1, b''.join(field(1, field(1, name)+field(2, value)) for name, value in labels.items()))


def test_actual_remote_write_series_schema_and_privacy_filter():
    labels = {'__name__': 'agent_capacity_probe', 'job': 'google-connector-api', 'application': 'google-connector-app'}
    names, jobs, count = sink.inspect_write(write(labels))
    assert names == {'agent_capacity_probe'} and jobs == {'google-connector-api'} and count == 1
    for key in sink.FORBIDDEN:
        with pytest.raises(ValueError, match='privacy_failure'):
            sink.inspect_write(write({**labels, key: 'redacted-but-still-forbidden-label'}))
    with pytest.raises(ValueError, match='privacy_failure'):
        sink.inspect_write(write({**labels, '__name__': 'forbidden_capacity_probe'}))
    with pytest.raises(ValueError, match='privacy_failure'):
        sink.inspect_write(write({**labels, 'other': 'SYNTHETIC_PRIVATE_email'}))


@pytest.mark.parametrize('raw', [b'', b'\x00', b'\x0a\xff\x7f', b'\x0a\x03\x09\x01\x01', b'\x0b', b'\x80'*20])
def test_remote_write_empty_or_malformed_payload_does_not_count_as_success(raw):
    with pytest.raises(ValueError):
        sink.inspect_write(raw)


def test_exact_768_finite_embedding_shape_and_batch_count():
    value = {'model': 'nomic-embed-text', 'embeddings': [[0.1]*768 for _ in range(8)], 'prompt_eval_count': 100}
    assert sink.check_embeddings(value, 8)['dimensions'] == 768
    for changed in [{'model': 'different'}, {'embeddings': []}, {'embeddings': [[0]*768]*8},
                    {'embeddings': [[float('nan')]*768]*8}, {'embeddings': [[True]*768]*8},
                    {'embeddings': [[0.1]*767]*8}]:
        with pytest.raises(ValueError): sink.check_embeddings({**value, **changed}, 8)
    assert len(capacity.embedding_inputs()) == 8
    assert all(len(text) == 6000 for text in capacity.embedding_inputs())
    assert len(set(capacity.embedding_inputs())) == 8


def test_cgroup_measurement_without_python_in_production_sidecar_images():
    raw = b'memory.current\n100000000\nmemory.peak\n200000000\nmemory.max\n999997440\nmemory.swap.max\n0\npids.current\n5\npids.max\n1000\nmemory.events\nlow 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n'
    value = capacity.parse_cgroup(raw, 4096)
    capacity.base.validate_cgroup(value)
    assert value['peak_bytes'] == 200000000
    with pytest.raises(capacity.base.CapacityError):
        capacity.parse_cgroup(raw.replace(b'memory.peak\n200000000\n', b''), 4096)


def test_metric_delivery_requires_both_services_privacy_and_all_filters():
    value = {'writes': 1, 'series': 6, 'validation_errors': 0, 'metrics': list(sink.METRICS),
             'jobs': ['google-connector-api', 'google-connector-worker'], 'scrapes': ['/metrics', '/monitoring/metrics']}
    assert capacity.verify_metrics(value)
    for change in [{'writes': 0}, {'series': 3}, {'validation_errors': 1}, {'metrics': []},
                   {'jobs': ['google-connector-api']}, {'scrapes': ['/metrics']}]:
        assert not capacity.verify_metrics({**value, **change})


def test_manual_no_secret_sidecar_workflow_does_not_replace_pr_validation():
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root/'.github/workflows/ci.yml').read_text())
    job = workflow['jobs']['sidecar-capacity']
    assert job['if'] == "github.event_name == 'workflow_dispatch' && inputs.sidecar_capacity"
    assert job['permissions'] == {'contents': 'read'} and job['timeout-minutes'] == 25
    assert 'secrets.' not in json.dumps(job)
    assert job['steps'][0]['with']['persist-credentials'] is False
    text = '\n'.join(step.get('run', '') for step in job['steps'])
    assert text.count('--label "org.opencontainers.image.revision=$GITHUB_SHA"') == 3
    assert 'python -m scripts.verify_sidecar_capacity --source-commit "$GITHUB_SHA"' in text
    for role in ('coding-runtime', 'backend', 'web', 'flutter'):
        assert workflow['jobs'][role]['if'] == "github.event_name != 'workflow_dispatch' || !inputs.sidecar_capacity"
    compose = (root/'docker-compose.yml').read_text()
    assert 'Dockerfile.ollama-capacity' not in compose
    assert 'sha256:51aeb9d829239345070619dad3edd6873186f913c84f45b365b74574fcb38ec0' in (root/'Dockerfile.alloy').read_text()
