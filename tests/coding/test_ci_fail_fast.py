"""Execute real workflow shell structure with inert commands and injected failures."""
import os
from pathlib import Path
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = yaml.safe_load((ROOT / '.github/workflows/candidate-validation.yml').read_text())
GATES = {
    'backend': 'Validate candidate with no production credentials',
    'web': 'Validate candidate web',
    'flutter': 'Validate candidate mobile',
}


def gate(job):
    return next(step for step in WORKFLOW['jobs'][job]['steps'] if step.get('name') == GATES[job])


def inert_script(job, fail_at=None):
    # Preserve the actual shell options, brace group, redirection and tee pipeline.
    # Replace only the approved command lines, never execute repository programs.
    lines, count = [], 0
    for line in gate(job)['run'].splitlines():
        stripped = line.strip()
        if stripped.startswith(('python ', 'alembic ', 'pytest ', 'bandit ', 'pip-audit ',
                                'docker ', 'npm ', 'flutter ')):
            count += 1
            lines.append(f"printf 'command-{count}\\n'")
            lines.append('false' if count == fail_at else 'true')
        else:
            assert stripped == '' or stripped.startswith(('set ', '{', '}'))
            lines.append(line)
    return '\n'.join(lines), count


CASES = [(job, index) for job in GATES for index in range(1, inert_script(job)[1] + 1)]


@pytest.mark.parametrize('job,index', CASES)
def test_every_failed_command_stops_gate_and_survives_tee(tmp_path, job, index):
    assert gate(job)['shell'] == 'bash'
    script, _ = inert_script(job, index)
    result = subprocess.run(['bash', '--noprofile', '--norc', '-c', script], cwd=tmp_path,
                            env={'PATH': os.environ['PATH']}, capture_output=True, timeout=5)
    assert result.returncode != 0
    assert result.stdout.decode().splitlines() == [f'command-{n}' for n in range(1, index + 1)]
    log = next(tmp_path.glob('candidate-*.log'))
    assert log.read_bytes() == result.stdout


@pytest.mark.parametrize('job', GATES)
def test_success_requires_all_commands(tmp_path, job):
    script, count = inert_script(job)
    result = subprocess.run(['bash', '--noprofile', '--norc', '-c', script], cwd=tmp_path,
                            env={'PATH': os.environ['PATH']}, capture_output=True, timeout=5)
    assert result.returncode == 0
    assert result.stdout.decode().splitlines() == [f'command-{n}' for n in range(1, count + 1)]


def test_candidate_backend_does_not_silently_omit_integration_suite():
    assert gate('backend')['env']['RUN_INTEGRATION_TESTS'] == '1'
    assert '-W error::pytest.PytestUnraisableExceptionWarning' in gate('backend')['run']
    assert 'python -m scripts.verify_cloud_migrations_local' in gate('backend')['run']


def test_negative_control_reproduces_original_successful_tee_masking(tmp_path):
    script, _ = inert_script('backend', fail_at=3)
    original = script.replace('set -euo pipefail', '', 1)
    result = subprocess.run(['bash', '--noprofile', '--norc', '-e', '-c', original], cwd=tmp_path,
                            env={'PATH': os.environ['PATH']}, capture_output=True, timeout=5)
    # The subshell does stop, but its failure is invisible at the pipeline level.
    assert result.returncode == 0
    assert result.stdout.decode().splitlines() == ['command-1', 'command-2', 'command-3']


@pytest.mark.parametrize('name', ['Dockerfile', 'Dockerfile.worker', 'Dockerfile.candidate', 'Dockerfile.builder'])
def test_native_tls_dependencies_are_installed_before_rust_build(name):
    builder = (ROOT / name).read_text().split('FROM python:', 1)[0]
    install = next(line for line in builder.splitlines() if 'apt-get install' in line)
    assert 'pkg-config' in install and 'libssl-dev' in install
    assert builder.index(install) < builder.index('RUN cargo build --release --locked')
