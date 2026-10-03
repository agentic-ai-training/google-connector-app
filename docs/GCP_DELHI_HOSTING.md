# Delhi generated-application host — approved provisioning runbook

## Status and spending authority

Provisioned 2026-09-19; setup continued 2026-09-20. User reconfirmed INR 28,694
unspent trial balance before creation; the billing API does not expose those fields.
On 2026-09-29 the user supplied a newer Billing screen: INR 24,938 promotional
credit remains, Free trial, 53 days remaining, expiry 2026-11-21. The difference
from the earlier snapshot is INR 3,756 of **total** credit use, not verified
Compute/VM cost. Keep VM, disk, network and other-service charges unattributed
until the itemized Billing report is checked; do not book the INR 3,756 entirely
against hosting or spend the protected Vertex/reserve allocations on that basis.
On 2026-09-30 the user supplied a newer 1–29 September Billing report: INR
24,544.99 remains out of INR 28,693.88. The report showed INR 4,096.68
Compute Engine and INR 23.50 Networking usage offset to INR 0 payable. Trial
credit nonetheless declined by INR 4,148.89 overall; INR 28.71 is not
accounted for by those two displayed rows. The report was not shown filtered
to this project and broken down by VM/disk SKU, so do not attribute its full
amount to this host or import it as a reconciled provider charge. See
[cost ledger](CLOUD_COST_LEDGER.md).
On 2026-10-01, an authenticated read-only Cloud Billing API call showed this
account has only the target project linked. Current Compute inventory showed
only the approved VM and its 100-GiB disk, with no snapshots. Thus the displayed
September service totals can be associated with this project, but no SKU split,
post-29-September cost or fresh promotional balance was supplied. The audited
VM import schema was corrected to the exact initial ₹28,693.88; it remains
unapplied until fresh, attributable evidence is available.
Read-only instance describe on 2026-09-29 still reported RUNNING,
`e2-standard-4`, and the fixed 2026-10-19 stop timestamp.
The same fixed-target read-only describe on 2026-09-30 again returned VM ID
`822115653245959101`, RUNNING, e2-standard-4, and the unchanged STOP timestamp.
On 2026-09-29, `docker ps` through pinned IAP SSH reported Coolify, PostgreSQL,
Redis, realtime, proxy and sentinel all `healthy` after nine days uptime. This
is infrastructure health only, not a generated-application or backup proof.
The same six containers were still healthy on 2026-09-30. Docker network inventory
still contained only `bridge`, `coolify`, `host` and `none`; no generated-app
destination/network existed at that checkpoint. The Coolify database still had
zero projects before the scoped-token bootstrap, so no project or environment
was created in that step.
Read-only queries through pinned IAP SSH into the installed Coolify application on
2026-09-29 found one server and one standalone Docker destination, both with
provider numeric ID `0`; there were no Coolify projects or environments. One user
and one team row exist, consistent with the verified 2026-09-20 administrator
registration noted below; current interactive login was not retested. The
`personal_access_tokens` table contained zero rows, so no scoped Coolify API
token existed at that checkpoint.
On 2026-09-30 the count was still zero before a scoped bootstrap. Four separate
team-0 tokens were then created by the operator-only
`scripts/bootstrap_coolify_scoped_tokens.php`, expiring exactly at the VM's fixed
2026-10-19T17:24:32Z STOP deadline: inventory `read` (ID 2), configuration
`read`+`write` (ID 3), deployment `deploy` (ID 4), and recovery
`read`+`read:sensitive` (ID 5). No `root` token was issued. Their plaintext was
copied only into ignored `.gca-local/coolify-*-deployment-v1.env` files in an
owner-only directory, each mode 0600; temporary container/VM files were removed.
Inventory, configuration and recovery tokens each passed an authenticated
read-only team/server probe over pinned IAP. The deploy-only token was **not**
used to start a Service, and the sensitive token was **not** exercised against
an actual Service's Compose. Rotation and connection to a durable controller
remain incomplete. This is credential bootstrap, not deployment proof.
After that private token check, an operator-only, one-shot API bootstrap created
Coolify project `mnv84tsssicw2y7dtogvbmwr` (`gca-generated-apps-pilot`) and
its `pilot` environment `pi3o2fpjo2ituxrppav8izj4` on 2026-09-30. A second
read-only invocation found both exact identities and issued no POST. The bootstrap
is `scripts/bootstrap_coolify_pilot.py`; provider timeouts stop as uncertain.
This creates no Docker destination, network, Service, domain or generated app.
Later on 2026-09-30, the operator-only
`scripts/bootstrap_coolify_pilot_destination.py` created one separate pilot
destination: UUID `2rxr0cpp0x91g47o9flwcbzk`, network
`gca-app-login-pilot-20260930`, on the existing server. Coolify's private
database mapped it to standalone destination ID `1` and server ID `0`.
Read-only API replay found the same UUID without another POST. Docker confirmed
an attachable bridge network with zero containers. This destination has no app
and no proven east-west isolation; the Service controller separately pins
`connect_to_docker_network=false` so generated containers must not join a
predefined destination network. Before any live start, inspect actual network
membership, proxy routing and cross-app/core reachability.
On 2026-10-01 a read-only controller preflight initially rejected the target
because Coolify's server-detail API omitted numeric `id`. The private Coolify
database had already established server ID `0`, and destination ID `1` was
independently read back. The immutable target schema now pins both numeric IDs;
the controller accepts an omitted API server ID but rejects a conflicting one.
After this correction, the exact team/server/project/environment/destination
preflight passed through the private IAP tunnel with **no provider write**.
This is target-identity proof only; the cost ledger, trusted build, secrets,
Service creation/start and functional app checks are still outstanding.
The read-only inventory probe now resolves its scoped token through the
operator-only private-file resolver, which enforces file ownership/mode, an exact
registered reference and the 2026-10-19 token expiry without printing plaintext.
The local inventory file passed a no-network resolver check on 2026-10-01. The
first live probe exposed single-quoted bootstrap values; after strict quote
handling was added, the read-only IAP probe verified the exact team and pilot
target. The tunnel was then closed. The controller has not yet been wired to all
four scoped credentials in production.
The original destination uses Docker network `coolify`; it must not host
generated user applications. A separate pilot destination now exists, but real
east-west isolation proof is still required before any generated Service starts.
The earlier read-only queries created no resources. Project/environment bootstrap
is now verified, as is the pilot destination. Target registration and any Service
remain pending until the other admission gates are satisfied.
The user has already approved provisioning and the limited reserve draw. Do not ask
for duplicate region, machine-size or spending authority unless this scope changes.

