# Delhi hosting: updated operator billing report

Received **13 October 2026, approximately 04:59 UTC / 10:29 IST**. This is the
receipt time of the user's pasted report, not an invented provider capture time.
Selected period: **19 September–12 October 2026**. The intended filter is the
existing project `project-ee0c1944-144c-4364-ad4`; the user explicitly confirmed
it was copied just now for this project. This is a fresh operator attestation,
not an independently downloaded provider billing export. No new billing authority,
provider resource, paid model call or protected-budget transfer is implied.

## All 17 reported SKU rows

| SKU ID | SKU | Usage | Usage cost (INR) |
| --- | --- | --- | ---: |
| 0B29-DA5B-5E84 | Inbound carrier peering, APAC | 0.68 GiB | 0.00 |
| 5D31-95B0-C174 | Google Services inbound Delhi–Delhi | 0.78 GiB | 0.00 |
| 615A-2683-CA35 | Google Services outbound Delhi–Delhi | 0.01 GiB | 0.00 |
| 64AA-1252-8212 | Ubuntu 24.04 LTS licensing | 552.96 hours | 0.00 |
| 849E-6CE2-3D9B | Internet inbound Americas–Delhi | 0.49 GiB | 0.00 |
| 9428-46F3-3796 | Outbound carrier peering, APAC | 0.01 GiB | 0.06 |
| 9BF8-CD36-F9B8 | Network Analyzer resource hours | 188 count | 19.80 |
| BDBA-22FA-3925 | Internet to Google Cloud Performance resource hours | 188 count | 14.40 |
| C054-7F72-A02E | Standard VM external IP | 552.96 hours | 0.00 |
| D9AD-28F8-05D8 | Topology and Google Cloud Performance resource hours | 188 count | 19.80 |
| DE0D-57DD-929C | Internet outbound Delhi–EMEA | rounded 0 GiB | 0.00 |
| E709-23E2-DCD9 | Balanced PD capacity in Delhi | 76.8 GiB-month | 868.37 |
| F3B9-BEC4-E49B | Internet inbound APAC excluding Korea/Indonesia–Delhi | 1.43 GiB | 0.00 |
| 3279-15A3-4278 | Internet outbound Delhi–APAC excluding Korea/Indonesia | rounded 0 GiB | 0.04 |
| F9CF-797A-6DA6 | Internet outbound Delhi–Americas | rounded 0 GiB | 0.03 |
| 0B33-C7D0-C5A9 | E2 RAM in Delhi | 8,847.43 GiB-hours | 2,974.57 |
| 210D-FDFA-448C | E2 cores in Delhi | 2,211.86 core-hours | 5,549.55 |

The account display says **Free trial credits: INR 19,247.60 remaining out of
INR 28,693.88**. The zero payable subtotal results from credit offsets; it does
not prove zero resource usage. Displayed rounded `0 GiB` rows with positive charges
are retained. No Vertex SKU appears in this selected table; that alone does not
establish absence of Vertex/other-project usage outside this scope.

## Reconciliation, with rounding and coverage differences preserved

| Item | INR |
| --- | ---: |
| CPU + RAM | 8,524.12 |
| Balanced disk | 868.37 |
| Network Intelligence | 54.00 |
| Outbound network | 0.13 |
| **Sum of displayed SKU rows** | **9,446.62** |
| **Selected-period headline/credit-offset figure** | **9,446.64** |
| Headline minus displayed rows | 0.02 |
| Initial credit minus current reported balance | 9,446.28 |
| Credit decrease minus selected-period headline | -0.36 |

The two-paise row/headline difference may be display rounding; the 36-paise
balance/headline difference may be reporting timing, scope or adjustment. Neither
cause is independently proven. Do not silently rewrite either observation or
label either difference a billing error. The earlier INR 291.74 discrepancy is
historical, not an extra charge to add again to this newer report.

The separate **19 September–13 October forecast**, INR 8,804.89, is not accrued
spend and is not substituted for the selected-period figure. Its being below the
selected-period headline is another reason not to combine the two views.

## Planning against unchanged envelopes

Compute quantities independently imply approximately the same duration:

```text
CPU: 2211.86 / 4  = 552.965 hours
RAM: 8847.43 / 16 = 552.964375 hours
```

Their 2.25-second difference is consistent with rounded displayed quantities,
not proof of an extra machine. Projecting each component over 720 hours using
the CPU-implied duration and Decimal arithmetic gives:

```text
compute: 8524.12 / 552.965 * 720 = INR 11,099.01
disk:     868.37 / 552.965 * 720 = INR  1,130.68
network:   54.13 / 552.965 * 720 = INR     70.48
total from unrounded component calculations = INR 12,300.18
```

This is a measured-rate planning estimate, not a guaranteed final charge. Rounded
component amounts sum to INR 12,300.17; rounding only the aggregate yields
INR 12,300.18. Preserve that one-paise distinction rather than adjusting a row.

The reported balance leaves **INR 4,747.60** above the protected INR 8,500 Vertex
plus INR 6,000 future-reserve allocations. Using the conservative larger
INR 9,446.64 headline, the still-unspent part of the INR 14,000 hosting ceiling is
INR 4,553.36. Reserving that whole remainder would leave only **INR 194.24** above
the protected allocations. This is not new spending authority or a generous buffer.
Provider reporting lag, further services/egress and retained-disk costs still matter.

At the measured rate, projected remaining term usage is INR 2,853.54, leaving
approximately INR 16,394.06 after that usage if nothing else consumes credit.
The estimated draw beyond the original INR 11,000 hosting allocation is
INR 1,300.18, with INR 1,699.82 of the permitted reserve earmark projected unused.
Do not release it before actual final reconciliation.

The prior rounded INR 17,000 scenario is superseded as the latest reported
balance. `scripts.estimate_delhi_credit` now uses this report for its separately
labelled stress scenario, still with no ledger admission or provider-write authority.
Existing-VM import must preserve true observation/cutoff provenance and current
provider identity; no backdating, fabricated balance, or freshness bypass.

VM STOP remains **19 October 2026, 17:24:32 UTC**. The fixed 100 GiB disk may
continue charging after compute stops; backup, retention and any disk deletion
must follow separately authorized data/budget policy. No implicit extension,
billing upgrade or transfer from Vertex/future reserve is authorized here.
