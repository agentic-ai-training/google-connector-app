#!/usr/bin/env python3
"""Fixed non-executing packaging worker. Production launch/receipt transport pending.

CLI use fails closed unless the Linux process has the expected keyless resource
and network envelope. The trusted host controller must also authenticate the
consumed dispatch, verify container/runtime identity, own its lifetime and confirm
termination before issuing any central cleanup receipt. This process cannot prove
its own termination and never claims deployment or publishing authority.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import resource
import signal
import stat
import sys
import time
import types


def _bootstrap():
    """Support python -I with only exact operator-owned sibling modules."""
    root = Path(__file__).absolute().parent
    names = ("build_artifact", "oci_packager", "runtime_image_store", "image_output_store")
    for path in (Path(__file__).absolute(), root, *root.parents,
                 *(root / (name + ".py") for name in names)):
        info = path.lstat()
        if path.is_symlink() or info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o022:
            raise RuntimeError("packaging bundle is not operator protected")
    for name in ("app", "app.coding"):
        module = types.ModuleType(name)
        module.__path__ = []
        sys.modules[name] = module
    for name in names:
        identity = "app.coding." + name
        spec = importlib.util.spec_from_file_location(identity, root / (name + ".py"))
        if spec is None or spec.loader is None:
            raise RuntimeError("packaging bundle is unavailable")
        module = importlib.util.module_from_spec(spec)
        sys.modules[identity] = module
        # Only the exact protected sibling source is executed. Ignore stale .pyc
        # files; no generated repository module is imported.
        exec(compile((root / (name + ".py")).read_bytes(), str(root / (name + ".py")), "exec"),
             module.__dict__)  # nosec B102


if not __package__:
    _bootstrap()

from app.coding import build_artifact, image_output_store as outputs, oci_packager as oci  # noqa: E402
from app.coding import runtime_image_store as runtimes  # noqa: E402


class PackagingWorkerError(RuntimeError):
    pass


SAFE_ENV = {"PATH", "HOME", "CI", "LANG", "LC_ALL", "TZ", "HOSTNAME", "PYTHONUNBUFFERED",
            "PYTHONDONTWRITEBYTECODE", "PYTHONUTF8"}


def _keyless_guard():
    if sys.platform != "linux" or os.geteuid() != 65532 or set(os.environ) - SAFE_ENV:
        raise PackagingWorkerError("keyless Linux worker environment is required")


def require_enforcement(*, binding=None, proof_path=None, workspace=None):
    """Guest-side checks are additional evidence, not a replacement for host inspection."""
    _keyless_guard()
    try:
        status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines()
                      if ":" in line)
        if int(status["CapEff"].strip(), 16) != 0 or status["NoNewPrivs"].strip() != "1":
            raise PackagingWorkerError("worker capabilities/privilege filter differ")
        if {path.name for path in Path("/sys/class/net").iterdir()} != {"lo"}:
            raise PackagingWorkerError("worker network must be isolated")
        mounts = [line.split() for line in Path("/proc/self/mountinfo").read_text().splitlines()]
        root = [fields for fields in mounts if len(fields) > 6 and fields[4] == "/"]
        if len(root) != 1 or "ro" not in root[0][5].split(","):
            raise PackagingWorkerError("worker root must be read-only")
        if proof_path is None:
            # Native-only legacy guard. Missing virtual cgroups is NOT success.
            if status["Seccomp"].strip() != "2":
                raise PackagingWorkerError("native worker privilege filter differs")
            cgroup = Path("/sys/fs/cgroup")
            memory = int((cgroup / "memory.max").read_text().strip())
            swap = int((cgroup / "memory.swap.max").read_text().strip())
            tasks = int((cgroup / "pids.max").read_text().strip())
            quota, period = map(int, (cgroup / "cpu.max").read_text().split())
            if not (0 < memory <= 3 * 1024**3 and swap == 0 and 0 < tasks <= 16 and 0 < quota <= period):
                raise PackagingWorkerError("worker resource limits differ")
        else:
            if (proof_path != Path("/gca/input/host-proof.json") or workspace != Path("/gca/workspace")
                    or status["Seccomp"].strip() != "0"):
                raise PackagingWorkerError("unsupported host-proof runtime envelope")
            inputs = [fields for fields in mounts if len(fields) > 6 and fields[4] == "/gca/input"]
            if len(inputs) != 1 or "ro" not in inputs[0][5].split(","):
                raise PackagingWorkerError("host readiness input is not read-only")
            wait_for_host_proof(binding, proof_path, workspace)
    except (KeyError, ValueError, OSError) as error:
        raise PackagingWorkerError("worker enforcement evidence unavailable") from error
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_FSIZE, (outputs.MAX_OUTPUT_BYTES, outputs.MAX_OUTPUT_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (900, 901))


def verify_host_proof(binding, proof):
    """Binding/type validation, not cryptographic authentication of arbitrary JSON.

    Origin is the fixed host gateway's verified read-only per-job mount, backed by
    an operator-private filesystem. Never accept this document from a public API,
    a model message or a writable workspace as authority.
    """
    fields = {"version", "runtime", "native_profile", "binding_hash", "container_id", "native_pid", "native_start_ticks",
              "native_cgroup", "memory_max", "swap_max", "cpu_quota", "cpu_period", "tasks_max",
              "aggregate_memory_max", "aggregate_swap_max", "aggregate_cpu_quota", "aggregate_cpu_period",
              "aggregate_tasks_max", "native_seccomp", "native_cap_eff", "native_no_new_privs",
              "helper_pids", "observed_at_unix", "expires_at_unix"}
    numbers = fields - {"runtime", "native_profile", "binding_hash", "container_id", "native_cgroup", "helper_pids"}
    if (not isinstance(proof, dict) or set(proof) != fields
            or any(type(proof[name]) is not int for name in numbers)
            or proof["version"] != 2 or proof["runtime"] != "runsc"
            or proof["native_profile"] != "runsc_release20260921_directfs_v2"
            or proof["binding_hash"] != hashlib.sha256(oci.canonical(binding)).hexdigest()
            or not isinstance(proof["container_id"], str) or not oci.DIGEST.fullmatch("sha256:" + proof["container_id"])
            or not 1 < proof["native_pid"] < 2**31 or proof["native_start_ticks"] <= 0
            or proof["native_cgroup"] != "/gcapackaging.slice/docker-" + proof["container_id"] + ".scope"
            or proof["native_seccomp"] != 2 or proof["native_cap_eff"] != 0x8001F or proof["native_no_new_privs"] != 1
            or not 0 < proof["memory_max"] <= 3 * 1024**3 or proof["swap_max"] != 0
            or not 0 < proof["cpu_quota"] <= proof["cpu_period"] or not 0 < proof["tasks_max"] <= 64
            or not 0 < proof["aggregate_memory_max"] <= 3 * 1024**3 or proof["aggregate_swap_max"] != 0
            or not 0 < proof["aggregate_cpu_quota"] <= proof["aggregate_cpu_period"]
            or not 0 < proof["aggregate_tasks_max"] <= 128
            or not isinstance(proof["helper_pids"], list) or not 1 <= len(proof["helper_pids"]) <= 128
            or any(type(pid) is not int or not 1 < pid < 2**31 for pid in proof["helper_pids"])
            or len(proof["helper_pids"]) != len(set(proof["helper_pids"]))
            or proof["expires_at_unix"] != binding["execution_deadline_unix"]
            or not int(time.time()) - 15 <= proof["observed_at_unix"] <= int(time.time())
            or proof["expires_at_unix"] <= int(time.time())):
        raise PackagingWorkerError("host readiness proof is not bound, current and enforced")
    return proof


def wait_for_host_proof(binding, proof_path, workspace):
    binding = outputs.verify_binding(binding)
    deadline = min(time.monotonic() + 12, time.monotonic() +
                   max(0, binding["execution_deadline_unix"] - time.time()))
    while time.monotonic() < deadline:
        try:
            body = runtimes._read(proof_path, 16384, workspace, private=True)
        except FileNotFoundError:
            time.sleep(0.1)
            continue
        return verify_host_proof(binding, oci._metadata(body))
    raise PackagingWorkerError("host readiness deadline expired before private input")


def assemble_and_retain(binding, reference, artifact: bytes, *, runtime_policy_path: Path,
                        output_root: Path, workspace: Path):
    """Fixed worker logic. Caller must establish isolation and authenticated dispatch first."""
    binding = outputs.verify_binding(binding)
    runtimes._protected(output_root, workspace.resolve(strict=True))
    reference = build_artifact.verify_artifact_reference(
        reference, profile=binding["profile"], source_hash=binding["source_hash"],
        claim_id=binding["validation_claim_id"], job_id=binding["validation_job_id"],
    )
    if (not isinstance(artifact, bytes) or len(artifact) > build_artifact.MAX_ARTIFACT_ARCHIVE_BYTES
            or hashlib.sha256(artifact).hexdigest() != binding["artifact_sha256"]
            or reference["archive_sha256"] != binding["artifact_sha256"]):
        raise PackagingWorkerError("build artifact differs from the consumed job")
    policy = runtimes._read(runtime_policy_path, 32768, workspace, private=True)
    if hashlib.sha256(policy).hexdigest() != binding["runtime_policy_hash"]:
        raise PackagingWorkerError("operator runtime policy changed")
    role = "nginx_static_v1" if binding["profile"] == "react_build" else "temurin_java17_v1"
    base = runtimes.load_runtime_base(runtime_policy_path, role=role, workspace=workspace)
    if (base.approved_manifest_digest != binding["base_manifest_digest"]
            or runtimes._read(runtime_policy_path, 32768, workspace, private=True) != policy):
        raise PackagingWorkerError("operator base/policy differs from consumed job")
    image = oci.package_validated_artifact(
        base, artifact, reference, profile=binding["profile"], source_hash=binding["source_hash"],
        claim_id=binding["validation_claim_id"], job_id=binding["validation_job_id"],
    )
    return outputs.save(output_root, binding, image)


def serve(stream, *, runtime_policy_path, output_root, workspace, expected_binding=None):
    runtimes._protected(output_root, workspace.resolve(strict=True))
    header = stream.readline(16385)
    if len(header) > 16384 or not header.endswith(b"\n"):
        raise PackagingWorkerError("invalid packaging request framing")
    request = oci._metadata(header)
    if set(request) == {"operation", "binding"} and request["operation"] == "observe":
        return outputs.load(output_root, request["binding"])[1]
    if (set(request) != {"operation", "binding", "reference", "archive_bytes"}
            or request["operation"] != "assemble" or type(request["archive_bytes"]) is not int
            or not 0 < request["archive_bytes"] <= build_artifact.MAX_ARTIFACT_ARCHIVE_BYTES):
        raise PackagingWorkerError("invalid packaging operation")
    binding = outputs.verify_binding(request["binding"])
    if expected_binding is not None and binding != expected_binding:
        raise PackagingWorkerError("packaging frame differs from verified readiness dispatch")
    remaining = binding["execution_deadline_unix"] - int(time.time())
    if not 1 <= remaining <= 1920:
        raise PackagingWorkerError("packaging execution deadline is unavailable")
    artifact = stream.read(request["archive_bytes"])
    if len(artifact) != request["archive_bytes"]:
        raise PackagingWorkerError("truncated build artifact")
    signal.alarm(min(remaining, binding["timeout_seconds"]))
    return assemble_and_retain(
        binding, request["reference"], artifact, runtime_policy_path=runtime_policy_path,
        output_root=output_root, workspace=workspace,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-policy", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--request-file", type=Path)
    parser.add_argument("--dispatch-file", type=Path)
    parser.add_argument("--host-proof", type=Path)
    args = parser.parse_args()
    try:
        _keyless_guard()
        signal.alarm(45)
        binding = None
        if args.host_proof is not None or args.dispatch_file is not None:
            if (args.host_proof != Path("/gca/input/host-proof.json")
                    or args.dispatch_file != Path("/gca/input/dispatch.json")
                    or args.request_file != Path("/gca/input/request.frame") or args.workspace != Path("/gca/workspace")):
                raise PackagingWorkerError("unsupported fixed readiness paths")
            # Dispatch metadata only; no private artifact or runtime base bytes.
            binding = outputs.verify_binding(oci._metadata(runtimes._read(
                args.dispatch_file, 16384, args.workspace, private=True)))
        require_enforcement(binding=binding, proof_path=args.host_proof, workspace=args.workspace)
        if args.request_file is None:
            result = serve(sys.stdin.buffer, runtime_policy_path=args.runtime_policy,
                           output_root=args.output_root, workspace=args.workspace)
        else:
            # The host launcher mounts its private fixed frame read-only. This is
            # an operator CLI path, never a generated tool/shell argument.
            runtimes._protected(args.request_file, args.workspace.resolve(strict=True))
            descriptor = os.open(args.request_file, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1
                        or info.st_mode & 0o077
                        or not 1 <= info.st_size <= build_artifact.MAX_ARTIFACT_ARCHIVE_BYTES + 16384):
                    raise PackagingWorkerError("packaging frame is not private and bounded")
                result = serve(stream, runtime_policy_path=args.runtime_policy,
                               output_root=args.output_root, workspace=args.workspace, expected_binding=binding)
        # No body, private source, environment or provider credential is printed.
        sys.stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n")
        sys.stdout.flush()
        return 0
    except (PackagingWorkerError, outputs.ImageOutputError, oci.ImagePackagingError,
            build_artifact.BuildArtifactError, OSError, ValueError):
        sys.stdout.write('{"packaging_state":"failed","private_details_omitted":true}\n')
        return 1
    finally:
        signal.alarm(0)


if __name__ == "__main__":
    raise SystemExit(main())
