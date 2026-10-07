# Cloud cost authority and reservation ledger

Status (1 October 2026): local implementation and PostgreSQL tests. Migration
016 has **not** been applied to production. The deployment controller is not yet
wired to this ledger; do not present it as a live billing cap.

The operator reported a Cloud Billing snapshot for 1–29 September 2026:
₹24,544.99 promotional credit remaining out of ₹28,693.88 (a ₹4,148.89
decrease); Compute Engine usage ₹4,096.68 and Networking usage ₹23.50,
both offset in the report, leaving ₹0 payable for that period. The two listed
services total ₹4,120.18, so ₹28.71 of the credit decrease is not explained
by those rows alone (it may reflect date ranges or other charges). A zero
payable total does **not** mean zero trial credit consumption. This is
user-reported cost evidence, not a provider-verified SKU-level charge export.
On 2026-10-01, the authenticated read-only Cloud Billing API listed exactly
one linked project for this account: `project-ee0c1944-144c-4364-ad4`.
Read-only Compute inventory listed one VM, its 100-GiB balanced disk and no
snapshots. This strengthens project/host attribution of the displayed Compute
and Networking totals, but does not split VM, disk, IP or egress SKUs, establish
charges after 29 September, or prove that the remaining balance is current.
The import schema now uses the exact ₹28,693.88 initial credit instead of the
earlier rounded ₹28,694. Obtain SKU-level costs and a fresh credit snapshot
before importing incurred costs or authorizing new paid resources.

## Authority

### Complete SKU evidence — received 7 October 2026

The operator has now supplied **all 16 rows**, superseding the partial checkpoint
below. CPU INR 3,986.56 + RAM INR 2,136.80 + disk INR 627.82 + networking INR 40.29
sum to **INR 6,791.47** for the selected 19 September–5 October period. The newer
remaining credit is **INR 21,610.67**, implying INR 7,083.21 consumed overall and
an **INR 291.74 difference** outside what this period table explains. Do not attribute
that difference to the VM or silently equate the table's cutoff with current time.
The six missing rows are no longer a blocker. Independent final charge coverage,
identity/freshness, reviewed migration/backup, and production controller enrollment
still require evidence; no live cost import has occurred.

Measured CPU/RAM usage both imply 397.5875 VM-hours for 4 cores/16 GiB. A 720-hour
projection of compute plus observed disk/network averages is **INR 12,298.82**.
This is a planning estimate, not a guaranteed future bill or authority to lower
the approved INR 14,000 ceiling, release outstanding obligations, extend the VM or
spend Vertex funds. The original INR 11,000 would need about INR 1,298.82 extra at
these averages; unused earmark is returned only after final cost reconciliation.
The protected INR 8,500 Vertex and INR 6,000 future reserve remain unchanged.
See [complete evidence, arithmetic and limitations](DELHI_BILLING_RECONCILIATION_2026_10_07.md).

### Newer operator billing evidence — received 6 October 2026

The project-filtered report requested by the operator covers **19 September–5 October**
(actual), with **19 September–6 October** separately forecast. The current reported
credit is ₹22,011.16 out of ₹28,693.88, implying **₹6,682.72 total credit consumed**.
The actual report credits/other savings total ₹6,664.35, leaving an **₹18.37 unexplained
difference** against the all-time balance decrease. Do not assign that difference to
a service or silently treat the forecast ₹7,374.87 as incurred charges.

At that earlier checkpoint only rows 1–10 of 16 were supplied. Known nonzero usage rows:

| SKU | Usage cost |
| --- | ---: |
| Balanced PD Capacity in Delhi (54.18 GiB-month) | ₹616.26 |
| Network Analyzer Resource Hours (140) | ₹14.74 |
| Internet to Google Cloud Performance Resource Hours (139) | ₹10.64 |
| Topology and Google Cloud Performance Resource Hours (139) | ₹14.63 |
| Carrier Peering outbound, APAC | ₹0.04 |

These displayed positive rows total **₹656.31**. Listed ingress, Ubuntu licensing
and Standard VM external IP rows show zero usage cost. CPU/RAM and any other rows
11–16 remain missing; exact SKU reconciliation/import remains pending. This evidence
advances the date and credit-balance checkpoint, not a provider-verified complete
charge export or migration-016 admission grant. Trial credit offsets explain zero
payable without implying zero consumption.

Protecting ₹8,500 Vertex and ₹6,000 future reserve leaves **₹7,511.16** of the
reported remaining credit outside those envelopes. That is a balance observation,
not new spending permission, a guaranteed full-term hosting forecast, or a second
host reservation. The ₹14,000 hosting ceiling and fixed STOP remain unchanged.

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
   A post-creation operator-reported promotional balance is now recorded above;
   project-filtered VM/disk attribution and a fresh snapshot immediately before
   production import are still required. Enrollment for a
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
