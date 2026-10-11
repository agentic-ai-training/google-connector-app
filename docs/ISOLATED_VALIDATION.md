# Keyless validation boundary

Status, 3 October 2026: live host, synthetic Python validation and offline image
recovery proofs passed; generated React/Spring artifact export is locally tested.
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

`app/coding/isolated_validation.py` and its sibling `build_artifact.py` form a
standard-library launcher bundle. Install both outside model-editable workspaces.
The sibling helper is loaded by its protected absolute path under `python -I`.
Operator policy and parent directories must not
be group/world writable. The legacy operator-probe configuration has `version: 2`,
`runtime: "runsc"`, `state_directory`, and `images`, a map from supported profile name to local
`sha256:<64 lowercase hex digits>` image ID. Workload execution never downloads images.
If Docker cleanup removes a pin, the launcher may reload its exact operator-owned
archive from `/etc/gca-validation/toolchain-registry.json`, verifying the protected
path, size, archive SHA-256 and resulting image ID. Provisioning and registry changes
remain operator maintenance; a model cannot choose a new image or archive.
Only reviewed, secret-free, dependency-complete toolchains may be registered.
The state directory must already exist with mode 0700, outside the source tree, under
operator-protected parents. All jobs on this validation host must use the same policy
and journal; a second journal is not an extra capacity grant.
Policy v3 adds an immutable `server_uuid` and requires a private central-claim envelope
for every execution. The host binds source/image/profile/deadline and journals consumed
claims; the deployed host now uses v3. Legacy v2 is retained for standalone test fixtures.
See [host dispatch contract](VALIDATION_CAPACITY.md#host-dispatch-contract) for the
authenticated spool and reconciliation boundary. A hash alone is not authentication.

Hosted settings `CODING_VALIDATION_LAUNCHER` and `CODING_VALIDATION_CONFIG` become
`GCA_VALIDATION_LAUNCHER` and `GCA_VALIDATION_CONFIG`. Hosted execution always sets
`GCA_REQUIRE_ISOLATED_VALIDATION=1`. Keyless preview preserves only these isolation
settings, not provider keys. Missing configuration/profile/image fails closed.

The private/non-Git local CLI remains usable for explicitly authored offline
plans. Its compatibility validation path is labelled `host_not_isolated`: a copied
directory and cleared environment do not contain arbitrary code. Cloud-model
plans now require an operator-owned launcher/config outside the workspace before
the provider call, and both preview and approved replay require isolated
validation. Missing or invalid isolation fails closed; it cannot silently use
the host compatibility path. Secure Docker Desktop local execution still needs
separate capability verification; Linux proof is not macOS proof.

## Bounds and limitations

- Source: 5,000 entries, 32 MiB file content, no links/devices/FIFOs. Known secret,
  configuration and dependency paths are excluded. The controller owns the snapshot
  and must prevent concurrent writers. Secret scanning and source consent are still
  required: a filename filter cannot find every credential embedded in source.
- One CPU, 1 GiB memory, no extra swap, 128 PIDs, 256 open files. `/workspace` is a
  256 MiB tmpfs and `/tmp` a 64 MiB tmpfs, charged to that memory limit. The current
  launcher source marks these disposable internal mounts `exec,nosuid,nodev` for
  test binaries/JNI. Packaging's host-backed volumes remain `noexec`. The 6 October
  correction is installed/read-back verified on the VM, with bound synthetic Python
  regression/cleanup proof and independent six-container Coolify health check;
  Node/Java host execution remains unproven. See
  [offline toolchains](OFFLINE_TOOLCHAINS.md).
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

### Generated React/Spring profiles — local code, not host activation

The trusted launcher now has fixed profiles for a generated `frontend/` Vite/React
application and `backend/` Maven/Spring application: `react_test`, `react_build`,
`spring_test`, and `spring_package`. The archive must contain the corresponding
lockfile/POM, source and tests; staged `dist/`, `target/` and `node_modules/` cannot
be supplied as proof of a build. React profiles run `npm ci --offline` with lifecycle
scripts disabled before invoking the local Vitest/Vite binaries. Vitest explicitly
denies zero-test and focused-only success and uses one worker. Spring profiles
use Maven offline mode with `failIfNoTests=true`. All commands and postchecks share
one bounded validation deadline. A successful React build must produce a non-empty
`dist/index.html`; Spring testing must produce bounded Surefire XML evidence with
at least one executed (not skipped) test and zero reported failures/errors, and
packaging must additionally produce a non-empty JAR. The reports are parsed by the
trusted controller without a host mount; malformed, oversized, missing, or all-skipped
reports fail closed. Missing outputs fail validation even if a command reports exit
code zero. Generated source could still forge a report, so these are tool-specific
postconditions, not proof of application behavior or source-to-image provenance.

The installed operator policy still approves only the synthetic Python image.
No dependency-complete Node or Java toolchain image, offline package cache,
compatible Maven repository, approved image ID or live host execution is proven
for these profiles. In particular, a read-only/offline cache that cannot satisfy
the requested lockfile must fail rather than trigger a network download or a
silently weaker validation. The generated-app deployment gate remains closed.

### Source-bound generated build outputs

Successful centrally bound `react_build` and `spring_package` jobs now export their
actual output before container cleanup. The controller reads only `dist/` or the
single package JAR through a bounded tar stream. The artifact inspector rejects
links, traversal, duplicate paths, secret-like filenames, malformed archives, and
missing build outputs. Spring packaging requires an executable Boot JAR structure.
It computes an archive digest and a sorted per-file digest set without extracting
generated code onto the host.

The exact archive is retained in the operator journal's private `build-artifacts/`
store. Files are mode 0600, the directory is mode 0700, writes are atomic, readers
rehash the bytes, and a separate lock coordinates readers, writers and the reaper.
Limits are 128 MiB per archive, eight stored objects and 512 MiB total; the retention
period is 24 hours. The existing host reaper purges expired objects and interrupted
temporary files. Unsafe or unknown store entries require operator inspection.

The result contains a compact reference bound to source, validation claim, host job,
profile, digests and expiry. The durable controller verifies that reference and
encrypts it with the rest of the private result. A successful build-profile result
without that reference is rejected. Workspace mutations are discarded after the
separately retained build output has been captured. Cleanup uncertainty still
overrides validation success.

This export supplies input for the next trusted image-packaging step. The image
assembly component now connects authorized private retrieval to an operator-pinned,
non-executing OCI packager; see [OCI packaging](OCI_PACKAGING.md). Production worker
activation, registry publication, signed provenance and live behavioral proof remain
open. Artifact structure alone does not prove application behavior or deployment.

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

### Bundle and offline toolchain recovery — 3 October 2026

A live transport probe found the old Python image absent. No validation container
was created, and the uncertain intent was reconciled by the existing journal/reaper.
Coolify has forced daily Docker cleanup configured; its involvement in the image's
disappearance is plausible, not proven. The correction retains a bounded private
offline archive instead of disabling general host cleanup or allowing workload pulls.

The operator provisioner pulled only this immutable official Python AMD64 manifest:
`docker.io/library/python@sha256:6b1f85a08c199d29d5b6d71ab9c27bd5b3b393492e01216a15758ff69c4be8b8`.
Its registry config digest is `sha256:414a398990af718f018ff9c23cea0e7489b7986eb54f2b1d7cc874c99ebc7364`.
This VM's Docker containerd store reports the **manifest** digest as the local image
ID. A first config-digest comparison stopped without updating policy;
the provisioner now checks the exact host image ID and Linux/AMD64 platform.
It does not treat config and manifest identities as interchangeable.

Valid synthetic compilation passed and invalid syntax was rejected before the
Python pin was updated. The root-private 46,237,696-byte image archive has SHA-256
`36723f7fff07fb1712479bdbf5b09a31d67f38b0a45af4648e1c00b1e80d3778`.
A controlled fault probe verified the archive, idle journal and no image consumers,
removed only that reviewed synthetic image, then restored its exact ID offline.
The bound IAP transport proof passed, including replay denial and cleanup observation.

The protected two-file bundle was installed with an idle journal and isolated import
check. Installed SHA-256 values:

- `isolated_validation.py`: `e972e697dfae8181a0cd2683366f67b289f904de00b51da0f4014b7f548ea7a0`
- `build_artifact.py`: `1a6e04a0e56f0893335bd9d6476b794e01cd4c563d9dd9e1fe7d4cf9136fdc0d`

Local coding regressions passed: 380 tests using unique disposable schemas on
loopback PostgreSQL, plus targeted Flake8/Bandit/diff checks. No private source,
paid model call, new cloud resource, generated app or production migration was used.
The React/Spring export tests remain mocked host proofs: their dependency-complete
toolchains and live source-to-image packaging are not yet provisioned.

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

On 4 October 2026 the complete seven-file validation/packaging stdlib bundle replaced
the two-file bundle under the idle shared host slot. Independent read-back verified
all hashes; the old complete tree is retained. A live nonsensitive Python transport
probe passed compilation, exact cleanup observation and same-claim replay rejection.
All six Coolify containers remained healthy; the reaper service reported success/0;
no validation containers remained. Policy/toolchain images, production admission,
budgets and STOP deadline were unchanged. See [host bundle operations](HOST_BUNDLE_OPERATIONS.md)
for current hashes and the crash-recoverable operator installation/rollback protocol.
This does not prove live React/Spring builds or packaging execution.

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

1. Dependency-complete Python/Node/Java/Rust/Flutter toolchains, immutable scan/build
   attestations and adversarial workload tests.
2. Bind durable host validation jobs to authenticated core run/step IDs and the central
   funded capacity ledger. The central claim model and shared-capacity admission are
   now implemented/tested locally in migration 019. Host claim verification/replay
   prevention and central observation matching are implemented with a synthetic live
   proof. Fixed-command IAP/SSH transport also passed its synthetic live proof;
   the durable controller is implemented locally, but its protected IPC connection to
   the Rust broker and financial enrollment remain pending.
   See [validation capacity](VALIDATION_CAPACITY.md). The host journal
   does not grant cloud spending or tenant access. Exercise host reboot and Docker outage recovery.
3. Linux hosted-worker deployment and full Rust-to-launcher live proof. The Railway
   worker does not gain remote Docker access merely because the VM has a launcher.
4. Production-authorized source transport, tenant isolation and authenticated artifact
   consumption by a keyless builder. Local export/CAS policy exists; live React/Spring
   execution and image-packaging provenance remain unproven.
5. Generated-app behavioral checks, actual cost reconciliation and deployment proof.

Sources: [gVisor installation](https://gvisor.dev/docs/user_guide/install/),
[Docker resource constraints](https://docs.docker.com/engine/containers/resource_constraints/),
[npm clean install](https://docs.npmjs.com/cli/commands/npm-ci/),
[Vitest CLI](https://vitest.dev/guide/cli),
[Maven offline mode](https://maven.apache.org/guides/introduction/introduction-to-repositories),
[Surefire no-test policy](https://maven.apache.org/surefire/maven-surefire-plugin/test-mojo).

## Native descriptor-limit correction — 11 October 2026

The first instrumented Spring fixture exposed a real Maven/Javac failure:
`json-path-2.10.0.jar: Too many open files`. Its retained result was read through
the no-execution diagnostic operation, with source/job/result/native hashes and
cleanup checked. Spring was not approved. A following reviewed source change gives
only `spring_test` and `spring_package` 1,024 open files per process (soft and hard);
all other profiles retain 256. Native readiness verifies the actual guest limits
as well as Docker metadata before extracting/executing the fixture.

This changes a bounded descriptor dimension, not VM size, budget, execution deadline,
CPU, memory, network, privileges or persistent mounts. It does not by itself prove
Spring success. New source-bound positive test/package and deliberate negative
proofs are required. See the exact receipts in `HOST_BUNDLE_OPERATIONS.md`.
