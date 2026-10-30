# Protected host bundle installation, forward recovery and rollback

This is an **operator** workflow, never a model-selected command or generated-app
tool. It updates reviewed stdlib files on the existing VM through pinned IAP/SSH.
It does not approve toolchain/runtime images, change policy or sudoers, prepare
resource groups, enroll financial capacity, run generated code or deploy apps.

## Installation protocol

The reviewed loader/proof/publication/rejection source protocol accepts fourteen exact files.
Historical completed twelve/thirteen-file transactions remain readable, but do not
authorize installing a partial new source closure. The live fourteen-file recovery
release is recorded below; the earlier eleven-file custody checkpoint is historical.
The installer preserves the 256,000-byte wire cap. Large apply
frames use a version-2 zlib/base64 envelope with declared count/SHA-256 and a separate
384,000-byte decoded ceiling. Decompression is limited before allocation; trailing
streams, incomplete streams, wrong sizes/hashes, duplicate keys and compressed
operations other than apply/exact forward resume are rejected. Historical exact seven/eleven/twelve/thirteen-file transactions
remain readable; a new install requires the exact current fourteen-file inventory.
No missing helper is ignored or installed outside the reviewed complete transaction.

An `enrolled` import is not treated as completed merely because its phase says so.
The installer checks its publication plan, receipt, actual protected policy/registry
pair and complete provenance chain, using read-only SQLite while holding the same
host flock. The verifier comes from protected current source, or the exact recorded
predecessor when an interrupted swap has removed the active directory. Unknown,
drifted or unreadable history remains pending. This permits verified completed
enrollment to coexist with later source updates without authorizing rollback into
an older launcher that lacks the retained-import/publication barriers.

The next local recovery checkpoint also recognizes `rejected`, but only after
read-only verification of its immutable receipt, journal CAS and retained cleaned
proof/job snapshot. An uncertain load or partial publication is still nonterminal.
No old reader may silently ignore rejection history. New import identities bind
the reviewed source bundle (v2); retained v1 identities remain compatible. An
explicit ID-only custody reuse can avoid another upload without granting loading
authority or deleting any retained bytes. See `OFFLINE_TOOLCHAINS.md` for the
operator rejection/reuse protocol and the distinction from image approval.

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

## 8 October live recovery-controller rollout

Source checkpoint `c312172` installed the complete fourteen-file operator closure.
Installation ID:
`a87e3b4b62784103c957f58935ffecd0e485a24f8c33cee531829e0f8802a8b5`.
Independent read-back snapshot:
`512d6bfc80bab82c64902b98a41e00c95fd3e203b0f1d2b2347617330c6700f4`.
The isolated import passed; policy was unchanged and no workload was executed by
the installation. Post-install probe confirmed an idle journal, no pending import
and no pending retirement. The read-only gateway confirmed the two historical v1
imports still cancelled, the same policy/registry hashes, verified host limits,
87.31 GiB free and all six Coolify containers healthy.

One exact obsolete seven-file code copy, installation
`9bdec0e235d9cc16ef66795026da59ed674513e6cccbfcd14bdf7ef111f9748f`, was retired after
fresh snapshot/retention verification. Its code snapshot was
`dec0b984fb1fbce0848d9e7b6f0f5a809c5d11a6e5fcfd41e6f3873e2e443f8d`.
The active bundle and data journal were unchanged by retirement; the latest
predecessor and all image/archive/job evidence were preserved. There was no
resource, deadline, paid model or production deployment change.

The nonsensitive pinned-IAP Python transport regression passed compilation,
replay rejection and bound cleanup after rollout; it did not activate production
admission. The first local invocation was rejected before SSH because the probe
requires an absolute operator configuration path. The corrected absolute-path
invocation passed; no failed remote dispatch was replayed.

Local final verification before rollout: 1,254 coding tests passed without skips,
255 targeted recovery/installer/transport checks passed, and Flake8/Bandit/diff
checks passed (existing dependency/suppression warnings only). This is not full
goal acceptance or live React/Spring proof; those operations are separate from
installing their reviewed controller.

### First live React import: retained readback failure

Fresh v2 import `341dd8fd9ee3cc0ab781027bf705300f27c72fee97f1b62909097371738d4e50`
reused the exact retained 108,749,312-byte React archive without upload. The retained
load unit acknowledged dispatch and subsequently exited successfully with a positive
daemon-completion receipt. However, the clean-environment Docker template failed
on the absent optional `Entrypoint` key. The controller correctly retained
`load_uncertain`, `reconciliation_required=true`, failure stage `image_readback`,
and `image_approved=false`; no fixture or publication was started.

