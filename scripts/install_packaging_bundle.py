"""Operator-only reviewed seven-file rollout over existing pinned IAP transport.

Default probes only. Neither apply nor rollback changes policy, toolchains, resource
limits, sudoers, cloud resources or model routes. Not callable by generated code.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess  # nosec B404

from app.coding import operator_bundle as bundle
from app.coding.validation_transport import IAPValidationHost, _protected, command


def payload(args):
    if getattr(args, 'retire', None):
        return {'operation': 'retire', 'installation_id': args.retire,
                'expected_snapshot_hash': args.expected_snapshot_hash,
                'expected_retention_hash': args.expected_retention_hash}
    if args.rollback:
        return {"operation": "rollback", "installation_id": args.rollback}
    if not args.apply:
        return {"operation": "probe"}
    source = Path(bundle.__file__).parent
    return {"operation": "apply", "expected_snapshot_hash": args.expected_snapshot_hash,
            "files": {name: (source / name).read_text() for name in bundle.NAMES}}


def verify(request, evidence):
    if not isinstance(evidence, dict) or evidence.get("operation") != request["operation"]:
        raise RuntimeError("Operator bundle evidence is unconfirmed")
    if request["operation"] == "apply":
        expected = {name: hashlib.sha256(source.encode()).hexdigest() for name, source in request["files"].items()}
        if (evidence.get("hashes") != expected or evidence.get("isolated_import_passed") is not True
                or evidence.get("policy_changed") is not False or evidence.get("workload_executed") is not False):
            raise RuntimeError("Installed operator bundle differs from reviewed source")
    elif request["operation"] == "rollback":
        if evidence.get("installation_id") != request["installation_id"] or evidence.get("rolled_back") is not True:
            raise RuntimeError("Operator rollback identity is unconfirmed")
    elif request['operation'] == 'retire':
        if (evidence.get('installation_id') != request['installation_id'] or evidence.get('retired') is not True
                or evidence.get('active_bundle_preserved') is not True or evidence.get('data_journal_changed') is not False
                or evidence.get('snapshot_hash') != request['expected_snapshot_hash']):
            raise RuntimeError('Operator retirement evidence is unconfirmed')
    return evidence


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--operator-config", type=Path, required=True)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true")
    group.add_argument("--rollback", metavar="INSTALLATION_ID")
    group.add_argument('--retire', metavar='OBSOLETE_INSTALLATION_ID')
    parser.add_argument("--expected-snapshot-hash")
    parser.add_argument('--expected-retention-hash')
    args = parser.parse_args()
    if (args.apply or args.retire) and not re.fullmatch(r"[a-f0-9]{64}", args.expected_snapshot_hash or ""):
        parser.error("Apply requires the previously observed full bundle snapshot hash")
    if args.rollback and not re.fullmatch(r"[a-f0-9]{64}", args.rollback):
        parser.error("Rollback requires the exact installation ID from the probe")
    if args.retire and (not re.fullmatch(r'[a-f0-9]{64}', args.retire)
                        or not re.fullmatch(r'[a-f0-9]{64}', args.expected_retention_hash or '')):
        parser.error('Retirement requires the exact obsolete ID and fresh retention hash')
    config = _protected(args.operator_config.resolve(), private=True)
    host = IAPValidationHost.model_validate_json(config.read_text())
    argv, environment = command(host)
    reviewed_source = Path(bundle.__file__).read_text()
    argv[-1] = shlex.join(["sudo", "-n", "/usr/bin/python3", "-I", "-B", "-c", reviewed_source])
    request = payload(args)
    body = json.dumps(request, separators=(",", ":")).encode()
    if len(body) > bundle.MAX_FRAME:
        raise RuntimeError("Reviewed operator bundle exceeds framing limit")
    result = subprocess.run(  # nosec B603
        argv, input=body, capture_output=True, env=environment, timeout=60, check=False,
    )
    if result.returncode or len(result.stdout) > 8192:
        raise RuntimeError("Operator installation unconfirmed; probe before retry, never blind redispatch")
    print(json.dumps(verify(request, json.loads(result.stdout)), sort_keys=True))


if __name__ == "__main__":
    main()
