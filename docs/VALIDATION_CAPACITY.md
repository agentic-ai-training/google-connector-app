# Central validation capacity and host reconciliation

Status, 28 September 2026 IST: implementation and local PostgreSQL proof. Migration
019 is **not** applied to production. The deployed host launcher/reaper works, but
central claim dispatch is not yet wired into the hosted worker. Policy-v3 host binding,
IAP/SSH transport and reconciliation contracts are now implemented; a synthetic live
transport proof passed without activating production admission.

## One funded resource pool

Validation and generated applications share a host's already-funded CPU, RAM and
storage allowance. `host_capacity.admit` locks the funded account and capacity row,
then counts **both** unreleased deployment allocations and validation claims.
An expired or silent job remains allocated until it is reconciled.

```text
Authenticated coding run + registered tenant target + immutable source/toolchain
                            |
                    central transaction
         account -> target/run -> capacity pool -> claim/event
                            |
                         reserved
                            |
        recheck owner, run, target, deadline, funding and capacity
                            |
                 one committed dispatch claim
                            |
             pinned SSH/IAP transport to keyless host
                            |
              host journal + isolated validation
                            |
           authenticated, identity-bound host evidence
                  /                              \
        uncertain, retain allocation       cleanup verified, release
```

`app/coding/validation_admission.py` binds a claim to user ID, coding run ID, step
sequence, target identity hash, source hash, pinned image, profile and deadline.
It requires an active, consented run owned by the target owner. Validation does not
grant branch publication, merge, deployment, canary activation or promotion authority.
User-service and core-improvement callers retain their separate higher-level policies.

Current fixed validation resources match the launcher: 1,000 CPU millis and 1,024 MiB
memory, including its memory-backed working filesystems. An additional 64 MiB of
transient host storage is reserved for source staging/accounting. Persistent toolchain
images and controller overhead must already be excluded from the host's usable pool;
they are not made free by releasing an individual validation claim.
The 64 MiB staging allowance covers only the bounded 32 MiB source payload and archive
on the execution host. It does not authorize a full repository checkout, dependency
download or image build there; those phases require their own admitted resources.

## Replay, revocation and cleanup

- Identical user/idempotency replays return one claim. A changed source or other
  manifest field with that key is rejected. One run/step/request fingerprint cannot
  create multiple claims under alternate keys.
- Only one caller receives dispatch authority. A lost response after that transaction
  is uncertainty, not permission to dispatch again.
- Dispatch rechecks cancellation/deletion/consent, target revocation, parent funding,
  fixed host expiry and shared capacity. A reservation alone is not execution authority.
- An undispatched claim can be cancelled safely and idempotently. Once dispatched,
  cancellation/expiry alone cannot release CPU or memory accounting.
- Reconciliation continues after target revocation. It accepts only evidence already
  authenticated by the trusted transport, not arbitrary client `cleanup_verified` input.
  This module is internal and is not exposed as a public/model-callable endpoint.
- A host job ID is immutable and unique per pool, preventing one cleanup observation
  from releasing two distinct claims. Evidence replay cannot grant another dispatch.
- The original VM funding obligation remains active; these claims do not reserve its
  monthly cost twice or take money from Vertex/the protected future reserve.

## Schema rollout and rollback

Apply migration 019 before deploying the new shared-capacity reader. Otherwise admission
fails closed because it cannot count validation allocations. The migration adds a
composite run/owner foreign key and indexed validation claims/events. It has no provider
calls. A downgrade refuses to erase unreleased claims; never bypass that check with
`CASCADE` or by deleting active records. Prefer forward repair for deployed systems.

Local tests use disposable loopback PostgreSQL schemas. They cover deployment/validation
competition, concurrent replay/dispatch, foreign owners, revoked authority, deadline
bounds, uncertain results, immutable host identity, duplicate cleanup, transactional audit
failure and downgrade refusal. The separate full Alembic proof upgraded to 019,
downgraded to 002, and upgraded to 019 again in a disposable local database.

## Remaining integration, not completed by this module

1. Enroll/import the existing VM's real financial obligation and usable host capacity,
   with fresh credit/cost evidence; do not pretend the already-running host is unused.
