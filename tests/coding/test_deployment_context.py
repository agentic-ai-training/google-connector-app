from pathlib import Path
import subprocess

import pytest

from scripts import verify_deployment_context as context


def policies(tmp_path):
    root = Path(__file__).resolve().parents[2]
    for name in ('.dockerignore', '.railwayignore'):
        (tmp_path / name).write_bytes((root / name).read_bytes())


def test_real_git_ignore_engine_blocks_private_canaries_and_keeps_runtime_source(tmp_path):
    policies(tmp_path)
    result = context.audit(tmp_path)
    assert result['railway_pattern_contract'] == 'passed'
    assert result['private_canaries'] == 30 and result['public_canaries'] == 10
    assert result['docker_engine'] == 'not_requested'


def test_removed_railway_protection_is_caught_without_reading_private_files(tmp_path):
    policies(tmp_path)
    (tmp_path / '.railwayignore').write_text('')
    with pytest.raises(context.ContextAuditError, match='private-boundary policy'):
        context.audit(tmp_path)


@pytest.mark.parametrize('name', ['.dockerignore', '.railwayignore'])
def test_negated_secret_rules_cannot_silently_reinclude_private_files(tmp_path, name):
    policies(tmp_path)
    target = tmp_path / name
    target.write_bytes(target.read_bytes() + b'\n!.env.local\n')
    with pytest.raises(context.ContextAuditError, match='negated'):
        context.audit(tmp_path)


def test_ignore_policy_symlinks_and_oversized_files_are_rejected(tmp_path):
    policies(tmp_path)
    original = tmp_path / '.railwayignore'
    original.unlink()
    original.symlink_to(tmp_path / '.dockerignore')
    with pytest.raises(context.ContextAuditError, match='bounded'):
        context.audit(tmp_path)
    original.unlink()
    original.write_bytes(b'x' * 65537)
    with pytest.raises(context.ContextAuditError, match='bounded'):
        context.audit(tmp_path)


def test_dockerfile_specific_override_cannot_bypass_the_audited_root_policy(tmp_path):
    policies(tmp_path)
    (tmp_path / 'Dockerfile.dockerignore').write_text('!**\n')
    with pytest.raises(context.ContextAuditError, match='overrides'):
        context.audit(tmp_path)


def test_children_do_not_receive_saved_credentials_or_real_context(tmp_path, monkeypatch):
    policies(tmp_path)
    monkeypatch.setenv('RAILWAY_API_TOKEN_MAHTO', 'SYNTHETIC PRIVATE TOKEN')
    called = []
    def inspect(arguments, **kwargs):
        assert 'RAILWAY_API_TOKEN_MAHTO' not in kwargs['env']
        assert str(tmp_path) not in arguments
        called.append(arguments[0])
        if arguments[0] == 'git':
            # Real Git semantics, still only against the synthetic directory.
            return subprocess.run(arguments, **kwargs)
        assert arguments[arguments.index('--host') + 1].startswith('unix:///')
        assert arguments[arguments.index('--builder') + 1] == 'default'
        assert '--config' in arguments
        return subprocess.CompletedProcess(arguments, 1, b'', b'SYNTHETIC PRIVATE ERROR')
    with pytest.raises(context.ContextAuditError, match='Docker context audit failed'):
        context.audit(tmp_path, docker=True, runner=inspect)
    assert called[-1] == 'docker'


def test_cli_never_prints_private_tool_error_details(monkeypatch, capsys):
    import sys
    def fail(*args, **kwargs):
        raise RuntimeError('PRIVATE TOKEN AND PATH')
    monkeypatch.setattr(context, 'audit', fail)
    monkeypatch.setattr(sys, 'argv', ['verify_deployment_context'])
    assert context.main() == 1
    assert 'PRIVATE TOKEN' not in capsys.readouterr().out
