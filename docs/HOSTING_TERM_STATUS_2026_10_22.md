# Hosting term ended: read-only state and operator billing update

Checkpoint: 22 October 2026. This supersedes historical **current/running** language
in earlier hosting checkpoints; their recorded source/test evidence stays historical.

## Independently observed resource state

A read-only Compute Engine instance describe matched project
`project-ee0c1944-144c-4364-ad4`, VM `gca-coolify-delhi-01`, ID
`822115653245959101` and returned:

```text
status:             TERMINATED
lastStartTimestamp: 2026-09-19T10:24:45.771-07:00
lastStopTimestamp:  2026-10-19T10:25:32.869-07:00
```

The returned stop timestamp is **19 October, 17:25:32.869 UTC**. The approved
deadline remains **19 October, 17:24:32 UTC**. The API's terminal timestamp is not
the exact moment compute charging stopped, and does not alone establish which
actor caused the stop. Do not silently backdate it or claim an exact billing cutoff.

Bounded read-only inventory (limit 21, not reached) returned one 100-GiB balanced
disk, `gca-coolify-delhi-01`, READY and attached to this VM; no reserved addresses
and no snapshots were returned. No start, stop, delete, snapshot, image load, SSH,
source installation, disk attachment change or billing upgrade was performed.
The first minimal-environment describe was unconfirmed; repeating the read using
the existing transport's reviewed CLI environment succeeded. No credential rotation
or new authorization was required. No raw CLI stderr/credential was exposed.

The last observed installed source snapshot remains
`0d06e31b5b8b07faccdebc46bc0b508ad9aea3542a925de75f4597e755bb1a49`
from 13 October. It was not re-read from the stopped VM. Historical six-container
health checks are not current availability: Coolify/apps cannot serve while this
VM is stopped. The new scanner host module is local and has not been installed.

## New operator billing observation

Received 22 October 2026 in response to the scoped billing-report request. The
exact provider capture time and project-filter state were not separately attested;
this is the supplied operator observation, not an independent billing export.
Selected period: **1–21 October**, not the earlier 19 September–12 October period.

| Observation | INR |
| --- | ---: |
| Remaining trial credit | 16,375.42 |
| Original credit | 28,693.88 |
| All-time credit decrease | 12,318.46 |
| Compute Engine, selected period | 7,594.50 |
| Networking, selected period | 40.89 |
| Sum of displayed service rows | 7,635.39 |
| Selected-period headline | 7,635.40 |
| Headline minus displayed rows | 0.01 |
| Decrease since 13 October's reported 19,247.60 balance | 2,872.18 |
| Balance above protected 8,500 Vertex + 6,000 reserve | 1,875.42 |

The one-paise difference is retained, not assigned an unverified cause. The
₹12,744.12 October forecast is neither accrued spend nor a new budget. ₹0 payable
after credits does not mean zero resource usage. Do not subtract overlapping
selected-period reports, equate all-time credit decline with attributable VM spend,
or automatically register ₹1,875.42 as authorized hosting funds. Final attributable
SKU/cost coverage, unposted charges and protected allocations still need reconciliation.

Stopping compute does not remove all resource costs. Google documents that retained
disks continue charging; the retained 100-GiB disk is not deleted merely to improve
the budget. Its data/backup/retention decision requires separate authority.
[Google's stop/start documentation](https://docs.cloud.google.com/compute/docs/instances/stop-start-instance)
and [instance lifecycle](https://docs.cloud.google.com/compute/docs/instances/instance-lifecycle).

## Verification banner and next decisions

The supplied screen says: **“To avoid losing access to Google Cloud services, an
administrator must verify this account.”** It is not proof of an unpaid bill, a paid
upgrade, or the cause of the VM's earlier stop. Google's documentation describes
mandatory identity verification for India billing addresses and possible suspension
if incomplete. Exact required documents depend on the existing legal profile.
[Official verification instructions](https://docs.cloud.google.com/billing/docs/how-to/create-billing-account).

1. Open [this billing account overview](https://console.cloud.google.com/billing/01C9EC-FBE657-88466A/overview).
2. Sign in as its actual billing administrator and follow the verification banner.
3. Submit requested personal/legal documents **only to Google**, never in chat or Git.
   Use the real existing individual/business profile; do not invent an organization.
4. Do not select Upgrade/Activate paid billing to complete this project. If the flow
   requests a payment or paid conversion, stop and report the non-secret prompt.
5. After verification, a new proposed duration, explicit cost ceiling, fresh credit,
   final prior-term reconciliation and updated expiry/resource controls are required
   before any restart. No protected Vertex/reserve transfer or term extension is implied.

Local coding/testing can continue. Live scans/deployment are not authorized after
the old STOP; old databases and temporary credentials must not be treated as fresh.