- Project: `project-ee0c1944-144c-4364-ad4` (454922895512).
- Named CLI configuration: `google-connector-preflight`.
- Billing: `01C9EC-FBE657-88466A`, INR, open, project billing link verified.
- VM name: `gca-coolify-delhi-01`; zone: `asia-south2-a` (UP at preflight).
- Machine: `e2-standard-4`; 4 vCPU / 16,384 MiB, live machine-type verification passed.
- Image: Ubuntu 24.04 LTS amd64, `ubuntu-2404-noble-amd64-v20260918`,
  project `ubuntu-os-cloud`, READY. Recheck lifecycle/status immediately before create.
- Boot disk: 100 GiB zonal `pd-balanced`.
- Networking: one ephemeral IPv4; no Cloud NAT/load balancer/static IP by default.
- Initial term: exactly 30 days from provisioning, not an automatically renewing month.
- VM ID `822115653245959101`; created `2026-09-19T17:24:34.872Z`.
- Stored STOP deadline: `2026-10-19T17:24:32Z` (19 October, 22:54:32 IST).
- Budget alert ID: `449fc995-ee51-4a11-9755-42d9f4208999`, INR 14,000,
  project/Compute scoped, 50/75/90/100% actual-cost alerts, promotional credits excluded.
  Default billing IAM recipients retained; delivery has not been triggered/tested.
