"""Fixed public fixture smoke tests; never production provisioning or gVisor proof."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess  # nosec B404
import tarfile
import time
import uuid

from app.coding import toolchain_spec as spec
from app.coding.isolated_validation import PROFILES, PREPARE_COMMANDS, surefire_report_passed


ROOT = Path(__file__).resolve().parents[1]
DOCKER = '/usr/local/bin/docker' if Path('/usr/local/bin/docker').is_file() else '/usr/bin/docker'
ENV = {'PATH': '/usr/local/bin:/usr/bin:/bin:/Applications/Docker.app/Contents/Resources/bin',
       'HOME': str(Path.home())}
FIXTURES = {
    'react': ('frontend/index.html', 'frontend/src/main.jsx', 'frontend/src/status.jsx',
              'frontend/src/status.test.jsx'),
    'spring': ('backend/src/main/java/com/example/OfflineProofApplication.java',
               'backend/src/test/java/com/example/OfflineProofTest.java'),
}
FAILURE_MARKER = 'gca-reviewed-deliberate-test-failure'


def fixture_archive(role, negative=False):
    directory = ROOT / 'toolchains' / role
    if role == 'react':
        manifest, lock = (directory / 'package.json').read_bytes(), (directory / 'package-lock.json').read_bytes()
        evidence = spec.inspect_react(manifest, lock)
        files = {'frontend/package.json': manifest, 'frontend/package-lock.json': lock}
    else:
        pom = (directory / 'pom.xml').read_bytes()
        evidence = spec.inspect_spring(pom)
        files = {'backend/pom.xml': pom}
    for name in FIXTURES[role]:
        path = ROOT / 'tests/fixtures/coding_toolchains' / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
            raise RuntimeError('Fixed operator fixture differs')
        files[name] = path.read_bytes()
    if negative:
        if role == 'react':
            name = 'frontend/src/status.test.jsx'
            old = b"expect(html).toContain('&lt;script&gt;');"
            new = b"expect(html, '" + FAILURE_MARKER.encode() + b"').toContain('impossible-fixture-value');"
        else:
            name = 'backend/src/test/java/com/example/OfflineProofTest.java'
            old = b'assertTrue(encoder.matches("fixed-nonsensitive-fixture", encoded));'
            new = b'assertTrue(false, "' + FAILURE_MARKER.encode() + b'");'
        if files[name].count(old) != 1:
            raise RuntimeError('Fixed negative fixture mutation differs')
        files[name] = files[name].replace(old, new)
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w') as archive:
        for name, body in sorted(files.items()):
            item = tarfile.TarInfo(name)
            item.size = len(body)
            item.mode = 0o644
            archive.addfile(item, io.BytesIO(body))
    body = output.getvalue()
    return body, {**evidence, 'fixture_sha256': hashlib.sha256(body).hexdigest()}


def docker(*args, payload=None, timeout=30, expected_test_failure=False):
    result = subprocess.run([DOCKER, *args], input=payload, env=ENV,  # nosec B603
                            capture_output=True, timeout=timeout, check=False)
    if len(result.stdout) > 256000 or len(result.stderr) > 256000:
        raise RuntimeError('Local smoke output exceeded bound')
    if expected_test_failure:
        if not result.returncode or FAILURE_MARKER.encode() not in result.stdout + result.stderr:
            raise RuntimeError('Deliberate test failure was not observed')
        return b'Fixed deliberate test failure observed'
    if result.returncode:
        # Only fixed public fixtures run. No user source, token or raw model input.
        detail = (result.stdout[-4096:] + b'\n' + result.stderr[-4096:]).decode(errors='replace')
        raise RuntimeError('Local operator smoke failed: ' + detail)
    return result.stdout


def inspect(role):
    context = docker('context', 'show').decode().strip()
    endpoint = json.loads(docker('context', 'inspect', context))[0]['Endpoints']['docker']['Host']
    if not endpoint.startswith('unix://') or os.environ.get('DOCKER_HOST'):
        raise RuntimeError('Explicit local Unix Docker context required')
    image = json.loads(docker('image', 'inspect', spec.TAGS[role]))[0]
    config = image['Config']
    labels = config.get('Labels') or {}
    if (image['Architecture'] != 'amd64' or image['Os'] != 'linux'
            or config.get('User') != '65532:65532' or config.get('Volumes')
            or config.get('Entrypoint') or config.get('Healthcheck')
            or labels.get('org.gca.toolchain.role') != spec.ROLES[role]
            or labels.get('org.gca.toolchain.base') != spec.BASES[role].split('@')[1]
            or labels.get('org.gca.toolchain.operator-only') != 'true'):
        raise RuntimeError('Reviewed toolchain image metadata differs')
    return image['Id']


def smoke(role, image, source, evidence, negative=False):
    name = 'gca-local-toolchain-' + uuid.uuid4().hex
    cid = None
    deadline = int(time.time()) + 300
    try:
        cid = docker('create', '--name', name, '--rm', '--pull=never', '--platform=linux/amd64',
                     '--label=org.gca.operator-fixture=' + evidence['fixture_sha256'],
                     '--network=none', '--read-only', '--cap-drop=ALL',
                     '--security-opt=no-new-privileges:true', '--user=65532:65532',
                     '--memory=1g', '--memory-swap=1g', '--cpus=1', '--pids-limit=128',
                     '--tmpfs=/workspace:rw,nosuid,nodev,exec,size=256m,mode=1777',
                     '--tmpfs=/tmp:rw,nosuid,nodev,exec,size=64m,mode=1777', '--log-driver=none',
                     '--entrypoint=/bin/sh', image, '-ec',
                     'remaining=$((' + str(deadline) + ' - $(date +%s))); '
                     'test "$remaining" -gt 0; exec sleep "$remaining"').decode().strip()
        observed = json.loads(docker('inspect', cid))[0]
        if observed['Name'] != '/' + name or observed['Image'] != image:
            raise RuntimeError('Local fixture container identity differs')
        docker('start', cid)
        profiles = ('react_test', 'react_build') if role == 'react' else ('spring_test', 'spring_package')
        if negative:
            profiles = profiles[:1]
        steps = ['tar --no-same-owner --no-same-permissions -xf -']
        cwd = PROFILES[profiles[0]][0]
        steps.append('cd ' + shlex.quote(cwd))
        if profiles[0] in PREPARE_COMMANDS:
            steps.append(shlex.join(PREPARE_COMMANDS[profiles[0]]))
        steps.extend(shlex.join(PROFILES[profile][1]) for profile in profiles)
        if not negative:
            steps.append('test -s dist/index.html' if role == 'react' else
                         'test -s target/gca-offline-proof-1.0.0.jar')
        output = docker('exec', '-i', '--workdir=/workspace', cid, '/usr/bin/env', '-i',
                        'PATH=/usr/local/bin:/usr/bin:/bin:/opt/java/openjdk/bin', 'HOME=/tmp',
                        'JAVA_HOME=/opt/java/openjdk', 'MAVEN_OPTS=-Xmx512m -XX:ActiveProcessorCount=1',
                        'CI=true', '/bin/sh', '-ec', '; '.join(steps), payload=source, timeout=240,
                        expected_test_failure=negative)
        if role == 'spring' and not negative:
            report = docker('exec', '--workdir=/workspace/backend', cid,
                            'tar', '-cf', '-', 'target/surefire-reports')
            if not surefire_report_passed(report):
                raise RuntimeError('Executed passing JUnit report required')
        return {'profiles': profiles, 'offline_smoke_passed': not negative,
                'deliberate_test_failure_rejected': negative,
                'fixed_fixture_output': output[-4096:].decode(errors='replace')}
    finally:
        # The container's own finite sleep bounds lifetime after client loss. A
        # lost create response still requires inspecting this exact random name.
        candidates = docker('ps', '-a', '--filter', 'name=^/' + name + '$', '--format', '{{.ID}}').decode().split()
        if candidates:
            if len(candidates) != 1:
                raise RuntimeError('Local fixture cleanup identity uncertain')
            observed = json.loads(docker('inspect', candidates[0]))[0]
            if (observed['Name'] != '/' + name or observed['Image'] != image
                    or observed['Config'].get('Labels', {}).get('org.gca.operator-fixture') != evidence['fixture_sha256']):
                raise RuntimeError('Local fixture cleanup ownership differs')
            docker('rm', '--force', '--volumes', candidates[0])
        if docker('ps', '-a', '--filter', 'name=^/' + name + '$', '--format', '{{.ID}}').strip():
            raise RuntimeError('Local fixture cleanup unconfirmed')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--role', choices=tuple(spec.ROLES), required=True)
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--negative', action='store_true', help='Run only the fixed deliberately failing test')
    args = parser.parse_args()
    source, evidence = fixture_archive(args.role, negative=args.negative)
    image = inspect(args.role)
    result = {'role': args.role, 'image_id': image, 'specification': evidence,
              'private_source_used': False, 'production_activated': False,
              'gvisor_enforcement_proven': False, 'model_calls': 0}
    if args.run:
        result.update(smoke(args.role, image, source, evidence, negative=args.negative))
        result['local_container_cleanup_verified'] = True
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