The public image index identity, Linux/amd64, UID 65532 and reviewed role/base/operator
labels were independently observed, but image presence alone does not close the
reservation. The local readback now uses optional map lookup for `Entrypoint` and
`Volumes`, matching the existing `Healthcheck` handling. That source correction is
**not installed** over pending maintenance. The next operation must be an audited,
source-bound readback repair of this exact completed load, preserving the original
claim/receipt and invoking no new Docker load. Ordinary bundle installation,
rejection, new import and profile approval must not bypass the retained barrier.

The corrected projection was then exercised read-only on the actual VM using the
same clean environment. It returned the exact reviewed image metadata and passed
the local recipe validator. The 176 loader/rejection/publication regressions,
Flake8 and whitespace checks passed after the projection change. These prove the
readback correction, not installation of the correction or reconciliation of the
live reserved import; no second Docker load or fixture execution was requested.

### Audited transient readback repair (local preparation)

`scripts.repair_toolchain_readback` is a separate operator-only IAP controller,
not an ordinary bundle installation or model tool. It requires the exact c312172
fourteen-file snapshot and loader hash; no arbitrary patch, path or command can be
provided. It derives only the two optional-field lookups from those protected
source bytes. The repair program's actual Linux command-line source hash is also
verified. A transient no-write receipt validator, derived from the same source,
checks the original execution claim/unit/receipt before any audit or state change.

```text
positive original daemon receipt + exact original claim/source/configuration
    -> audit plan durably committed (old/new verifier + validator/controller hashes)
    -> independently inspect exact current image
    -> recover exact receipt update + journal CAS
    -> loaded: STILL RESERVED, no role approval
    -> separately explicit reject_repaired: retained terminal rejection, no approval
```

The installed dependency closure is unchanged. The original import and execution
binding remain immutable; the supplemental audit records the changed observer
instead of claiming that the original verifier ran successfully. No Docker load,
source installation, fixture, file deletion, policy publication or cloud resource
change is invoked by repair. A live worker, negative/absent daemon receipt, altered
claim/configuration/custody or earlier fixture execution blocks preparation.
Retry accepts only the same plan, verifier/controller hashes and exact before/after
receipt states. Foreign staging bytes are preserved for review.

The explicit `reject_repaired` option is only for abandoning the old unpublished
verifier transaction after positive reconciliation. It retains the image/archive
and original completion evidence; a fresh source-bound transaction must still
validate any later role approval. This is not compensation of a user application.

```sh
python -m scripts.repair_toolchain_readback \
  --operator-config .gca-local/validation-transport.json \
  --operation repair_readback \
  --intent-id 341dd8fd9ee3cc0ab781027bf705300f27c72fee97f1b62909097371738d4e50 \
  --expected-snapshot-hash 512d6bfc80bab82c64902b98a41e00c95fd3e203b0f1d2b2347617330c6700f4
```

Local checks include 47 tests with real protected SQLite/custody and actual process
death after audit persistence, receipt replacement, loaded CAS and terminal rejection.
Docker/systemd are mocked in these tests: live repair is a separate evidence step.
Final local verification: 1,301 coding tests passed against disposable loopback
PostgreSQL with no skips, plus Flake8/Bandit/whitespace checks. Only the existing
Vertex SDK deprecation and unused suppression warnings remained.

### 8 October follow-up: public-source reader and independent image cleanup

The first live repair stopped **before** creating an audit: the source reader
incorrectly used `custody.file_stream`, which deliberately requires private 0600
data. Installed operator source is protected public 0644 code. The correction adds
a distinct fixed-path source reader, retaining no-follow open, owner/mode/link/inode,
size and exact old-source hash checks. Archive and receipt custody remain private;
their reader is not weakened. Sixteen new tests cover both accepted source modes,
symlink/hardlink/writeability/content/size rejection and replacement during open.

