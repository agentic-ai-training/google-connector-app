"""Keyless preflight: candidate checkout is upload data, never controller code."""
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess


class SourceError(RuntimeError):
    pass


def git(root, *args):
    env = {'PATH': os.environ['PATH'], 'HOME': os.environ['HOME'],
           'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_TERMINAL_PROMPT': '0'}
    result = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
        '-C', str(root), *args], env=env, capture_output=True, timeout=20, check=False)
    if result.returncode or len(result.stdout) > 4_000_000:
        raise SourceError('Candidate Git identity or inventory is unavailable')
    return result.stdout


def verify(root, controller, version, repository, surfaces, proposal_key):
    root, controller = Path(root), Path(controller)
    if (not root.is_absolute() or root.is_symlink() or root.resolve() == controller.resolve()
            or root.resolve().parent != controller.resolve().parent
            or root.resolve() in controller.resolve().parents or controller.resolve() in root.resolve().parents):
        raise SourceError('Candidate and controller must be separate sibling checkouts')
    if not re.fullmatch('[0-9a-f]{40}', version) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise SourceError('Immutable candidate/repository identity is invalid')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}', proposal_key):
        raise SourceError('Candidate proposal identity is invalid')
    selected = surfaces.split(',')
    if not selected or len(selected) != len(set(selected)) or set(selected) - {'api', 'worker', 'frontend', 'registry'}:
        raise SourceError('Candidate runtime surfaces are invalid')
    if git(root, 'rev-parse', 'HEAD').decode().strip() != version:
        raise SourceError('Candidate checkout does not match its frozen commit')
    origin = git(root, 'remote', 'get-url', 'origin').decode().strip().removesuffix('.git')
    if origin != 'https://github.com/' + repository:
        raise SourceError('Candidate checkout belongs to another repository')
    git(root, 'diff', '--quiet', '--no-ext-diff', '--no-textconv', 'HEAD', '--')
    if git(root, 'ls-files', '--others', '-z'):
        raise SourceError('Candidate checkout contains untracked upload content')
    entries = git(root, 'ls-files', '--stage', '-z').split(b'\0')
    count, total = 0, 0
    for entry in filter(None, entries):
        metadata, raw_path = entry.split(b'\t', 1)
        mode, _, stage = metadata.split()
        path = raw_path.decode('utf-8')
        if (mode not in {b'100644', b'100755'} or stage != b'0' or '\\' in path
                or PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts
                or PurePosixPath(path).as_posix() != path):
            raise SourceError('Candidate upload requires canonical regular files, not symlinks/submodules')
        target = root / path
        if any(parent.is_symlink() for parent in (target, *target.parents)):
            raise SourceError('Candidate upload path follows a symlink')
        info = target.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 16_777_216:
            raise SourceError('Candidate upload inode or file size is invalid')
        count += 1
        total += info.st_size
        if count > 20000 or total > 134_217_728:
            raise SourceError('Candidate upload tree exceeds its resource bound')
    if not count:
        raise SourceError('Candidate upload tree is empty')
    return {'source_commit': version, 'files': count, 'bytes': total, 'candidate_code_executed': False}


def main():
    if os.environ.get('GITHUB_REF') != 'refs/heads/main':
        raise SourceError('Deployment controller must run from the trusted main branch')
    controller = Path(__file__).resolve().parents[1]
    result = verify(os.environ['CANDIDATE_SOURCE_ROOT'], controller, os.environ['CANDIDATE_VERSION'],
        os.environ['GITHUB_REPOSITORY'], os.environ['CANDIDATE_RUNTIME_SURFACES'], os.environ['PROPOSAL_KEY'])
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        raise SystemExit('Candidate deployment source rejected: ' + type(exc).__name__) from None
