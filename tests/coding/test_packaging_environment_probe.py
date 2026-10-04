"""Probe protocol tests, not a simulated successful production isolation proof."""
import ast
import json
import runpy
from types import SimpleNamespace

import pytest

from scripts.probe_packaging_environment import REMOTE_PROBE


def test_reviewed_probe_compiles_and_only_mounts_reviewed_code():
    tree = ast.parse(REMOTE_PROBE)
    strings = [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)
               and isinstance(node.value, str)]
    assert 'type=bind,src=/usr/local/libexec/gca,dst=/gca/bundle,readonly' in strings
    assert '--pids-limit=64' in strings and '--network=none' in strings
    assert '--read-only' in strings and '--pull=never' in strings
    assert '--user=65532:65532' in strings and '--cap-drop=ALL' in strings
    assert '--security-opt=no-new-privileges:true' in strings
    assert not any('docker.sock' in value or 'API_KEY=' in value for value in strings)
    guest = next(node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)
                 and isinstance(node.value, str) and node.value.startswith('import json,pathlib,runpy'))
    compile(guest, 'fixed-guest-environment-probe', 'exec')


@pytest.mark.parametrize('outcome', ['negative_guest', 'startup_failure', 'lost_start'])
def test_probe_never_turns_negative_evidence_into_packaging_success(monkeypatch, capsys, outcome):
    state = SimpleNamespace(cleaned=False, calls=[], running=False)

    class Journal:
        scope = 'operator-test-scope'

        def __init__(self, directory):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def begin(self, profile, image, timeout):
            assert profile == 'python_compile' and timeout == 20
            return {'id': 'b' * 32, 'name': 'gca-validation-synthetic'}

        def verify_identity(self, job, cid):
            assert cid == 'a' * 64

        def record(self, job, phase, cid):
            assert phase == 'running'
            state.running = True

        def cleanup(self, job, *, create_confirmed):
            assert create_confirmed
            state.cleaned = True

    def docker(*args):
        state.calls.append(args)
        if args[0] == 'create':
            assert '--pids-limit=64' in args
            return ('a' * 64).encode()
        assert args[:2] == ('inspect', 'a' * 64)
        return json.dumps([{'State': {'Pid': 0}, 'HostConfig': {'Runtime': 'runsc', 'PidsLimit': 64}}]).encode()

    def execute(args, timeout, *, output_limit):
        assert state.running and output_limit == 16384
        if outcome == 'lost_start':
            raise RuntimeError('simulated connection loss')
        guest = {'worker_enforcement_passed': False, 'guest_cgroup': {'memory.max': 'unavailable'},
                 'virtual_proc': {'Seccomp': '0'}}
        return {'success': outcome == 'negative_guest', 'output_truncated': False,
                'stdout': json.dumps(guest) if outcome == 'negative_guest' else '',
                'stderr': '' if outcome == 'negative_guest' else 'synthetic runtime startup failed'}

    reviewed = {'load_policy': lambda path, workspace: {'images': {'python_compile': 'sha256:' + 'c' * 64},
                                                      'state_directory': '/synthetic-private-journal'},
                'Journal': Journal, 'ensure_toolchain_image': lambda image: None,
                'docker': docker, 'execute_bounded': execute}
    monkeypatch.setattr(runpy, 'run_path', lambda path: reviewed)
    if outcome == 'lost_start':
        with pytest.raises(RuntimeError, match='connection loss'):
            exec(compile(REMOTE_PROBE, 'reviewed-probe', 'exec'), {})  # nosec B102
        assert state.cleaned and not capsys.readouterr().out
    else:
        exec(compile(REMOTE_PROBE, 'reviewed-probe', 'exec'), {})  # nosec B102
        result = json.loads(capsys.readouterr().out)
        assert state.cleaned and result['synthetic_cleanup_verified'] is True
        assert result['production_packaging_activated'] is False
        assert result['native_enforcement_verified'] is False
        assert result['exited_host_pid'] == 0
        if outcome == 'negative_guest':
            assert result['worker_enforcement_passed'] is False
        else:
            assert result['observation_complete'] is False