Subsequent actual-host inspection revealed **a different current failure**:
`No such image` for the reviewed React index. The Docker inventory contained only
the six Coolify infrastructure images; the previously approved Python validation
image was absent too. Docker has not restarted since 20 September. Coolify cleanup
execution `18` succeeded on `2026-10-08 00:00:06`–`00:00:13` (database timestamps);
its bounded log contained both exact missing image IDs. Installed cleanup code
always prunes dangling images and also removes unused non-application images.
Settings had `force_docker_cleanup=true` with daily `0 0 * * *`, despite an 80%
disk threshold. Therefore the optional-field fix alone cannot recover current
image availability. The original normal daemon-completion receipt is retained,
but it is not current image availability or permission to replay its RPC.

The operator-only `configure_coolify_cleanup` guard now changed **only**
`force_docker_cleanup` to `false`, with a root-private write-ahead plan and receipt,
fixed server identity, exact before-state SHA, transactional DB comparison and
independent readback. Daily checks and the 80% threshold remain. Volume/network
deletion remain disabled; application image retention remains enabled. It ran no
cleanup, restored no image and changed no allocation, host policy or source bundle.
The guard is not a model tool and does not expose any token or database credential.

```text
forced midnight cleanup -> unused validation/toolchain images disappear
             |
root audit -> change to threshold-based cleanup -> independent readback
             |
original import STILL RESERVED (not approved or completed)
             |
separately audited restoration of exact approved content
             |
source-bound repair -> host fixtures -> verified role publication
```

Live guard plan SHA:
`80fd177f2612a7cb2bec3e453474ca529f019773fab0425c90c1c82f18dab320`.
After-settings SHA:
`af0422634323927f859e9318a9f6aa99ba6000791b621f98ca3d6b67caa67b4e`.
Independent readback confirmed the unchanged fourteen-file snapshot, policy and
registry, six running healthy Coolify containers, and the retained `load_uncertain`
React import. Its 108,749,312-byte archive custody was reverified; no repair audit
has been committed and no second Docker load was invoked.

The guard prevents **forced daily** removal at low disk usage, not all possible
deletion. Threshold/manual cleanup still needs coordination with retained operator
images and the shared execution owner. Missing images must be restored through a
new audited restoration decision after proving the prior RPC ended; never replay
an uncertain RPC, forge a successful readback, or bypass the pending bundle barrier.
Python availability, React/Spring live fixtures, image-retention coordination and
generated-app deployment remain unproven. Local checks: **1,338 coding tests passed**
without skips on loopback PostgreSQL; Flake8/Bandit/diff passed with existing
dependency/unused-suppression warnings only. These are not full-goal acceptance.

### 8 October restoration and terminal reconciliation

The preceding absent-image checkpoint is superseded for **React only**. The
operator-only `scripts.restore_reviewed_toolchain` prepared an immutable root
SQLite plan, claimed a distinct systemd unit once before the RPC, restored the
retained exact public archive, and independently verified current metadata and
worker exit. The original import/load receipt was not rewritten by restoration.
No generated code, provider key, policy approval or new resource entered the unit.
It is bounded by the existing packaging slice, one CPU/1 GiB/no swap/32 tasks,
300-second lifetime and the unchanged VM deadline. Exact daemon absence is required;
authentication/template/network errors do not authorize a load. Unknown claimed
outcomes are observation-only, never automatic replay.

| Evidence | SHA-256 / identity |
| --- | --- |
| Restoration plan | `f54189a9fa591b6540756e4fb311e72a83235660448436aea3fe34d82c7a372f` |
| Exact restoration controller | `4d5df079af64b0440ce864992b2de746a86c4243157668e0fffdc3354cce30b7` |
| Restoration result | `d1df1a0b71416875cbb83f902f1def32b1344f2bd692cfc0c3ab77ab2ec61bd8` |
| Restoration unit invocation | `2d88b522eea341a1a1f52206710b6eaa` |
| Repair controller | `9a74ba1c9a3b278a918b24654714e109dafb01752ec01649f0f462c8a29f33d2` |
| Repair plan | `96aadf8f17a26d5fa27205549fd08a9c3f190dd59d92b1af9336de175e5d424b` |
| Restoration provenance bound into repair | `dac0615324276d90a763a56f11c93186aa6181a633ecb810d38a601780b79643` |

The restoration finished `success`, `ExecMainStatus=0`, `MainPID=0`. Its exact
51,601-byte source is retained as local Git blob
`ca4a087323268b58968caa33aa7610dfeef71253` under
`refs/gca/operator-restorations/<controller-sha256>`. Historical observation uses
`--reviewed-program-sha256`, never regenerates a different controller from newer
helper files. The allowlist, source hash, size and compilation are checked before
transport; Git replacement objects and user/global config are disabled.

