"""Operator-only pinned IAP custody transport; defaults to read-only inspection.

Streams an already-reviewed local image by immutable ID, without a Mac archive.
Docker load requires an explicit separate fixed-unit operation; no policy publication
or production access. Actions require the protected config/source fingerprint.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess  # nosec B404

from app.coding import operator_toolchain_host as gateway, toolchain_spec as catalog
from app.coding import toolchain_staging as custody
from app.coding.validation_transport import IAPValidationHost, _protected, command
from scripts.probe_local_offline_toolchains import DOCKER, ENV, inspect
from scripts.probe_local_offline_toolchains import fixture_archive
from app.coding import toolchain_proof as proof


MAX_RESPONSE = 32768


def bundle_hash():
    root = Path(gateway.__file__).parent
    values = {name: {"hash": hashlib.sha256((root / name).read_bytes()).hexdigest(),
                     "mode": 0o755 if name == "isolated_validation.py" else 0o644}
              for name in gateway.BUNDLE_NAMES}
    return custody.checksum(custody.canonical(values))


def envelope(operation, **values):
    request = {"version": 1, "operation": operation, "bundle_sha256": bundle_hash(), **values}
    body = custody.canonical(request)
    if len(body) > gateway.MAX_FRAME:
        raise RuntimeError("operator request exceeds framing bound")
    return request, len(body).to_bytes(4, "big") + body


def stop_owned(process):
    if process is None:
        return
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=5)
    for stream in (process.stdin, process.stdout):
        if stream is not None and not stream.closed:
            try:
                stream.close()
            except BrokenPipeError:
                pass


def proof_receipt(value, identity, case):
    role = case.split('_', 1)[0]
    if (not isinstance(value, dict) or value.get('intent_id') != identity
            or value.get('case') != case or value.get('version') != 1
            or type(value.get('version')) is not int
            or value.get('source_sha256') != proof.FIXTURES[role][case.rsplit('_', 1)[1]][1]
            or value.get('cleanup_verified') is not True or type(value.get('passed')) is not bool
            or type(value.get('exit_code')) is not int
            or value['passed'] and value['exit_code'] != (1 if case.endswith('_fail') else 0)
            or type(value.get('observed_at_unix')) is not int or value['observed_at_unix'] < 1
            or not isinstance(value.get('job_id'), str) or not re.fullmatch(r'[a-f0-9]{32}', value['job_id'])
            or any(not isinstance(value.get(key), str) or not re.fullmatch(r'[a-f0-9]{64}', value[key])
                   for key in ('binding_sha256', 'native_enforcement_sha256', 'result_sha256'))):
        raise RuntimeError('host fixed-fixture evidence differs')


def proof_evidence(evidence, operation, identity, case=None):
    value = evidence.get('proof')
    if not isinstance(value, dict) or evidence.get('image_approved') is not False:
        raise RuntimeError('host fixed-fixture evidence differs')
    if value.get('intent_id') != identity or value.get('image_approved') is not False:
        raise RuntimeError('host fixed-fixture evidence differs')
    if operation == 'proof_run':
        proof_receipt(value, identity, case)
        if type(value.get('replayed_observation')) is not bool:
            raise RuntimeError('host fixed-fixture evidence differs')
    else:
        cases = value.get('cases')
        if (not isinstance(cases, dict) or set(cases) not in [set(group) for group in proof.CASES.values()]
                or type(value.get('all_fixed_cases_passed')) is not bool
                or any(item is not None and not isinstance(item, dict) for item in cases.values())):
            raise RuntimeError('host fixed-fixture evidence differs')
        for name, receipt in cases.items():
            if receipt is not None:
                proof_receipt(receipt, identity, name)
        if value['all_fixed_cases_passed'] != all(item is not None and item['passed'] for item in cases.values()):
            raise RuntimeError('host fixed-fixture evidence differs')


def rpc(host, operation, *, export_role=None, fixture_role=None, **values):
    """No automatic retries. Client loss leaves the host reservation for inspection."""
    request, control = envelope(operation, **values)
    role = export_role
    source = None
    if operation == 'proof_run':
        case = values.get('case')
        if fixture_role not in proof.CASES or case not in proof.CASES[fixture_role]:
            raise RuntimeError('fixed local fixture role/case required')
        source, _ = fixture_archive(fixture_role, negative=case.endswith('_fail'))
        proof.fixture(fixture_role, case, source)
    argv, environment = command(host)
    argv[-1] = shlex.join(["sudo", "-n", "/usr/bin/python3", "-I", "-B",
                         "/usr/local/libexec/gca/operator_toolchain_host.py"])
    previous = signal.getsignal(signal.SIGALRM)

    def timeout(*_):
        raise TimeoutError("operator transport deadline exceeded; inspect the same intent")

    exporter = remote = None
    signal.signal(signal.SIGALRM, timeout)
    signal.alarm(300 if operation == 'proof_run' else 240)
    try:
        if operation == "receive" and role is not None:
            if role not in catalog.REVIEWED_ARCHIVES or inspect(role) != catalog.REVIEWED_ARCHIVES[role]["image_id"]:
                raise RuntimeError("local public toolchain differs from reviewed acquisition")
            exporter = subprocess.Popen(  # nosec B603
                [DOCKER, "image", "save", catalog.REVIEWED_ARCHIVES[role]["image_id"]],
                env=ENV, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
        remote = subprocess.Popen(  # nosec B603
            argv, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            remote.stdin.write(control)
            if operation == 'proof_run':
                remote.stdin.write(source)
            if exporter is not None:
                size, digest = 0, hashlib.sha256()
                while body := exporter.stdout.read(1024 * 1024):
                    size += len(body)
                    if size > catalog.REVIEWED_ARCHIVES[role]["archive_bytes"]:
                        raise RuntimeError("export stream exceeds reviewed archive count")
                    digest.update(body)
                    remote.stdin.write(body)
                if (exporter.wait(timeout=10) != 0 or size != catalog.REVIEWED_ARCHIVES[role]["archive_bytes"]
                        or digest.hexdigest() != catalog.REVIEWED_ARCHIVES[role]["archive_sha256"]):
                    raise RuntimeError("export stream custody differs; inspect the same host intent")
            remote.stdin.close()
        except BrokenPipeError:
            # A complete earlier transfer may be reconciled without reading this
            # exporter. Only an exact positive host custody receipt can count.
            stop_owned(exporter)
            exporter = None
        body = remote.stdout.read(MAX_RESPONSE + 1)
        if len(body) > MAX_RESPONSE or remote.wait(timeout=15) != 0:
            raise RuntimeError("host custody observation unconfirmed; inspect before retry")
        evidence = json.loads(body, object_pairs_hook=catalog.unique_object)
        if (not isinstance(evidence, dict) or evidence.get("operation") != request["operation"]
                or evidence.get("production_policy_changed") is not False):
            raise RuntimeError("host custody evidence differs")
        if operation == "receive":
            result = evidence.get("custody", {})
            if (not isinstance(result, dict) or not isinstance(result.get("archive"), dict)
                    or result.get("intent_id") != values.get("intent_id")
                    or result["archive"].get("image_loaded") is not False
                    or result["archive"].get("image_approved") is not False):
                raise RuntimeError("host custody receipt differs")
            if role is not None and any(result["archive"].get(key) != catalog.REVIEWED_ARCHIVES[role][key]
                                        for key in ("archive_sha256", "archive_bytes")):
                raise RuntimeError("host archive custody differs from reviewed role")
            if role is not None and result["archive"].get("image_index_digest") != catalog.REVIEWED_ARCHIVES[role]["image_id"]:
                raise RuntimeError("host image identity differs from reviewed role")
        if operation in {'proof_run', 'proof_observe'}:
            proof_evidence(evidence, operation, values.get('intent_id'), values.get('case'))
        if operation == 'publish_prepare':
            result = evidence.get('publication')
            if (not isinstance(result, dict) or result.get('intent_id') != values.get('intent_id')
                    or result.get('phase') != 'prepared' or result.get('image_approved') is not False
                    or evidence.get('image_approved') is not False or result.get('configuration_published') is not False
                    or any(not isinstance(result.get(key), str) or not re.fullmatch(r'[a-f0-9]{64}', result[key])
                           for key in ('plan_sha256', 'policy_sha256', 'registry_sha256'))):
                raise RuntimeError('host publication preparation evidence differs')
        if operation == 'publish':
            result = evidence.get('publication')
            if (not isinstance(result, dict) or result.get('intent_id') != values.get('intent_id')
                    or result.get('plan_sha256') != values.get('plan_sha256')
                    or type(result.get('version')) is not int or result.get('version') != 1
                    or result.get('image_approved') is not True or evidence.get('image_approved') is not True
                    or result.get('configuration_published') is not True
                    or evidence.get('validation_policy_changed') is not True
                    or type(result.get('replayed_observation')) is not bool
                    or any(not isinstance(result.get(key), str) or not re.fullmatch(r'[a-f0-9]{64}', result[key])
                           for key in ('policy_sha256', 'registry_sha256'))):
                raise RuntimeError('host verified publication evidence differs')
        return evidence
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
        stop_owned(exporter)
        stop_owned(remote)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--operator-config", type=Path, required=True)
    parser.add_argument("--operation", choices=("probe", "prepare", "receive", "quarantine", "cancel_preload",
                                              "load_prepare", "load_dispatch", "load_observe",
                                              "proof_run", "proof_observe", "publish_prepare", "publish"),
                        default="probe")
    parser.add_argument("--role", choices=("react", "spring"))
    parser.add_argument("--intent-id")
    parser.add_argument("--policy-sha256")
    parser.add_argument("--registry-sha256")
    parser.add_argument('--plan-sha256')
    parser.add_argument("--partial-sha256")
    parser.add_argument("--partial-bytes", type=int)
    parser.add_argument("--expected-phase", choices=("prepared", "archive_verified"))
    parser.add_argument('--case', choices=tuple(case for cases in proof.CASES.values() for case in cases))
    args = parser.parse_args()
    values = {}
    if args.operation == "prepare":
        if not args.role:
            parser.error("prepare requires a reviewed role")
        for name in ("policy_sha256", "registry_sha256"):
            value = getattr(args, name)
            if not re.fullmatch(r"[a-f0-9]{64}", value or ""):
                parser.error("prepare requires fresh policy and registry observations")
            values[name] = value
        values["role"] = args.role
    elif args.operation != "probe":
        if not re.fullmatch(r"[a-f0-9]{64}", args.intent_id or ""):
            parser.error("operation requires the exact reserved intent ID")
        values["intent_id"] = args.intent_id
        if args.operation == 'publish':
            if not re.fullmatch(r'[a-f0-9]{64}', args.plan_sha256 or ''):
                parser.error('publication requires the exact independently reviewed preparation hash')
            values['plan_sha256'] = args.plan_sha256
        if args.operation == "quarantine":
            if not re.fullmatch(r"[a-f0-9]{64}", args.partial_sha256 or "") or args.partial_bytes is None:
                parser.error("quarantine requires exact partial custody")
            values.update(partial_sha256=args.partial_sha256, partial_bytes=args.partial_bytes)
        if args.operation == "cancel_preload":
            if not args.expected_phase:
                parser.error("cancel requires the exact pre-load phase")
            values["expected_phase"] = args.expected_phase
        if args.operation == 'proof_run':
            if args.role not in proof.CASES or args.case not in proof.CASES[args.role]:
                parser.error('proof requires the fixed case belonging to the reviewed role')
            values['case'] = args.case
    host = IAPValidationHost.model_validate_json(_protected(args.operator_config.resolve(), private=True).read_text())
    role = args.role if args.operation == "receive" else None
    # prepare's role is a control field, not an instruction to export an image.
    print(json.dumps(rpc(host, args.operation, export_role=role,
                         fixture_role=args.role if args.operation == 'proof_run' else None, **values),
                     sort_keys=True))


if __name__ == "__main__":
    main()
