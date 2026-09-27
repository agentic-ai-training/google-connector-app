# Central validation capacity and host reconciliation

Status, 28 September 2026 IST: implementation and local PostgreSQL proof. Migration
019 is **not** applied to production. The deployed host launcher/reaper works, but
central claim dispatch is not yet wired into the hosted-worker transport.

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
             trusted transport to keyless host [pending]
                            |
              host journal + isolated validation
                            |
           authenticated, identity-bound host evidence [pending]
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
2. Authenticate the worker-to-host transport, bind the central claim ID to the host job,
   and verify the exact source/image/profile/deadline before executing. Do not accept
   the model as the authority for these values.
3. Bind actual host cleanup evidence back to the central claim through authenticated
   transport. The SQLite reaper alone cannot release a PostgreSQL claim.
4. Wire worker rescheduling, lease recovery, tenant-visible status and alerts. Preserve
   uncertainty through network partitions and controller/host restart drills.
5. Install/attest the required language toolchains and complete live end-to-end proof.

See [host isolation](ISOLATED_VALIDATION.md), [cost authority](CLOUD_COST_LEDGER.md), and
[deployment admission](DEPLOYMENT_ADMISSION.md).