The separately reviewed repair binds original receipts plus the complete
restoration plan/claim/result lineage. A changed source, claim, enforcement,
image, receipt or nonterminal worker prevents repair. Independent readback then
confirmed the repaired receipt and hashes, zero unfinished jobs and six healthy
Coolify containers. Explicit `reject_repaired` terminally closed the unpublished
old-source import; a second read-only probe verified rejection, archive custody
and `require_available()`. This is **negative retirement, not role enrollment**.

```text
old load definitely completed -> external cleanup -> exact image absence
                                         |
audit restoration -> single-use claim -> one load -> receipt + worker exit
                                         |
repair audit binds BOTH histories -> truthful readback -> reject old import
                                         |
maintenance available; archive/image/audits retained; approval still false
                                         |
new-source completed-load adoption + native fixtures + verified publication
```

Local focused validation: **182 tests passed**, including abrupt real process
death with mocked Docker/systemd, incomplete/changed restoration rejection and
historical-source custody tests. Flake8/Bandit/whitespace checks passed. These
tests are distinct from the live evidence above. The full coding regression suite
passed **1,436 tests without skips** against disposable loopback PostgreSQL schemas,
with one existing Vertex SDK deprecation warning. Python restoration, reviewed
cross-source adoption, native React/Spring fixtures, role publication, funded
central dispatch, application behavior and Chat URL proof remain incomplete.

## Commands and preceding custody checkpoint

### Reviewed cross-source adoption — local preparation, 8 October

`load_adopt` is a distinct operator gateway operation, not Docker loading. It
requires a new v2 source-bound reservation with verified retained custody and a
terminally rejected, definitely completed prior reservation for the exact same
role/image/archive/recipe/host/policy/registry/deadline. The previous worker must
be terminal and current immutable image metadata must match. Unknown completion,
missing images, new load controls/units, unfinished jobs or changed configuration
cannot authorize adoption.

The immutable `toolchain_adoptions` plan binds both source versions, both custody
hashes, the source completion/rejection, auxiliary repair/restoration history and
current metadata. It is persisted before CAS transitions
`archive_verified -> adopting -> loaded`. `adopting` remains a maintenance barrier,
not workload authority. Process death after audit, intermediate CAS or final CAS
recovers that same plan without a new daemon RPC. Adoption creates **no fabricated
dispatch/claim/result files**. Returned evidence explicitly says
`load_origin=completed_load_adoption`, `docker_load_invoked=false` and
`image_approved=false`. Publication still requires all three independent native
fixture results; subsequent rejection hashes the typed adoption receipt itself.

```text
terminal old source-bound load + custody + exact current image
                                  |
new source bundle -> new intent -> reuse custody -> immutable adoption plan
                                  |
                         adopting -> loaded
                                  |
native positive tests + native build + deliberate negative test
                                  |
                 verified publication (separate authority)
```

Only after the new full closure is installed and independently probed:

```sh
python -m scripts.transfer_reviewed_toolchain \
  --operator-config .gca-local/validation-transport.json \
  --operation load_adopt \
  --intent-id "$NEW_SOURCE_BOUND_INTENT" \
  --source-intent-id "$TERMINALLY_REJECTED_COMPLETED_SOURCE"
```

Local focused validation: **332 tests passed**, covering adoption, repaired source
compatibility, ordinary loading, native-fixture/publication integration with mocked
host operations, and restoration. Flake8/Bandit/diff checks passed. The immutable
c312172 verifier is now retained as a hash-checked text fixture, so extending the
current loader cannot change historical repair tests. Initial full regression was
refused at 319 MiB local free disk; after headroom rose to about 1.6 GiB, the full
suite passed **1,492 tests without skips** using disposable loopback PostgreSQL
schemas, with one existing Vertex SDK deprecation warning. No user/Docker data was
deleted. No new closure installation, adoption on the VM, native proof or role
approval is claimed by this preparation checkpoint.

A separate read-only Python recovery preflight verified the approved 46,237,696-byte
archive, SHA `36723f7fff07fb1712479bdbf5b09a31d67f38b0a45af4648e1c00b1e80d3778`,
and exact daemon absence of the already-approved Python image. It verified an
available maintenance slot and unchanged source/configuration/deadline. It invoked
no load. Configuration is read with the configuration reader, not a canonical
receipt reader: the protected legacy registry may use valid noncanonical JSON.

