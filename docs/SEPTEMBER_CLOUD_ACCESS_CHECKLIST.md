# Access needed for the September architecture

No cloud resources, training jobs or paid inference are created by reading this guide.
Never paste API keys, service-account JSON, SSH private keys or database passwords in chat.

## 1. Find the GCP project ID

Received from the user's console selection on 2026-09-13:

- Project name: My First Project
- Project ID: `project-ee0c1944-144c-4364-ad4`
- Organization: `achintyat256-org` (`342585805923`)
- Billing account name: My Billing Account
- Billing account ID: `01C9EC-FBE657-88466A`

Live CLI verification now matches project number `454922895512`, organization parent
and the open billing account above. Project and billing-link discovery are complete;
do not ask the user to repeat them. See [the preflight evidence](GCP_PREFLIGHT_2026_09.md).
The navigation below is retained as a reference, not an outstanding user task.

1. Open [Google Cloud Console](https://console.cloud.google.com/).
2. Sign in with the account used for the GCP trial. The supplied history names
   `achintyat256@gmail.com`; this is historical context, not a live verification.
3. Click the project selector beside the Google Cloud logo.
4. Find the relevant project and copy its **Project ID**, not only its display name.
   The history calls it “My First Project” with project number `454922895512`.
5. If necessary open [that historical project's dashboard](https://console.cloud.google.com/home/dashboard?project=454922895512)
   and inspect Project info. If access is denied, do not create another project blindly;
   check which account/project owns the credit and resources.

The ID, project number and region are not secrets. These identify the exact target and
prevent us from provisioning into another account by mistake.

## 2. Confirm remaining credit and expiry

The user supplied INR 28,694 and expiry 21 November 2026 from the billing console.
Those values were not independently returned by the billing-account API. Do not ask
for the same screenshot again; refresh balance evidence before spending.

1. Open [Cloud Billing](https://console.cloud.google.com/billing).
2. Select the billing account linked to that project.
3. On Overview, record remaining promotional credit, expiry, and whether the account is
   a Free trial account or a Paid account. Do not click Activate/Upgrade for this check.
4. Provide those non-secret values. The 2026-09-19 approved envelopes are:
   - INR 14,000: initial 30-day hosting planning ceiling, including ancillary costs.
     Up to INR 3,000 comes only from the original future cloud reserve; use less if possible.
   - INR 6,000: remaining protected future cloud deployment reserve.
   - INR 8,500: Vertex tuning, evaluation and tuned inference combined.

An allocation is not evidence of current remaining credit. Billing alerts do not stop
charges, and a smaller runtime schedule does not extend credit expiry.

## 3. Choose the deployment location

Read-only quota checks found E2 CPU quota 0 in Mumbai, 24 in Delhi and 24 in Iowa.
Delhi `e2-standard-4` (4 vCPU / 16 GiB) is approved as of 2026-09-19. The
[cost comparison and limited reallocation](GCP_PREFLIGHT_2026_09.md) record the
INR 13,850.56 estimate and INR 14,000 planning ceiling. The user reconfirmed current
credit and the VM now exists; see [live hosting evidence](GCP_DELHI_HOSTING.md).
No further region/shape decision is needed. Prior zero-spend evidence predates creation.

The region comparison is complete. Do not assume that Mumbai, Delhi or us-central1 is
always mandatory or cheapest.
The selected model's tuning/inference availability and any data-residency constraints
must also be checked. A VM size and estimate will be recorded before provisioning;
INR 11,000 does not itself guarantee a month of hosting.

If you already created a VM, provide its name, zone, operating system and whether it
contains existing data. If Coolify is installed, provide only its URL and version now.
We should reuse a suitable target rather than create a duplicate chargeable instance.

## 4. Grant access only after the target is known

Google Cloud CLI is installed and authenticated as `achintyat256@gmail.com` using
the named `google-connector-preflight` configuration. No application-default credential
login was performed. This CLI credential has the user's existing IAM authority, not
read-only enforcement; operations in this preflight were read-only. For runtime access,
prefer scoped workload identity over downloading an unrestricted service-account key.
Do not assign Project Editor, broad administrator roles, or public SSH/dashboard access
merely because the historical transcript recommended it.

Google Workspace OAuth, Gemini Developer API, Vertex identity, GitHub App authority,
Coolify API access and SSH identity are separate. Possessing one does not grant the others.

## 5. Resolve the model-training conflict

The latest user preference keeps AI Studio-assigned roles on its free tier and
Vertex-assigned roles on Vertex. That routing preference is preserved. However,
[Google's official availability page](https://ai.google.dev/gemini-api/docs/model-tuning)
states that fine-tuning is unavailable in AI Studio and the Gemini API.

Therefore:

- Free AI Studio roles can use supported **base** models with prompts, schemas, tools,
  OKF and retrieval; none of those mechanisms should be labeled weight fine-tuning.
- Supported Vertex tuning produces versioned Vertex endpoints, with separate inference
  charges inside the INR 8,500 combined envelope.
- An API key is an authentication credential, not a trained model. Training a Vertex
  endpoint does not make it callable as a free AI Studio tuned model.

The user accepted free base-model alternatives for the former AI Studio SFT roles
and supplied the project's quota table. Preserve Vertex's broader SFT/AI-feedback/
verifiable-feedback objectives; actual method/model eligibility must be resolved
before training. No provider migration should rely on unsupported free tuning claims.

Completed 2026-09-19: a restricted key from the explicitly approved separate unbilled
project `gca-free-models-454922895512` is saved as `RUNTIME_API_KEY` in Git-ignored
`.env.local` (0600). The Gemini 3.5 Flash-Lite tool-call probe passed with 69 input and
25 output tokens. Do not request the key again. This solves the original project's
prepay access blocker for the tested call, not private-data eligibility or production
model routing. See [the updated preflight evidence](GCP_PREFLIGHT_2026_09.md).

## Current code evidence

The optional Vertex backend and role selectors are implemented and tested locally;
they are not enabled in production. `app/coding/cloud_budget.py` is a pure preflight
evaluator: it does not reserve money, impose a provider billing cap, or authorize any
cloud action. Atomic persistent reservations, adapters and live validation remain
tracked in Sprints 61–68 of `project_progress_upgrade.md`.
