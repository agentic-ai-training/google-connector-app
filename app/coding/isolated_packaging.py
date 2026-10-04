#!/usr/bin/env python3
"""Fixed keyless packaging host launcher; never a model-selected command.

Only the authenticated operator controller uses this gateway. A consumed job owns
one shared journal slot. Its worker sees private per-job mounts, not Docker, keys,
other tenants or generated source. A result is returned only after verified process
termination and rehashing the durable OCI store. Publication/deployment stay separate.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import select
import signal
import stat
import subprocess  # nosec B404
import sys
import time
import types


BUNDLE_NAMES = ("build_artifact", "isolated_validation", "oci_packager", "runtime_image_store",
                "image_output_store", "image_packaging_worker", "isolated_packaging")


def _bootstrap():
    directory = Path(__file__).absolute().parent
    for path in (directory, *directory.parents,
                 *(directory / (name + ".py") for name in BUNDLE_NAMES)):
        info = path.lstat()
        if path.is_symlink() or info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o022:
            raise RuntimeError("packaging host bundle is not operator protected")
    for name in ("app", "app.coding"):
        package = types.ModuleType(name)
        package.__path__ = []
        sys.modules[name] = package
    for name in BUNDLE_NAMES[:5]:
        identity = "app.coding." + name
        spec = importlib.util.spec_from_file_location(identity, directory / (name + ".py"))
        if spec is None or spec.loader is None:
            raise RuntimeError("packaging host bundle is unavailable")
        module = importlib.util.module_from_spec(spec)
        sys.modules[identity] = module
        # Execute only this exact reviewed sibling source, never cached bytecode
        # or a module from the generated workspace.
        exec(compile((directory / (name + ".py")).read_bytes(), str(directory / (name + ".py")), "exec"),
             module.__dict__)  # nosec B102


if not __package__:
    _bootstrap()

from app.coding import build_artifact as artifacts, isolated_validation as isolation  # noqa: E402
from app.coding import image_output_store as outputs, oci_packager as oci  # noqa: E402
from app.coding import runtime_image_store as runtimes  # noqa: E402


class PackagingHostError(RuntimeError):
    pass


MAX_STAGING_BYTES = 2 * 1024**3
MAX_JOB_DIRECTORIES = 8
GUEST = "/gca"
CGROUP_PARENT = "gcapackaging.slice"
WORKER_TASKS = 64
AGGREGATE_TASKS = 128  # Runtime helpers plus controller; CPU/memory stay unchanged.
VOLUME_BYTES = 2040 * 1024**2  # Leave room for small host-owned job metadata.
STORAGE_COMMANDS = {"mkfs": "/usr/sbin/mke2fs", "mount": "/usr/bin/mount", "umount": "/usr/bin/umount"}


def _path(value):
    if not isinstance(value, str) or not re.fullmatch(r"/[A-Za-z0-9._/-]+", value):
        raise PackagingHostError("operator mount path is invalid")
    path = Path(value)
    if path.as_posix() != value or ".." in path.parts:
        raise PackagingHostError("operator mount path is not canonical")
    return path


def _protected(path, *, private=False, directory=False):
    path = _path(str(path))
    for entry in (path, *path.parents):
        info = entry.lstat()
        if entry.is_symlink() or info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o022:
            raise PackagingHostError("operator packaging path is not protected")
    info = path.lstat()
    if (not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or (not directory and info.st_nlink != 1) or (private and info.st_mode & 0o077)):
        raise PackagingHostError("operator packaging inode differs")
    return path


def load_policy(path):
    path = _protected(path, private=True)
    if path.stat().st_size > 32768:
        raise PackagingHostError("packaging policy exceeds its bound")
    document = oci._metadata(path.read_bytes())
    fields = {"version", "server_uuid", "project_id", "image_id", "validation_policy_path",
              "runtime_policy_path", "bundle_directory", "bundle_hashes"}
    if (set(document) != fields or type(document["version"]) is not int or document["version"] != 1
            or not isinstance(document["server_uuid"], str)
            or not re.fullmatch(r"[A-Za-z0-9-]{1,128}", document["server_uuid"])
            or not isinstance(document["project_id"], str)
            or not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", document["project_id"])
            or not isinstance(document["image_id"], str)
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", document["image_id"])):
        raise PackagingHostError("packaging host policy is invalid")
    bundle = _protected(document["bundle_directory"], directory=True)
    hashes = document["bundle_hashes"]
    if not isinstance(hashes, dict) or set(hashes) != {name + ".py" for name in BUNDLE_NAMES}:
        raise PackagingHostError("complete reviewed packaging bundle is required")
    for name, checksum in hashes.items():
        source = _protected(bundle / name)
        if (not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum)
                or not 1 <= source.stat().st_size <= 256000
                or hashlib.sha256(source.read_bytes()).hexdigest() != checksum):
            raise PackagingHostError("installed packaging bundle changed")
    validation = isolation.load_policy(_path(document["validation_policy_path"]), None)
    if (validation["version"] != 3 or validation["server_uuid"] != document["server_uuid"]
            or validation["images"].get("python_compile") != document["image_id"]):
        raise PackagingHostError("packaging and validation hosts/toolchains differ")
    _protected(document["runtime_policy_path"], private=True)
    return document, validation


def require_host_limits():
    """Join the operator-prepared shared limit before consuming private inputs.

    Docker must put the worker below the same slice. Its memory allowance is not
    added to the controller's allowance; the aggregate is capped at 3 GiB/one CPU.
    Neither models nor the gateway create/raise these operator limits.
    """
    if sys.platform != "linux" or os.geteuid() != 0:
        raise PackagingHostError("packaging host gateway requires operator Linux root")
    root = Path("/sys/fs/cgroup") / CGROUP_PARENT
    try:
        memory = int((root / "memory.max").read_text().strip())
        swap = int((root / "memory.swap.max").read_text().strip())
        quota, period = map(int, (root / "cpu.max").read_text().split())
        tasks = int((root / "pids.max").read_text().strip())
        controller = root / "controller"
        occupants = (controller / "cgroup.procs").read_text().split()
        if (not 0 < memory <= 3 * 1024**3 or swap != 0 or not 0 < quota <= period
                or not 0 < tasks <= AGGREGATE_TASKS or any(value != str(os.getpid()) for value in occupants)):
            raise PackagingHostError("shared packaging host limits or ownership differ")
        (controller / "cgroup.procs").write_text(str(os.getpid()))
        membership = Path("/proc/self/cgroup").read_text().strip()
        if membership != "0::/" + CGROUP_PARENT + "/controller":
            raise PackagingHostError("packaging controller did not join its resource group")
    except (OSError, ValueError) as error:
        raise PackagingHostError("shared packaging host resource limits are unavailable") from error


def _sync(directory):
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write(path, body, *, worker=False):
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())
        if worker:
            os.fchown(stream.fileno(), 65532, 65532)


def _worker_directory(path):
    path.mkdir(mode=0o700)
    os.chown(path, 65532, 65532)


def _storage_command(operation, *arguments):
    executable = _protected(Path(STORAGE_COMMANDS[operation]))
    result = subprocess.run(  # nosec B603
        [str(executable), *arguments], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, timeout=30, check=False, env=isolation.CLEAN_ENV,
    )
    if result.returncode:
        raise PackagingHostError("private packaging filesystem operation is unconfirmed")


def storage_mount(directory):
    """Inspect kernel identity, not a worker 'mounted:true' assertion."""
    body = Path("/proc/self/mountinfo").read_text()
    if len(body) > 1024 * 1024:
        raise PackagingHostError("host mount inventory exceeds its bound")
    destination = str(directory / "work")
    entries = [line.split() for line in body.splitlines()]
    matches = [fields for fields in entries if len(fields) > 6 and fields[4] == destination]
    if not matches:
        return False
    if len(matches) != 1:
        raise PackagingHostError("ambiguous private packaging filesystem")
    fields = matches[0]
    split = fields.index("-")
    if (fields[3] != "/" or fields[split + 1] != "ext4"
            or not {"rw", "nosuid", "nodev", "noexec"} <= set(fields[5].split(","))
            or not re.fullmatch(r"/dev/loop[0-9]{1,6}", fields[split + 2])):
        raise PackagingHostError("private packaging mount enforcement differs")
    device = fields[split + 2].split("/")[-1]
    backing = Path("/sys/block") / device / "loop/backing_file"
    if backing.read_text().strip().lstrip("/") != str(directory / "storage.img").lstrip("/"):
        raise PackagingHostError("packaging filesystem uses another job's backing file")
    return True


def storage(directory, *, create=False):
    """Fixed-size private ext4 volume: no Docker volume or model-supplied device."""
    _protected(directory, private=True, directory=True)
    image = directory / "storage.img"
    work = directory / "work"
    if create:
        descriptor = os.open(image, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            os.posix_fallocate(stream.fileno(), 0, VOLUME_BYTES)
            os.fsync(stream.fileno())
        work.mkdir(mode=0o700)
        _storage_command("mkfs", "-t", "ext4", "-q", "-m", "0", "-F", str(image))
        _sync(directory)
    info = _protected(image, private=True).stat()
    _protected(work, directory=True)
    if info.st_size != VOLUME_BYTES:
        raise PackagingHostError("packaging backing volume has the wrong size")
    if not storage_mount(directory):
        _protected(work, private=True, directory=True)
        _storage_command("mount", "-o", "loop,nosuid,nodev,noexec", str(image), str(work))
        if not storage_mount(directory):
            raise PackagingHostError("private packaging mount was not verified")
    work.chmod(0o700)
    return work


def _jobs_root(journal):
    directory = journal.directory / "packaging-jobs"
    if not directory.exists():
        directory.mkdir(mode=0o700)
        _sync(journal.directory)
    _protected(directory, private=True, directory=True)
    children = list(directory.iterdir())
    if len(children) >= MAX_JOB_DIRECTORIES:
        raise PackagingHostError("packaging staging capacity exhausted")
    for child in children:
        _protected(child, private=True, directory=True)
        row = journal.db.execute("SELECT purpose FROM jobs WHERE id=?", (child.name,)).fetchone()
        if row is None or row[0] != "image_packaging":
            raise PackagingHostError("untracked packaging staging directory")
    return directory


def prepare(journal, job, policy, binding, reference, artifact, runtime_policy, base):
    """Only trusted controller bytes are written; no archive member is extracted."""
    directory = _jobs_root(journal) / job["id"]
    directory.mkdir(mode=0o700)
    _sync(directory.parent)
    _write(directory / "binding.json", oci.canonical(binding))
    _sync(directory)
    work = storage(directory, create=True)
    inputs, output, workspace = (work / name for name in ("input", "output", "workspace"))
    for path in (inputs, output, workspace):
        _worker_directory(path)
    _write(inputs / "runtime-policy.json", runtime_policy, worker=True)
    _write(inputs / "dispatch.json", oci.canonical(binding), worker=True)
    header = oci.canonical({"operation": "assemble", "binding": binding, "reference": reference,
                            "archive_bytes": len(artifact)}) + b"\n"
    if len(header) > 16384:
        raise PackagingHostError("packaging worker frame exceeds its bound")
    _write(inputs / "request.frame", header + artifact, worker=True)
    layout = inputs / "runtime"
    _worker_directory(layout)
    blobs = layout / "blobs"
    _worker_directory(blobs)
    checksums = blobs / "sha256"
    _worker_directory(checksums)
    _write(layout / "oci-layout", oci.canonical({"imageLayoutVersion": "1.0.0"}), worker=True)
    _write(layout / "index.json", oci.canonical({"schemaVersion": 2, "manifests": [{
        "digest": base.approved_manifest_digest, "size": len(base.manifest),
        "mediaType": json.loads(base.manifest)["mediaType"],
    }]}), worker=True)
    for body in {oci.digest(body): body for body in (base.manifest, base.config, *base.layers)}.values():
        _write(checksums / oci.digest(body).split(":")[1], body, worker=True)
    bundle = inputs / "bundle"
    _worker_directory(bundle)
    for name, checksum in policy["bundle_hashes"].items():
        source = _protected(Path(policy["bundle_directory"]) / name)
        body = source.read_bytes()
        if len(body) > 256000 or hashlib.sha256(body).hexdigest() != checksum:
            raise PackagingHostError("reviewed worker bundle changed before staging")
        _write(bundle / name, body, worker=True)
    selected = json.loads(runtime_policy)["roles"][base.role]
    destination = _path(selected["layout_directory"])
    # The trusted policy chooses the OCI store, not arbitrary container roots.
    if (destination == Path(GUEST) or destination.is_relative_to(Path(GUEST))
            or len(destination.parts) < 3
            or destination.parts[1] in {"proc", "sys", "dev", "bin", "sbin", "usr", "lib", "lib64", "etc"}):
        raise PackagingHostError("runtime mount overlaps protected guest paths")
    mounts = [(inputs, GUEST + "/input", True), (output, GUEST + "/output", False),
              (workspace, GUEST + "/workspace", True), (layout, str(destination), True),
              (bundle, GUEST + "/bundle", True)]
    for folder in (checksums, blobs, layout, bundle, inputs, output, workspace, work, directory, directory.parent):
        _sync(folder)
    return directory, mounts


def create_arguments(journal, job, mounts):
    arguments = ["create", "--name", job["name"], "--pull=never", "--runtime=runsc",
                 "--label=gca.purpose=image_packaging", "--label=gca.scope=" + journal.scope,
                 "--label=gca.job=" + job["id"], "--network=none", "--read-only",
                 "--cap-drop=ALL", "--security-opt=no-new-privileges:true", "--user=65532:65532",
                 "--ipc=private", "--cgroupns=private",
                 "--memory=3g", "--memory-swap=3g", "--cpus=1", "--pids-limit=" + str(WORKER_TASKS),
                 "--cgroup-parent=" + CGROUP_PARENT,
                 "--ulimit=nofile=64:64", "--log-driver=none", "--entrypoint=/usr/bin/env"]
    for source, destination, readonly in mounts:
        arguments.extend(["--mount", "type=bind,src=" + str(_path(str(source))) + ",dst="
                          + str(_path(destination)) + (",readonly" if readonly else "")])
    arguments.extend([job["image"], "-i", "PATH=/usr/local/bin:/usr/bin:/bin", "HOME=/nonexistent",
                      "CI=true", "LANG=C.UTF-8", "PYTHONDONTWRITEBYTECODE=1", "PYTHONUNBUFFERED=1",
                      "python3", "-I", "-B", GUEST + "/bundle/image_packaging_worker.py",
                      "--runtime-policy", GUEST + "/input/runtime-policy.json", "--output-root",
                      GUEST + "/output", "--workspace", GUEST + "/workspace", "--request-file",
                      GUEST + "/input/request.frame", "--dispatch-file", GUEST + "/input/dispatch.json",
                      "--host-proof", GUEST + "/input/host-proof.json"])
    return arguments


def verify_container(journal, job, container_id, mounts):
    journal.verify_identity(job, container_id)
    spec = json.loads(isolation.docker("inspect", container_id))[0]
    host = spec["HostConfig"]
    expected = {(str(source), destination, not readonly) for source, destination, readonly in mounts}
    observed = {(mount.get("Source"), mount.get("Destination"), mount.get("RW"))
                for mount in spec["Mounts"] if mount.get("Type") == "bind"}
    if (len(spec["Mounts"]) != len(expected) or observed != expected
            or any(mount.get("Propagation") != "rprivate" for mount in spec["Mounts"])
            or host.get("Runtime") != "runsc" or host.get("NetworkMode") != "none"
            or host.get("Privileged") is not False or host.get("Devices") or host.get("DeviceRequests")
            or host.get("PidMode") != "" or host.get("IpcMode") != "private" or host.get("UTSMode") != ""
            or host.get("CgroupnsMode") != "private" or host.get("PortBindings")
            or host.get("PublishAllPorts") is not False or host.get("GroupAdd")
            or host.get("ReadonlyRootfs") is not True or spec["Config"].get("User") != "65532:65532"
            or host.get("CapAdd") or host.get("CapDrop") != ["ALL"]
            or host.get("SecurityOpt") != ["no-new-privileges:true"]
            or host.get("Memory") != 3 * 1024**3 or host.get("MemorySwap") != 3 * 1024**3
            or host.get("NanoCpus") != 1_000_000_000 or host.get("PidsLimit") != WORKER_TASKS
            or host.get("CgroupParent") != CGROUP_PARENT
            or host.get("Ulimits") != [{"Name": "nofile", "Hard": 64, "Soft": 64}]
            or host.get("RestartPolicy", {}).get("Name") not in {"", "no"}
            or host.get("RestartPolicy", {}).get("MaximumRetryCount") != 0
            or host.get("LogConfig", {}).get("Type") != "none"
            or spec["Config"].get("Healthcheck") is not None
            or spec["Config"].get("Entrypoint") != ["/usr/bin/env"]
            or spec["Config"].get("OpenStdin") is not False or spec["Config"].get("Tty") is not False
            or spec["Config"].get("Cmd") != create_arguments(journal, job, mounts)[
                create_arguments(journal, job, mounts).index(job["image"]) + 1:]):
        raise PackagingHostError("packaging container enforcement or mounts differ")
    return spec


def _native_text(path, maximum=65536):
    # Fixed kernel paths below /proc or /sys only. Never read process environments
    # or return raw native process command lines to a model/controller client.
    with path.open("rb") as stream:
        body = stream.read(maximum + 1)
    if len(body) > maximum:
        raise PackagingHostError("native enforcement observation exceeds bound")
    return body.decode("ascii").strip()


def _start_ticks(pid):
    body = _native_text(Path("/proc") / str(pid) / "stat")
    return int(body.rsplit(")", 1)[1].split()[19])


def native_helpers(container_id):
    if not isinstance(container_id, str) or not re.fullmatch(r"[0-9a-f]{64}", container_id):
        raise PackagingHostError("native helper container identity is invalid")
    inventory = [path for path in Path("/proc").iterdir() if path.name.isdecimal()]
    if len(inventory) > 4096:
        raise PackagingHostError("native helper inventory exceeds bound")
    result = []
    for process in inventory:
        try:
            if os.readlink(process / "exe") != "/usr/bin/runsc":
                continue
            arguments = _native_text(process / "cmdline").split("\x00")
            membership = _native_text(process / "cgroup")
            if container_id in arguments or ("/docker-" + container_id + ".scope") in membership:
                result.append((int(process.name), membership))
        except (FileNotFoundError, ProcessLookupError):
            continue
    return result


def require_native_cleanup(container_id):
    """Docker absence alone cannot release capacity while native helpers survive."""
    if native_helpers(container_id):
        raise PackagingHostError("native runtime helpers remain after container removal")
    events = Path("/sys/fs/cgroup") / CGROUP_PARENT / ("docker-" + container_id + ".scope/cgroup.events")
    try:
        values = dict(line.split() for line in _native_text(events).splitlines())
    except FileNotFoundError:
        return
    if values.get("populated") != "0":
        raise PackagingHostError("native container resource group remains populated")


def verify_native_worker(spec, binding):
    """Observe the live native kernel, not the guest's emulated /proc/cgroup.

    Fixed systemd topology only. Different runtime/driver layouts need an operator
    profile and real proof; they are never accepted via a guessed path or JSON flag.
    """
    container_id = spec.get("Id")
    state = spec.get("State", {})
    pid = state.get("Pid")
    if (not isinstance(container_id, str) or not re.fullmatch(r"[0-9a-f]{64}", container_id)
            or state.get("Running") is not True or type(pid) is not int or not 1 < pid < 2**31):
        raise PackagingHostError("native worker is not a live exact container")
    identity = "/" + CGROUP_PARENT + "/docker-" + container_id + ".scope"
    if not callable(getattr(os, "pidfd_open", None)):
        raise PackagingHostError("native process handles are unavailable on this host")
    descriptor = os.pidfd_open(pid, 0)
    try:
        initial = _start_ticks(pid)
        poller = select.poll()
        poller.register(descriptor, select.POLLIN)
        status = dict(line.split(":", 1) for line in _native_text(Path("/proc") / str(pid) / "status").splitlines()
                      if ":" in line)
        membership = _native_text(Path("/proc") / str(pid) / "cgroup")
        if (membership != "0::" + identity or int(status["CapEff"].strip(), 16) != 0
                or status["NoNewPrivs"].strip() != "1" or status["Seccomp"].strip() != "2"):
            raise PackagingHostError("native sandbox ownership or privilege filter differs")
        group = Path("/sys/fs/cgroup") / identity.lstrip("/")
        memory = int(_native_text(group / "memory.max"))
        swap = int(_native_text(group / "memory.swap.max"))
        quota, period = map(int, _native_text(group / "cpu.max").split())
        tasks = int(_native_text(group / "pids.max"))
        parent = group.parent
        parent_memory = int(_native_text(parent / "memory.max"))
        parent_swap = int(_native_text(parent / "memory.swap.max"))
        parent_quota, parent_period = map(int, _native_text(parent / "cpu.max").split())
        parent_tasks = int(_native_text(parent / "pids.max"))
        if (not 0 < memory <= 3 * 1024**3 or swap != 0 or not 0 < quota <= period
                or not 0 < tasks <= WORKER_TASKS or not 0 < parent_memory <= 3 * 1024**3
                or parent_swap != 0 or not 0 < parent_quota <= parent_period
                or not 0 < parent_tasks <= AGGREGATE_TASKS):
            raise PackagingHostError("native worker or aggregate resource limits differ")
        # Every runsc helper with this exact container ID must also be covered by
        # the aggregate. No cmdline is emitted or stored in the receipt.
        helpers = []
        for helper_pid, helper_group in native_helpers(container_id):
            if not helper_group.startswith("0::/" + CGROUP_PARENT + "/") or "\n" in helper_group:
                raise PackagingHostError("native runtime helper is outside aggregate resource limits")
            helpers.append(helper_pid)
        if not helpers or _start_ticks(pid) != initial or poller.poll(0):
            raise PackagingHostError("native runtime identity expired or helpers were not observed")
        now = int(time.time())
        if now >= binding["execution_deadline_unix"]:
            raise PackagingHostError("native readiness authority expired")
        return dict(version=1, runtime="runsc", binding_hash=hashlib.sha256(oci.canonical(binding)).hexdigest(),
                    container_id=container_id, native_pid=pid, native_start_ticks=initial, native_cgroup=identity,
                    memory_max=memory, swap_max=swap, cpu_quota=quota, cpu_period=period, tasks_max=tasks,
                    aggregate_memory_max=parent_memory, aggregate_swap_max=parent_swap,
                    aggregate_cpu_quota=parent_quota, aggregate_cpu_period=parent_period,
                    aggregate_tasks_max=parent_tasks, native_seccomp=2, native_cap_eff=0,
                    native_no_new_privs=1, helper_pids=sorted(helpers), observed_at_unix=now,
                    expires_at_unix=binding["execution_deadline_unix"])
    except (OSError, ValueError, KeyError, IndexError) as error:
        raise PackagingHostError("native enforcement evidence unavailable") from error
    finally:
        os.close(descriptor)


def publish_readiness(journal, job, container_id, mounts, binding, inputs, child):
    deadline = min(time.monotonic() + 10, time.monotonic() +
                   max(0, binding["execution_deadline_unix"] - time.time()))
    while time.monotonic() < deadline and child.poll() is None:
        spec = verify_container(journal, job, container_id, mounts)
        if spec.get("State", {}).get("Running") is True:
            proof = verify_native_worker(spec, binding)
            again = verify_container(journal, job, container_id, mounts)
            if (again.get("State", {}).get("Running") is not True
                    or again["State"].get("Pid") != proof["native_pid"]):
                raise PackagingHostError("native worker changed before readiness publication")
            pending = inputs / "host-proof.pending"
            final = inputs / "host-proof.json"
            if final.exists() or final.is_symlink():
                raise PackagingHostError("readiness was already published")
            _write(pending, oci.canonical(proof), worker=True)
            os.rename(pending, final)
            _sync(inputs)
            return
        time.sleep(0.1)
    raise PackagingHostError("native worker did not become ready within its deadline")


def _takeover(directory):
    """After exact process termination only: make private output operator-owned.

    Mixed ownership after an interrupted takeover is recoverable. Links, special
    objects, unexpected owners, public inodes and excessive data are never adopted.
    """
    entries = [directory]
    cursor, total = 0, 0
    while cursor < len(entries):
        path = entries[cursor]
        info = path.lstat()
        if (path.is_symlink() or info.st_uid not in {os.geteuid(), 65532} or info.st_mode & 0o077
                or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
                or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)):
            raise PackagingHostError("packaging output inode is unsafe")
        if stat.S_ISDIR(info.st_mode):
            entries.extend(path.iterdir())
        else:
            total += info.st_size
        if len(entries) > 128 or total > MAX_STAGING_BYTES:
            raise PackagingHostError("packaging output inventory exceeds its bound")
        cursor += 1
    for path in reversed(entries):
        os.chown(path, os.geteuid(), os.getegid(), follow_symlinks=False)
    _sync(directory)


def recover(journal, binding):
    binding = outputs.verify_binding(binding, allow_expired=True)
    observation = journal.observe_claim(binding["build_job_id"])
    if (observation.get("purpose") != "image_packaging"
            or observation.get("binding_hash") != hashlib.sha256(oci.canonical(binding)).hexdigest()
            or observation.get("cleanup_verified") is not True):
        raise PackagingHostError("image worker termination is not verified")
    directory = journal.directory / "packaging-jobs" / observation["packaging_job_id"]
    _protected(directory, private=True, directory=True)
    record = _protected(directory / "binding.json", private=True)
    if record.stat().st_size > 16384:
        raise PackagingHostError("packaging staging binding exceeds its bound")
    saved = record.read_bytes()
    if saved != oci.canonical(binding):
        raise PackagingHostError("packaging staging belongs to another dispatch")
    work = storage(directory)
    _takeover(work / "output")
    _, measurement = outputs.load(work / "output", binding)
    return {**measurement, "packaging_job_id": observation["packaging_job_id"],
            "execution_cleanup_verified": True}


def remove(journal, binding):
    """Dispose only the exact job's private volume after verified termination.

    Corrupt/interrupted output need not be blessed as a successful image to clean
    its dedicated filesystem. No unrelated CAS, bind source or Docker volume is
    deleted. Normal unmount is mandatory; a busy or mismatched mount stays held.
    """
    binding = outputs.verify_binding(binding, allow_expired=True)
    observation = journal.observe_claim(binding["build_job_id"])
    checksum = hashlib.sha256(oci.canonical(binding)).hexdigest()
    if (observation.get("purpose") != "image_packaging" or observation.get("binding_hash") != checksum
            or observation.get("cleanup_verified") is not True):
        raise PackagingHostError("storage cleanup lacks exact terminated worker evidence")
    root = journal.directory / "packaging-jobs"
    directory = root / observation["packaging_job_id"]
    if root.exists():
        _protected(root, private=True, directory=True)
    mounted = storage_mount(directory)
    if directory.exists() or directory.is_symlink():
        _protected(directory, private=True, directory=True)
        names = {path.name for path in directory.iterdir()}
        if names - {"binding.json", "storage.img", "work"}:
            raise PackagingHostError("unknown object outside the packaging filesystem")
        if "binding.json" in names:
            record = _protected(directory / "binding.json", private=True)
            if record.stat().st_size > 16384 or record.read_bytes() != oci.canonical(binding):
                raise PackagingHostError("packaging cleanup belongs to another dispatch")
        image = directory / "storage.img"
        if image.exists() or image.is_symlink():
            image = _protected(image, private=True)
            if image.stat().st_size > VOLUME_BYTES:
                raise PackagingHostError("packaging backing volume size is untracked")
        elif mounted:
            raise PackagingHostError("mounted packaging filesystem lost its backing file")
        if mounted:
            _storage_command("umount", str(directory / "work"))
            if storage_mount(directory):
                raise PackagingHostError("packaging filesystem is still mounted")
        work = directory / "work"
        if work.exists() or work.is_symlink():
            _protected(work, directory=True)
            if list(work.iterdir()):
                raise PackagingHostError("unmounted staging has unknown host data")
            work.rmdir()
        image.unlink(missing_ok=True)
        (directory / "binding.json").unlink(missing_ok=True)
        directory.rmdir()
        _sync(root)
    elif mounted:
        raise PackagingHostError("packaging mount is not tracked by its directory")
    # The host SQLite claim tombstone survives removal of the per-job volume.
    row = journal.db.execute("SELECT observed_at FROM events WHERE job_id=? AND state='storage_removed' "
                             "ORDER BY sequence LIMIT 1", (observation["packaging_job_id"],)).fetchone()
    if row is None:
        observed_at = int(time.time())
        with journal.db:
            journal.db.execute("INSERT INTO events(job_id,state,observed_at) VALUES(?,'storage_removed',?)",
                               (observation["packaging_job_id"], observed_at))
    else:
        observed_at = row[0]
    return {"build_job_id": binding["build_job_id"], "packaging_job_id": observation["packaging_job_id"],
            "binding_hash": checksum, "execution_cleanup_verified": True, "output_removed": True,
            "staging_removed": True, "observed_at": datetime.fromtimestamp(observed_at, timezone.utc).isoformat()}


def purge_expired(journal):
    """Internal host reaper extension, never admit/redispatch expired work."""
    root = journal.directory / "packaging-jobs"
    if not root.exists():
        return 0
    _protected(root, private=True, directory=True)
    children = list(root.iterdir())
    if len(children) > MAX_JOB_DIRECTORIES:
        raise PackagingHostError("packaging cleanup inventory exceeds its bound")
    purged = 0
    for directory in children:
        _protected(directory, private=True, directory=True)
        row = journal.db.execute("SELECT purpose,state FROM jobs WHERE id=?", (directory.name,)).fetchone()
        if row is None or row["purpose"] != "image_packaging":
            raise PackagingHostError("packaging cleanup found an untracked job")
        record = _protected(directory / "binding.json", private=True)
        if record.stat().st_size > 16384:
            raise PackagingHostError("packaging cleanup binding exceeds its bound")
        binding = outputs.verify_binding(oci._metadata(record.read_bytes()), allow_expired=True)
        if row["state"] == "cleaned" and binding["output_expires_at_unix"] <= int(time.time()):
            remove(journal, binding)
            purged += 1
    return purged


def assemble(journal, policy, binding, validation_binding, reference):
    binding = outputs.verify_binding(binding)
    if binding["server_uuid"] != policy["server_uuid"] or binding["project_id"] != policy["project_id"]:
        raise PackagingHostError("packaging dispatch is for another host")
    reference = artifacts.verify_artifact_reference(
        reference, profile=binding["profile"], source_hash=binding["source_hash"],
        claim_id=binding["validation_claim_id"], job_id=binding["validation_job_id"],
    )
    if reference["archive_sha256"] != binding["artifact_sha256"]:
        raise PackagingHostError("packaging artifact differs from dispatch")
    journal.require_available(binding)
    job = journal.begin("image_packaging", policy["image_id"], binding["timeout_seconds"],
                        binding, purpose="image_packaging")
    # Hard controller lifetime is independent of client connectivity. If the
    # process is killed, its persisted intent survives for the host reaper.
    signal.alarm(min(binding["timeout_seconds"] + 90,
                     binding["execution_deadline_unix"] - int(time.time())))
    create_attempted = create_confirmed = False
    try:
        artifact = isolation.retained_artifact(journal, validation_binding, reference)
        runtime_policy = runtimes._read(Path(policy["runtime_policy_path"]), 32768,
                                       journal.directory, private=True)
        if hashlib.sha256(runtime_policy).hexdigest() != binding["runtime_policy_hash"]:
            raise PackagingHostError("runtime policy changed since dispatch")
        role = "nginx_static_v1" if binding["profile"] == "react_build" else "temurin_java17_v1"
        base = runtimes.load_runtime_base(Path(policy["runtime_policy_path"]), role=role,
                                        workspace=journal.directory)
        if base.approved_manifest_digest != binding["base_manifest_digest"]:
            raise PackagingHostError("runtime base differs from dispatch")
        isolation.ensure_toolchain_image(policy["image_id"])
        directory, mounts = prepare(journal, job, policy, binding, reference, artifact, runtime_policy, base)
        create_attempted = True
        container_id = isolation.docker(*create_arguments(journal, job, mounts)).decode().strip()
        if not re.fullmatch(r"[a-f0-9]{64}", container_id):
            raise PackagingHostError("packaging create response is uncertain")
        create_confirmed = True
        verify_container(journal, job, container_id, mounts)
        journal.record(job, "running", container_id)
        remaining = min(binding["timeout_seconds"], binding["execution_deadline_unix"] - int(time.time()))
        if remaining < 1:
            raise PackagingHostError("packaging authority expired before start")
        result = isolation.execute_bounded(
            ["start", "-a", container_id], remaining, output_limit=16384,
            on_started=lambda child: publish_readiness(journal, job, container_id, mounts, binding,
                                                       directory / "work/input", child),
        )
    finally:
        try:
            journal.cleanup(job, create_confirmed=create_confirmed, create_not_attempted=not create_attempted)
        except (isolation.IsolationError, OSError, ValueError, subprocess.TimeoutExpired):
            journal.record(job, "cleanup_uncertain")
            raise PackagingHostError("packaging worker cleanup remains uncertain") from None
    if not result["success"] or result["output_truncated"]:
        raise PackagingHostError("packaging worker did not return a bounded successful result")
    measurement = recover(journal, binding)
    worker_receipt = oci._metadata(result["stdout"].encode())
    expected_receipt = {key: value for key, value in measurement.items()
                        if key not in {"packaging_job_id", "execution_cleanup_verified"}}
    if expected_receipt != worker_receipt:
        raise PackagingHostError("worker receipt differs from retained measured bytes")
    return measurement


def gateway(stream, policy, validation):
    with isolation.Journal(Path(validation["state_directory"])) as journal:
        require_host_limits()
        header = stream.readline(16385)
        if len(header) > 16384 or not header.endswith(b"\n"):
            raise PackagingHostError("invalid packaging host framing")
        request = oci._metadata(header)
        if set(request) == {"operation", "binding"} and request["operation"] == "recover":
            return recover(journal, request["binding"])
        if set(request) == {"operation", "binding"} and request["operation"] == "cleanup":
            return remove(journal, request["binding"])
        if (set(request) != {"operation", "binding", "validation_binding", "reference"}
                or request["operation"] != "assemble"):
            raise PackagingHostError("unsupported packaging host operation")
        signal.alarm(0)
        return assemble(journal, policy, request["binding"], request["validation_binding"], request["reference"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", type=Path, required=True)
    args = parser.parse_args()
    try:
        if sys.platform != "linux" or os.geteuid() != 0:
            raise PackagingHostError("packaging host gateway requires operator Linux root")
        policy, validation = load_policy(args.policy)
        signal.alarm(45)
        result = gateway(sys.stdin.buffer, policy, validation)
        sys.stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n")
        sys.stdout.flush()
        return 0
    except (PackagingHostError, isolation.IsolationError, artifacts.BuildArtifactError,
            outputs.ImageOutputError, oci.ImagePackagingError, OSError, ValueError, KeyError,
            subprocess.TimeoutExpired):
        sys.stdout.write('{"packaging_state":"unconfirmed","private_details_omitted":true}\n')
        return 1
    finally:
        signal.alarm(0)


if __name__ == "__main__":
    raise SystemExit(main())