### Live adoption and React enrollment — 8 October; rechecked 11 October

The complete `ca86020` closure was installed as
`cf3fc740139edd5faad2598279c75dbc292bad37b60c3a4ca209a2bd5a9b3edb`,
snapshot `8b032cb82bfe249c8390c0b22860fbe1ef53790e2489eb07de6dd1a8fbfa40ed`.
The obsolete seven-file rollback copy `a457b819dec51ee6d83f7c70f3d86908e60b38a0e4926dad3d698f2d0a74b241`
was retired under fresh audit before installation; the latest predecessor and all
data/journals/artifacts were preserved.

New React intent `492f3a01417ca8dcc51f27d1f1ad3ab0fd5fd7b769165b7ad99ad68fe3fe1559`
reused protected custody from the terminally rejected old intent. Adoption plan
`b587130d3193de64ca39461727ad00e75c32f30e04c0eeaf46b715ca09ca0c5a`
was retained and independently checked; no Docker load was invoked. Native cases:

| Case | Job | Expected/observed exit | Cleanup |
| --- | --- | --- | --- |
| `react_test_pass` | `07f5632178854f33ac2bb5965c465f0b` | 0 / 0 | verified |
| `react_build_pass` | `9f13aded53cb4cca9608378e5484076c` | 0 / 0 | verified |
| `react_test_fail` | `6f40328dcca94fb7a95f7ad0d844f2e0` | 1 / 1, fixed failure marker | verified |

All three are actual Linux gVisor executions, distinct from mocked regression
coverage. Independently reviewed publication plan
`bc206f5ab1869f202569e8a424859c45f954e2812c9867b8a115162c01be90b3`
adds only the two React profiles and their provenance. Post-publication readback
verified the enrolled ledger, all retained receipts, native cleanup, exact image,
maintenance availability and six running/healthy infrastructure containers.
Published policy SHA is
`86d0d41fc1ddc06e15f9b33b412f2765c074b5da58da7ea1386bcb0c70ca48db`;
registry SHA is
`327de3e82664728b4108a3c452242cd11015fdf2f344b8908cc0a0f22840f7e6`.
The 11 October gateway probe reconfirmed the current source/configuration and
enrolled ledger. Spring/Python recovery and full generated-app deployment remain
separate unfinished requirements. STOP `1792430672` remains unchanged.

### Spring first native attempt and diagnostic gap — 11 October

Intent `dbdd8edbc2a2227082231c902817deb9a257e98acc323dd22a307b97ef998c3d`
reused the cancelled v1 Spring archive. One fixed unit loaded it; independent
readback verified `MainPID=0`, `Result=success`, exit 0 and invocation
`8bd00c9dbf1742b9940e95eda9a76326`. The first observation was unconfirmed; inspecting
the same unit/receipt and observing again confirmed completion. **No second load
was dispatched.**

Native `spring_test_pass` job `0604acd6b88c46e4909b2f5ba84d094b` returned exit 1,
`passed=false`, cleanup verified. Result SHA:
`9b6e20f06a8bcfb9c8b612db2f58199a516fd698ec349136806f07d9e81c05aa`.
Native enforcement SHA:
`9d15a7e2ab0f26a08119a535892e5e613c1229b7e3d99811118f0668a2c6c3be`.
The import was terminally rejected with retained image/custody, not approved.
Its completed-load SHA is
`473fe9c9764b7b4cf668984894299e81239c1e558f22a9d0215000e101e94459`.

The old implementation retained only the complete result's hash. It did not retain
the compiler/test output itself, and Docker logging was deliberately disabled.
Consequently the exact underlying test error is **unknown**, not proven to be a
memory, dependency, JVM or gVisor error. Do not claim a cause or bypass the failed
fixture. The next reviewed closure adds atomic full-result/receipt retention under
the protected SQLite journal and a separate read-only `proof_diagnostics` operation.
This does not change the existing proof/publication projection or its hashes.
Only fixed public fixture output is permitted; no generated/customer input is added.
Each full result is capped at 2 MiB, at most 48 results are retained, and output
excerpts are capped by serialized JSON bytes (including Unicode/control escaping).
Diagnostics verify receipt/result/native-enforcement hashes and cleaned-job lineage.
Missing historical logs are explicitly marked unavailable, never reconstructed.

