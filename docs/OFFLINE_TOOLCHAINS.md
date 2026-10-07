# Reviewed offline React and Spring toolchains

## Authority and workflow

These are operator-owned dependency-acquisition recipes, not generated Dockerfiles
or model-callable build commands. Only fixed public dependency specifications and
the two fixed public Spring seed files enter the bounded build contexts. No customer
repository, OAuth/provider key or production database enters acquisition. Generated
source runs later in a separate keyless, network-disabled sandbox. Operator Docker
credential helpers may authenticate a public-image pull; credentials are never copied
into either image.

```text
review public specs + immutable AMD64 bases + exact build contexts
                       |
             operator dependency acquisition
                       |
        fixed positive and deliberate negative tests
                       |
     local Docker proof (NOT Linux/gVisor enforcement proof)
                       |
  protected image export + durable import reservation [local; live import pending]
                       |
        live Linux/gVisor four-profile proof [pending]
                       |
       funded central dispatch and image approval [pending]
                       |
   generated source -> tests/build -> verified packaging
                       |
  signing/deployment/application behavior/Chat proof [pending]
```

The pure bounded inspector in `app/coding/toolchain_spec.py` is not image approval
authority. It rejects unreviewed direct versions, npm lifecycle hooks, duplicate JSON
keys, private/credentialed URLs, missing SHA-512 integrity, links and traversal. The
Maven catalog rejects entities, alternate repositories, executable plugin configuration,
extra dependencies, duplicated fields and extra build authority. Dependency changes
require a new reviewed recipe and fresh offline proof. This is not an arbitrary-POM
execution service.

## Crash-persistent import reservation — local implementation, 6 October

### Subsequent local durable-loader checkpoint

`toolchain_loader.py` adds the fixed operator systemd outbox/worker/read-back path.
That checkpoint's source closure had twelve files; the subsequent local fixed-fixture
proof controller below made the reviewed closure thirteen files. The subsequent
publication controller expands it to fourteen. **The VM still runs the independently
verified eleven-file custody release** `529bc18...`. No live load/unit dispatch,
registry change, role approval or generated code execution occurred in this checkpoint.
Both verified public imports remain cancelled with retained bytes/audit. Do not
dispatch a real load before host profile-proof, verified publication and the
forward-compatible recovery path is ready.

### Local fixed-fixture proof controller — 7 October

`toolchain_proof.py` adds operator-only `proof_run` and read-only `proof_observe`.
Each role has three independently hash-pinned fixture cases: React tests/pass,
React build/pass, React tests/deliberate failure; Spring tests/pass, package/pass,
tests/deliberate failure. All six cases use checked-in public source, not a generated
repository or caller-selected archive. Both positive and negative source archives
are pinned by exact byte count and SHA-256. The client and host independently check
them; the client also validates returned intent/case/source/cleanup/result lineage.

```text
verified load + actual image read-back + unchanged protected controller binding
    -> same journal: proof_pending (ordinary validation/packaging still blocked)
    -> durable case/attempt/job before Docker create
    -> fixed sandbox metadata + actual native Sentry/Gofer/cgroup enforcement
    -> unprivileged guest observation BEFORE fixture extraction
    -> offline fixed test/build command
    -> exact container removal + native termination check
    -> protected result receipt (expected failure is a separate test case)
    -> observe all three cases; image_approved remains FALSE
```

The proofs reuse the existing exclusive journal and bounded aggregate cgroup, not
a second capacity pool. Each sandbox has one CPU, 1-GiB memory/no swap, 64 tasks,
private IPC/cgroup namespaces, no network, read-only root, UID/GID 65532, no Linux
capabilities, no-new-privileges and only bounded `/workspace` and `/tmp` tmpfs.
Docker metadata alone is insufficient: native runtime enforcement and fixed guest
privilege observations are required before source extraction. The same execution
implementation is reused; proof-only Python hooks are not accepted in JSON frames.

Each case permits at most two attempts. A lost process/result cannot invent a
pass; an unfinished job or uncertain native cleanup blocks another case. Successful
observation replay does not execute the fixture again. A failed test is accepted as
the deliberate negative case only with exit 1, the reviewed failure marker and
evidence that the intended test command actually ran, not a preparation failure.
Cancelled/unloaded imports are refused before any systemd inspection.

