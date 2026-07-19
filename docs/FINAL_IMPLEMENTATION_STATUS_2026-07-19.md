# Upgrade implementation status — 2026-07-19

This report reconciles the approved implementation ledger with what is deployed,
what is locally verified, what intentionally requires real pilot data, and what
still requires an external account action.

## Implemented

- Durable user-scoped runs, typed plans, steps, append-only events, artifacts,
  approvals, cancellation, resume, leases, heartbeats, and PostgreSQL queue.
- Dependency-aware bounded execution, selective transient retries, deterministic
  Google-write idempotency, tool allowlists, read-after-write verification, and
  truthful partial completion/incident summaries.
- Gmail, Calendar/Meet, Drive, Docs, Sheets, Tasks, Contacts, and Chat tool
  surfaces with per-user OAuth scopes and encrypted stored credentials.
- Multi-user browser OAuth with PKCE and a frontend callback fragment that avoids
  leaking the application JWT in query logs.
- Source-aware RAG, tenant ACLs, hybrid full-text/vector retrieval, citations,
  incremental versioned indexing, asynchronous embedding jobs, and prompt-
  injection filtering.
- Public, human-approved OKF operational bundle, deterministic draft generator,
  trusted-only retrieval, schema/link/tool/secret/PII validation, and retrieval
  version telemetry.
- Protected Improvement Center, frozen proposal hashes, two-stage human approval,
  selected-user canaries, multi-objective evaluation, automatic rollback, feature
  flags, pilot controls, and a hard live-RL lock.
- Consented recursively sanitized trajectories with stable user-level
  train/validation/test splits, deletion/export, retention jobs, and read-only
  DBeaver reporting views.
- Local Prometheus/Grafana dashboards and alerts plus Neon-backed high-cardinality
  session, step, artifact, failure, token, tool, retrieval, canary, experiment,
  and masked security-audit panels.
- Legacy `/chat` rollback path, immutable deployment version labels, public GitHub
  CI, Railway API/worker deployment workflow, migration rollback, and runbooks.
- A no-network Google mutation simulator and replay suite for idempotency,
  dependency propagation, retry, partial failure, breaking-point detection, and
  compensation.
- Governed artifact cleanup requests and exact action-bound decisions for preserve,
  retry population, sharing rollback, Calendar cancellation, and safe deletion.
- Tenant-scoped filtered run history, full live plan/step/progress/approval UI,
  workflow/policy evaluation facts, low-risk validated bandit assignment, and
  sanitized human-triggered email/GitHub draft-proposal publication adapters.

## Verified state

- Production Neon: migration `007`; 14 reporting views; `dbeaver_analyst` can
  query reporting but not OAuth credentials.
- Tenant-safe production RAG import: 14,521 owner-scoped legacy chunks; unrelated
  users retrieve zero of those chunks.
- Production OKF: 12 trusted documents.
- Railway: API and dedicated worker are successful on immutable merge commit
  `ac1ee81afa8129b1342d8591a0bd8336401616e1`.
- Vercel frontend and `/api/health`: HTTP 200; OAuth login redirects to Google
  with the required Workspace and Meet scopes.
- LangSmith: project and production trace access verified read-only after deploy.
- GitHub Actions: PR #17 and the explicit main-branch CI run passed all backend,
  web, and Flutter jobs; deployment run `29698803558` passed for Railway and
  Vercel. GitHub did not automatically emit a push run for this merge, so the
  repository's supported `workflow_dispatch` entry points were used on `main`.
- Final local pre-merge gate: 57/57 backend unit/integration tests; 20/20 planner
  golden cases at 1.0 correctness; four/four mutation replays; candidate policy
  with zero regressions and promotion correctly blocked below 30 verified samples;
  Next lint/build; Flutter analyze/test/debug APK; npm and pip audits; full-repo
  Bandit medium/high; secret/history scans; migration 007 downgrade/upgrade;
  healthy Docker API/worker/PostgreSQL/Ollama; both Prometheus targets; 12 valid
  alerts; two provisioned Grafana dashboards; eight upgrade tables and 14 views.

## Intentionally data-gated work

These are not software defects and must not be manufactured from synthetic
production claims:

- 5–10, 20–30, 40–50, and 80–90-user rollout gates require actual consenting
  pilot users and enough completed control/candidate runs.
