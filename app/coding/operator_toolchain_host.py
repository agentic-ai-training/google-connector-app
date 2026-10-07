#!/usr/bin/env python3
"""Fixed operator custody gateway on the approved Delhi VM; never a model tool.

Protected source only, pinned target/bundle, bounded frames and independent alarm.
It stages and explicitly loads only reviewed public images. Loading never changes
policy/registry, approves roles or activates funded production dispatch.
"""
import http.client
import importlib.util
import json
import os
from pathlib import Path
import signal
import stat
import sys
import time
import types


DEPENDENCIES = ("build_artifact", "isolated_validation", "oci_packager",
                "toolchain_spec", "toolchain_archive", "toolchain_staging", "toolchain_loader")
BUNDLE_NAMES = ("isolated_validation.py", "build_artifact.py", "oci_packager.py",
                "runtime_image_store.py", "image_output_store.py", "image_packaging_worker.py",
                "isolated_packaging.py", "toolchain_spec.py", "toolchain_archive.py",
                "toolchain_staging.py", "operator_toolchain_host.py", "toolchain_loader.py")


def _bootstrap():
    root = Path(__file__).absolute().parent
    for path in (root, *root.parents, *(root / name for name in BUNDLE_NAMES)):
        info = path.lstat()
        if (path.is_symlink() or info.st_uid != os.geteuid() and info.st_uid != 0
                or info.st_mode & 0o022 or (path.name in BUNDLE_NAMES and
                                           (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                                            or not 0 < info.st_size <= 100000))):
            raise RuntimeError("toolchain source is not operator protected")
    for name in ("app", "app.coding"):
        module = types.ModuleType(name)
        module.__path__ = []
        sys.modules[name] = module
    for name in DEPENDENCIES:
        identity = "app.coding." + name
        path = root / (name + ".py")
        spec = importlib.util.spec_from_file_location(identity, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[identity] = module
        exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)  # nosec B102


if not __package__:
    _bootstrap()

from app.coding import isolated_validation as isolation, toolchain_staging as custody, toolchain_loader as loader  # noqa: E402
from app.coding.toolchain_spec import REVIEWED_ARCHIVES, unique_object  # noqa: E402


ROOT = Path("/usr/local/libexec/gca")
POLICY = Path("/etc/gca-validation/policy.json")
REGISTRY = Path("/etc/gca-validation/toolchain-registry.json")
TARGET_PROJECT = "project-ee0c1944-144c-4364-ad4"
TARGET_INSTANCE = "822115653245959101"
STOP_UNIX = 1792430672  # 2026-10-19T17:24:32Z; never extended by a frame.
MAX_FRAME = 4096


class OperatorToolchainError(ValueError):
    pass


def snapshot():
    custody.protected(ROOT)
    if {path.name for path in ROOT.iterdir()} != set(BUNDLE_NAMES):
        raise OperatorToolchainError("operator source inventory differs")
    result = {}
    for name in BUNDLE_NAMES:
        path = custody.protected(ROOT / name)
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 100000:
            raise OperatorToolchainError("operator source bound differs")
        result[name] = {"hash": custody.checksum(path.read_bytes()), "mode": stat.S_IMODE(info.st_mode)}
    return custody.checksum(custody.canonical(result))


def configuration():
    def read(path):
        custody.protected(path, private=True)
        if not path.is_file() or not 0 < path.stat().st_size <= 32768:
            raise OperatorToolchainError("operator configuration bound differs")
        body = path.read_bytes()
        value = json.loads(body, object_pairs_hook=unique_object)
        if not isinstance(value, dict):
            raise OperatorToolchainError("operator configuration is invalid")
        return value, custody.checksum(body)
    policy, policy_hash = read(POLICY)
    registry, registry_hash = read(REGISTRY)
    if (policy.get("runtime") != "runsc" or policy.get("version") not in (2, 3)
            or not isinstance(policy.get("state_directory"), str)
            or not Path(policy["state_directory"]).is_absolute()
            or not isinstance(policy.get("images"), dict)
            or set(registry) != {"version", "images"} or type(registry["version"]) is not int
            or registry["version"] != 1
            or not isinstance(registry["images"], dict) or not 1 <= len(registry["images"]) <= 8):
        raise OperatorToolchainError("operator policy or registry differs")
    custody.protected(Path(policy["state_directory"]), private=True)
    return policy, policy_hash, registry_hash


