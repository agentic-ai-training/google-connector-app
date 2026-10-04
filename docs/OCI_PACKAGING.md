# Validated artifact to immutable image

Status, 3 October 2026: internal assembly and private retrieval are implemented
locally. This is not an activated production builder or a completed deployment.

```text
Four tenant/source-bound validations
  React tests + build; Spring tests + package
                 |
                 v
Encrypted durable passing result + live artifact reference
                 |
Tenant/run/target authorization; active immutable target
                 |
Pinned IAP/SSH fixed gateway
  exact consumed host binding + cleaned job + private CAS
  bounded binary frame; rehash; no arbitrary file path
                 |
Recheck deletion, revocation and expiry
                 |
Operator-owned runtime policy + pinned OCI base blobs
  rehash manifest/config/layers; verify Linux/AMD64 and rootfs
                 |
Fixed non-executing OCI assembler
  React -> Nginx static files + same-origin /api/ proxy
  Spring Boot JAR -> Java 17 startup contract
                 |
Measured layer/config/image-manifest digests + build evidence
                 |
OCI layout archive (LOCAL COMPONENT)
                 |
----- not yet activated/proven below this line -----
Scoped registry publication and exact readback
Activate durable build-job ledger + separate protected signer
Compose bound to the two measured image digests + approved PostgreSQL
Cost/capacity/user grant + one-time deployment dispatch
Private runtime + behavioral/security/restart verification
Authorized Google Chat delivery of the exact verified URL
```

## Implemented boundaries

`validation_controller.fetch_build_artifact` reads only an existing authenticated
tenant's unexpired passing result. The requested run, target and frozen source
must match. The remote envelope is reconstructed from immutable claim/target facts,
not accepted from a model. The host verifies its stored consumed-binding hash and
cleaned job before opening the exact private CAS object. A binary response binds
the reference, size and archive hash. The gateway holds its host slot and transfer
alarm while writing the bytes, preventing parallel slow readers from multiplying
staging memory. There is no new public endpoint or listener.

Deletion, expiry and target revocation are rechecked after transfer. Read-only
retrieval can be retried; it does not rerun compilation or re-create an artifact.
A cleaned host job alone is not proof of a passing validation. The central result
must separately be passing, immutable and present.

`runtime_image_store.load_runtime_base` reads a private operator policy outside
the generated workspace. Policy v1 maps the two supported roles to a manifest
digest and protected OCI layout directory. Symlinks, unsafe ownership/permissions,
hardlinked files, unknown roles and changed blobs are rejected. The selected image
index must contain the approved manifest. All compressed layers and their uncompressed
diff IDs are verified with bounded reads; no base archive is extracted onto the host.

`oci_packager.package_validated_artifact` re-inspects actual artifact bytes and their
file-set hash before constructing an image. It never runs generated code, a
Dockerfile, a shell, a build hook, or a network operation. Its role-specific runtime
commands and configuration are trusted code, not model-provided strings. Layer paths,
ownership, permissions, ordering, timestamps and compression are normalized. Whiteout
names are rejected so generated outputs cannot delete lower-layer files. The output
contains actual content-addressed manifest/config/layer blobs, not guessed image IDs.

The frontend role runs Nginx as UID/GID 65532 on port 8080, serves the built React
bundle, and proxies `/api/` to `backend:8080` without stripping the path. The backend
role starts the exact Boot JAR using the approved Temurin Java 17 binary as the same
non-root user. Known incompatible application class headers (including Java 21 and
preview bytecode) are rejected. These structural checks do **not** prove application
behavior or dependency compatibility; actual startup/API/database/browser tests remain
mandatory. Additional languages or runtime versions require separate versioned
contracts, not an implicit weakening of these profiles.

`validation_controller.package_build_artifact` connects authorized retrieval to the
operator-policy loader and real assembler off the event loop, then rechecks private
result and active target state. This function is internal controller code. Its policy
path and source workspace are trusted worker configuration, never request text.
The production worker and Rust broker do not yet call this path.

