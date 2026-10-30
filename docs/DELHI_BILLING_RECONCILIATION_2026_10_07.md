# Delhi hosting: complete operator-reported SKU reconciliation

Historical report. The newer [13 October report](DELHI_BILLING_RECONCILIATION_2026_10_13.md)
supersedes its balance and current planning scenario. Preserve the older arithmetic
as provenance; do not add its earlier discrepancies again to the new totals.

Received 7 October 2026. Source: the user's Cloud Billing report, configured using
the previously requested project filter and actual date range **19 September–5 October
2026**. This is operator-reported evidence, not an independently fetched billing
export. No forecast is used as an accrued charge. No production ledger migration,
resource expansion, billing upgrade, training or paid inference is authorized by
this record.

## Complete 16-row table

| SKU ID | SKU | Usage | Usage cost (INR) |
| --- | --- | --- | ---: |
| 0B29-DA5B-5E84 | Inbound carrier peering, APAC | 0.48 GiB | 0.00 |
| 3279-15A3-4278 | Internet outbound Delhi to APAC excluding Korea/Indonesia | rounded 0 GiB | 0.03 |
| 5D31-95B0-C174 | Google Services inbound Delhi to Delhi | 0.17 GiB | 0.00 |
| 64AA-1252-8212 | Ubuntu 24.04 LTS licensing | 397.59 hours | 0.00 |
| 849E-6CE2-3D9B | Internet inbound Americas to Delhi | 0.41 GiB | 0.00 |
| 9428-46F3-3796 | Outbound carrier peering, APAC | rounded 0 GiB | 0.04 |
| 9BF8-CD36-F9B8 | Network Analyzer resource hours | 140 count | 14.74 |
| BDBA-22FA-3925 | Internet to Google Cloud Performance resource hours | 140 count | 10.72 |
| C054-7F72-A02E | Standard VM external IP | 397.59 hours | 0.00 |
| D9AD-28F8-05D8 | Topology and Google Cloud Performance resource hours | 140 count | 14.74 |
| DE0D-57DD-929C | Internet outbound Delhi to EMEA | rounded 0 GiB | 0.00 |
| E709-23E2-DCD9 | Balanced PD capacity in Delhi | 55.22 GiB-month | 627.82 |
| F3B9-BEC4-E49B | Internet inbound APAC excluding Korea/Indonesia to Delhi | 1.03 GiB | 0.00 |
| F9CF-797A-6DA6 | Internet outbound Delhi to Americas | rounded 0 GiB | 0.02 |
| 0B33-C7D0-C5A9 | E2 RAM running in Delhi | 6,361.4 GiB-hours | 2,136.80 |
| 210D-FDFA-448C | E2 cores running in Delhi | 1,590.35 core-hours | 3,986.56 |

Negotiated savings are zero; savings programmes are blank. Other savings offset
the full INR **6,791.47**, leaving zero payable for this selected period. Positive
outbound charges with displayed `0 GiB` are retained: rounded usage is not zero
liability. The zero-cost IP row is an observation for this report, not a promise
that future IP usage is free.

## Reconciliation

| Component | INR |
| --- | ---: |
| E2 compute, CPU + RAM | 6,123.36 |
| Balanced persistent disk | 627.82 |
| Network Intelligence | 40.20 |
| Outbound network | 0.09 |
| **Selected-period total** | **6,791.47** |
| Initial trial credit | 28,693.88 |
| Newly reported remaining trial credit | 21,610.67 |
| **All-time credit decrease** | **7,083.21** |
| **Difference not attributable from this period table** | **291.74** |

Do not book INR 7,083.21 entirely as VM compute, add the credit offset as a second
charge, or label INR 291.74 a billing error. It may reflect usage outside this date
range, reporting updates or other adjustments; this table does not prove the cause.
The new table supersedes the earlier partial report and its INR 18.37 difference.
The earlier current balance was INR 22,011.16: the balance fell INR 400.49 while the
same labelled period's reported costs rose INR 127.12. Thus both observations have
changed, and mixing old SKU rows with the newer balance is invalid.

The received report does not give a capture timestamp or reporting timezone. Its
balance is recorded as a new operator observation, not invented provider real-time
evidence. A final ledger import needs refreshed identity/balance and clear charge
coverage through its reconciliation cutoff; the model's `observed_at` must not be
fabricated to bypass freshness rules.

## Measured 30-day planning estimate, not a guaranteed bill

CPU and RAM independently imply the same running duration:

```text
1,590.35 core-hours / 4 cores = 397.5875 VM-hours
6,361.4 GiB-hours / 16 GiB   = 397.5875 VM-hours
```

That is consistent with the approved e2-standard-4 shape; it does not itself prove
a numeric VM ID. The license/IP rows show the same duration rounded to 397.59 hours.
Using Decimal arithmetic and projecting 720 hours (30 days):