This remains **local source/test evidence**. The VM is still on its eleven-file
custody release; public archives remain cancelled and retained. No live image load,
fixed-fixture execution, role approval, registry publication or budget mutation
has occurred. Verified publication is now implemented locally below; live daemon
capacity and failure-recovery checks are still required before the first real import.
Do not use these case receipts as generated-app validation
attestations or as authorization for deployment.

Final local verification: **1,114 coding tests passed** using only disposable
loopback PostgreSQL schemas, with no skipped cases. Flake8, targeted Bandit and
`git diff --check` passed. The existing Vertex SDK deprecation and pre-existing
unused Bandit suppressions remain warnings. These results are not trusted CI,
cloud runtime enforcement evidence or production enrollment.

### Verified two-file publication — local implementation, 7 October

`toolchain_publication.py` adds `publish_prepare` and an explicit `publish` operation.
Preparation verifies all three retained fixed-case receipts, actual immutable image
read-back and positive loader completion, complete archive custody, original policy
and registry bytes, clean execution state and the fixed host stop. It durably stores
one immutable, hash-bound plan per import. Preparing a plan does not write either
configuration file, clear maintenance or approve a role.

```text
proof_pending -> immutable plan -> explicit publish(plan SHA + reviewed source SHA)
      -> publishing (ordinary admission remains closed)
      -> exact registry replacement + fsync/read-back
      -> exact policy replacement + fsync/read-back
      -> deterministic receipt + enrolled
      -> ordinary admission verifies actual pair + receipt + publication chain
```

The same explicit operation recovers an interrupted publication. Each current file
must be either the plan's exact original bytes or exact candidate bytes. A partial
staging file must be an exact candidate prefix; foreign bytes remain for review.
The publisher never rolls back unrelated edits or chooses another plan. Failure
after one replacement, after both replacements or before the final journal update
keeps the shared slot closed. Phase `enrolled` alone is insufficient: admission
checks protected current files, the immutable plan, deterministic receipt and an
unbroken hash chain when the second role is subsequently published. Source/binding
changes, expiry, incomplete evidence or ambiguous native cleanup remain barriers.

Registry v2 distinguishes `reviewed_operator_archive` from the existing repository
origin. The original Python archive record is preserved exactly. React/Spring
records retain import ID, index/platform/config identities, recipe/controller hashes,
three case receipt hashes, original configuration hashes, archive custody and stop.
There is no invented `repository_digest` for locally built images. A missing operator
image cannot trigger hidden reload during ordinary validation; explicit maintenance
reconciliation is required. Local role approval does not enroll central funded
capacity, sign a generated image, publish a core candidate or deploy an application.

Operator commands (the complete reviewed controller must first be installed):

```sh
python -m scripts.transfer_reviewed_toolchain \
  --operator-config .gca-local/validation-transport.json \
  --operation publish_prepare --intent-id "$VERIFIED_IMPORT_ID"

# Review and reuse the exact returned plan hash; do not invent one.
python -m scripts.transfer_reviewed_toolchain \
  --operator-config .gca-local/validation-transport.json \
  --operation publish --intent-id "$VERIFIED_IMPORT_ID" \
  --plan-sha256 "$REVIEWED_PUBLICATION_PLAN_SHA256"
```

Evidence: 64 local publication tests cover preserved Python provenance, bounded
immutable plans, refusal of missing/changed proofs, missing-image fail-closed behavior,
prefix staging, receipt/configuration drift, two-role chains and real `os._exit`
after reservation, either rename and completion. Systemd/Docker/native operations
are mocked; this is not live gVisor proof. The complete local coding suite passes
**1,187 tests** after the final installer-reader cases, with disposable loopback
PostgreSQL and no skips; lint/security/whitespace
checks pass with existing warnings. The VM remains on its eleven-file custody release;
both imports are cancelled. Live rollout, complete host proof and generated-app
validation/deployment/Chat proof remain open. Before loading, also review the recovery
route for a completed image whose fixed host proof cannot pass; uncertain daemon
operations must never be released merely to unblock progress.

### Explicit failed-proof recovery (local controller, 7–8 October)