Validation of the local diagnostic closure: **334 focused tests** and **1,530 full
coding tests** passed without skips using disposable loopback PostgreSQL schemas;
one existing Vertex SDK deprecation warning remained. Flake8, Bandit and diff
checks passed. Tests cover atomic receipt/result commit failure, hash/inventory
tampering, failed results, legacy unavailable output, bounded Unicode/control
excerpts and the no-execution/no-approval gateway/client contract. These are local
regression results, not proof that the subsequent Spring native test succeeds.

The diagnostic closure `be3a641` was subsequently installed as
`0150b9a3440623091f500fe4ffebbd8ee30f9430a8a7cb927685a41819fef9d6`,
snapshot `1ed9ac7575dd2ffe613538bdb2c1e00055af4bf50b115e66ee3398b848b12307`.
Independent gateway readback verified the full closure, unchanged policy/registry,
old Spring rejection and existing React enrollment. Installation executed no
workload. The obsolete seven-file copy
`529bc18bc56ed8f7b2a9c63aeb15cabc12d5ec10af7ee326605f9d27ec0bbe4e`
was retired beforehand under fresh active/retention hashes; active code, the latest
predecessor and all data/journals/artifacts were preserved.

Read diagnostics for the exact current source/configuration-bound reservation:

```sh
python -m scripts.transfer_reviewed_toolchain \
  --operator-config .gca-local/validation-transport.json \
  --operation proof_diagnostics --intent-id "$CURRENT_RESERVED_INTENT"
```

This operation does not accept source bytes, shell commands or rerun instructions.
It reports the latest attempt for each fixed case, not merely the first passing
attempt. A changed source/configuration requires explicit handover; do not rewrite
an older binding or claim unavailable historical logs are recoverable.

The diagnostic native Spring attempt (`c0ccc001acd82dfa5e62bbe286f916f5129257d06cf33627856c4c23d086e701`,
job `36532c3eae164cefa37efe1b235e6319`) adopted the completed image without a new
Docker load. Its retained result SHA is
`21e346e015ca20315f20158b8de5e14a6d0033837b9a3bff2476da70b611cd68`.
Read-only diagnostics now establish the actual failure: Maven's test compilation
could not open `json-path-2.10.0.jar`, reporting **Too many open files**. This is
concrete evidence for the latest attempt; it is not a reconstructed log of the
earlier uninstrumented job. Cleanup was verified and this import was explicitly
rejected, retaining all evidence without Spring approval.

The follow-up gives only `spring_test`/`spring_package` a bounded 1,024-descriptor
soft/hard limit; other validation profiles remain at 256. Native proof checks
both Docker metadata and the actual guest soft/hard limits before source execution.
CPU, memory, task count, UID, read-only root, tmpfs, no-network and cleanup constraints
remain unchanged. Compilation/test/package success and deliberate failure must be
reproved after this reviewed source handover; larger descriptor limits are not
themselves success evidence.

The descriptor correction passed **330 focused tests** and **1,554 full coding
tests without skips** on disposable loopback PostgreSQL, plus Flake8/Bandit/diff
checks (one existing Vertex SDK deprecation warning). Both lower/unbounded/wrong
guest limits and wrong per-profile Docker metadata are rejected by the tests.

The full descriptor-corrected closure `2f3ba14` was installed/read back as
`653048597d29520d60c23b912b8a38f28ebb90174ec1c45c0dec46d0b7da2385`,
snapshot `c893a53f8ce2564ee8e2adf3da6313209fc16a61ef090565893a8d259eb1a95d`,
without policy/registry changes or workload execution. A second obsolete seven-file
copy (`c11d29d43f04bf580bfbfb458a6db4f10dcc9e41231f697893d3de688520403e`)
was retired under fresh hashes before installation, preserving current code,
the latest predecessor and data. New intent
`1c4488141c534f5181d40b3dae4da967e1e0d0215305d6e5fcc67e8eae4ba67f`
reused custody from the rejected diagnostic intent. Adoption plan
`39b82d8f92782fa4bfd0a1aec585b3db2e464ddab427d21bb1a0dc0ebe938fa9`
binds that completed-load lineage; no additional Docker load occurred. Native proof
and publication are still required at this checkpoint.

### Verified Spring enrollment — 11 October

The descriptor-corrected native cases completed with expected outcomes:

