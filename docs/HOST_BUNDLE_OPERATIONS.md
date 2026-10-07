# Protected host bundle installation, forward recovery and rollback

This is an **operator** workflow, never a model-selected command or generated-app
tool. It updates reviewed stdlib files on the existing VM through pinned IAP/SSH.
It does not approve toolchain/runtime images, change policy or sudoers, prepare
resource groups, enroll financial capacity, run generated code or deploy apps.

## Installation protocol

The subsequent **local** loader/proof source protocol accepts thirteen exact files.
Historical completed twelve-file loader transactions remain readable, but do not
authorize installing a partial new source closure. The live VM remains on the
eleven-file custody release documented below; no load unit
was dispatched. The new installer preserves the 256,000-byte wire cap. Large apply
frames use a version-2 zlib/base64 envelope with declared count/SHA-256 and a separate
384,000-byte decoded ceiling. Decompression is limited before allocation; trailing
streams, incomplete streams, wrong sizes/hashes, duplicate keys and compressed
operations other than apply/exact forward resume are rejected. Historical exact seven/eleven/twelve-file transactions
remain readable; a new install requires the exact current thirteen-file inventory.
No missing helper is ignored or installed outside the reviewed complete transaction.

```text
read-only probe: exact old-tree hash + idle host journal
        |
review the exact current source closure; compile and test locally
        |
apply with observed old-tree hash
        |
same exclusive host slot + no unresolved jobs
        |
durable external intent -> staged complete bundle -> isolated import
        |
rename old root to protected rollback directory
        |                 crash here: gateway absent, fail closed
rename staged root to fixed gateway directory
        |
rehash + isolated import -> committed intent
        |
independent read-back + synthetic validation/replay/cleanup + health check
```

The two directory renames are **not** one atomic transaction. Each rename and the
intent are fsynced separately. If the process dies, a subsequent probe reports the
interrupted installation ID even when the gateway path is absent. Rollback checks
actual directory hashes, not just the last recorded phase. It never deletes the
host job journal, retained validation artifacts or data. The complete old bundle
is preserved; old bytecode caches are not copied into the new bundle.

Fixed defaults are `/usr/local/libexec/gca`, protected rollback storage at
`/usr/local/libexec/gca-bundle-releases`, and the existing state directory from
`/etc/gca-validation/policy.json`. Release count and payload sizes are bounded.
Unknown files, symlinks, hard links, unsafe ownership, changed hashes and occupied
rollback destinations fail closed. Retention exhaustion needs operator review;
this workflow does not silently prune old releases.

## Commands

The current eleven-file installation is
`529bc18bc56ed8f7b2a9c63aeb15cabc12d5ec10af7ee326605f9d27ec0bbe4e`;
independently read-back snapshot:
`c39a8d73ee88a66e096cee7f42497f056d9e6e2744f752f1b8260ad9439a449d`.
It adds the reviewed catalog, archive inspector, staging and fixed operator gateway.
The installed gateway's default probe verified the original public metadata identity,
zero imports and unchanged policy/registry; the bound Python/replay/cleanup regression
passed. This does not approve Node/Java roles or execute an imported image.

The eleven-file source passed 967 coding tests with verified loopback PostgreSQL
and no skips, plus Flake8, Bandit and whitespace checks. Bandit only reported unused
suppression warnings; the existing user-owned Vertex test retains its dependency
deprecation warning. These are local tests, not trusted CI or full-goal acceptance.

Use the existing private operator configuration; never paste its keys into chat.
From the project root:

```sh
source .venv/bin/activate
python -m scripts.install_packaging_bundle \
  --operator-config .gca-local/validation-transport.json
```

This defaults to read-only inspection. Review its `snapshot_hash` and the exact
source/tests. Only then install:

```sh
python -m scripts.install_packaging_bundle \
  --operator-config .gca-local/validation-transport.json \
  --apply --expected-snapshot-hash "$OBSERVED_SNAPSHOT_HASH"
```

