"""Explicit reviewed operator repair over existing pinned IAP; no blind retries."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess  # nosec B404

from app.coding import operator_readback_repair as repair
from app.coding.validation_transport import IAPValidationHost, _protected, command


def verify(request, result):
    if (not isinstance(result, dict) or result.get('operation') != request['operation']
            or result.get('intent_id') != request['intent_id']
            or result.get('phase') != ('rejected' if request['operation'] == 'reject_repaired' else 'loaded')
            or result.get('image_approved') is not False or result.get('configuration_changed') is not False
            or result.get('docker_load_invoked') is not False
            or not isinstance(result.get('repair_plan_sha256'), str)
            or not re.fullmatch(r'[a-f0-9]{64}', result['repair_plan_sha256'])):
        raise RuntimeError('readback repair evidence is unconfirmed')
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--operator-config', type=Path, required=True)
    parser.add_argument('--operation', choices=('repair_readback', 'reject_repaired'), required=True)
    parser.add_argument('--intent-id', required=True)
    parser.add_argument('--expected-snapshot-hash', required=True)
    args = parser.parse_args()
    if any(not re.fullmatch(r'[a-f0-9]{64}', value) for value in (args.intent_id, args.expected_snapshot_hash)):
        parser.error('exact original intent and installed snapshot are required')
    host = IAPValidationHost.model_validate_json(_protected(args.operator_config.resolve(), private=True).read_text())
    source = Path(repair.__file__).read_text()
    request = {'version': 1, 'operation': args.operation, 'intent_id': args.intent_id,
               'expected_snapshot_hash': args.expected_snapshot_hash,
               'controller_sha256': hashlib.sha256(source.encode()).hexdigest()}
    argv, env = command(host)
    argv[-1] = shlex.join(['sudo', '-n', '/usr/bin/python3', '-I', '-B', '-c', source])
    result = subprocess.run(argv, env=env, input=repair.canonical(request), capture_output=True,  # nosec B603
                            timeout=120, check=False)
    if result.returncode or len(result.stdout) > repair.MAX_PLAN:
        raise RuntimeError('operator repair unconfirmed; inspect this exact audit before retry')
    print(json.dumps(verify(request, json.loads(result.stdout)), sort_keys=True))


if __name__ == '__main__':
    main()