```text
compute = (3986.56 + 2136.80) / 397.5875 * 720 = INR 11,088.93
disk    = 627.82 / 397.59 * 720                = INR  1,136.93
network = 40.29 / 397.59 * 720                 = INR     72.96
total                                               INR 12,298.82
```

The disk/network projection assumes their observed average persists across the
same hours. Disk GiB-month and network counters are rounded; exact unit prices,
report timezone and future traffic were not independently queried here. Actual
fees, traffic, retained disks and service placement can change the total. At these
same averages, 31 days would be INR **12,708.78**; this does **not** authorize an
extra day or alter the fixed stop.

This measured-rate estimate is below the approved INR 14,000 initial hosting
ceiling. Its estimated draw beyond the original INR 11,000 is INR **1,298.82**, not
the full INR 3,000 reserve allowance. INR **1,701.18** of that allowance is estimated
unused, but must not be released/repurposed before final reconciliation.

The INR **8,500 Vertex allocation** and INR **6,000 future reserve** remain
protected. INR 21,610.67 minus these two allocations leaves INR **7,110.67** of
reported remaining credit outside them, not new spending authority. The hosting
ceiling and VM STOP remain **2026-10-19T17:24:32Z**. Stopping compute does not delete
the persistent disk or guarantee its charges cease. Retention/deletion must follow
the authorized data policy and must be budgeted separately.

## What this closes and what it does not

- Closes the missing-six-SKU-rows request and arithmetic for the selected period.
- Supplies a newer operator-reported credit balance and a measured monthly estimate.
- Does not reconcile the INR 291.74 difference, establish live ledger enforcement,
  prove the final 30-day charge, apply migrations 016–022, or approve new resources.
- Safe toolchain implementation/proof preparation can continue independently.

See [cost ledger admission requirements](CLOUD_COST_LEDGER.md) and
[hosting runbook](GCP_DELHI_HOSTING.md). Google explains that promotional credits
offset bills in [Billing reports: savings and credits](https://docs.cloud.google.com/billing/docs/how-to/reports/savings-and-credits).

## 13 October follow-up: estimated planning, not new spending authority

The user cannot refresh the portal report now and authorized using a cautious
estimate for continued preparation. The connected read-only APIs independently
confirmed the expected project/billing-account link, `billingEnabled: true`, and
an open account. They did **not** confirm Free Trial status, expiry or promotional
balance. The standard [BillingAccount resource](https://docs.cloud.google.com/billing/docs/reference/rest/v1/billingAccounts)
has no remaining-promotional-credit field. A separate metadata-only BigQuery API
read found zero datasets in this project; no export was created, no API enabled,
and no billed query was run. Exports in other projects were not ruled out.

The reproducible `scripts.estimate_delhi_credit` scenario uses:

- The last operator-reported INR 21,610.67 balance; capture time remains unknown.
- **Assumed** debit anchor 5 October 00:00 UTC, not a manufactured `observed_at`.
- INR 500/day, above the earlier INR 13,850.56/30-day quote's approximate INR 462/day.
- INR 500 additional uncertainty reserve, not a new allocation or charge.
- The unchanged compute STOP, 19 October 2026 17:24:32 UTC.

At 12 October 19:42 UTC / 13 October 01:12 IST, this produces approximately
**INR 17,200**; use a rounded-down **INR 17,000 planning figure**. The same stress
scenario at STOP produces **INR 13,747.98**, below the protected INR 14,500 combined
Vertex/future-reserve allocations by **INR 752.02**. These are estimates under
stated assumptions, not actual balances, guaranteed lower bounds or final bills.
Unknown other-project/service usage could reduce the balance further. Conversely,
the earlier measured INR 12,298.82/30-day rate is lower than this stress allowance;
neither scenario establishes which actual final charge will occur.

Keep the existing INR 14,000 hosting ceiling, INR 8,500 Vertex allocation and
INR 6,000 future reserve. Do not release the previously estimated unused hosting
earmark, transfer from protected allocations, extend the VM, or enable paid billing.
Stopping the VM does not stop retained-disk charges, so the scenario continues
debits after STOP rather than silently treating storage as free.

`app/coding/credit_planning.py` returns an explicitly non-authorizing scenario,
with no `observed_at` and `ledger_admission_allowed: false`. Its schema is rejected
by `CloudCreditSnapshot`; the verified-balance admission and freshness requirements
remain unchanged. Non-spending implementation, source verification, tests and
preparation can continue. New cloud liabilities and funded controller activation
still require reconciliation, not treating the estimate as verified evidence.

Verification: 35 focused planning/budget tests and all 1,635 coding tests passed
without skips against local PostgreSQL (66.19 seconds). Lint, medium/high security
and whitespace checks passed. One existing Google SDK deprecation warning remains.
