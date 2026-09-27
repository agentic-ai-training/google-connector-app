# Keyless validation boundary

Status, 27 September 2026: live host and synthetic Python validation proofs passed.
Rust/worker integration is implemented locally and regression-tested, but the production
coding worker has **not** been activated on this host. Full hosted coding/deployment
acceptance remains incomplete.

```text
Groq planner: bounded reads + structured patch proposals
    |
Trusted Rust broker: hash-bound edits to private temporary copy
    | fixed validation profile, not model-provided shell commands
Keyless operator launcher: Docker authority stays here
    | bounded source archive over stdin, NOT a host directory mount
gVisor: non-root, network=none, read-only root, pinned toolchain
    | /workspace and /tmp are bounded ephemeral memory filesystems
Exit status + bounded output + image ID
    |
Destroy container; uncertain cleanup overrides success
```

## Authority

`app/coding/isolated_validation.py` is a standalone standard-library launcher. Install
it outside model-editable workspaces. Operator policy and parent directories must not
be group/world writable. Configuration is a JSON object with exactly `version: 2`,
`runtime: "runsc"`, `state_directory`, and `images`, a map from supported profile name to local
`sha256:<64 lowercase hex digits>` image ID. Images are never pulled automatically.
Only reviewed, secret-free, dependency-complete toolchains may be registered.
The state directory must already exist with mode 0700, outside the source tree, under
operator-protected parents. All jobs on this validation host must use the same policy
and journal; a second journal is not an extra capacity grant.

Hosted settings `CODING_VALIDATION_LAUNCHER` and `CODING_VALIDATION_CONFIG` become
`GCA_VALIDATION_LAUNCHER` and `GCA_VALIDATION_CONFIG`. Hosted execution always sets
`GCA_REQUIRE_ISOLATED_VALIDATION=1`. Keyless preview preserves only these isolation
settings, not provider keys. Missing configuration/profile/image fails closed.

The private/non-Git local CLI remains usable. Its compatibility validation path is
labelled `host_not_isolated`. A copied directory and cleared environment do not contain
arbitrary code. Use managed isolation for untrusted tests. Secure Docker Desktop local
execution still needs separate capability verification; Linux proof is not macOS proof.

## Bounds and limitations

- Source: 5,000 entries, 32 MiB file content, no links/devices/FIFOs. Known secret,
  configuration and dependency paths are excluded. The controller owns the snapshot
  and must prevent concurrent writers. Secret scanning and source consent are still
  required: a filename filter cannot find every credential embedded in source.
- One CPU, 1 GiB memory, no extra swap, 128 PIDs, 256 open files. `/workspace` is a
  256 MiB tmpfs and `/tmp` a 64 MiB tmpfs, charged to that memory limit.
- No network, Docker socket, host bind mounts, persistent volumes, privileges or caps.
- Fixed profile commands, no dependency downloads. Images must already contain needed
  tools/dependencies. Isolated Git diff is unsupported because Git metadata is excluded.
- 256,000 combined output bytes with explicit truncation; 1–1,800 second validation
  deadline. A finite container lifetime also limits execution after controller loss.
- A private SQLite WAL journal commits intent before Docker creation and keeps an
  append-only transition history, without source, request text, output or credentials.
  FULL synchronization protects the recorded intent across process crashes.
- One process holds the host validation lock. A crashed process releases the lock, but
  its uncleared journal row still blocks a new job. This is one local validation slot,
  not a replacement for the central funded-host/deployment capacity ledger.
- Cleanup verifies the exact name, pinned image, host scope, job label and previously
  recorded container ID before removal. Identity mismatch or API failure retains the
  slot. A create timeout followed by absence is uncertain until the recorded deadline.
- The periodic reaper revisits expired jobs and late-arriving scoped containers.
  Unknown containers are reported, not deleted. No global Docker prune is used.
  A confirmed absence after expiry permits local reconciliation; late appearances
  remain detectable through the persistent job history and labels.

