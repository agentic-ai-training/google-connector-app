# Cloud cost authority and reservation ledger

Status (28 September 2026): local implementation and PostgreSQL tests. Migration
016 has **not** been applied to production. The deployment controller is not yet
wired to this ledger; do not present it as a live billing cap.

## Authority

The approved initial envelope is INR 14,000 hosting, INR 8,500 combined Vertex
training/evaluation/inference, and INR 6,000 protected future reserve. All values
are integer paise. The hosting deadline is pinned at enrollment, not extended on
each request. For the existing Delhi VM it must remain 19 October 2026 at
17:24:32 UTC. A billing alert is not an enforcement mechanism.

`app/coding/cloud_budget.py` performs pure admission checks.
`app/coding/cost_ledger.py` persists reservations and audit events atomically using
migration 016. These are internal trusted-controller operations, not model tools
or public endpoints. An authenticated service must derive project and user identity;
never accept model-supplied identity, cost estimates or reconciliation claims as authority.

## State and crash behavior

```text
trusted estimate + fresh credit evidence + approved project/category/lifetime
                                  |
                         account row lock
                                  |
                     reserved + audit event
                                  |
             revalidate credit/caps; atomic dispatch intent
                                  |
                             dispatched
                                  |
                    provider call OUTSIDE transaction
                                  |
               +------------------+-------------------+
               |                  |                   |
           uncertain           settled             released
         retain estimate     record actual      proven zero liability
               |
       trusted provider reconciliation
               +----------> settled / released
```

Only the caller receiving `True` from `start_dispatch` may issue the external
operation. A replay receiving `False` must inspect and reconcile provider state,
not repeat a potentially completed write. If the process crashes after committing
dispatch intent but before sending the request, the same conservative rule applies.
This offers at-most-one local dispatch authorization, **not exactly-once delivery**
to an external provider. Provider idempotency keys and resource identity reconciliation
remain necessary.

All users of the funded project share one locked account row. Transactions lock
that row before reading obligations. Reservation and audit records commit together.
Identical idempotency replays return the existing reservation; changed cost/category/
lifetime with the same key is a conflict. A replay alone grants no dispatch authority.

Outstanding and uncertain reservations retain their entire estimated liability.
Expiry, a worker timeout, or a stopped VM does not automatically release funds.
Disks, IPs and backups can continue to incur costs after compute stops. Settlement
records an actual overrun truthfully rather than rejecting the evidence to keep a
misleading under-budget ledger. New admission uses both the observed balance and
the opening balance minus recorded charges, conservatively accounting for billing lag.

For running resources, `record_accrual` accepts cumulative, attributable provider
charges. Recorded spending increases by that amount and remaining reservation becomes
`max(estimate - accrued, 0)`. This prevents a refreshed remaining-credit balance from
double-counting the same usage. Accrual evidence is time-ordered and monotonic; exact
replays are idempotent. Settlement cannot erase accrued charges, and an obligation
with incurred charges cannot be released as if it had cost nothing.

Apps deployed inside an already-funded VM use a separate capacity allocation through
migration 018. Its parent hosting obligation stays reserved once for the whole host.
The capacity path checks active coverage, expiry, envelope overruns and CPU/RAM/storage
availability. It does not request new financial authority when verified incremental
cost is zero. Additional costs still use the ordinary fresh-evidence reservation path.
Runtime enforcement and the production adapter integration remain required before use.
Migration 019 adds tenant/run-bound validation claims to that same resource pool, so
validation cannot overbook resources already allocated to generated applications.
No second monthly VM charge is reserved. See [validation capacity](VALIDATION_CAPACITY.md)
for dispatch/reconciliation guarantees and the still-pending authenticated host transport.

## Activation requirements still outstanding

1. Import the already-running VM's incurred and remaining obligations using the
   operator-only `import_existing_delhi_vm` path. It reads the fixed GCE instance
   and disk identity through protected local gcloud configuration before opening
   a database transaction. The exact project, numeric VM ID, shape, 100-GiB disk,
   creation time and STOP deadline must match. A missing/mismatched provider read
   creates no ledger row. Exact import replay is idempotent; changed evidence or
   owner cannot replace the account. Existing incurred cost is accrued, not hidden
   as unused capacity; an underfunded import is marked uncertain and cannot back
   new host-capacity allocations. The user-supplied credit expiry was date-only,
   so admission uses a conservative 20 November 2026 UTC cutoff, not a fabricated
   exact expiry hour. This path was validated locally and against a live **read-only**
   GCE identity probe; no real cost import or production migration has occurred.
   A disposable PostgreSQL integration test additionally proves that an imported
   funded VM can back a host-capacity pool and one approved zero-incremental-cost
   deployment without reserving a second VM charge. It proves an underfunded import
   cannot back the pool, and a deployment claim cannot be dispatched twice. This
   does not constitute a Coolify API write or a real application deployment.
   Current post-creation promotional balance and attributable VM/disk charges still
   require fresh operator evidence before any production import. Enrollment for a
   *new* account still rejects snapshots with historical obligations.
2. Apply the reviewed migration through the established backup/migration workflow.
3. Bind deployment and Vertex adapters to authenticated principals, approved immutable
   operation manifests, trusted upper-bound estimates and provider resource IDs.
4. Enforce the dispatch boundary in those adapters, including crash reconciliation,
   bounded polling, resource deadlines and verification of continuing ancillary costs.
5. Collect fresh, attributable credit evidence. The pre-creation user confirmation is
   not a fresh post-creation balance. Never fabricate the promotional balance from the
   billing-link API, which does not expose it.
6. Complete live admission/denial and recovery proof before marking Sprint 61 complete.

The ledger cannot prevent manual console spending or charges from unrelated clients.
IAM controls, resource scheduling, provider reconciliation and operational alerts are
also needed. The current DB event table is application append-only by convention;
deployment roles and database audit/immutability controls need separate enforcement.

## Local validation

Use the opt-in `COST_LEDGER_TEST_DSN` environment variable pointing to a loopback
PostgreSQL database, then run:

```sh
python3 -m pytest tests/coding/test_cost_ledger_postgres.py tests/coding/test_cloud_budget.py \
  tests/coding/test_existing_vm_import_postgres.py tests/coding/test_gce_identity.py -q
```

Tests create and remove only random `cost_ledger_test_*` schemas. They refuse
non-loopback hosts. Coverage includes cross-user competition, idempotency conflicts,
dispatch races, transaction rollback, stale evidence, uncertainty, cancellation,
settlement, overruns, fixed deadlines and protection of the Vertex allocation.
Never substitute the production Neon URL for this test connection.