- Set an absolute `--termination-time` and `--instance-termination-action=STOP`.
  Do not use a relative runtime limit that resets whenever the VM restarts.
- Planning ceiling INR 14,000. Original hosting INR 11,000 plus at most INR 3,000
  reserve draw; expected estimate INR 13,850.56. Remaining reserve INR 6,000,
  Vertex INR 8,500 unchanged. Actual usage, not the full earmark, is charged.

## Pre-create gates

1. Obtain timestamped current credit/trial-mode evidence. Do not manufacture a fresh
   snapshot from an old balance. Validate the workload ends before credit expiry.
2. Re-list instances/disks/addresses and look for the exact target to avoid duplicate
   resources/charges. The 2026-09-19 read-only inventory has no VM in this project.
3. Verify machine/region, image availability, project/billing identity and permissions.
   No billing upgrade, payment, organization change, or broad IAM grant is permitted.
4. Record the exact start/end timestamps and run the pure budget evaluator with current
   spend/reservations. It is not a persistent reservation or a live spending controller.
5. Configure a dated hosting budget alert before VM creation; filter by this project
   and Compute service `6F81-5844-456A`, use INR, and exclude promotional credits from
   the cost calculation so free-trial usage is visible. Alert at 50%, 75%, 90%, 100%.
   Check recipients. Alerts can lag and never stop charges by themselves.
6. No extra paid services or 31-day renewal without reevaluating/authorizing the budget.

## Security and creation sequence

Verified live: dedicated VPC/subnet, IAP-only TCP 22 firewall, OS Login, project SSH
keys blocked, serial interactive access disabled, all three Shielded VM settings on,
no service account/scopes, 100 GiB persistent disk preserved on instance deletion.
SSH and sudo succeeded with pinned host key. Fingerprint from authenticated serial
output: `SHA256:xwgp7/eb5aOMrNhLynkhFBtafIIEK/hxIWoTADNcUJg`.
The admin tunnel is SSH, not a replacement terminal protocol. No public port 22 rule exists.

Coolify install was dispatched through systemd unit `gca-coolify-install`, pinned to
version `4.3.23`, automatic updates disabled, Docker pool `172.20.0.0/16` to avoid
the VPC subnet. Installer SHA256:
`8ef02dce49339208f5abc247bff0277c73d04538d7a36dcfd21331e314e0f2cd`.
Installation completed with exit status 0. Coolify, DB, Redis, realtime, proxy and
sentinel are healthy. First administrator created through the normal registration
flow; application evidence confirms exactly one user and registration disabled.
Credentials are in Git-ignored `.gca-local/coolify-admin.env`, mode 0600, under a
0700 directory. No password was printed or placed in tracked configuration.
All dashboard ingress remains blocked by GCP firewall. Tests from the public client
could not reach TCP 22, 8000, 5432 or 6379. SSH through IAP succeeded.

The dashboard can be accessed privately at `http://127.0.0.1:18000` while this tunnel
is running on the operator's machine:

```sh
gcloud --configuration=google-connector-preflight compute ssh gca-coolify-delhi-01 \
  --zone=asia-south2-a --project=project-ee0c1944-144c-4364-ad4 \
  --tunnel-through-iap --strict-host-key-checking=yes -- \
  -N -L 127.0.0.1:18000:127.0.0.1:8000 \
  -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3
```

Local HTTP is transported inside encrypted SSH/IAP, not served over public plain HTTP.
The tunnel is an operator process, not a permanent public URL or production controller.
The pinned public host key is also in the local gcloud known-hosts file; a new operator
must verify identity rather than disable checking.

On 28 September 2026, a fixed-target read-only GCE probe again confirmed this VM
was `RUNNING`, with `e2-standard-4`, a ready 100-GiB `pd-balanced` boot disk and
the same stored STOP deadline. The local cost-ledger import now requires this
provider identity proof, fresh current promotional-credit evidence and attributable
accrued compute/disk charges. It has **not** been run against production, because
the latter two current billing observations are not yet established. A completed
identity probe alone is not authority to assume the full original credit remains.

## Bootstrap backup and restore evidence — 2026-09-20