The fixed artifact-read gateway was installed on the existing VM using the idle-slot,
exact-base-hash two-file installer. Its latest launcher SHA-256 is
`6e76e0b4268530dc50847237da91a2f289be1194b2408db9c458db1f241e44e1`;
the helper remains `1a6e04a0e56f0893335bd9d6476b794e01cd4c563d9dd9e1fe7d4cf9136fdc0d`.
The isolated import check passed and image policy was unchanged. Actual React/Spring
artifact transfer on that host still awaits the real offline toolchains; installation
and local mocked proofs must not be described as a live generated-image build.

## Bounds

| Resource | Bound |
|---|---:|
| Artifact archive | 128 MiB; 4,000 files |
| Base metadata object | 1 MiB |
| Compressed base layers | 512 MiB total; 64 layers |
| Unpacked base data for diff-ID checks | 2 GiB, streamed |
| Runtime policy | 32 KiB; two roles |
| Artifact transfer frame header | 8 KiB |
| Host transfer alarm | 45 seconds |
| Client transfer timeout | 120 seconds |

Assembly still needs a dedicated keyless worker with enforced CPU/memory/storage/time
limits and activation of durable build-job ownership before production use. In-process thread
cancellation does not stop CPU work; it is not equivalent to an isolated worker.
No runtime base has been enrolled on the VM, no image published, no signing key
installed and no generated Service started by this component.

## Remaining integration and proof

Checkpoint verification: 426 coding tests passed with the real loopback PostgreSQL
fixtures; targeted Flake8/Bandit and diff checks passed. The OCI tests use synthetic
runtime/class fixtures, not a running Nginx/Java app. Controller integration exercises
the actual assembler and protected base-store loader, with the remote transfer mocked.
Do not use this count to claim live production deployment or behavior.

1. Provision and scan approved Nginx/Java/PostgreSQL bases and complete offline
   Node/Maven validation toolchains under the existing hosting envelope.
2. Activate the migration-021 image-job ledger through the authenticated worker;
   implement its host journal and durable output CAS. Do not hold database locks
   over assembly or network I/O. Local ledger implementation is described below.
3. Run the dedicated keyless builder and authenticated publication controller.
   Record exact registry manifest readback; a local image ID or a tag is insufficient.
4. Bind measured image evidence, run/tenant/source/target/validation proof set to a
   protected signer; keep its key outside model and generated-code authority.
5. Wire actual Compose generation, secrets, Service create/start/reconciliation,
   security/behavioral verification and final authorized URL delivery.
6. Demonstrate the complete durable request and fault recovery, not merely mocked
   HTTP or a structurally valid OCI layout.

## Durable image-job ownership (local implementation, migration 021)

`app/coding/image_build_jobs.py` records the exact tenant/run, passed validation
claim, artifact/source digests, immutable target, runtime-policy hash, base manifest,
execution deadline and output-retention deadline. Enqueue and dispatch separately
recheck the current coding-run lease, consent, target, passing artifact evidence and
funded host capacity. Transactional events and an immutable request hash make
idempotent repeats observable without giving them a second dispatch token.

```text
consented, leased run + passing private build artifact
                       |
  reserve shared funded CPU / memory / storage
                       |
  commit ONE dispatch token + owner + original deadline
                       |
  [keyless resource-enforced host worker: STILL TO BE WIRED]
          |                             |
   bound measured receipt          interruption/lost response
          |                             |
   finish + encrypted result       uncertain; capacity retained
   release CPU/memory                   |
   retain output disk             authenticated observation
          |                       (never blind redispatch)
          +---------+-------------------+
                    |
   verified staging/output cleanup -> release disk -> retired
```

Fixed admission is 1 CPU, 3 GiB memory and 2 GiB temporary/output storage, shared
with validation and deployed-app claims. The worker must enforce those limits on
that exact funded host; a local Python thread is not such a worker. Successful
packaging releases CPU/memory only after authenticated process-cleanup evidence;
disk remains charged until its retained output is actually removed. Neither a
heartbeat timeout nor an output expiry frees capacity by itself.