`reject` is an operator-only terminal decision for an unpublished `loaded` or
`proof_pending` import. It requires the positive completed daemon receipt, current
immutable image readback, unchanged original configuration, reverified archive and
identity-safe cleanup of every fixture job/native sandbox. A failed or absent test
result is retained as such, never converted to a pass. The image and archive remain;
no profile, runtime base, financial capacity or application is approved.

```text
loaded / proof_pending -> bounded rejection receipt committed -> rejected
                                 crash: same decision resumes safely
load_started / load_uncertain -> reconciliation only, NOT rejection
publishing -> exact forward publication only, NOT rejection/rollback
```

Admission and installer mutation independently verify the rejection receipt hash,
terminal journal transition and unchanged retained proof/job snapshot. Merely
writing `rejected` cannot free the slot. Later separately approved configuration
updates do not invalidate historical rejection; they retain their own publication
gate. No bytes are deleted, and no uncertain Docker action is replayed.

New reservations use import intent v2, including the reviewed controller bundle
hash. Historical v1 identities remain readable. A different reviewed controller
therefore gets a different intent rather than resurrecting a rejected transaction.
The existing eight-intent/sixteen-observation limits and fixed STOP still apply.
`reuse_custody` accepts only destination/source intent IDs, verifies the exact
terminal source and matching role/image/archive/recipe/host/STOP, and copies root-
protected bytes through normal destination quota, fsync and hash readback. It never
hard-links, uploads, loads an image or discards the old evidence.

```sh
python -m scripts.transfer_reviewed_toolchain \
  --operator-config .gca-local/validation-transport.json \
  --operation reject --intent-id "$COMPLETED_UNPUBLISHED_IMPORT_ID"

# First prepare the fresh source-bound destination using current configuration.
python -m scripts.transfer_reviewed_toolchain \
  --operator-config .gca-local/validation-transport.json \
  --operation reuse_custody --intent-id "$FRESH_IMPORT_ID" \
  --source-intent-id "$RETAINED_TERMINAL_IMPORT_ID"
```

These commands require the reviewed fourteen-file controller to be installed;
they are not a claim of live load, proof, publication or deployment completion.
Final local verification for this recovery checkpoint: **1,254 coding tests passed**
against disposable loopback PostgreSQL schemas, with no skips and the existing
Vertex SDK deprecation warning. Flake8, Bandit and whitespace checks passed;
Bandit reported only unused suppression warnings. The 255 targeted recovery,
staging, gateway, installer and transport tests also passed. Real process death
is covered, while Docker/systemd/native operations in these tests remain mocked.

Completed enrolled history is also checked by the bundle installer through a protected
current-source reader and read-only SQLite under its already-owned flock. During an
interrupted directory swap it may use only the exact recorded predecessor reader.
It does not open another Journal or clear a phase. Invalid or unreadable publication
history stays pending; rollback after retained imports remains prohibited. A fresh
read-only VM probe still reports eleven source files and snapshot
`c39a8d73ee88a66e096cee7f42497f056d9e6e2744f752f1b8260ad9439a449d`;
the new fourteen-file controller has not been installed in that observation.

```text
cancelled verified custody -> re-read archive/receipt/history + current config
                                      |
                   exact same-custody reopen (no new allocation)
                                      |
          fsynced fixed dispatch -> unique retained systemd unit
                                      |
           worker boot/PID/start-time + unit invocation + limit read-back
                                      |
                 fsynced claim -> SQLite load_started -> Docker load
                                      |
                +---------------------+--------------------+
                |                                          |
         no normal daemon ack                      normal daemon ack
                |                                          |
         load_uncertain                     exact immutable image read-back
          remains reserved                       |                 |
                                           verified           unavailable
                                              |                 |
                          terminal receipt -> loaded     ack true, readback false
                                              |                 |
                       host-profile proof     +---- observe only --+
                       publication/approval [local controller above; live pending]
```

The worker runs separately from SSH, with a retained unique unit, a 300-second
lifetime, one CPU, 1-GiB memory/no swap and 32 tasks. Its environment is explicitly
emptied before Python. The protected keyless worker reads back effective unit
memory/swap/task/privilege/filesystem limits before Docker access and records them
in its claim. No generated command/path/model/credential is accepted. These limits
bound the loader client, **not the separate Docker daemon**; live daemon import
capacity/disk checks and all profile enforcement proofs remain separate obligations.

