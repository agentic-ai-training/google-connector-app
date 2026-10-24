"""Audit synthetic upload/build canaries; never copy real private project contents."""
import argparse
import json
import os
from pathlib import Path
import subprocess  # nosec B404
import sys
import tempfile


PRIVATE_PATHS = (
    '.env', '.env.local', '.env.production', '.env.preview', 'web/.env.local',
    'nested/deep/.env.private', 'credentials.json', 'nested/credentials-client.json',
    'google-oauth-web.json', 'nested/client_secret_example.json', 'token.pkl', 'nested/token.json',
    'nested/private.pem', 'nested/private.key', 'nested/id_ed25519', 'nested/id_rsa.pub',
    '.gca-local/secret.json', 'nested/.gca-local/receipt.json', '.railway/state.json', '.vercel/project.json',
    'knowledge-private/private.md', 'backups/database.sql', 'nested/backups/database.sql',
    'nested/node_modules/private.js', '.venv/bin/python', '.git/config',
    'run.log', 'nested/database.dump', 'nested/data.sqlite3', 'dump.sql.gz',
)
PUBLIC_PATHS = (
    'Dockerfile', 'requirements.txt', 'app/api/main.py', 'migrations/versions/example.py',
    'knowledge/index.md', 'scripts/operator.py', 'monitoring/alloy/config.alloy',
    'coding_runtime/Cargo.toml', 'coding_runtime/Cargo.lock', 'coding_runtime/src/main.rs',
)
REQUIRED_DENY_PATTERNS = frozenset({
    '**/.env*', '**/.gca-local', '**/.railway', '**/.vercel', '**/knowledge-private',
    '**/backups', '**/*credentials*.json', '**/*oauth*.json', '**/client_secret*.json',
    '**/token*.json', '**/token*.pkl', '**/*.pem', '**/*.key', '**/id_rsa*', '**/id_ed25519*',
})


class ContextAuditError(RuntimeError):
    pass


def _read_policy(root, name):
    path = root / name
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
        raise ContextAuditError('regular bounded deployment ignore policies required')
    return path.read_bytes()


def _run(arguments, *, env, cwd, timeout=30, runner=subprocess.run):
    try:
        result = runner(arguments, cwd=cwd, env=env, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise ContextAuditError('audit tool unavailable or timed out') from None
    if len(result.stdout) + len(result.stderr) > 1048576:
        raise ContextAuditError('audit tool output exceeded bound')
    return result


def audit(root: Path, *, docker=False, runner=subprocess.run) -> dict:
    root = root.resolve(strict=True)
    if any(path.name != '.dockerignore' for path in root.glob('*.dockerignore')):
        raise ContextAuditError('Dockerfile-specific ignore overrides require separate review')
    policies = {name: _read_policy(root, name) for name in ('.dockerignore', '.railwayignore')}
    for name, content in policies.items():
        lines = {line.strip() for line in content.decode('utf-8').splitlines()
                 if line.strip() and not line.lstrip().startswith('#')}
        # A closed no-negation contract prevents a later ! rule from silently
        # re-including private state. Real Docker matching is still tested in CI.
        if not REQUIRED_DENY_PATTERNS <= lines or any(line.startswith('!') for line in lines):
            raise ContextAuditError('deployment private-boundary policy is incomplete or negated')
    # No provider, deployment, Google or repository credentials reach child tools.
    env = {'PATH': os.environ.get('PATH', ''), 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull}
    with tempfile.TemporaryDirectory(prefix='gca-context-audit-') as directory:
        temporary = Path(directory)
        context = temporary / 'synthetic-context'
        context.mkdir()
        for name in PRIVATE_PATHS + PUBLIC_PATHS:
            target = context / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('SYNTHETIC CONTEXT CANARY ONLY\n')
        for name, content in policies.items():
            (context / name).write_bytes(content)
        (context / 'Dockerfile').write_text('FROM scratch\nCOPY . /context/\n')
        # An external Git directory avoids interpreting the fake .git/config canary.
        git_dir = temporary / 'git-control'
        initialized = _run(['git', 'init', '--bare', str(git_dir)], env=env, cwd=temporary, runner=runner)
        if initialized.returncode:
            raise ContextAuditError('synthetic git control initialization failed')
        for name in PRIVATE_PATHS + PUBLIC_PATHS:
            result = _run(['git', '--git-dir', str(git_dir), '--work-tree', str(context),
                '-c', 'core.excludesFile=' + str(context / '.railwayignore'),
                'check-ignore', '--no-index', '--', name], env=env, cwd=context, runner=runner)
            if result.returncode not in (0, 1) or (result.returncode == 0) != (name in PRIVATE_PATHS):
                raise ContextAuditError('Railway-pattern synthetic boundary failed: ' + name)
        result = {'railway_pattern_contract': 'passed', 'private_canaries': len(PRIVATE_PATHS),
                  'public_canaries': len(PUBLIC_PATHS), 'docker_engine': 'not_requested'}
        if docker:
            output = temporary / 'exported'
            config = temporary / 'empty-docker-config'
            config.mkdir(mode=0o700)
            socket = Path.home() / '.docker/run/docker.sock' if sys.platform == 'darwin' else Path('/var/run/docker.sock')
            # No real source/secret file, image pull, RUN instruction or provider
            # connection. Pin a local daemon/default builder and an empty config;
            # a saved remote context or registry credential must not be selected.
            built = _run(['docker', '--host', 'unix://' + str(socket), '--config', str(config),
                          'build', '--builder', 'default', '--network=none', '--output', 'type=local,dest=' + str(output),
                          str(context)], env=env, cwd=temporary, timeout=60, runner=runner)
            if built.returncode:
                raise ContextAuditError('synthetic Docker context audit failed; no build success claimed')
            copied = output / 'context'
            if any((copied / name).exists() for name in PRIVATE_PATHS):
                raise ContextAuditError('Docker context includes a private synthetic canary')
            if any(not (copied / name).is_file() for name in PUBLIC_PATHS):
                raise ContextAuditError('Docker context omitted a required synthetic source')
            result['docker_engine'] = 'passed_synthetic_export'
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--docker', action='store_true', help='Also test real Docker ignore semantics with a scratch-only synthetic export')
    args = parser.parse_args()
    try:
        print(json.dumps(audit(Path(__file__).resolve().parents[1], docker=args.docker), sort_keys=True))
        return 0
    except Exception as error:
        print(json.dumps({'context_audit': 'unconfirmed', 'error_class': type(error).__name__,
                          'private_details_omitted': True}, sort_keys=True))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