Only the exact thirteen explicit known filenames are accepted by the new source for
a new installation. Saved transactions accept the exact historical seven/eleven/twelve-file
inventory or the current inventory, with typed hash/mode evidence and a rederived ID.
Arbitrary partial inventories are not compatible transactions. A lost response does **not**
authorize blind retry. Probe again; if it reports `interrupted=true`, inspect its
exact installation ID, phase, `old_snapshot_hash`, and `candidate_snapshot_hash`.

### Exact forward recovery (local implementation, not yet a live rollout)

The current installer has an explicit `resume_forward` operation for completing the
**same** recorded current candidate. It does not restore an older launcher and
does not create a different release. This addresses the recovery gap where rollback
is prohibited because import/packaging history must remain readable.

```sh
python -m scripts.install_packaging_bundle \
  --operator-config .gca-local/validation-transport.json \
  --resume-forward "$OBSERVED_INSTALLATION_ID" \
  --expected-old-snapshot-hash "$OBSERVED_OLD_SNAPSHOT_HASH"
```

Use the exact source revision of the interrupted candidate. The client sends the
whole candidate; the host compiles and hashes it, requires equality with the recorded
new-tree inventory, and rechecks actual old/staged/current trees under the same host
flock. A partial stage is completed only before the original root has been saved and
only when every existing file has its expected hash/mode. After the old root is saved,
a complete verified staging tree must already exist. A published candidate is
rehashed and imported again rather than swapped a second time.

```text
recorded candidate + exact source + original hash + idle host slot
    -> inspect actual trees (phase can lag one rename)
       -> old root: finish exact staged prefix -> import -> save predecessor
       -> root absent: require exact predecessor + complete stage
       -> new root: require exact predecessor + no occupied stage
    -> publish/reverify/import -> committed
```

Pending/uncertain imports, unresolved jobs, changed source, symlinks/hard links,
foreign staged data, missing predecessor/stage, contradictory phases, unfinished
retirement and rolled-back transactions are rejected. Cancelled import history and
cleaned packaging history remain intact; neither the SQLite journal nor protected
policy is changed. Recovery may temporarily observe the existing staging tree, not
raise the eight-entry release-retention allowance or allocate a new release. Torn
temporary control files are unrecognized data requiring operator review; this path
does not silently remove them. Real child-process `os._exit` tests exercise both
directory-renaming crash boundaries with forward recovery and retained history.
These tests are local filesystem/SQLite evidence, not live VM or power-loss proof.

Verification checkpoint (7 October 2026): 1,050 coding tests passed with verified
loopback PostgreSQL and no skips; 257 focused host-bundle/import/load/journal tests
passed. Flake8, targeted Bandit and whitespace checks passed. The sole full-suite
warning remains the existing user-owned Vertex dependency deprecation. A fresh
read-only IAP probe still reports installation `529bc18...bbe4e`, snapshot
`c39a8d...449d`, an idle journal, no pending imports/retirements, and eight retained
release entries. No new bundle, image load, role approval or policy edit was applied
to the VM. The active predecessor remains protected; retention must be reviewed
before a future new installation.

### Historical rollback (only when journal/history permits)

For a recorded candidate without retained import/packaging history, the older exact
rollback operation remains available:

```sh
python -m scripts.install_packaging_bundle \
  --operator-config .gca-local/validation-transport.json \
  --rollback "$OBSERVED_INSTALLATION_ID"
```

Rollback is refused while any job is unresolved or when packaging history/staging
exists. Restoring an old bundle after packaging could remove cleanup capability.
That state requires exact forward recovery for an interrupted current candidate,
or a separately reviewed new forward-compatible repair. Never delete records or
use a bypass flag. A committed update is not permission to revert to its old code.

The 6 October journal-barrier rollout is separately read-back verified as installation
`a457b819dec51ee6d83f7c70f3d86908e60b38a0e4926dad3d698f2d0a74b241`, snapshot
`29bba63ff5d024d2397c62225b5e60b19f154dcbbf4055b591ee835dff1d9e03`.
Only `isolated_validation.py` changed among the seven installed files. A pending
toolchain-import reservation now blocks validation/packaging and bundle mutations
but remains read-only probeable. Old-launcher rollback after import history requires
a forward-compatible review. At that checkpoint the archive-staging library was local only, not
an untracked extra file in the installed bundle. See `OFFLINE_TOOLCHAINS.md` for the
source hashes, test/live evidence and unfinished transfer/publication controller.