Normal Docker completion and image verification are separate facts. A timeout,
nonzero exit or missing terminal receipt cannot be resolved merely because an image
exists or the unit is absent. A protected normal-completion receipt plus exact
current projected image metadata can repair a lost final journal write without a
second load. Claims bind boot ID, PID/start ticks, unit invocation, dispatch/custody
hashes and journal history. Partial claim files also prohibit redispatch. A loaded
image remains reserved and unapproved until the verified publication controller above
has completed the exact pair and receipt. This paragraph describes the earlier loader checkpoint.

Real `os._exit` tests cover death immediately before the mocked RPC and after the
terminal receipt but before the final SQLite update. Other tests reject changed
custody/config, wrong worker/unit lineage, relaxed effective limits, uncertain load
cancellation/replay and unsafe decoding. Systemd/Docker are mocked in these tests;
neither role's fake OCI layer executes. A read-only VM observation verified the
actual seven-property `not-found` unit projection, not a running loader.

Verification: the complete coding suite passed **1,030 tests** with local PostgreSQL
and no skips; the final focused lifecycle/custody/installer suite passed **223** again
after the optional-key template correction. That final projection was verified against
both real public Docker Desktop images. Full suite results are local, not trusted CI
or live Linux/gVisor enrollment. Flake8/Bandit/whitespace checks passed; only the
existing Vertex dependency deprecation and unused Bandit suppressions remain.

The existing `isolated_validation.Journal` now reserves toolchain maintenance under
the **same flock and SQLite database** as validation and packaging. This prevents a
lost SSH/controller process from releasing host capacity merely by dying. The
reservation records a canonical intent fingerprint: role, VM instance ID, immutable
image ID, archive byte count/hash, reviewed recipe hash, prior policy/registry hashes,
and the fixed host stop. There are no caller-selected paths, commands or credentials.

```text
prepared -> archive_verified -> load_started -> loaded -> proof_pending
   |              |                 |
   +-> cancelled  +-> cancelled     +-> load_uncertain -> loaded
                                                    |
                                  validation/packaging remain blocked

proof_pending -> publishing -> enrolled [new local controller; live pending]
```

Cancellation is permitted only before a load starts. Timeout/expiry never clears
uncertain load state; an exact same-intent retry returns the existing reservation,
not permission to invoke Docker again. Observations use compare-and-set phases and
bounded SHA-256 evidence references. These references record custody, **not** proof
that a trusted verifier ran. There are at most eight retained import intents and
sixteen observations per intent, with no automatic pruning.

The bundle operator can inspect pending maintenance but cannot apply, retire or
roll back a bundle through it. Even cancelled import history prevents rollback to
an older launcher without a forward-compatible review. Existing job cleanup is
still legal; maintenance does not acquire a second independent capacity pool.

Actual `os._exit` tests cover abrupt controller death at four phases. Additional
tests cover shared ownership, duplicate observations, intent corruption, both job
purposes, fixed-stop expiry, cancellation restrictions and retention exhaustion.
At the original ledger checkpoint this was **local source/test evidence only**. It does not upload or load an
image, grant role approval, publish a registry, reserve money or dispatch production
work. The reviewed custody-only controller below can reserve, transfer, reconcile
and explicitly cancel a never-started load, retaining its archive and audit history.
Do not advance a live reservation to loading until the protected load/reconciliation,
four-profile host proof and verified publication controller are complete. There is
no implicit release-from-proof-pending API; the explicit verified publisher above is
the new local completion route.

## Fixed-target operator custody transport — 6 October

`operator_toolchain_host.py` is an operator-only Linux/root gateway, not a model
tool. Its complete eleven-file protected source closure is hashed before and after
acquiring the existing journal slot. It verifies only two public GCE metadata
identity fields against the original project/VM, requires fresh policy/registry
hashes, and preserves the fixed 19 October stop. Its request has a 4-KiB framed
control limit and an independent 300-second alarm; the client has a 240-second
watchdog. No frame accepts a command, target, path, credential or deadline extension.