A replacement leased controller may recover the old worker's measured receipt
without changing its dispatch token. The receipt's completion time must fall
between the original dispatch and execution deadline, even if delivery is later.
Recovery still requires live output retention and private validation evidence.
The result is immutable and encrypted; deletion/expiry suppress private access.
Deleting a run preserves orphan resource obligations and scrubs the stored private
receipt. Downgrade refuses outstanding resources or retained private receipts.

These are internal trusted-controller operations, not public/model-callable APIs.
Typed receipts and matching hashes are **not** authentication or proof that output
exists: the controller must authenticate the worker, rehash the actual durable CAS
object and verify cleanup before calling `record_result`/`release_resources`.
Activation of the worker/output CAS, authenticated receipt/cleanup transport,
registry and signer are still incomplete. No production migration has been applied.

Verification of this ledger checkpoint: all 465 coding tests passed against the
real loopback PostgreSQL fixtures (no skips). The disposable full Alembic chain
upgraded to 021, downgraded to 002 and upgraded back to 021 successfully using
`scripts/verify_cloud_migrations_local.py`. Targeted Flake8, Bandit and `git diff
--check` passed. The image-job tests measure actual synthetic OCI assembly for
receipt fixtures, but the worker receipt/cleanup authority is simulated; no live
keyless worker or durable OCI output-store proof is claimed.

## Private retained OCI outputs and fixed worker logic

The next local component, `image_output_store.py`, retains actual OCI archive bytes
in owner-only per-job directories (0700 directories, 0600 files). SQLite WAL/FULL
tracks the exact dispatch binding, image evidence, archive digest/size and expiry.
File and directory fsync precede the retained state. Interrupted writes remain
`writing`, occupy their storage allowance and cannot be blindly overwritten.
Repeated fully committed requests recover the same immutable measurement.

The store permits at most eight live outputs, 768 MiB per archive and 2 GiB total;
it retains bounded tombstones to prevent reuse of removed job identities. Reads
rehash actual bytes and verify the complete OCI blob graph, compressed descriptors
and uncompressed rootfs diff IDs without extracting anything onto the host.
Unbound blobs, links, unsafe ownership/permissions, changed content and foreign
dispatch bindings are rejected. Cleanup removes only tracked filenames and refuses
unexpected content. Its observation explicitly says `execution_cleanup_verified:
false`: deleting an output is not proof that a worker stopped.

`image_packaging_worker.py` exposes only fixed assembly and read-only observation.
It binds the actual artifact, approved runtime-policy hash and base manifest to the
central job envelope, assembles with the non-executing packager and stores the image.
Its isolated-Python bootstrap imports only reviewed, protected sibling modules,
not generated repository code. Responses contain measured references, never source
bodies, model keys or a claim of worker termination. The central dispatch now emits
the exact worker envelope including target/server/project, timeout and deadlines.

The CLI requires Linux UID 65532, an allowlisted keyless environment, empty effective
capabilities, no-new-privileges, seccomp filtering, finite cgroup CPU/memory/pid limits,
zero swap, loopback-only networking and a read-only root; it sets additional process
and transfer/time bounds. These guest-side checks do not authenticate the runtime
or its host. The trusted launcher still must consume the exact dispatch in its host
journal, enforce gVisor/container/mount identity, preserve only per-job storage,
own cancellation/reaping and verify process termination before recording a central
success/cleanup receipt. That host launcher/transport and activation are **not yet
implemented/proven**; this component has not been installed or run on the VM.

Local tests exercise real assembly and filesystem/database retention with synthetic
runtime/JAR fixtures. They also prove standalone `python -I` bootstrap and CLI
rejection of the current unenforced environment. They do not simulate a successful
enforced Linux launch as live evidence, and do not prove a running generated app.