- Created a custom-format dump of **only the new Coolify database**, not core Neon or
  any Workspace data. Restored it into fresh temporary `gca_restore_probe_20260920`.
- `pg_restore --exit-on-error` succeeded; restored user count was 1. Removed only that
  disposable verification database afterward. This is bootstrap restore proof, not a
  future generated-application backup/restore guarantee.
- Copied database dump and Coolify encryption/configuration `.env` to the operator's
  ignored `.gca-local/gca-bootstrap-backup/` directory (0700, files 0600). Contains secrets:
  never commit/upload to an unapproved provider or print its contents.
- The same bootstrap backup also remains in a private directory on the VM. It is not
  a scheduled off-machine backup service. Recurring retention/restore operations and
  generated-app backups remain pending.
- Scoped API credentials, automated controller connectivity, public application
  TLS/domain and end-to-end generated-app deployment are still pending. No root API
  token has been created; use per-team least-privilege credentials when integrating.

1. Create/reuse a dedicated custom-mode VPC `gca-generated-apps`, not the default
   network with its permissive existing rules. Create region-specific subnet
   `gca-delhi-apps`, candidate CIDR `10.42.0.0/24` after checking overlap.
2. Allow admin SSH only through IAP (`35.235.240.0/20`, TCP 22) and only to a dedicated
   VM target tag. Do not expose SSH, Docker, PostgreSQL, Redis or the Coolify dashboard
   to `0.0.0.0/0`. Existing application hosts must not be affected.
3. Use OS Login for administrator access, block project SSH keys, disable serial-port
   access, and enable Shielded VM vTPM/integrity/secure boot if the selected image supports it.
4. Create the VM with `--no-service-account --no-scopes`; generated apps must not gain
   a project service-account token from the metadata server. Do not copy API/provider
   keys, GCP ADC, core DB credentials or local `.env` files onto this host.
5. Use the fixed termination timestamp, STOP action and preserved persistent data.
   Do not replace STOP with DELETE. Restrict restart authority and verify the stored
   scheduling fields after creation.
6. Validate SSH host identity using provider-authenticated evidence before trusting
   the key. Connect through IAP; never disable host-key checking globally.
7. Install supported Docker/Coolify using reviewed official installation inputs.
   Protect the installer-generated secrets and installation logs. Pin/record the
   actual installed version and image digests; do not blindly pipe an unknown script.
8. Keep Coolify registration/admin access private via an SSH/IAP tunnel. Establish
   its first admin safely, back up its encryption key and disable open registration.
   No public domain has been provided; do not claim public TLS/domain setup is complete.
9. Open app ingress only when deploying an approved app; expose only HTTP/HTTPS as
   needed. No unrestricted arbitrary-code multi-tenant execution on this Docker host:
   the stronger sandbox boundary remains a separate implementation requirement.

## Verification and cost lifecycle

### Private API checkpoint — 21 September 2026 IST

The installed Coolify API is enabled, still reachable only through the existing
private administration path (no public port was opened). Inventory token ID `1`
belongs to team `0` (the default team named "Root Team"), but has **only `read`**,
not the `root`, `write`, `deploy`, or `read:sensitive` abilities. It expires on
27 September 2026 at 20:04:48 UTC. The plaintext is stored in ignored
`.gca-local/coolify-inventory.env`, mode 0600. Temporary remote copies were removed
after a successful authenticated API test. Do not print the file or add it to Git.

`app/coding/coolify.py` provides the bounded inventory transport. It disables HTTP
redirects and environment proxy inheritance, permits plain HTTP only on loopback
for the SSH tunnel, checks the token's actual team, bounds response size/time and
emits only a narrow server projection. Provider bodies and headers are not exposed
as error messages. This transport is internal to trusted controller code; it is not
a model tool or a public user-supplied URL fetcher.

With the IAP tunnel running, verify without exposing the credential:

```sh
source .venv/bin/activate
python3 -m scripts.probe_coolify_inventory
```