Validation output is untrusted. A zero exit code proves only that command's result,
not complete application correctness or deployment authority. Empty Python-source
selection is rejected instead of being labelled a successful compilation.

## Live evidence

`scripts/install_gvisor_host.sh` added signed-APT `runsc` release-20260921.0 and held
the package for reviewed upgrades. `/usr/bin/runsc` SHA-256:
`2eb94b58769293ce5041beecf4cbc14b173604371bfaac7cb3f3a84a28e44248`.
Docker's default runtime stayed unchanged. All six Coolify containers remained healthy.
No new VM, public port, paid model call or training job was added.

`scripts/probe_gvisor_host.py` passed 14 configuration/behavior checks using the existing
Redis image: non-root execution, read-only root, temporary writes, blocked outbound
request, configured limits and absence of host mounts. This is not universal escape proof.

The first launcher probe found Docker `cp` could not resolve gVisor's internal tmpfs
destination. The fix extracts a bounded, link-free archive **inside** the sandbox as
the unprivileged UID. It does not relax filesystem isolation. The repeated probe compiled
valid synthetic Python, rejected invalid syntax, left host source unchanged and removed
both containers. No private source or provider key was used. Tested Python image ID:
`sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f`.

### Crash proof and scheduled recovery — 28 September 2026 IST

`scripts/probe_validation_crash_recovery.py` terminated a synthetic controller with
`os._exit(97)` after the container was running, bypassing `finally` blocks. The SQLite
intent survived; the next validation was denied while cleanup was unresolved. The
probe expired only its own isolated test record (not the host clock), verified scoped
removal, then completed another valid compilation. No private source was used.

The actual host now has `gca-validation-reaper.timer` and its hardened oneshot service,
using `/etc/gca-validation/policy.json` and `/var/lib/gca-validation`. The timer is
enabled every minute; initial service result was success/exit 0. Active validation
causes a harmless deferred result. Uncertainty or untracked scoped containers causes
a nonzero service result for operator attention. Full Grafana alert wiring is pending.
An automatic timer execution was independently observed at 2026-09-27 19:39:38 UTC,
also successful. All six Coolify containers remained healthy; no validation containers
were left behind. After hardening renamed/replaced-container handling, the live crash
probe and service check passed again. Local coding regression count: 153 passed.

The installed policy currently approves only the synthetic-tested Python compile image.
Do not infer that pytest, Node, Java, Rust or Flutter dependencies have been provisioned.
The VM stop deadline and cloud/model budgets were not changed.

Useful operator checks (do not print the SQLite data into public logs):

```sh
sudo systemctl status gca-validation-reaper.timer --no-pager
sudo systemctl show gca-validation-reaper.service -p Result -p ExecMainStatus
sudo journalctl -u gca-validation-reaper.service -n 10 --no-pager
```

To stop scheduled cleanup, disable its timer; this does not authorize new validation
jobs or release unresolved capacity. Preserve the journal during rollback. Never
remove a blocked row merely to make another job start.

## Remaining activation work

1. Dependency-complete Python/Node/Rust/Flutter toolchains, immutable scan/build
   attestations and adversarial workload tests.
2. Bind durable host validation jobs to authenticated core run/step IDs and the central
   funded capacity ledger. The host journal/reaper now exists, but it does not grant
   cloud spending or tenant access. Exercise host reboot and Docker outage recovery.
3. Linux hosted-worker deployment and full Rust-to-launcher live proof. The Railway
   worker does not gain remote Docker access merely because the VM has a launcher.
4. Authorized source transport, tenant isolation and artifact export policy.
5. Generated-app behavioral checks, actual cost reconciliation and deployment proof.

Sources: [gVisor installation](https://gvisor.dev/docs/user_guide/install/),
[Docker resource constraints](https://docs.docker.com/engine/containers/resource_constraints/).