2. Connect the authenticated worker's committed claim to the tested IAP/SSH transport,
   without exposing its credentials to the model or generated processes. The exact
   source/image/profile/deadline are verified at the host, but production worker
   integration/lease recovery remains pending.
3. Invoke central observation reconciliation from that worker's trusted transport path.
   Both sides are tested; the complete production loop is not active. The SQLite
   reaper alone cannot release a PostgreSQL claim.
4. Wire worker rescheduling, lease recovery, tenant-visible status and alerts. Preserve
   uncertainty through network partitions and controller/host restart drills.
5. Install/attest the required language toolchains and complete live end-to-end proof.

## Host dispatch contract

After `claim_dispatch` commits, its trusted caller receives `host_envelope`. Only that
caller may transmit it to the private operator spool `<state_directory>/dispatch/<claim
UUID>.json`, mode 0600 in a mode-0700 directory. This spool is an **OS authority boundary**,
not a cryptographic signature: its writer must be the authenticated trusted controller,
never a model, repository process or public upload endpoint. The host policy v3 binds
`server_uuid` and rejects validation without `--claim-id`. Policy v2 remains for the
legacy standalone test fixtures, not centrally admitted production work. The actual
host is now on v3. Its fixed `--gateway` entry point accepts the same bound envelope
over the authenticated SSH stream without creating a source staging file.

The host verifies profile, timeout, pinned image, audience, short deadline, and SHA-256
of the canonical filtered tar produced by `source_archive`. This hash is **not** a Git
commit or an unrelated file-manifest digest. Both sides must hash that exact source
representation. A private journal atomically binds claim ID, request/envelope hashes and
host job before container creation. A consumed claim cannot execute again, even after
cleanup. Claim failure cannot silently fall back to the unbound path under v3.

`--observe-claim <UUID>` reports cleanup, not task success. Unknown claims retain central
capacity. For known claims, the controller's `reconcile_host_observation` checks exact
claim, request and envelope hashes and immutable host job identity before accounting
release. Hash comparison does not authenticate the sender; output must come from the
pinned SSH/other approved host transport. No public endpoint accepts these observations.
A lost validation result remains unknown even when cleanup is proved.

The synthetic `scripts/probe_bound_validation.py` proof on the existing gVisor host
checked successful compilation, rejection of changed source/unbound/replayed authority,
and clean observation without inferring a lost test result. It did not enroll the VM,
apply a production migration, mint production claims or activate hosted coding.

## Fixed-command IAP/SSH transport

`app/coding/validation_transport.py` accepts an operator-registered host configuration,
not model arguments. It checks protected credential/config/program paths and a dedicated
known-hosts file containing exactly the expected Ed25519 key for the numeric GCE instance
ID. SSH ignores user configuration, disables agent/port forwarding, fixes the remote
command and sends no inherited provider/DB environment. IAP supplies the network route;
OS Login/SSH supplies host access. No public SSH port or validation HTTP listener is added.

The protocol is one bounded JSON header followed by a bounded plain tar. The gateway
holds its single host slot during transfer, checks paths/types/counts/sizes, and never
extracts source on the host. The source is passed directly into the networkless sandbox.
Receive time, total output and execution time are bounded. SSH failure is uncertainty,
not permission to retransmit the same work. Observation is a separate read-only command.
Cleanup observations are not validation attestations: a lost test result stays unknown.

Live proof `scripts/probe_validation_transport.py` used synthetic source and the existing
host. Unknown-claim lookup, successful compile, exact cleanup observation and rejected
replay passed through the actual encrypted transport. The first attempt was correctly
blocked by group-writable Homebrew ancestry. A private operator-owned APFS clone of the
existing SDK was used instead; shared Homebrew permissions were not changed. Google CLI
credentials stay local in the protected SDK config, never in source or the sandbox.
The SDK/dependency installation remains part of the operator trust base.

This operator proof intentionally uses a synthetic claim; it does not pretend the
unapplied production PostgreSQL admission chain ran. Financial import, worker identity,
production migrations and run/result/lease integration remain required before activation.
Local coding regression evidence at this checkpoint: 219 tests passed, including real
loopback PostgreSQL fixtures, plus targeted lint/security/diff checks.

See [host isolation](ISOLATED_VALIDATION.md), [cost authority](CLOUD_COST_LEDGER.md), and
[deployment admission](DEPLOYMENT_ADMISSION.md).