```text
default read-only probe -> exact catalog + fresh configuration -> prepare
                                      |
local immutable Docker-save stream -> bounded root custody + fsync/read-back
                                      |
receive same intent without a stream -> verify retained custody
                                      |
explicit pre-load cancellation -> retain bytes/audit, release maintenance slot

load / approval / registry publication / production dispatch: not implemented here
```

`scripts/transfer_reviewed_toolchain.py` streams the already-reviewed React/Spring
image directly over the existing pinned IAP transport, without a large local tar.
Client loss is not permission for a blind retry. An existing complete archive can
be reverified without another export. Partial corruption needs explicit hash/count
quarantine; cancelled intents are not silently reopened. Policy and the existing
Python registry remain unchanged. This is an intermediate prerequisite, not the
complete toolchain enrollment or generated-application proof.

The first read-only live probe exposed a migration bug: the new bundle inventory
was rejecting the saved exact seven-file transaction. The controller now accepts
only the historical seven-file or current eleven-file transaction, validates each
tree's hash/mode schema, and rederives its identity. Tests reject partial/foreign
inventories, malformed hashes/modes and changed IDs; exact legacy probe/upgrade
is covered. This does not weaken the new eleven-file installation requirement.

### Actual public archive custody proof on the existing VM

The eleven-file gateway was installed as
`529bc18bc56ed8f7b2a9c63aeb15cabc12d5ec10af7ee326605f9d27ec0bbe4e`,
independently read-back snapshot
`c39a8d73ee88a66e096cee7f42497f056d9e6e2744f752f1b8260ad9439a449d`.
967 local coding tests passed without skips, and 83 final focused controller/
transport/bundle tests passed again. The actual nonsensitive archives transferred
directly from local Docker Desktop to the original VM, without a Mac tar file.

| Role | Reserved intent | Bytes | SHA-256 |
| --- | --- | ---: | --- |
| React | f26bc68ec963d8c2e347858554d8e7a9e61ece066d68e9f95c0c32b7543c7b4a | 108,749,312 | bbe2dde5593ef0fff2d589f8fe9de801aa02d601ddc66992d3cf2a3550eeed07 |
| Spring | a7e261be57ffdbc6b8373778bc261ef6df8968677e6d55fb5c6f4f9292418b44 | 509,887,488 | 14f01f1dfcbe010f885012f01b56c583552935304310c9a31a35928462706595 |

Each initial transfer verified the full OCI graph, hash/count and persisted bytes;
a separate receive with **no exporter/archive stream** re-read the retained file
and returned the identical custody receipt. The index/platform/config namespaces
remain distinct. The first React attempt stopped before SSH/export because local
Docker Desktop was stopped; the application was started, the same prepared intent
was observed, and the later measured transfer used that intent, not a duplicate.

No Docker load was requested and no image was approved. Pre-load cancellation
releases maintenance only, retaining both verified archives/audit. These bytes are
not a role enrollment or permission to reopen/reload. A future reviewed durable
load/proof/publication controller must reconcile retained custody, execute the four
real host profiles plus negative cases, and preserve the existing Python registry.
No private repository, provider key, paid model call, new VM, financial-pool
activation, runtime approval, core deployment or generated-app/Chat proof occurred.

Verification at this checkpoint: 869 coding tests passed against the verified
loopback PostgreSQL database without skips. After the final prepared-intent
fingerprint hardening, the 89 focused import/journal/bundle tests passed again.
Targeted Flake8, Bandit and `git diff --check` passed. Bandit reported only existing
unused suppression warnings; the user-owned Vertex test retains the google-genai
deprecation warning. No trusted CI, live import or full-project acceptance proof is
implied by these subsystem results.

## Protected archive staging — local implementation, 6 October

`app/coding/toolchain_staging.py` consumes only a trusted operator stream under the
existing host journal's flock. It derives the private operation directory from the
canonical intent ID; no request supplies a filesystem path or shell command.
Symlinks, hard links, special files, untracked objects, unsafe ownership and
nonprivate custody files are rejected. Transfers enforce the reserved archive
length before writing, the 512 MiB inventory ceiling, bounded control JSON and
64 MiB of free-disk headroom beyond the proposed archive. These are storage
controls, not financial reservations or sandbox-execution proof.