| Case | Job | Expected/observed exit | Cleanup |
| --- | --- | --- | --- |
| `spring_test_pass` | `5771d45ee6504dabac35c1bb0f035d1b` | 0 / 0 | verified |
| `spring_package_pass` | `b828fe361be64d75b0a714dac37b75b9` | 0 / 0 | verified |
| `spring_test_fail` | `ee32ed00163c47db9d3f11063da57f8a` | 1 / 1, fixed failure marker | verified |

All three use binding SHA
`b2223152b7dc2f739846f576ff45c90cf91366e23b93b6dd61cc78c2569fbc4c`.
Independent receipt/result readback verified guest soft/hard descriptor limits of
1,024, unchanged 1 GiB memory/64-task bounds and zero guest capabilities.
Publication plan
`a28e45a6f3642cd268d7c74e6f76ca4cfb1e538fcc87d93579372caea1ceef4e`
was reviewed independently: only `spring_test` and `spring_package` plus their
provenance were added; all prior policy/registry records and STOP were preserved.
It was explicitly published, then independently verified with both React/Spring
enrolled, exact current images, six retained passing case receipts, native cleanup,
available maintenance and six running/healthy infrastructure containers.

Current policy SHA:
`afa4901baca18a451e704b275beaebd1c1efcc7edf521debdd5925d9027fda73`.
Current registry SHA:
`6eb2f72e3b1b901e1fd9387f144f0612985318070f32df9e0d5c82f84014f345`.
This proves the fixed React/Spring toolchain cases, not arbitrary generated-app
behavior, PostgreSQL/cross-language deployment, funded dispatch or Chat delivery.

The separate fresh Python recovery preflight confirmed its exact current image
absence and rehashed the private 46,237,696-byte approved archive to
`36723f7fff07fb1712479bdbf5b09a31d67f38b0a45af4648e1c00b1e80d3778`.
Maintenance was available and the current policy/registry/STOP matched. No load
occurred. Audited legacy-Python restoration remains the next step; calling an
ordinary validation profile to trigger its implicit reload would not prove that
recovery has the required durable authority and single-claim evidence.

After the user cleared local disk space, a fresh check showed 6.1 GiB available.
The full `tests/coding` suite was rerun against verified loopback PostgreSQL:
**1,554 passed, no skips, in 44.52 seconds**, with one Google SDK deprecation
warning. Coding lint and Bandit's medium/high severity checks passed; Bandit also
emitted existing suppression/comment warnings. The read-only pinned-IAP bundle
probe reconfirmed snapshot
`c893a53f8ce2564ee8e2adf3da6313209fc16a61ef090565893a8d259eb1a95d`,
installation `653048597d29520d60c23b912b8a38f28ebb90174ec1c45c0dec46d0b7da2385`,
an idle journal and no pending toolchain import. This removes the disk-space test
blocker, but does not restore Python or complete the broader goal.

### Audited legacy Python restoration — 12 October 2026

Source `5944088` removes implicit legacy loading from workload admission and adds
an explicit singleton restoration ledger. Corrected source `7703dee` verifies the
existing registry reader's `(raw_bytes, document)` contract. The first preparation
failed on that contract before creating any ledger row or loading an image;
independent readback confirmed the absence of recovery mutations.

The complete fourteen-file barrier closure was installed with installation
`0e6d0195f5b7562384ce1b79ec646360e5cc1b26f12e7023198c81eee913ced8`,
independently observed snapshot
`deb340f2f45957b7ec4420224727d7128567e9a1debc93855a10f2e4b5723481`.
Only the reviewed validation source changed; policy, registry, resource limits and
STOP were preserved. The verified obsolete `0150b9a...` predecessor copy was
retired under fresh source/retention hashes; current code, latest predecessor,
data, journals and image archives were preserved.

The corrected operator program is retained in Git blob
`0c06afcba38a5e946feea9b5c912ea023ac5c2dd`, under
`refs/gca/legacy-restorations/129b5eb777639135943c35c62d2abf33ddd3dc0ae9cfe6cd4b3e6919527f8ab0`.
The CLI can use `--reviewed-program-sha256` to keep observing this exact controller
after local source changes. No generated code or provider key is involved.