def target_identity():
    # Two public identity fields only. No service-account token/credential request,
    # DNS/proxy, caller-selected URL or billable Google API operation.
    connection = http.client.HTTPConnection("169.254.169.254", timeout=3)
    try:
        observed = []
        for path in ("/computeMetadata/v1/project/project-id", "/computeMetadata/v1/instance/id"):
            connection.request("GET", path, headers={"Metadata-Flavor": "Google"})
            response = connection.getresponse()
            body = response.read(129)
            if (response.status != 200 or response.getheader("Metadata-Flavor") != "Google"
                    or len(body) > 128):
                raise OperatorToolchainError("host identity is unconfirmed")
            observed.append(body.decode("ascii").strip())
        if observed != [TARGET_PROJECT, TARGET_INSTANCE]:
            raise OperatorToolchainError("fixed authorized target differs")
    finally:
        connection.close()


def frame(stream):
    prefix = stream.read(4)
    count = int.from_bytes(prefix, "big")
    if len(prefix) != 4 or not 1 <= count <= MAX_FRAME:
        raise OperatorToolchainError("operator frame bound differs")
    body = stream.read(count)
    if len(body) != count:
        raise OperatorToolchainError("operator frame is incomplete")
    try:
        request = json.loads(body, object_pairs_hook=unique_object)
    except (ValueError, UnicodeError, RecursionError):
        raise OperatorToolchainError("operator frame is invalid") from None
    return request


def operate(request, stream):
    common = {"version", "operation", "bundle_sha256"}
    operations = {"probe": set(), "prepare": {"role", "policy_sha256", "registry_sha256"},
                  "receive": {"intent_id"},
                  "load_prepare": {"intent_id"}, "load_dispatch": {"intent_id"},
                  "load_observe": {"intent_id"},
                  "cancel_preload": {"intent_id", "expected_phase"},
                  "quarantine": {"intent_id", "partial_sha256", "partial_bytes"}}
    if (not isinstance(request, dict) or not isinstance(request.get("operation"), str)
            or request["operation"] not in operations or type(request.get("version")) is not int
            or request["version"] != 1 or set(request) != common | operations[request["operation"]]):
        raise OperatorToolchainError("operator request authority differs")
    if request["operation"] != "receive" and stream.read(1):
        raise OperatorToolchainError("unexpected operator frame body")
    if request["bundle_sha256"] != snapshot():
        raise OperatorToolchainError("operator source changed since review")
    target_identity()
    policy, policy_hash, registry_hash = configuration()
    with isolation.Journal(Path(policy["state_directory"])) as journal:
        # Re-read under the SAME slot: a concurrent protected configuration or
        # bundle change between initial inspection and flock is not a new grant.
        locked_policy, locked_policy_hash, locked_registry_hash = configuration()
        if (locked_policy != policy or locked_policy_hash != policy_hash
                or locked_registry_hash != registry_hash or request["bundle_sha256"] != snapshot()):
            raise OperatorToolchainError("operator configuration changed before slot ownership")
        if request["operation"] == "probe":
            return {"version": 1, "operation": "probe", "policy_sha256": policy_hash,
                    "registry_sha256": registry_hash, "bundle_sha256": request["bundle_sha256"],
                    "imports": journal.toolchain_imports(), "image_loaded": False,
                    "image_approved": False, "production_policy_changed": False}
        if request["operation"] == "prepare":
            role = request["role"]
            if (not isinstance(role, str) or role not in REVIEWED_ARCHIVES
                    or request["policy_sha256"] != policy_hash or request["registry_sha256"] != registry_hash):
                raise OperatorToolchainError("reviewed catalog or configuration changed")
            intent = {"version": 1, "role": role, "host_instance_id": TARGET_INSTANCE,
                      "policy_sha256": policy_hash, "registry_sha256": registry_hash,
                      "stop_unix": STOP_UNIX, **REVIEWED_ARCHIVES[role]}
            return {"operation": "prepare", "reservation": journal.begin_toolchain_import(intent),
                    "image_loaded": False, "image_approved": False, "production_policy_changed": False}
        identity = request["intent_id"]
        item = next((row for row in journal.toolchain_imports() if row["id"] == identity), None)
        if item is None:
            raise OperatorToolchainError("reserved operator intent is unknown")
        intent = item["intent"]
        if request["operation"] == "cancel_preload":
            if (intent["host_instance_id"] != TARGET_INSTANCE or intent["stop_unix"] != STOP_UNIX
                    or request["expected_phase"] not in ("prepared", "archive_verified")):
                raise OperatorToolchainError("pre-load cancellation authority differs")
            # Cleanup must remain possible after config drift/expiry. This releases
            # only a never-started load; retained custody bytes are not deleted.
            item = journal.record_toolchain_import(identity, request["expected_phase"], "cancelled",
                                                   custody.checksum(custody.canonical(request)))
            return {"operation": "cancel_preload", "reservation": item, "custody_retained": True,
                    "image_loaded": False, "image_approved": False, "production_policy_changed": False}
        if (intent["host_instance_id"] != TARGET_INSTANCE or intent["stop_unix"] != STOP_UNIX
                or intent["policy_sha256"] != policy_hash or intent["registry_sha256"] != registry_hash
                or any(intent.get(key) != value for key, value in REVIEWED_ARCHIVES[intent["role"]].items())):
            raise OperatorToolchainError("reserved operator intent or configuration changed")
        if request["operation"] == "receive":
            return {"operation": "receive", "custody": custody.stage_archive(journal, identity, stream),
                    "production_policy_changed": False}
        if request['operation'].startswith('load_'):
            binding = {'bundle_sha256': request['bundle_sha256'], 'policy_sha256': policy_hash,
                       'registry_sha256': registry_hash}
            action = {'load_prepare': loader.prepare, 'load_dispatch': loader.dispatch,
                      'load_observe': loader.reconcile}[request['operation']]
            return {'operation': request['operation'], 'load': action(journal, identity, binding),
                    'production_policy_changed': False, 'image_approved': False}
        return {"operation": "quarantine", **custody.quarantine_partial(
            journal, identity, expected_sha256=request["partial_sha256"], expected_bytes=request["partial_bytes"]),
            "production_policy_changed": False}