- Source-specific chunk-size, query transformation, reranker, and retrieval
  comparisons require labelled relevance judgments per Google source.
- Offline policy evaluation needs enough verified trajectories in each stable
  user-level split.
- Fine-tuning and reinforcement learning remain intentionally disabled until the
  approved consent, holdout, safety, cost, rollback, and evidence thresholds exist.
- Real external-write smoke tests need an explicit request describing the exact
  recipient, Chat space, content, time/timezone, and cleanup policy. Test doubles
  are used by CI so it never sends real email, Chat, invitations, or sharing.

## External blockers and exact completion steps

### Grafana Cloud and Railway Alloy

The Alloy service/configuration exists, but it is deliberately stopped because
all three Grafana Cloud remote-write values are absent:

1. In Grafana Cloud, open the stack, then **Connections -> Add new connection ->
   Hosted Prometheus -> Send Metrics -> Alloy**.
2. Copy the Prometheus remote-write URL, instance/user ID, and create an access
   policy token with `metrics:write` only.
3. In Railway, open the project and add these variables to the Alloy service:
   `GRAFANA_CLOUD_PROMETHEUS_URL`, `GRAFANA_CLOUD_PROMETHEUS_USERNAME`, and
   `GRAFANA_CLOUD_API_KEY`.
4. Do not paste the values into this public repository or chat.
5. Redeploy/start Alloy. Confirm its `/metrics`/health UI, then confirm both
   `google-connector-api` and `google-connector-worker` series in Grafana Cloud.
6. Import/provision the repository dashboards and configure private access for
   the Neon read-only datasource. Never expose it through anonymous/public access.

Until then, local Grafana works, but production telemetry retention in Grafana
Cloud cannot start when the laptop is off.

### Google OAuth pilot users

Google testing mode cannot programmatically self-add arbitrary test users. For the
approved pilot approach:

1. Open Google Cloud Console -> Google Auth Platform -> Audience.
2. Keep publishing status at Testing while the pilot remains under the platform's
   tester limit.
3. Add each consenting pilot Google account under Test users before sign-in.
4. Confirm the production Vercel origin and Railway callback are exact authorized
   JavaScript origins/redirect URIs for the Web OAuth client.
5. Existing users do not need a new downloaded JSON file. Download/update client
   JSON only if the client ID/secret itself changes; tester-list changes do not
   change credentials.
6. Move to production/verification only when ready for users outside the manual
   pilot and after privacy/domain/scope verification preparation.

### DBeaver local GUI secret storage

The connection definitions and read-only role are prepared. The final master
password and OS secure-storage confirmation can only be completed in the user's
DBeaver GUI. Store the Neon analyst password there; never save the owner URL or
password in shared project files.

### Railway GitHub source metadata

Deployments work through the authenticated GitHub workflow and `railway up`, but
Railway may still display the former personal repository as source metadata. To
reconnect it, open the API and worker service **Settings -> Source -> Disconnect**,
then connect `agentic-ai-training/google-connector-app` while logged into a GitHub
organization owner account and grant the Railway GitHub App access to that public
repository. Keep the existing workflow deployment path until the reconnected
source succeeds once.

### Governed GitHub proposal PR notification

The publisher is implemented and remains disabled until production receives a
separately scoped credential. Create a GitHub App owned by `agentic-ai-training`
with Contents and Pull requests write access only to this repository, generate a
short-lived installation token, and store it as `GITHUB_PROPOSAL_TOKEN` in Railway;
set `GITHUB_PROPOSAL_REPOSITORY=agentic-ai-training/google-connector-app`. After a
proposal passes its canary and final human promotion approval, an administrator
must press **Publish sanitized draft PR** and confirm the frozen hash. The adapter
publishes only the curated proposal Markdown, records the URL in the notification
ledger, never reads private evidence, never modifies workflow files, and never
auto-merges.

## Not implemented by design

- Live exploratory RL or immediate fine-tuning.
- Automatic trusted OKF/prompt/tool/policy publication without a human decision.
- Automatic forward deployment after a canary regression; only rollback is
  automatic.
- Public Grafana dashboards containing session/user/email data.
- Self-enrolment into Google's OAuth test-user list.