Live result: target `gca-delhi-coolify`, team verified, existing server UUID
`frdwr0tdglbolht3kqtokzqa`, Coolify-reported `reachable=true`, `usable=true`.
This verifies inventory access, not application behavior or deployment authority.
The lifecycle controller, immutable target registration, scoped deployment credential,
cost admission integration and generated-app behavioral proof remain pending.

The trusted operator bootstrap helper is `scripts/bootstrap_coolify_read_token.php`.
It refuses duplicate token creation and writes secrets to a private file rather than
stdout. Run it only through the authenticated Coolify Laravel runtime after reviewing
the operator and team; it must never be available to generated code or model tools.
Future rotation must verify the replacement before revoking the old token. The current
read token cannot be reused as deployment authority. See the official
[Coolify token permissions](https://coolify.io/docs/api/permissions).

### Remaining operational verification

On 27 September, gVisor release-20260921.0 was added and package-held on this same VM.
Docker default runtime was preserved; all six Coolify containers remained healthy.
Host isolation and synthetic valid/invalid Python validation probes passed without
private source or provider keys. No new VM or port exposure was introduced. This does
not activate production coding workers. See [isolated validation](ISOLATED_VALIDATION.md)
for exact image/runtime hashes, the corrected source-copy defect and remaining gates.
On 28 September IST, an abrupt-controller-crash probe passed and the scoped validation
reaper timer was enabled. Its root-owned policy is `/etc/gca-validation/policy.json`;
its durable private state is `/var/lib/gca-validation`. Do not delete that journal during
rollback or interpret an expired process as released capacity without reconciliation.

Read-back on 27 September 2026 confirmed instance ID `822115653245959101` remains
RUNNING as e2-standard-4, with STOP at `2026-10-19T17:24:32Z`. No new instance or
deadline extension was made during the deployment-admission work. This read-back
does not provide an updated promotional-credit balance or application health proof.

- Check real VM size, image, disk, firewall, absence of service account, metadata,
  scheduling deadline, connectivity and health. Store nonsensitive resource IDs.
- Test Coolify privately and verify dashboard ports remain blocked from the internet.
- Configure finite backup retention after data exists. The estimate assumes average
  50 GiB billable snapshots; retention days do not enforce a 50 GiB cap. Monitor actual
  retained bytes. Do not claim snapshot/restore protection before a restore test.
- Limit concurrent builds and container CPU/memory/disk growth; avoid installing the
  whole future orchestration stack on this one machine without measured capacity.
- Check actual incurred cost against estimates and protected allocations. Budget
  alerts and the pure preflight are NOT an atomic cost-reservation system.
- At the fixed deadline, STOP ends normal VM compute use. Disk, snapshots, and any
  subsequently reserved IP can still incur charges. Review retention with the user;
  never delete data automatically just to match the planning ceiling.
- If provisioning fails, inventory partially created resources, report them and their
  cost status, and remove only verified empty resources created by this operation.

Sources: [Coolify installation](https://coolify.io/docs/start-with-self-hosted),
[GCE runtime limits](https://docs.cloud.google.com/compute/docs/instances/limit-vm-runtime),
[billing budget alerts](https://docs.cloud.google.com/billing/docs/how-to/budgets).

### User-provided billing report — received 3 October 2026

The pasted report covers **1–29 September**, not current October usage. It shows
Compute Engine usage INR 4,096.68 and Networking INR 23.50, total INR 4,120.18,
offset by the same amount of trial-credit/other savings and net INR 0.00. This is
credit consumption, not zero-cost infrastructure. The trial balance is INR 24,544.99
of INR 28,693.88, implying INR 4,148.89 consumed overall. The INR 28.71 difference
is unexplained by that displayed service table; do not invent its service or period.
The September forecast is not a current October accrued-cost measurement.

Fresh project-filtered charges through today, separated by SKU (VM, disk, network
and other services), and a current trial balance are still required for attributable
funded production enrollment. Do not double-charge the net-zero credit-offset
invoice, extrapolate this partial period as a full month, spend the protected Vertex
allocation on hosting, or extend the fixed STOP deadline from this report.
