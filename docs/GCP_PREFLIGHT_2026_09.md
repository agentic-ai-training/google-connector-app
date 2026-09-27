# GCP access and hosting preflight — September 2026

Status: VM/limited reserve use approved and VM provisioned on 2026-09-19 after user
reconfirmed INR 28,694 unspent trial credit. Host setup continued 2026-09-20. No training
job, paid inference, billing upgrade or core-platform production deployment performed.
Standard E2/disk/snapshot/IP rates rechecked at 2026-09-19T07:00:00Z catalog pricing.

## Verified identity and resources

- Google Cloud CLI 584.0.0 installed locally through Homebrew.
- Authorized account: `achintyat256@gmail.com`.
- Named CLI configuration: `google-connector-preflight`; default configuration not replaced.
- Project: `project-ee0c1944-144c-4364-ad4`, number `454922895512`, ACTIVE.
- Organization parent: `342585805923`.
- Billing link: `billingAccounts/01C9EC-FBE657-88466A`, billing enabled; account open.
- Compute Engine and Vertex AI APIs already enabled.
- Project-wide VM, disk and reserved address lists returned no resources.
- CLI login uses the account's existing IAM permissions; it is not a read-only credential.
  Only read operations were used in this preflight. No IAM grants were changed.
- This login did not configure application-default credentials for application SDKs.

User-supplied console evidence, not returned by the billing-account API: INR 28,694
trial credit and expiry 21 November 2026. The original allocation was INR 11,000
hosting, INR 9,000 protected reserve, and INR 8,500 Vertex; see the explicitly approved
limited hosting reallocation below. INR 194 remains unallocated. All promotional
credit, including the reserve, shares its expiry.

## Quota and configuration checks

| Region | E2 vCPU quota | Used |
| --- | ---: | ---: |
| Mumbai `asia-south1` | 0 | 0 |
| Delhi `asia-south2` | 24 | 0 |
| Iowa `us-central1` | 24 | 0 |

Project-wide CPU quota: 32, used 0. Delhi machine-type describe calls confirmed
`e2-standard-2` (2 vCPU/8 GiB), `e2-custom-2-12288` (2 vCPU/12 GiB), and the earlier
4-vCPU/16-GiB option is an E2 standard shape. Catalog/quotas do not guarantee physical
capacity, permissions for creation, or satisfaction of every organization policy.
Delhi is now approved and the VM is deployed. Read-only revalidation on 2026-09-19
confirmed `asia-south2-a` UP and `e2-standard-4` at 4 vCPU / 16,384 MiB.

## Price evidence

Read from the authenticated public Cloud Billing Catalog endpoint:
`GET https://cloudbilling.googleapis.com/v1/services/6F81-5844-456A/skus?currencyCode=INR`.
All pages were read (32,873 SKUs). This is list-price evidence, not a billing-account
invoice or a guaranteed upper-bound quote. No tokens or credentials are recorded here.

| Component | SKU | INR rate |
| --- | --- | ---: |
| Delhi E2 standard vCPU | `210D-FDFA-448C` | 2.503212118 / hour |
| Delhi E2 standard RAM | `0B33-C7D0-C5A9` | 0.335431742 / GiB-hour |
| Delhi E2 custom vCPU | `E4A0-2D5F-084A` | 2.628372246 / hour |
| Delhi E2 custom RAM | `9B29-4E63-CD5C` | 0.352203329 / GiB-hour |
| Delhi zonal balanced PD | `E709-23E2-DCD9` | 11.4654 / GiB-month |
| Delhi standard snapshot storage | `246B-6075-A49D` | 5.7327 / GiB-month |
| Standard VM external IP | `C054-7F72-A02E` | 0.477725 / hour after initial tier |

The IP SKU returned a zero-price tier for the first 720 hours, but the public network
pricing page currently describes one free hour per month. Do not rely on the larger
allowance without reconciliation. This estimate charges every hour at the paid rate.

## Thirty-day comparison

Assumptions: on-demand Linux VM running 720 hours; 100 GiB balanced disk; average
50 GiB billable standard snapshot storage; one public IPv4 allowance; INR 1,000
unpriced contingency for egress and other variable usage. No commitment, Spot discount,
GPU, paid OS license, managed database, load balancer or additional VM assumed.

| Option | VM only | Disk/snapshot/IP allowance | Variable allowance | Estimated total |
| --- | ---: | ---: | ---: | ---: |
| 2 vCPU / 8 GiB | 5,536.71 | 1,777.14 | 1,000 | 8,313.85 |
| 2 vCPU / 12 GiB custom | 6,827.89 | 1,777.14 | 1,000 | 9,605.03 |
| 4 vCPU / 16 GiB | 11,073.42 | 1,777.14 | 1,000 | 13,850.56 |

Calculation: `720 * (vCPU_count * core_rate + RAM_GiB * RAM_rate)` plus storage
and allowances. Taxes, account-specific adjustments, price changes and usage beyond
these assumptions are not established. A 31-day period costs more. INR 1,000 is a
contingency, not a measured egress quote or an enforced spending ceiling.