def main():
    if sys.platform != "linux" or os.geteuid() != 0:
        raise OperatorToolchainError("fixed Linux operator gateway required")

    def timeout(*_):
        raise TimeoutError("operator custody deadline exceeded")

    signal.signal(signal.SIGALRM, timeout)
    signal.alarm(300)
    try:
        if len(sys.argv) == 4 and sys.argv[1] == '--load-worker':
            if set(os.environ) - {'PATH', 'HOME', 'LC_CTYPE'}:
                raise OperatorToolchainError('load worker environment is not keyless')
            target_identity()
            policy, policy_hash, registry_hash = configuration()
            binding = {'bundle_sha256': snapshot(), 'policy_sha256': policy_hash,
                       'registry_sha256': registry_hash}
            deadline = time.monotonic() + 25
            while True:
                journal = isolation.Journal(Path(policy['state_directory']))
                try:
                    journal.__enter__()
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise OperatorToolchainError('worker journal ownership deadline exceeded')
                    time.sleep(0.1)
            try:
                locked_policy, locked_policy_hash, locked_registry_hash = configuration()
                if (locked_policy != policy or locked_policy_hash != policy_hash
                        or locked_registry_hash != registry_hash or snapshot() != binding['bundle_sha256']):
                    raise OperatorToolchainError('worker configuration changed before ownership')
                loader.run_worker(journal, sys.argv[2], sys.argv[3], binding)
            finally:
                journal.__exit__(None, None, None)
            return
        if len(sys.argv) != 1:
            raise OperatorToolchainError('unsupported fixed worker arguments')
        request = frame(sys.stdin.buffer)
        print(json.dumps(operate(request, sys.stdin.buffer), sort_keys=True))
    finally:
        signal.alarm(0)


if __name__ == "__main__":
    main()
