"""Operator-only source-bound missing-image restoration via existing pinned IAP."""
import argparse
import hashlib
import json
from pathlib import Path
import shlex
import subprocess  # nosec B404

from app.coding import operator_readback_repair as repair, operator_cleanup_guard as cleanup
from app.coding import operator_image_restore as restore
from app.coding.validation_transport import IAPValidationHost, _protected, command


REVIEWED_SOURCES = {'4d5df079af64b0440ce864992b2de746a86c4243157668e0fffdc3354cce30b7'}


def frozen_program(expected):
    if expected not in REVIEWED_SOURCES:
        raise RuntimeError('unreviewed historical restoration source')
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(  # nosec B603
        ['/usr/bin/git', '-C', str(root), '--no-replace-objects', 'cat-file', 'blob',
         'refs/gca/operator-restorations/' + expected], capture_output=True, timeout=10, check=False,
        env={'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent', 'GIT_CONFIG_NOSYSTEM': '1',
             'GIT_CONFIG_GLOBAL': '/dev/null'})
    if (result.returncode or not 0 < len(result.stdout) <= 100000
            or hashlib.sha256(result.stdout).hexdigest() != expected):
        raise RuntimeError('reviewed restoration source custody differs')
    source = result.stdout.decode()
    compile(source, 'reviewed_historical_restore_controller', 'exec')
    return source


def program():
    # Complete reviewed stdlib helpers, not generated code or arbitrary tool input.
    source = "import runpy,sys,types\ng=runpy.run_path('/usr/local/libexec/gca/operator_toolchain_host.py')\n"
    for module in (repair, cleanup, restore):
        body = Path(module.__file__).read_text()
        compile(body, module.__name__, 'exec')
        source += ('m=types.ModuleType(' + repr(module.__name__) + ')\n'
                   'sys.modules[' + repr(module.__name__) + ']=m\n'
                   'exec(compile(' + repr(body) + ',' + repr(module.__name__) + ",'exec'),m.__dict__)\n")
    source += 'm.main(g)\n'
    if len(source.encode()) > 100000:
        raise RuntimeError('reviewed restoration controller exceeds bound')
    return source


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--operator-config', type=Path, required=True)
    parser.add_argument('--intent-id', required=True)
    parser.add_argument('--operation', choices=('prepare', 'dispatch', 'observe'), default='observe')
    parser.add_argument('--reviewed-program-sha256', choices=sorted(REVIEWED_SOURCES))
    args = parser.parse_args()
    restore.unit_name(args.intent_id)
    source = frozen_program(args.reviewed_program_sha256) if args.reviewed_program_sha256 else program()
    request = {'version': 1, 'operation': args.operation, 'intent_id': args.intent_id,
               'expected_snapshot_hash': repair.OLD_SNAPSHOT,
               'controller_sha256': hashlib.sha256(source.encode()).hexdigest()}
    host = IAPValidationHost.model_validate_json(_protected(args.operator_config.resolve(), private=True).read_text())
    argv, env = command(host)
    argv[-1] = shlex.join(['sudo', '-n', '/usr/bin/python3', '-I', '-B', '-c', source])
    result = subprocess.run(argv, env=env, input=restore.canonical(request), capture_output=True,  # nosec B603
                            timeout=120, check=False)
    if result.returncode or len(result.stdout) > restore.MAX_DOCUMENT:
        raise RuntimeError('restoration unconfirmed; inspect this exact plan/unit before retry')
    evidence = json.loads(result.stdout)
    if evidence.get('operation') != args.operation or evidence.get('intent_id') != args.intent_id:
        raise RuntimeError('restoration response identity differs')
    print(json.dumps(evidence, sort_keys=True))


if __name__ == '__main__':
    main()
