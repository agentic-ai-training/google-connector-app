"""Fixed synthetic-data/protocol tests; live sandbox proof is separate."""
import ast
import hashlib
import io
from pathlib import Path
import tarfile
from types import SimpleNamespace

import pytest

from app.coding import isolated_packaging as host
from scripts.probe_packaging_pipeline import REMOTE_SOURCE, validate


def evidence(**changes):
    return dict(synthetic_artifact_only=True, synthetic_base_not_runtime_approval=True,
                production_packaging_activated=False, authenticated_production_dispatch=False,
                cloud_resource_created=False, generated_code_executed=False, model_calls=0,
                worker_barrier_verified=False, output_rehashed=False, staging_removed=False,
                journal_idle=True, **changes)


def test_default_probe_is_not_interpreted_as_execution_proof():
    result = evidence()
    assert validate(result, executed=False) == result
    assert result['worker_barrier_verified'] is False
    with pytest.raises(RuntimeError, match='unconfirmed'):
        validate(result, executed=True)


@pytest.mark.parametrize('field', ['production_packaging_activated', 'authenticated_production_dispatch',
                                 'cloud_resource_created', 'generated_code_executed'])
def test_synthetic_observation_cannot_authorize_production(field):
    result = evidence()
    result[field] = True
    with pytest.raises(RuntimeError, match='unconfirmed'):
        validate(result, executed=False)


@pytest.mark.parametrize('field,value', [('model_calls', 1), ('model_calls', False),
                                       ('synthetic_base_not_runtime_approval', False),
                                       ('journal_idle', False)])
def test_missing_safety_or_budget_evidence_rejected(field, value):
    result = evidence()
    result[field] = value
    with pytest.raises(RuntimeError, match='unconfirmed'):
        validate(result, executed=False)


def test_negative_worker_result_is_not_success_but_requires_verified_cleanup():
    result = evidence()
    result.update(execution_cleanup_verified=True, staging_removed=True, synthetic_worker_exit_code=1)
    assert validate(result, executed=True)['worker_barrier_verified'] is False
    result['staging_removed'] = False
    with pytest.raises(RuntimeError, match='unconfirmed'):
        validate(result, executed=True)


def test_fixed_probe_has_no_model_or_network_or_production_policy_write():
    assert ast.parse(REMOTE_SOURCE)
    for required in ('host.prepare(', 'host.publish_readiness(', 'host.verify_container(',
                     'host.recover(', 'host.remove(', 'journal.cleanup(', 'host.require_host_limits()',
                     'host.require_optional_sidecar_excluded()', 'shutil.disk_usage('):
        assert required in REMOTE_SOURCE
    assert 'requests.' not in REMOTE_SOURCE and 'urllib.' not in REMOTE_SOURCE
    assert "'/etc/gca-validation/policy.json'" in REMOTE_SOURCE
    assert 'write_text(' not in REMOTE_SOURCE and 'extractall(' not in REMOTE_SOURCE
    assert "request['operation']=='probe':return evidence" in REMOTE_SOURCE


def test_synthetic_artifact_and_base_are_real_valid_bytes_but_not_real_runtime(monkeypatch):
    # Only map the fixed operator bundle path to its reviewed local source. This
    # does not pretend that Linux/root/mount enforcement runs on macOS.
    protected = host._protected
    monkeypatch.setattr(host, '_protected', lambda path:
                        Path(host.__file__).parent / path.name if path.parent == Path('/usr/local/libexec/gca')
                        else protected(path))
    namespace = {'__name__': 'tested_synthetic_pipeline'}
    exec(compile(REMOTE_SOURCE, 'tested_synthetic_pipeline', 'exec'), namespace)
    artifact, reference, runtime_policy, base, binding, policy = namespace['synthetic_data'](
        host, {'server_uuid': 'fixed-synthetic-server'})
    assert host.outputs.verify_binding(binding) == binding
    assert binding['artifact_sha256'] == hashlib.sha256(artifact).hexdigest()
    assert binding['runtime_policy_hash'] == hashlib.sha256(runtime_policy).hexdigest()
    assert reference['archive_sha256'] == binding['artifact_sha256']
    assert set(policy['bundle_hashes']) == {name + '.py' for name in host.BUNDLE_NAMES}
    image = host.oci.package_validated_artifact(base, artifact, reference, profile=binding['profile'],
        source_hash=binding['source_hash'], claim_id=binding['validation_claim_id'], job_id=binding['validation_job_id'])
    assert image.evidence['generated_code_executed'] is False
    with tarfile.open(fileobj=io.BytesIO(artifact)) as archive:
        assert archive.extractfile('dist/index.html').read() == b'<html>Fixed nonsensitive assembly probe</html>'
    assert b'Not an executable application runtime' in host.oci.gzip.decompress(base.layers[0])


@pytest.mark.parametrize('termination_confirmed', [True, False])
def test_execution_exception_disposes_storage_only_after_verified_termination(monkeypatch, tmp_path, termination_confirmed):
    # Host decisions are simulated here. This tests exception ordering only, not
    # physical termination or unmount; the separately recorded live proof does.
    namespace = {'__name__': 'tested_synthetic_cleanup'}
    exec(compile(REMOTE_SOURCE, 'tested_synthetic_cleanup', 'exec'), namespace)
    calls = []

    class Journal:
        directory = tmp_path
        scope = 'fixed-test'

        def __init__(self, *args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def require_available(self):
            pass

        def begin(self, *args, **kwargs):
            return {'name': 'fixed-test', 'id': 'fixed-job', 'image': 'fixed-pin'}

        def record(self, job, state, *args):
            calls.append(state)

        def cleanup(self, *args, **kwargs):
            calls.append('termination_checked')
            if not termination_confirmed:
                raise RuntimeError('uncertain native helper')

    def execute(*args, **kwargs):
        raise OSError('synthetic observation lost')

    def remove(*args):
        calls.append('storage_removed')
        return {'staging_removed': True, 'output_removed': True}

    fake_host = SimpleNamespace(VOLUME_BYTES=1, require_host_limits=lambda: None,
        require_optional_sidecar_excluded=lambda: None,
        prepare=lambda *args: (tmp_path, []), create_arguments=lambda *args: [],
        verify_container=lambda *args: None, remove=remove)
    modules = {'load_policy': lambda *args: {'state_directory': str(tmp_path),
        'images': {'python_compile': 'fixed-pin'}}, '_packaging_helper': lambda: fake_host,
        'Journal': Journal, 'ensure_toolchain_image': lambda *args: None,
        'docker': lambda *args: b'fixed-container', 'execute_bounded': execute}
    monkeypatch.setattr(namespace['runpy'], 'run_path', lambda *args: modules)
    monkeypatch.setattr(namespace['shutil'], 'disk_usage', lambda *args: SimpleNamespace(free=1024**3))
    monkeypatch.setattr(namespace['signal'], 'alarm', lambda *args: None)
    namespace['synthetic_data'] = lambda *args: (b'', {}, b'', None, {}, {})
    with pytest.raises((OSError, RuntimeError)):
        namespace['operate']({'operation': 'run'})
    assert 'termination_checked' in calls
    assert ('storage_removed' in calls) is termination_confirmed
    if termination_confirmed:
        assert calls.index('termination_checked') < calls.index('storage_removed')
    else:
        assert 'cleanup_uncertain' in calls
