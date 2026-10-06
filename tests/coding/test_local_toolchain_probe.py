"""Local fixed-fixture probe contracts; simulations are not Docker/gVisor proof."""
import json
import subprocess

import pytest

from scripts import probe_local_offline_toolchains as probe


@pytest.mark.parametrize('code,output,expected', [
    (1, b'provider/cache failed', False),
    (0, probe.FAILURE_MARKER.encode(), False),
    (1, probe.FAILURE_MARKER.encode(), True),
])
def test_negative_requires_real_nonzero_exit_and_exact_deliberate_marker(monkeypatch, code, output, expected):
    monkeypatch.setattr(probe.subprocess, 'run', lambda *a, **kw:
                        subprocess.CompletedProcess(a, code, stdout=output, stderr=b''))
    if expected:
        assert probe.docker('exec', expected_test_failure=True)
    else:
        with pytest.raises(RuntimeError, match='not observed'):
            probe.docker('exec', expected_test_failure=True)


def test_public_maven_stdout_error_is_available_and_output_still_bounded(monkeypatch):
    monkeypatch.setattr(probe.subprocess, 'run', lambda *a, **kw:
                        subprocess.CompletedProcess(a, 1, stdout=b'public cache missing', stderr=b''))
    with pytest.raises(RuntimeError, match='public cache missing'):
        probe.docker('exec')
    monkeypatch.setattr(probe.subprocess, 'run', lambda *a, **kw:
                        subprocess.CompletedProcess(a, 1, stdout=b'x' * 256001, stderr=b''))
    with pytest.raises(RuntimeError, match='output exceeded'):
        probe.docker('exec')


@pytest.mark.parametrize('mismatch', [False, True])
def test_failure_cleanup_reconciles_exact_owned_container_never_unknown_identity(monkeypatch, mismatch):
    image = 'sha256:' + 'a' * 64
    fixture = 'b' * 64
    observed = {}
    removals = []

    def docker(*args, **kwargs):
        if args[0] == 'create':
            observed.update({'Name': '/' + args[args.index('--name') + 1], 'Image': image,
                             'Config': {'Labels': {'org.gca.operator-fixture': fixture}}})
            return b'owned-container'
        if args[0] == 'inspect':
            if mismatch:
                observed['Image'] = 'sha256:' + 'c' * 64
            return json.dumps([observed]).encode()
        if args[0] == 'exec':
            raise RuntimeError('fixed public test failed')
        if args[0] == 'ps':
            return b'owned-container' if observed else b''
        if args[0] == 'rm':
            removals.append(args)
            observed.clear()
        return b''

    monkeypatch.setattr(probe, 'docker', docker)
    with pytest.raises(RuntimeError, match='identity differs|ownership differs|test failed'):
        probe.smoke('react', image, b'fixed-public', {'fixture_sha256': fixture})
    assert bool(removals) is not mismatch