## Explicit retirement of obsolete rollback copies

The eight-entry release limit is unchanged. It includes `transaction.json`;
installation never silently prunes. A probe now returns `retention_inventory` and
its `retention_hash`. Only an explicitly reviewed obsolete `<id>.previous` tree
may be retired, and never the latest installation's predecessor.

```sh
python -m scripts.install_packaging_bundle \
  --operator-config .gca-local/validation-transport.json
# Review both fingerprints, the obsolete tree and the preserved latest predecessor.
python -m scripts.install_packaging_bundle \
  --operator-config .gca-local/validation-transport.json \
  --retire "$REVIEWED_OBSOLETE_INSTALLATION_ID" \
  --expected-snapshot-hash "$FRESH_ACTIVE_SNAPSHOT" \
  --expected-retention-hash "$FRESH_RETENTION_SNAPSHOT"
```

The protected `bundle-retirements.json` audit records the exact filenames, modes
and content hashes before deletion. Files and directory changes are fsynced;
process death leaves a prepared intent. Probe and reconcile that same identity
using fresh fingerprints; do not install or roll back over a pending retirement.
Unknown/reappeared objects fail closed. Active source, its latest predecessor,
job journals, artifacts and deployment data are not retirement targets. Local
tests include abrupt process death immediately after a real unlink.

## Current live evidence — 6 October 2026

Source checkpoint `3ee954d` corrects only validation's disposable internal tmpfs
execution flags; host-backed packaging storage remains noexec. The full seven-file
bundle was installed through the existing protected transaction and independently
read back. Latest installation:
`fe1010fdde74f46221cd937f56b3f084bee4206978f6d294c6bf5c452df8e2bc`.
Snapshot: `835b2bd12a13d4249bb210c159ee5ddf441b3f60c34654e4059dcef77083ee2f`.
Launcher SHA-256: `59bf2018778577d9fbe23a35eaf8d95c2b1434fb22c268e068703f2debc58526`.
All six other file hashes remain those of the 5 October bundle below.

Retention was full. Obsolete code copy `22cc0233...` was explicitly reviewed and
retired with fresh active/retention hashes and prepared/committed audit. Independent
read-back verified active source unchanged before installation, no pending retirement
and preserved data/journals/artifacts. The current installation's predecessor is
preserved; the eight-entry limit remains unchanged.

The bound nonsensitive Python compile passed after installation. Changed-source and
unbound dispatch were rejected before execution; replay was rejected afterward.
Cleanup observation passed and lost result was not inferred as a passing test.
The installer read-back showed an idle journal. The initial broad health query
failed to confirm health; a subsequent bounded field-projection check independently
proved all six exact Coolify names running/healthy, an idle journal and zero scoped
sandbox containers. The policy still approves only Python compilation. No health
claim is inferred merely from installation or from the synthetic test.

Separately, actual local AMD64 Docker React and Spring offline tests/build/package
and deliberate test failures passed. This is not Node/Java Linux/gVisor execution
proof or approval for private/generated code. No toolchain/runtime policy, financial
pool, schema, cloud resource, VM size/deadline or model route was changed. No model
was called. See [offline toolchains](OFFLINE_TOOLCHAINS.md) for exact local identities,
the reproduced cache/native execution defects and remaining deployment gates.

## Earlier live evidence — 5 October 2026

Latest independently read-back verified installation:
`c11d29d43f04bf580bfbfb458a6db4f10dcc9e41231f697893d3de688520403e`.
Snapshot: `54783b15e1516d27190346dcb363d59194d6b8f6d3ee7592bad485fce99a93d6`.
Worker SHA-256: `d093497377186121e6fca3ac9a4392b2c1c5d83cb245f7efc8479882a655b59f`.
The six other file hashes remain as in the 4 October evidence below. The current
predecessor is retained. Obsolete code copies `ec87d7d8...` and `ae6f134d...` were
explicitly inspected/retired; independent probes showed an unchanged active tree
and no pending retirement. No journal or artifact data was deleted by retirement.