```text
durable prepared intent
        |
stream -> archive.partial -> complete OCI/hash inspection -> fsync data
        |                                    |
        |                            re-read persisted bytes
        |                                    |
        |                         rename archive.tar + fsync directory
        |                                    |
        |                         custody receipt + fsync publication
        |                                    |
        |                         journal archive_verified [NO approval]
        |
incomplete/failed bytes
        |
explicit expected SHA + length -> durable quarantine intent
        |
rename to hash-named quarantine -> verify -> committed audit
        |
new transfer only within retained-byte/count bounds
```

An already-complete partial or final archive is verified again without re-uploading.
A staged filename or old receipt is never sufficient evidence. A complete recovered
file is fsynced before custody publication, because a dead writer may have left
complete bytes only in the kernel cache. Torn control writes can be repaired only
when their bytes are an exact prefix of the independently reconstructed receipt.
Foreign prefixes fail closed and are never truncated.

An incomplete transfer is not automatically deleted. Explicit quarantine preserves
its exact bytes and uses a write-ahead intent so process death after rename is
reconciled by the same expected hash/length. At most three quarantines are retained,
within the aggregate byte bound; exhaustion requires review, not pruning. Pending
quarantine state blocks a new transfer. Expiry prevents a fresh upload but does not
prevent custody reconciliation of existing bytes. Callers must terminate any exporter
they started when recovery consumes no stream input.

Deadlines are cooperative checks between reads, not a guarantee that arbitrary
blocking readers can be interrupted. The future pinned-IAP transfer controller must
provide a separate watchdog and owned-process cleanup. This library invokes no Docker,
network, model, unpacking, policy publication or approval operation. It is **not yet
installed or connected to the VM transfer controller**. The seven-file host bundle
has not silently been expanded to include this library's dependency closure.

The broad coding suite passed 916 tests against loopback PostgreSQL without skips.
Five more fault-test cases were then added; all 52 final staging tests passed,
including real subprocess death around archive promotion, receipt/journal publication,
quarantine intent/rename and a torn control write. Both roles are covered by tiny
synthetic OCI archives; these bytes do not prove a real image can run. Flake8, Bandit
and diff checks passed. No actual toolchain archive was uploaded or loaded.

### Separate live journal-barrier rollout

The preceding journal change was installed as the reviewed seven-file bundle:

- Installation: `a457b819dec51ee6d83f7c70f3d86908e60b38a0e4926dad3d698f2d0a74b241`.
- Snapshot: `29bba63ff5d024d2397c62225b5e60b19f154dcbbf4055b591ee835dff1d9e03`.
- Launcher: `89d379a8d221df0a54bd99a1b5477a8378e5dc8c348b6a31efa292e2a4bc46af`.

The other six source hashes are unchanged. Fresh probe/read-back and isolated import
passed. Only the audited obsolete `3c8d9aa3ab350577044ccc8abd7dac7a94d8930446b4b353e33f85b691240080`
code copy was retired; data/journals/artifacts and the latest predecessor remain.
The eight-entry limit is unchanged. The live bound synthetic Python probe passed,
including mismatch, unbound/replay rejection and observed cleanup without inventing
test success from a lost result. Independent projected observations verified six
healthy Coolify containers, zero scoped sandbox leftovers and an idle main journal.
No import intent was created. Production policy, services, budgets and models were
not changed. The read-only Docker projection showed `overlayfs` with
`io.containerd.snapshotter.v1`, and 94,249,951,232 free bytes under `/var/lib`;
that is storage evidence, not successful image load or role approval.

## Fixed catalogs

| Role | Reviewed dependencies |
| --- | --- |
| React | Node 24 AMD64 base; React/react-dom 19.3.0; Vite 8.3.2; Vitest 5.0.3; plugin-react 6.1.2 |
| Spring | Maven/JDK 17 AMD64 base; Spring Boot 4.1.1; MVC/security/JPA; PostgreSQL driver; H2/test/security-test; dependency plugin 3.11.0 |

The complete React transitive lock is checked in. npm acquisition and offline
installation both disable lifecycle scripts. Native packages must already work from
the cache; enabling hooks inside validation is not a fallback.