Verification for this output/worker checkpoint: all 501 coding tests passed with
real loopback PostgreSQL fixtures and no skips; targeted Flake8, Bandit and diff
checks passed. The only warning is the existing Google GenAI Python 3.14 typing
deprecation from the user-owned Vertex-provider test. No production migration,
VM modification, model call, registry publication or generated Service occurred.

## Shared host journal and cleanup-only recovery

The local launcher journal now records `purpose=validation|image_packaging` in
the **same** SQLite database and flock-protected slot. Existing rows are migrated
in place as validation work. Both purposes persist their exact dispatch binding
before container creation, share the deadline reaper, require exact immutable
image/name/container/scope/job/purpose identity, and preserve consumed-claim
tombstones. A crashed owner cannot free the slot just by releasing its lock.
Packaging work cannot be used to authorize a validation-artifact read.

```text
Central one-time dispatch -> shared host journal intent
                                |
                         process outcome uncertain
                                |
                pinned IAP/SSH read-only observation
                                |
                    exact job + binding verified
                      /                    \
             process present          process removed
             CPU/memory held          CPU/memory free
                      \                    /
                       output storage still held
                                |
               authenticated CAS removal + host cleanup
                                |
                       full resources released
```

Migration 022 binds recovery to an immutable host job ID and stores only a
sanitized observation digest/time. `image_build_jobs.recover_execution` checks the
registered target before invoking the existing protected-credential, pinned-host
IAP/SSH observation transport. It never launches a worker. The reconciliation
path reconstructs the original envelope from immutable ledger facts, including
after run deletion, target revocation or private-result expiry. Unknown jobs,
transport failures, wrong fingerprints and guest claims cannot free resources.
The host's `packaging_success=null` is intentional: termination does not prove an
image was assembled. Output storage stays allocated until separately verified
removal. If a late container is observed after cleanup—even after retirement—the
resource obligation reopens without issuing a new dispatch.

These local journal and controller changes have **not** been installed/activated
on the VM. The fixed gVisor packaging launcher, private per-job mounts, authenticated
image receipt/cleanup transfer, registry publication and protected signing remain
unfinished. Migration 022 has not been applied to production. Passing mocked host
observations and real local database tests do not constitute live packaging proof.

Verification: all 537 coding tests passed against real loopback PostgreSQL fixtures,
with no skips. The full disposable migration chain passed upgrade to 022, downgrade
to 002 and re-upgrade to 022. Targeted Flake8, Bandit and `git diff --check` passed.
The existing Google GenAI/Python 3.14 deprecation warning remains unchanged.