| Evidence | Value |
| --- | --- |
| Recovery identity | `1dad05d61eba90764e48ce3073abad0bbb7a27d32e471777c608278ea8112627` |
| Plan SHA | `cce0783be4aff0344e2b891b824cec3c49209b9fe488fd65d76848451d3c5e2c` |
| Claim SHA | `b082c406aed9df0779417072cc44294e96ccf7d195542d74e4dcacae2ee35dad` |
| Result SHA | `4cdc8228faafff2e9ec9e644621864cf741e2d65a968679dd9671d0f55d70d52` |
| Unit invocation | `b729796652ef4f2095d27c199535c91a` |
| Positive Python job | `adccce50d1814835965fcff49aa2f308` |
| Negative Python job | `b9d516d297e24ee88bbb16631c6c2515` |

One fixed, retained systemd worker performed one Docker load. Its write-ahead
dispatch and claim preceded the RPC. The private archive's full digest and inode
were verified; Docker inherited that same open file descriptor rather than
reopening its pathname. Normal daemon completion and exact Linux/AMD64 image
readback were recorded. Explicit reconciliation independently checked the exited
worker (`MainPID=0`, success/exit 0), invocation identity and current image, then
released the shared journal. Timeouts/nonzero loads cannot be cancelled, repeated
or released merely because an image appears. Only an unused prepared plan may be
cancelled. This bounded singleton recovery does not authorize future reloads.

The live positive probe compiled public synthetic Python and rejected replay.
The negative probe returned exit 1 for `answer =` and independently observed
cleanup; its claim was `c896011d-764c-46d6-8e33-8167f91e2f3f`, source SHA
`df1f70472a1ebee34f01c9e749aefd30352f6a42c36163fd3adbffd7f485ab33`.
Both jobs were independently observed as cleaned. A fresh root readback verified
current Python image presence, React/Spring enrollment and exact images, available
maintenance, unchanged policy/registry/STOP and six running/healthy Coolify
containers. No production admission, migration, generated app, Chat delivery,
paid model call, new cloud resource or budget expansion occurred.

Operator flow (the flags require fresh exact hashes, not placeholders):

```text
restore_legacy_python_toolchain prepare
  -> reviewed singleton plan / host reservation
restore_legacy_python_toolchain dispatch
  -> one retained unit -> one claim -> one bounded Docker RPC
restore_legacy_python_toolchain observe
  -> read the same reservation; never redispatch after observation uncertainty
restore_legacy_python_toolchain reconcile
  -> positive receipt + exact image + exited matching worker -> release
```

Existing workload validation now fails closed when an approved image is missing,
including a legacy image. Source apply/retire/rollback and new workloads are blocked
while the legacy reservation lacks valid terminal reconciliation. A missing or
malformed ledger/receipt is not translated into successful recovery.

Final local verification of the corrected source passed **1,578 coding tests,
no skips, in 68.73 seconds**, against loopback PostgreSQL, with one existing Google
SDK deprecation warning. `flake8 app/coding scripts/restore_legacy_python_toolchain.py
tests/coding --max-line-length=100`, Bandit's medium/high severity checks and
`git diff --check` passed. The 23 focused restoration tests distinguish mocked
daemon/systemd boundaries from the separately recorded live evidence above.

### Runtime candidate acquisition — 12–13 October 2026

Sources `db88fe7` and `b652b1d` add fixed public-registry acquisition, protected
range-resumable candidate CAS, independent read-only graph/rootfs rehash and
bounded standard OCI annotations. The first attempt stopped before runtime blobs
because annotations were rejected; its original plan is retained and linked from
the later source-handover plan. No old failure receipt was rewritten.
The full fourteen-file OCI correction was installed as
`48421865d76f8661f00fe8381c45bf713fb4f392702fbaa0eecafdceb9fa51ad`,
snapshot `b84b3c9da1c21220fb9027de4be1d8cee7c4c6b8811705c54230c61b3a2d08bd`,
with policy/registry/STOP preserved. One exact obsolete predecessor copy was retired
under fresh source/retention hashes; the latest predecessor and data were retained.
The resumed single acquisition completed; independent probing rehashed both exact
candidates and rootfs and checked retained plan lineage. No runtime policy,
Docker load, scan, approval or deployment occurred. Python, React/Spring, journal
and six healthy infrastructure containers were independently rechecked.
All 1,617 coding tests passed without skips, with one existing SDK warning; 136
focused tests, lint/security/whitespace checks passed. See
[full runtime receipt and gate record](RUNTIME_BASE_CANDIDATES.md).

The preceding eleven-file installation was
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

Only the exact fourteen explicit known filenames are accepted by the new source for
a new installation. Saved transactions accept the exact historical seven/eleven/twelve/thirteen-file
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
