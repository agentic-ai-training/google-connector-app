"""Fixed reviewed Python-image recovery over pinned operator IAP, not model RPC."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess  # nosec B404

from app.coding import operator_cleanup_guard as cleanup, operator_readback_repair as repair
from app.coding import operator_image_restore as shared, operator_legacy_restore as restore
from app.coding.validation_transport import IAPValidationHost, _protected, command


REVIEWED_PROGRAMS = {'10c8d28a254f316af9f0f7bc72caec2bf79d07e5fba7b7be145735d90dc7c490'}


def frozen_program(expected):
    """Observe a retained reservation even after local source advances."""
    if expected not in REVIEWED_PROGRAMS:
        raise RuntimeError('legacy historical controller was not reviewed')
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(  # nosec B603
        ['/usr/bin/git', '-C', str(root), '--no-replace-objects', 'cat-file', 'blob',
         'refs/gca/legacy-restorations/' + expected], capture_output=True, timeout=10, check=False,
        env={'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent', 'GIT_CONFIG_NOSYSTEM': '1',
             'GIT_CONFIG_GLOBAL': '/dev/null'})
    if (result.returncode or not 0 < len(result.stdout) <= 120000
            or hashlib.sha256(result.stdout).hexdigest() != expected):
        raise RuntimeError('legacy historical controller custody differs')
    source = result.stdout.decode()
    compile(source, 'retained_reviewed_legacy_controller', 'exec')
    return source


def program():
    source = "import runpy,sys,types\ng=runpy.run_path('/usr/local/libexec/gca/operator_toolchain_host.py')\n"
    for module in (repair, cleanup, shared, restore):
        body = Path(module.__file__).read_text()
        compile(body, module.__name__, 'exec')
        source += ('m=types.ModuleType(' + repr(module.__name__) + ')\n'
                   'sys.modules[' + repr(module.__name__) + ']=m\n'
                   'exec(compile(' + repr(body) + ',' + repr(module.__name__) + ",'exec'),m.__dict__)\n")
    source += 'm.main(g)\n'
    if len(source.encode()) > 120000:
        raise RuntimeError('legacy recovery source exceeds bound')
    return source


def verify(request, value):
    if (not isinstance(value, dict) or value.get('operation') != request['operation']
            or value.get('prepared') is not False and (
                not isinstance(value.get('identity'), str)
                or not re.fullmatch('[a-f0-9]{64}', value['identity']))):
        raise RuntimeError('legacy recovery response identity differs')
    if request['operation'] in {'reconcile', 'cancel'} and (
        value.get('phase') != ('completed' if request['operation'] == 'reconcile' else 'cancelled')
        or value.get('image_approval_changed') is not False or value.get('allocation_changed') is not False
    ):
        raise RuntimeError('legacy recovery terminal evidence is unconfirmed')
    return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--operator-config', type=Path, required=True)
    parser.add_argument('--operation', choices=('prepare', 'dispatch', 'observe', 'reconcile', 'cancel'),
                        default='observe')
    for name in ('bundle', 'policy', 'registry'):
        parser.add_argument('--expected-' + name + '-sha256', required=True)
    parser.add_argument('--reviewed-program-sha256', choices=sorted(REVIEWED_PROGRAMS))
    args = parser.parse_args()
    source = frozen_program(args.reviewed_program_sha256) if args.reviewed_program_sha256 else program()
    authority = {name + '_sha256': getattr(args, 'expected_' + name + '_sha256')
                 for name in ('bundle', 'policy', 'registry')}
    authority['controller_sha256'] = hashlib.sha256(source.encode()).hexdigest()
    if any(not re.fullmatch('[a-f0-9]{64}', value) for value in authority.values()):
        parser.error('exact fresh source/configuration hashes required')
    request = {'version': 1, 'operation': args.operation, 'authority': authority}
    host = IAPValidationHost.model_validate_json(_protected(args.operator_config.resolve(), private=True).read_text())
    argv, environment = command(host)
    argv[-1] = shlex.join(['sudo', '-n', '/usr/bin/python3', '-I', '-B', '-c', source])
    result = subprocess.run(argv, env=environment, input=restore.canonical(request),  # nosec B603
                            capture_output=True, timeout=60, check=False)
    if result.returncode or len(result.stdout) > restore.MAX_DOCUMENT:
        raise RuntimeError('legacy recovery unconfirmed; observe the same reservation, never redispatch blindly')
    print(json.dumps(verify(request, json.loads(result.stdout)), sort_keys=True))


if __name__ == '__main__':
    main()