Recommendation for discussion: 2 vCPU / 12 GiB provides more RAM for Coolify and a
small generated-app pilot; 2 vCPU / 8 GiB preserves a larger financial buffer.
Neither has been capacity-tested. Limit concurrent builds, bound container resources,
and measure memory/CPU before adding applications. Do not assume either VM can host
the entire proposed Kafka/Temporal/Langfuse/LLM/coding stack simultaneously. This
estimate covers the generated-app host, not migration of the existing core platform.
No hosting budget reservation or VM creation has been made. The smaller shapes are
alternatives only: the user reaffirmed that the originally selected shape was
`e2-standard-4` (4 vCPU / 16 GiB). Do not silently resize or reallocate protected funds.

## Approved minimal reserve draw — 2026-09-19

The user approved the Delhi `e2-standard-4`, explicitly funding the hosting shortfall
only from the future cloud reserve, never the Vertex envelope. This supersedes the
earlier unresolved-choice language and no-borrowing rule only for this initial term.

| Envelope | New planning ceiling / protected balance (INR) |
| --- | ---: |
| Hosting, initial 30-day term | 14,000 |
| Remaining protected future reserve | 6,000 |
| Vertex tuning/evaluation/inference, unchanged | 8,500 |
| Total allocations | 28,500 |

- Estimated 30-day hosting including the INR 1,000 variable allowance: INR 13,850.56.
- Estimated draw above the original hosting allocation: INR 2,850.56; maximum
  earmarked draw INR 3,000. This is an allocation, not an actual transfer or charge.
- Do not use the full INR 3,000 if actual eligible hosting cost needs less. Release
  unused earmarked credit back to the protected reserve after reconciliation.
- Do not extend to 31 days or silently increase resources/allocations. No Vertex
  training job, model routing change or billing upgrade is authorized by this approval.
- Provider billing budgets are alerts, not hard caps. The local evaluator is also
  only preflight; it does not enforce live spend. A fixed provider STOP timestamp is
  planned for the initial 30-day VM term. Disk and snapshot storage continue billing
  after STOP until explicitly retired; never delete user data to enforce a cost cap.
- Before any chargeable create, obtain current promotional balance/trial-mode evidence.
  The billing API verifies open INR account and billing link, not promotional balance.
  User reconfirmed unchanged INR 28,694 trial balance before VM creation. This is not
  an API verification and must not be reused as a fresh zero-spend balance thereafter.
- Prepared provisioning/security checklist: [Delhi hosting runbook](GCP_DELHI_HOSTING.md).
- Pure budget tests: 17 passed, including protection of unreserved Vertex funds when
  unrelated cloud usage has reduced the real remaining balance.

## Free Gemini project — verified 2026-09-19

The original hosting project's key returned `429 RESOURCE_EXHAUSTED` with a prepay
credit error despite the user-visible Free-tier label. Following explicit approval,
a separate project was created for billing isolation, not to multiply quotas:

- Display name: `google-connector-free-models`.
- Project ID: `gca-free-models-454922895512`; project number: `843825281664`.
- Same organization: `342585805923`.
- Billing API verification: `billingEnabled=false`, empty `billingAccountName`.
- Enabled Gemini Developer API and API Keys API; no billing linked or upgraded.
- Active key resource: `projects/843825281664/locations/global/keys/gca-runtime-free-v2`.
- API restriction: `generativelanguage.googleapis.com` only.
- Initial unused key was deleted after the CLI unexpectedly emitted it in operation
  output. Replacement key creation/retrieval output was captured privately.
- Replacement saved as `RUNTIME_API_KEY` in Git-ignored `.env.local`, mode 0600.
  The original hosting project's remote key was not revoked or modified.
- Original discovery listed Gemini 3.8/3.7/3.6 Flash and 3.5/3.1 Flash-Lite.
- Live probe repeated from saved local settings: Gemini 3.5 Flash-Lite returned exactly
  one `runtime_probe_echo` call with the expected nonsensitive nonce; 69 input tokens,
  25 output tokens. Tracing was disabled. No Workspace operation was performed.
- Model/backend selection for that test was process-local; production configuration
  and the persistent role/model chain were not changed. One successful call does not
  establish full workflow reliability or every model's quota.

The prior project's quota screenshot is not proof of this project's quotas. Private
data-processing suitability and production release checks remain outstanding.

## Remaining activation checks

- Key storage and a nonsensitive tool-call probe are complete as described above.
  Verify the new project's actual quota and data terms before activation; do not send
  private Workspace content merely because the harmless credential probe passed.
- Keep Vertex SFT, AI-feedback/preferences, verifiable rewards and actual training
  distinct. Supported model/method and production-use eligibility remain unresolved;
  do not silently turn the original learning plan into SFT only.
- VM choice and limited reserve draw are now approved as above. Verify current credit,
  complete security/budget/retention controls and load assumptions before provisioning;
  do not mistake an allocation or preparation checklist for a created/running server.

Sources: [Compute pricing](https://cloud.google.com/products/compute/pricing),
[disk pricing](https://cloud.google.com/compute/disks-image-pricing),
[network pricing](https://cloud.google.com/vpc/network-pricing).
