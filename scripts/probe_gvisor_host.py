"""Operator-only smoke proof; never accepts generated commands or host mounts.

Run on the Linux host with one already-present, trusted Alpine-based image ID.
This proves configured controls, not universal escape resistance or broker integration.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess  # nosec B404
import uuid


def docker(*args: str, timeout: int = 20) -> str:
    result = subprocess.run(  # nosec B603 B607 - trusted operator-only Docker CLI
        ["docker", *args], check=True, capture_output=True, text=True,
        timeout=timeout, env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
    )
    return result.stdout


def probe(image: str) -> dict:
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
        raise ValueError("Supply an immutable local image ID, not a tag")
    name = "gca-isolation-probe-" + uuid.uuid4().hex
    created = False
    try:
        # Attempt cleanup even if create's response is lost after Docker commits it.
        created = True
        docker(
            "create", "--name", name, "--pull=never", "--runtime=runsc",
            "--label=gca.purpose=isolation-probe", "--network=none", "--read-only",
            "--cap-drop=ALL", "--security-opt=no-new-privileges:true",
            "--user=65532:65532", "--memory=128m", "--memory-swap=128m",
            "--cpus=0.5", "--pids-limit=32", "--ulimit=nofile=64:64",
            "--tmpfs=/tmp:rw,nosuid,nodev,noexec,size=16m,mode=1777",
            "--tmpfs=/data:rw,nosuid,nodev,noexec,size=1m,mode=1777",
            "--log-driver=none", "--entrypoint=/bin/sh", image, "-ec",
            'test "$(id -u)" = 65532; '
            'test ! -e /var/run/docker.sock; '
            'if touch /etc/gca-forbidden 2>/dev/null; then exit 31; fi; '
            'touch /tmp/gca-allowed; '
            'command -v wget >/dev/null; '
            'if wget -q -T 2 -O /tmp/net http://1.1.1.1 2>/dev/null; '
            'then exit 32; fi; '
            'printf "filesystem_and_egress_checks_passed\\n"',
        )
        output = docker("start", "--attach", name, timeout=30)
        data = json.loads(docker("inspect", name))[0]
        host = data["HostConfig"]
        checks = {
            "runtime_runsc": host["Runtime"] == "runsc",
            "network_none": host["NetworkMode"] == "none",
            "readonly_root": host["ReadonlyRootfs"] is True,
            "not_privileged": host["Privileged"] is False,
            "all_capabilities_dropped": "ALL" in host["CapDrop"],
            "no_privilege_escalation": "no-new-privileges:true" in host["SecurityOpt"],
            "memory_limit": host["Memory"] == 128 * 1024 * 1024,
            "swap_disabled": host["MemorySwap"] == host["Memory"],
            "cpu_limit": host["NanoCpus"] == 500_000_000,
            "pids_limit": host["PidsLimit"] == 32,
            "no_bind_mounts": not host.get("Binds"),
            "no_persistent_volumes": not any(
                item.get("Type") in {"volume", "bind"} for item in data["Mounts"]
            ),
            "nonroot": data["Config"]["User"] == "65532:65532",
            "probe_passed": (
                data["State"]["ExitCode"] == 0
                and output.strip() == "filesystem_and_egress_checks_passed"
            ),
        }
        if not all(checks.values()):
            raise RuntimeError("Isolation checks failed: " + ", ".join(
                key for key, value in checks.items() if not value
            ))
        return {"image_id": image, "checks": checks, "broker_integrated": False}
    finally:
        if created:
            docker("rm", "--force", "--volumes", name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    try:
        print(json.dumps(probe(parser.parse_args().image), sort_keys=True))
    except subprocess.CalledProcessError as error:
        raise SystemExit(f"Docker operation failed (exit {error.returncode}); inspect host")
    except subprocess.TimeoutExpired:
        raise SystemExit("Docker operation exceeded deadline; inspect cleanup state")