A fixed nonsensitive pipeline first failed at `guest_network`: this pinned guest
omits `/sys/class/net`, while `/proc/net/dev` and socket interface enumeration both
report only loopback. The corrected guard requires these two matching observations;
missing sysfs is allowed only on the mandatory verified host-proof path. Native
workers still require sysfs. Any non-loopback, missing or conflicting enumeration
fails. Docker `NetworkMode=none`, native namespace/process identity, read-only root,
zero guest capabilities and the job-bound readiness barrier remain mandatory.
See [gVisor networking](https://gvisor.dev/docs/user_guide/networking/).

```sh
# Default is idle-state inspection only.
python -m scripts.probe_packaging_pipeline \
  --operator-config .gca-local/validation-transport.json
# Fixed synthetic source/base only; not a model tool or production dispatch.
python -m scripts.probe_packaging_pipeline \
  --operator-config .gca-local/validation-transport.json --run
```

The fresh live test passed native readiness, the actual worker barrier, OCI
assembly, durable output storage and independent host rehash. Archive size: 10,240
bytes; SHA-256 `591b47ec49f03d75726394e907eaa7538bfd20507a4869f949c43edf8ec0ad53`.
Exact worker/native cleanup and normal temporary ext4 volume removal passed.
Independent health: zero unresolved jobs, zero sandbox containers, zero packaging
staging entries; all six Coolify containers healthy.

This is **synthetic assembly proof**, not approved Nginx/Java runtime execution,
React/Spring compilation, authenticated production dispatch, signing/publication,
application deployment or final Chat delivery. The fake base contains no executable
application runtime. There were no model calls, new cloud resources, production
migrations, funding activation, VM sizing/deadline changes or budget expansion.
Production packaging remains inactive. Worker error output now includes only a
fixed stage and exception class, never the private error message or artifact body.

Verification: **766 coding tests passed** with real loopback PostgreSQL and no
skips. Targeted Flake8, Bandit and `git diff --check` passed. One dependency
deprecation warning remains in the user-owned Vertex-provider test. These results
cover this coding subsystem, not the entire original project completion audit.
The synthetic probe's fault tests also ensure an execution/observation exception
cleans its storage only after exact termination is confirmed; uncertain helpers
leave the cleanup obligation held.

## Earlier live evidence — 4 October 2026: native-profile checkpoint

Latest independently read-back verified installation:
`9bdec0e235d9cc16ef66795026da59ed674513e6cccbfcd14bdf7ef111f9748f`.
Snapshot: `da50f4cb709f2efaa395aa4a261ae54cf8632fc299ec034a261e13f914335e7d`.
All seven file hashes were checked; updated native-profile files are:

| File | Installed SHA-256 |
| --- | --- |
| isolated_packaging.py | 3a6b4bf85c43aeb3eb993c250bb9e00c3c9217ab29427481846015b82c47900c |
| image_packaging_worker.py | c4f3a440f24335d104273fc337252935355fbd64fd6d52e1babd1cb00e7b08e9 |
| isolated_validation.py | 8409c9ac73408d206999159ef8eef27adeda22301a0ed7ce25872512a3f1fe2c |

The signed runtime's pinned Sentry and Gofer profiles now cover their separate
executables and ancestry-verified Systrap clones. Native scope discovery includes
unknown occupants rather than filtering them out. A fixed nonsensitive diagnostic
identified an optional `runsc-fd-parking` process with broad capabilities and seccomp
0. Its presence caused rejection, not acceptance. The upstream pinned source permits
this optional optimization to be unavailable; the operator retained its exact bytes
and excluded execution by changing 0755 to 0644. Mandatory native/guest guards remain.

Parking binary SHA-256:
`03af90d07ea466c10ecc3e3ba839b2a59540959dd3286aea3886917cba850c11`.
Independent exclusion snapshot:
`835d7dd941b98357913dad6c8809fd3650dc46f58158c4e783e640815e0be806`.
The prepared/committed intent is protected at the existing journal state's
`optional-sidecar.json`. No binary was deleted, Docker restarted, default runtime
changed or cloud resource created. The readiness verifier independently requires
the pinned parking binary to remain 0644; restoring it blocks private readiness.

```sh
python -m scripts.prepare_packaging_runtime \
  --operator-config .gca-local/validation-transport.json
# Review exact package hashes and a fresh idle-state snapshot before changing mode.
python -m scripts.prepare_packaging_runtime \
  --operator-config .gca-local/validation-transport.json \
  --exclude --expected-snapshot-hash "$FRESH_RUNTIME_SNAPSHOT"
# Restoration is operator-only, requires a new snapshot and blocks readiness.
python -m scripts.prepare_packaging_runtime \
  --operator-config .gca-local/validation-transport.json \
  --restore --expected-snapshot-hash "$FRESH_RUNTIME_SNAPSHOT"
```

An uncertain response requires read-back before retry. A prepared transition must
reconcile toward its same target; no opposite blind operation is permitted.
No generated source, model key or private artifact is admitted by these commands.
The first post-exclusion nonsensitive native sleeper passed complete enforcement
and cleanup, with seven verified process identities. Full private artifact/barrier,
OCI packaging, Node/Java runtime approval, deployment behavior and Chat proof remain
open. Production packaging remains inactive. The final probe after installing the
mandatory exclusion check also passed enforcement and cleanup in one observation:
exact container `ef4dc965863a6b8a528a3b5e9332a17e447c0ed7bd89434e287437c678da535d`,
seven scope/helper identities, no missing member, production activation false.
Post-probe read-only health reported zero unresolved host jobs, zero validation
leftovers, and all six Coolify containers healthy.

Verification: all **731 coding tests** passed with real loopback PostgreSQL and no
skips. Operator transition tests use real file/SQLite transactions but simulated
root/kernel observations. Flake8 and Bandit passed. These are not universal sandbox
escape proofs. The following checkpoints are historical, not current failures.

## Earlier live evidence — 4 October 2026

The first installation below is historical. A subsequent independently verified
installation of native-readiness source commit `88e1ff7` now has installation ID
`ae6f134da749435bb178f4662ef6c309b484d1782aae69b0f311775b74fb8a52`
and snapshot
`40d602f17cd77e24d9e0f608f97387c0c68de920ffda757c15cc2b7f961cbcf1`.
The unchanged four support-file hashes remain as listed below. New installed hashes:

| Updated file | Installed SHA-256 |
| --- | --- |
| isolated_validation.py | 8409c9ac73408d206999159ef8eef27adeda22301a0ed7ce25872512a3f1fe2c |
| image_packaging_worker.py | 63348b5ba411601969becb5d4162d8d0967fdd1b5c2cbaec8b4cb2c818c560ea |
| isolated_packaging.py | ee42fc7880f4427d5b0cfab2c608550886545a06d11eb83bbbe485f7aa66f22c |

The fixed operator resource-group setup is now applied and independently read back:
`cpu.max=100000 100000`, `memory.max=3221225472`, `memory.swap.max=0`,
`pids.max=128`, with an empty controller leaf. Unit hash:
`8336d6b4413c4eb1ff2d833396475abd0123e60f1869e39e51f062c88b03901e`;
read-back snapshot:
`4a995a283e04bbd6ebcb92c75688c79aefd5144d6138b34ef8776e4dd934b3e0`.
The operator CLI is `scripts/prepare_packaging_group.py`: default inspection, apply
only with the fresh exact snapshot, occupied/foreign state refused, interrupted
unit setup inspected before a reviewed roll-forward. It uses the existing exclusive
host journal slot. It neither changes VM sizing/deadline nor approves private work.

A nonsensitive native sleeper then ran under the aggregate and cleaned up. Native
seccomp was 2 and no-new-privileges 1, but `CapEff=000000000008001f`, so the current
zero-native-capability verifier correctly **rejected this profile**. This is negative
readiness evidence, not a packaging success. The native runtime namespace/capability
contract still needs verification; do not replace the zero test with an unchecked
allowlist. All six Coolify containers stayed healthy, with zero unresolved host jobs
or validation leftovers after cleanup. Private packaging is not activated.
After this bundle update, the nonsensitive Python transport probe again passed
compilation, replay rejection and bound cleanup observation using the exact
approved local image ID. The initial CLI attempts with a relative operator path
and repository-qualified image reference were rejected; no unresolved host job
remained before the corrected fixed-ID probe. Do not treat those rejected attempts
as runtime success or retry an uncertain execution without journal inspection.

Operator commands (not model tools):

```sh
python -m scripts.prepare_packaging_group \
  --operator-config .gca-local/validation-transport.json
# Review snapshot and idle-journal/kernel state before a fixed apply.
python -m scripts.prepare_packaging_group \
  --operator-config .gca-local/validation-transport.json \
  --apply --expected-snapshot-hash "$FRESH_GROUP_SNAPSHOT"
python -m scripts.probe_packaging_native \
  --operator-config .gca-local/validation-transport.json
```

The native probe's negative readiness outcome was expected at that historical
checkpoint; successful execution/cleanup must not be mistaken for positive security
acceptance. No private input or mounted artifact is used by this fixed probe.

### Historical first seven-file installation

Installed on the existing `gca-coolify-delhi-01` VM without a new resource, policy
change or production admission activation. Installation ID:
`ec87d7d8e3db1fd9f51a912bd9e0a501e6a953cedfa2eef10c9cfc8a0a4a77d2`.
Independent read-back snapshot:
`b03668e4cbbd2fe9567f24349a0efc3ae84ff7540193a38b0a149c5c8b54145f`.

| Reviewed file | Installed SHA-256 |
| --- | --- |
| isolated_validation.py | 55e35cea3acdf85d4465835e19a39d06d33c839e3ef9461d66d62e357265b6e1 |
| build_artifact.py | 1a6e04a0e56f0893335bd9d6476b794e01cd4c563d9dd9e1fe7d4cf9136fdc0d |
| oci_packager.py | 8c92a48d70d038bede9c4c10d5bc3e15f2d7ff0381f412b620cb8462b57ce28c |
| runtime_image_store.py | 82bd1842abec3cdd715c176e3c63b0dbb7c7baf7d77871d3119e6df8f8186ec2 |
| image_output_store.py | b74cc87918b86ccd0a5e5ce6bd5d6d5d9fbe871e22f09b4e9e3e77032b2c2c98 |
| image_packaging_worker.py | 97762c16ba0350955ae87b36d05ad759029f4a1905f64d67afb65dd8afe2b206 |
| isolated_packaging.py | b82602a2ddad4ede6636c5af9744bbb563840ff9a3dd8cb6e862c5a1663f4631 |

Read-only GCE identity verified the original instance ID, e2-standard-4, RUNNING
status, no service account and unchanged STOP at `2026-10-19T17:24:32Z`. The
nonsensitive synthetic Python transport probe passed compilation, replay rejection
and exact cleanup observation. All six Coolify containers were healthy, the reaper
reported success/exit 0, and zero validation containers remained.

Local verification: 620 coding tests passed with real loopback PostgreSQL and no
skips. Nineteen installer tests include real filesystem/SQLite transactions, abrupt
`os._exit(97)` after each rename, missing phase writes, occupied locks, rejected
foreign data and real isolated imports. These prove process-death recovery, **not**
power-loss behavior, live host rollback or a live packaging worker.

Packaging policy/cgroup preparation, actual offline React/Spring toolchains/runtime
bases, image publication/protected signing, production migrations/funded enrollment,
worker integration and complete behavioral deployment/Chat proof remain unfinished.
See [OCI packaging](OCI_PACKAGING.md). The trial allocation and VM deadline were
not extended; current attributable billing is still required.
