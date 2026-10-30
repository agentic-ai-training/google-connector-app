"""Reviewed operator-only cleanup guard via pinned IAP. Default is read-only."""
import argparse
import hashlib
import json
from pathlib import Path
import shlex
import subprocess  # nosec B404

from app.coding import operator_cleanup_guard as guard
from app.coding.validation_transport import IAPValidationHost, _protected, command


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--operator-config', type=Path, required=True)
    parser.add_argument('--expected-snapshot-hash', required=True)
    parser.add_argument('--expected-settings-sha256', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    source = Path(guard.__file__).read_text()
    host = IAPValidationHost.model_validate_json(_protected(args.operator_config.resolve(), private=True).read_text())
    argv, env = command(host)
    argv[-1] = shlex.join(['sudo', '-n', '/usr/bin/python3', '-I', '-B', '-c', source])
    request = {'version': 1, 'operation': 'apply' if args.apply else 'probe',
               'expected_snapshot_hash': args.expected_snapshot_hash,
               'expected_settings_sha256': args.expected_settings_sha256,
               'controller_sha256': hashlib.sha256(source.encode()).hexdigest()}
    result = subprocess.run(argv, env=env, input=guard.canonical(request), capture_output=True,  # nosec B603
                            timeout=90, check=False)
    if result.returncode or len(result.stdout) > guard.MAX_DOCUMENT:
        raise RuntimeError('cleanup operation unconfirmed; inspect audit before retry')
    evidence = json.loads(result.stdout)
    if evidence.get('operation') != request['operation']:
        raise RuntimeError('cleanup operation identity differs')
    if args.apply and (evidence.get('settings') != guard.AFTER
                       or evidence.get('settings_sha256') != guard.digest(guard.AFTER)
                       or any(evidence.get(key) is not False for key in
                              ('cleanup_invoked', 'images_restored', 'allocation_changed', 'policy_changed',
                               'volumes_deleted', 'networks_deleted'))):
        raise RuntimeError('cleanup update evidence differs')
    print(json.dumps(evidence, sort_keys=True))


if __name__ == '__main__':
    main()