References: [OCI manifests](https://github.com/opencontainers/image-spec/blob/main/manifest.md),
[OCI image configuration](https://github.com/opencontainers/image-spec/blob/main/config.md),
[OCI layers and whiteouts](https://github.com/opencontainers/image-spec/blob/main/layer.md),
[OCI layouts](https://github.com/opencontainers/image-spec/blob/main/image-layout.md).

## Fixed host launcher and durable controller — 3 October 2026

The formerly missing launcher and receipt path are now implemented **locally** in
`isolated_packaging.py`, `image_build_transport.py` and `image_build_controller.py`.
Earlier checkpoint paragraphs describe the state at those checkpoints, not current
installation status. This seven-file bundle is not yet installed on the VM.

```text
Leased controller + consent + funded capacity
        |
one-time dispatch binding persisted in PostgreSQL
        |
pinned IAP/SSH -> fixed root gateway -> shared host journal intent
        |
exact retained validation artifact + approved offline runtime base
        |
private 2,040 MiB filesystem; read-only per-job input/base/bundle mounts
        |
fixed UID 65532 gVisor worker; no network, keys, Docker socket or shell choice
        |
non-executing OCI assembly -> fsync/private output journal
        |
exact container termination -> host rehash -> authenticated bound receipt
        |
CPU/memory released; output disk remains held
        |
explicit/expiry cleanup -> normal verified unmount -> private volume removed
        |
authenticated removal receipt -> disk reservation released
```

The root controller and guest share the operator-prepared `gcapackaging.slice`
aggregate limit: at most one CPU, 3 GiB memory, no swap and 128 total tasks. The
current local worker contract permits at most 64 tasks after the measured 16-task
startup failure. These new task limits are not yet configured/proven on the VM.
A private fixed-size ext4 backing file bounds staging;
normal unmount is mandatory. Busy, foreign or ambiguous mounts remain allocated.
The reviewed source bundle is hashed and loaded from exact protected files, without
workspace imports or cached bytecode. The controller never extracts build archives
or runs generated code. Only the guest can write its private output mount.

Transport sends bounded metadata, not a second artifact upload. The host reads its
already retained validated artifact. Loss of a response, controller cancellation or
worker death preserves the original dispatch and capacity obligation. A replacement
controller observes/retrieves the same job; it cannot launch another one. Cleanup
remains available after run deletion or target revocation and cannot use a guest's
JSON assertion as authentication. Both execution and storage evidence are required.

Before activation, still required: install/hash the complete protected bundle;
prepare and verify the operator cgroup; approve real offline Node/Java validation
toolchains and runtime bases; run synthetic Linux/gVisor mount, interruption,
reaper and cleanup proofs; apply approved production migrations and enroll funded
capacity using fresh attributable billing; implement registry publication/protected
signing and worker integration; complete behavioral generated-app and Chat delivery
proof. Existing two-file VM installation and local mocks do not satisfy these gates.

Tests use real local SQLite/OCI files and disposable PostgreSQL schemas, while
Docker, privileged filesystem mounts, cgroups and pinned-host responses are simulated.
No new cloud resource, production migration, private-source remote execution,
provider call, registry publication or generated deployment occurred here.

Verification for this launcher/controller checkpoint: 601 coding tests passed with
real loopback PostgreSQL and no skips; targeted Flake8, Bandit and diff checks passed.
The existing Google GenAI/Python 3.14 deprecation warning remains. No migration SQL
changed after the prior successful disposable 022 -> 002 -> 022 round trip.

## Host installation checkpoint — 4 October 2026

The seven-file protected bundle is now installed on the existing Delhi VM and
independently rehashed. This supersedes earlier statements that it is not installed.
The new operator installer preserves a complete old-tree backup, records an external
durable intent before staging, swaps directories under the existing host slot and
allows identity/hash-verified rollback after interruption. It refuses old-bundle
rollback after packaging history or staging appears. Full installation hashes,
recovery commands and evidence are in [host bundle operations](HOST_BUNDLE_OPERATIONS.md).

All 620 local coding tests pass, including actual process-death filesystem recovery.
The live synthetic Python validation/replay/cleanup proof passed; six Coolify
containers remain healthy and the scheduled reaper reports success. Packaging policy,
shared cgroup, actual approved runtime/toolchain bases and live packaging execution
are still absent/unproven. No private-source execution, model call, production
migration, financial enrollment or generated deployment occurred. Installation does
not complete the image-to-application story or authorize new spending.

## Live runtime compatibility findings — 4 October 2026

`scripts/probe_packaging_environment.py` runs only a fixed nonsensitive observation
program with the approved offline Python image, keyless UID 65532, read-only root,
no network and a read-only mount of the reviewed bundle. It uses the existing host
journal slot and persistent intent; cleanup is verified before reporting results.
It neither admits private packaging inputs nor changes production worker limits.

The first 16-task probe failed before Python started, with gVisor sandbox startup
ending in EOF. A controlled comparison changing only the task ceiling to 64 started
successfully. This strongly supports an inadequate startup task allowance; it is
not evidence that all possible workloads fit in 64 tasks. Both probes cleaned up.
The successful observation reported virtual `CapEff=0`, `NoNewPrivs=1`, `Seccomp=0`
and unavailable guest `memory.max`, `memory.swap.max`, `pids.max` and `cpu.max`.
Therefore the current worker's guest-only enforcement function rejects this actual
runsc environment. Docker's configured limits are not proof of native enforcement.
The exited `State.Pid=0` snapshot cannot be used as a live host-process observation.

The required correction remains **unimplemented**, not bypassed:

1. Prepare the fixed aggregate native host resource group, with measured finite
   sandbox/controller task allowance and unchanged CPU/memory/swap envelopes.
2. Verify the exact running container ID, immutable image, native process identity,
   cgroup membership/limits and privilege filter from the trusted host while alive.
   Account for sandbox/helper processes, not just a guest's virtual PID.
3. Keep the worker behind a bounded readiness handshake. Publish a host-controlled,
   read-only, job/dispatch/profile/deadline-bound proof only after native verification.
   The worker must not consume private artifact bytes before that proof.
4. Separate gVisor virtual checks from host authority. Do not accept missing cgroup
   files or `Seccomp=0` as a blanket success, use a guest JSON assertion as host
   evidence, expose host cgroup mounts, or fall back to unapproved native execution.
5. Prove startup, timeout, cancellation, forged/cross-job readiness, host verification
   failure and cleanup through real nonsensitive probes before private activation.

This is a runtime-wide correction, not a per-user-request exception. Existing
packaging remains disabled. The probe's raw bounded diagnostics concern only its
fixed nonsensitive program; do not reuse this output policy for private workloads.
Official references: [gVisor compatibility](https://gvisor.dev/docs/user_guide/compatibility/)
and [host cgroup drivers](https://gvisor.dev/docs/user_guide/systemd/).

Local verification for this diagnostic checkpoint: all 624 coding tests passed with
real loopback PostgreSQL and no skips; targeted Flake8/Bandit and diff checks passed.
The four new probe tests cover negative guest evidence, startup failure and connection
loss with cleanup, without pretending to prove host enforcement. CLI observation
results explicitly carry `native_enforcement_verified=false` and
`production_packaging_activated=false`, even when observation itself completes.

### Local native readiness implementation — 4 October 2026

The launcher now starts the fixed worker behind a bounded readiness barrier. A
trusted callback verifies its exact running container, immutable configuration,
native PID/start-time and `pidfd`, native capabilities/no-new-privileges/seccomp,
child and aggregate cgroup limits, and associated runsc helper membership. This
version supports only the fixed systemd resource-group topology; a different live
layout fails closed and needs separately verified operator integration.

Only after verification does the host publish a private proof into the worker's
read-only input mount. The proof binds the complete dispatch and deadline. The
worker verifies metadata/proof first, then requires the frame header to match
before reading private artifact bytes. Neither a hash nor caller-supplied JSON
authenticates host origin. No host cgroup mount, key, shell or socket is exposed.

```text
durable intent -> fixed worker starts -> bounded wait (no artifact read)
                         |
                 host native observations
                         |
               exact-dispatch readonly proof
                         |
                frame binding -> artifact read -> assembly
                         |
             exact Docker removal + native helper/group absence
                         |
            execution capacity released (storage still retained)
```

Cleanup persists the exact container ID before removal. Docker absence alone is
insufficient: surviving native helpers or a populated native group keep cleanup
uncertain and capacity held. Lost identity also remains uncertain, except when
the trusted launcher knows that creation was never attempted. A later observation
of surviving helpers invalidates prior cleanup evidence. Private storage removal
continues to require its separate authenticated proof.

This checkpoint is local implementation/testing, **not installed or activated on
the VM**. Native process/helper ABI, resource-group preparation, readiness and
termination must still be proven with live nonsensitive workloads before admitting
private packaging inputs. Approved Node/Java toolchains/runtime bases, financial
enrollment, publication/signing and the generated-app/Chat demonstration remain open.

Verification: 156 targeted readiness/launcher/journal tests and all 669 coding
tests passed; the full suite used real loopback PostgreSQL with disposable schemas
and no skips. Targeted Flake8/Bandit passed. These results include simulated native
kernel/mount/Docker decisions, not live resource enforcement.