Maven `go-offline` alone did not fetch the dynamically selected
`surefire-junit-platform:3.5.6` provider. Acquisition now also tests/packages a fixed
public BCrypt/JUnit fixture and Boot main class. Seed source and build outputs are
removed from the final image. The context allows those two exact Java files, not every
file in the seed directory. Tests assert equality with the separate public validation
fixtures. There is no online fallback during generated-code validation.
See [npm ci](https://docs.npmjs.com/cli/commands/npm-ci/) and
[Maven go-offline](https://maven.apache.org/plugins/maven-dependency-plugin/go-offline-mojo).

## Local operator commands

Run from the repository root with Docker Desktop running. AMD64 emulation on this
Mac matches the target instruction set, but is not Linux/gVisor enforcement proof.

```sh
docker build --platform linux/amd64 --tag gca-reviewed-react:offline-v1 \
  --label org.gca.toolchain.operator-only=true toolchains/react
docker build --platform linux/amd64 --tag gca-reviewed-spring:offline-v1 \
  --label org.gca.toolchain.operator-only=true toolchains/spring

# Default: local Unix Docker target and image metadata inspection only.
.venv/bin/python -m scripts.probe_local_offline_toolchains --role react
.venv/bin/python -m scripts.probe_local_offline_toolchains --role spring

# Execute only the checked-in nonsensitive fixture.
.venv/bin/python -m scripts.probe_local_offline_toolchains --role react --run
.venv/bin/python -m scripts.probe_local_offline_toolchains --role react --run --negative
.venv/bin/python -m scripts.probe_local_offline_toolchains --role spring --run
.venv/bin/python -m scripts.probe_local_offline_toolchains --role spring --run --negative
```

The probe resolves the local tag to an immutable image ID before creation. It rejects
remote Docker contexts, unexpected platform/user, volumes, entrypoints, healthchecks
and role/base labels. Workloads have network none, read-only root, UID/GID 65532,
zero capabilities, no-new-privileges, one CPU, 1 GiB/no extra swap, 128 PIDs and a
finite lifetime. Cleanup checks the exact name/image/fixture hash and deletes only
that container. Unknown identities are not removed. A lost observation cannot prove
cleanup; this fixed local helper does not replace the production host journal.

Actual tests exposed Docker's implicit tmpfs noexec: Vitest could not execute its
workspace binary, and Jansi could not load a transient native library from `/tmp`.
The launcher source now permits execution on only the bounded sandbox-internal
workspace and tmp mounts, retaining nosuid/nodev and all other restrictions.
Host-backed packaging storage remains noexec. The reviewed source revision was
subsequently installed/read-back verified on the existing VM on 6 October; see the
separate host evidence below. Never substitute native host execution to work around
a toolchain failure.

## Measured local evidence — 6 October 2026

| Role | Docker Desktop image ID |
| --- | --- |
| React | `sha256:d3d2984e4d4b8e90a5b93b0f3a89591c8958fd42a8d8f68de4ac5eafaeeba9de` |
| Spring | `sha256:1f90c0fe3495fc3cc00f8b3a9cc6681782238fa1f1dd8a57c8583bac31c68943` |

React passed two Vitest tests and Vite build with network none, checking nonempty
dist/index.html. Spring passed offline test/package, produced a nonempty Boot JAR,
and its bounded Surefire XML proved an executed passing test. Both deliberately
failing mutations returned nonzero with the exact fixed assertion marker. Independent
cleanup found no remaining gca-local-toolchain containers. These are real executions,
not simulated test results. Source fingerprints:

- React positive: `16b1eca2c9b57ecafda6f4bc33cc9b6eab48e1bf055b34c851dbb90d912f21f0`.
- React negative: `1ae8ec620c9b3e98844f7c705701f9335e97271341102f7e24a05ac2b69edd57`.
- Spring positive: `6f6d5541ac1409745589933173b1b890b648b916544f312fb99c979c90aa45a8`.
- Spring negative: `b65e64b88b33a497766387d5a20c8a83bd6ac2a95e640a5ae95dde0fcb3e946f`.

Verification: 804 coding tests passed against real loopback PostgreSQL, no skips;
focused Flake8 and Bandit passed (Bandit reported existing unused nosec comments).
One google-genai dependency deprecation warning remains in the user-owned Vertex test.
This covers the coding subsystem, not the complete original project acceptance audit.

There were zero model calls, private-source inputs or new cloud resources. Nothing
was deployed or approved for production. Do not interchange image index, platform
manifest and config digests; read back each host's actual identity after archive load.
The seven-file host bundle was independently rehashed after installing this launcher
correction: installation `fe1010fdde74f46221cd937f56b3f084bee4206978f6d294c6bf5c452df8e2bc`,
snapshot `835b2bd12a13d4249bb210c159ee5ddf441b3f60c34654e4059dcef77083ee2f`.
The bound synthetic Python test passed, rejecting changed source, unbound dispatch
and replay; cleanup passed and lost result was not inferred as test success. The
final independent projected health check verified all six Coolify containers healthy,
an idle journal and zero scoped sandbox containers. The initial broad health query
did not establish health; its outcome was not used as positive evidence. Only an
explicitly inspected obsolete code rollback copy was retired;
journals/artifacts/data and the latest predecessor were preserved.

Current VM policy still approves only the Python synthetic profile. Protected export,
host enrollment, gVisor proof, real Nginx/Java runtime bases, central financial
admission, registry/signing, app behavior and final Chat delivery remain open.
See [isolated validation](ISOLATED_VALIDATION.md),
[host bundle operations](HOST_BUNDLE_OPERATIONS.md) and [OCI packaging](OCI_PACKAGING.md).

## Streaming custody inspection — 6 October 2026

The Mac's free space fell below the combined archive size. The fixed operator
scanner avoids saving a large archive file locally:

```sh
.venv/bin/python -m scripts.probe_toolchain_archive --role react
.venv/bin/python -m scripts.probe_toolchain_archive --role spring
```

It uses the same clean-environment local Unix Docker context as the fixture probe,
resolves the role tag to its immutable ID, then exports **by that ID**, without
tags. It never calls image load, extraction, execution, cloud transport or approval.
Docker's daemon may still use its own working storage; this is not a zero-disk-I/O
claim. The source-only scanner is not installed on the VM or exposed to models.

The archive inspector validates classic tar framing before tarfile can allocate
PAX/GNU extended-header bodies. It rejects links/devices/extensions, traversal,
duplicates, unknown files, extra blobs, malformed descriptors, tag overrides,
privileged/runtime-role mismatch and nonzero trailing data. It hashes every blob,
requires full reachable graph equality, distinguishes index/platform/config digests
and hashes the complete stream including padding. Limits: 512 MiB input, 128 entries,
16 MiB retained small metadata and 1 MiB read chunks. The library deadline is checked
between reads; the dedicated Unix CLI adds a 150-second blocking-read watchdog and
reaps only its own exporter process group. Transport still needs its own deadline.

| Role | Observed archive bytes | SHA-256 |
| --- | ---: | --- |
| React | 108,749,312 | `bbe2dde5593ef0fff2d589f8fe9de801aa02d601ddc66992d3cf2a3550eeed07` |
| Spring | 509,887,488 | `14f01f1dfcbe010f885012f01b56c583552935304310c9a31a35928462706595` |

Repeated scans of each current image produced the same exact archive digest; this
is observed stability for these exports, not a guarantee for every Docker release.
Node platform/config digests are `3ea6582e...` / `21dc647e...`; Spring's are
`b0eb5e11...` / `3269eb8c...`. They differ from the image index IDs listed above.
Full receipts are emitted by the command. None is interchangeable with the archive
byte hash or sufficient by itself to authorize a host policy change.

These are custody checks, not independent build provenance, approved base origin,
live gVisor execution, funded dispatch or deployment proof. The CLI's local tag and
labels do not grant production authority. The eventual operator enrollment must
bind reviewed recipe/provenance and exact immutable receipt, import through protected
state with crash recovery, read back the loaded identity, and complete real host
positive/negative tests before changing policy. Those stages remain open. Low local
disk space and current October billing reconciliation still need resolution.

Regression evidence for this source checkpoint: 834 coding tests passed against
real loopback PostgreSQL, without skips, before the final two end-marker cases were
added. Those cases and the final focused archive/probe suite were separately run.
Focused Flake8/Bandit and diff checks passed. This is not trusted CI or production
execution evidence; the single existing google-genai deprecation warning remains.
