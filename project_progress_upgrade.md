# Google Connector Application — Production Reliability and Learning Upgrade

Status: APPROVED FOR IMPLEMENTATION  
Approved: 2026-07-19  
Execution rule: implement in dependency order, preserve rollback paths, run each sprint's guardrails before promotion, and record deviations/blockers in this file.

## 1. Target outcome

Every user request becomes a durable, user-scoped run that can answer:

- What did the user request and what outcome did they intend?
- What plan was created, which assumptions were made, and what clarification was required?
- Which agents, Google services, tools, models, prompts, OKF concepts, retrieval strategies, and chunker versions were used?
- Was RAG necessary, why did it run, what evidence did it return, and was that evidence used?
- Which steps are pending, executing, complete, failed, cancelled, compensated, or awaiting approval?
- What was the last successful step and first breaking point?
- Which external artifacts were created, verified, shared, retained, or cleaned up?
- Can execution resume without duplicating external effects?
- What were the latency, token, quota, cost, and retry contributions of every step?
- What deterministic diagnosis and concise user-facing explanation resulted?
- What sanitized lesson or improvement candidate should be evaluated from the run?

Failed and partial runs must generate separate technical, functional, user-visible, and side-effect-integrity completion percentages and preserve useful verified artifacts.

## 2. Approved production architecture

```text
Vercel web / Flutter
        |
        v
FastAPI run API --------> Neon PostgreSQL
        |                  |- durable runs/steps/events/artifacts
        |                  |- queue/leases/idempotency
        |                  |- high-cardinality reporting views
        |                  `- RAG/OKF/evaluation metadata
        v
Railway worker(s)
        |- typed planner and plan validator
        |- policy/approval engine
        |- dependency-aware executor
        |- postcondition verifier
        |- recovery/compensation
        |- Google Workspace tools
        |- user-content RAG
        `- OKF operational knowledge

Telemetry:
Railway services -> Grafana Alloy -> Grafana Cloud metrics/logs/traces
Agent/model traces ----------------> LangSmith
Detailed session analysis ---------> Neon read-only Grafana datasource

Local development:
local PostgreSQL + local Prometheus + local Grafana + local Ollama

Dashboard source:
one version-controlled dashboard/provisioning set shared by local Grafana and Grafana Cloud
```

Production telemetry must continue when the developer laptop is off. Local Grafana may query Grafana Cloud and Neon, but production collection/storage must not depend on the local machine.

## 3. Settled governance decisions

### 3.1 External writes

- Reads, planning, retrieval, drafting, internal logs, evaluations, and low-risk reversible private artifact creation run autonomously.
- High-risk external writes require a just-in-time user confirmation unless the user explicitly says not to ask.
- High risk includes sending email/Chat, inviting others, sharing with new recipients, permission changes, publishing, deleting/trashing, bulk modification, substantial overwrite, ownership transfer, meeting cancellation, destructive migrations/deployments, and financial/legal/security/reputational effects.
- Compound workflows prepare and verify safe prerequisites before pausing at the first high-risk boundary.
- Approval is bound to the exact action, recipients, content/scope, proposal version, and expiry. Any material change invalidates approval.
- The rule is enforced deterministically in the policy layer and documented in OKF; it must not rely on an LLM prompt alone.

### 3.2 Learning and promotion

- Build RL-ready telemetry, trajectory schemas, evaluation, replay, datasets, and policy comparison now.
- Do not fine-tune or perform live RL until sufficient verified, consented, sanitized data and stable baselines exist.
- Automatic: observe, redact, aggregate, cluster, draft proposals, run offline tests, report, choose among already approved bounded runtime strategies, and safely roll back a bad canary.
- Human approval required: trusted OKF, prompts, routing, tools, chunkers, agents, production policies, model changes, fine-tuning/RL, and full promotion.
- No production session, thumbs-up, or thumbs-down directly becomes training data or trusted knowledge.

### 3.3 Improvement publication workflow

```text
DETECTED -> ANALYZING -> DRAFTED -> SANITIZED -> EVALUATED
-> AWAITING_REVIEW
   -> REJECTED / CHANGES_REQUESTED / APPROVED_FOR_CANARY
-> CANARY -> AUTO_ROLLED_BACK / AWAITING_PROMOTION
-> PUBLISHED
```

- Primary control surface: protected Admin Improvement Center.
- Notifications: sanitized admin email, Grafana pending-review/urgent panels, and GitHub PR notification for version-controlled changes.
- Review includes evidence count, root-cause confidence, exact diff, affected workflows, privacy/security checks, old/new evaluation metrics, expected impact, risk, rollback, and expiry.
- Approval never occurs through an email reply or an unsigned link.
- High-risk proposals require step-up confirmation using the proposal identifier.
- First approval authorizes a limited canary only. Final approval publishes broadly.
- Canary applies to selected trusted users or 5–10% of eligible workflows; control remains on the previous version.
- Unsafe canaries automatically roll back to last known good and notify the administrator. Automation may roll back, never silently deploy a forward fix.
- In-flight runs remain pinned to their starting versions.

### 3.4 Pilot and privacy

- Roll out through internal verification, 5–10 users, 20–30 users, 40–50 users, then 80–90 users using feature flags and measurable promotion gates.
- Default retention: raw prompts/responses and raw tool payloads 14 days; structured workflow metadata 90 days; sanitized daily aggregates and security audit events 12 months; credentials until disconnect/deletion; approved evaluation examples until removed/superseded; rejected OKF candidates 90 days.
- Grafana Cloud detailed telemetry follows available free-plan retention.
- Collect minimum necessary data, redact secrets/PII before export, never put user/session identifiers in high-cardinality Prometheus labels, and never commit private Workspace content to the public repository.
- Separate diagnostic retention from training consent. Provide user export/deletion and enforce user/tenant isolation.

### 3.5 DBeaver

- Create a dedicated production Neon read-only reporting role with SSL, connect/usage/select only, future-view/table grants where appropriate, query/connection limits, and no OAuth credential visibility.
- Add clearly colored connections: production Neon (red), local Homebrew PostgreSQL (green), local Docker PostgreSQL (blue).
- Organize under `Google Connector/Production` and `Google Connector/Local`.
- Do not commit credentials or owner connection details.

### 3.6 Groq-only candidate engineering

- Automated implementation candidates use only the configured Groq API/model family;
  they must not require an OpenAI, DeepSeek, or other coding-model credential.
- A single Groq coordinator handles small, localized candidates. It may escalate to
  separate investigator, patch-author, test-author, and reviewer roles when the
  reproduction, risk, or changed-file scope justifies the additional token cost.
- Deterministic sandbox tools perform repository search/read, patch application,
  allowlisted validation, diff inspection, hashing, and rollback. The LLM proposes
  bounded tool calls but receives no production OAuth token, production database
  credential, deployment credential, or raw private Workspace payload.
- A tool-extension role may propose a new registered tool, schema, adapter, tests, and
  OKF documentation as an ordinary code candidate. It cannot dynamically add trusted
  runtime authority, OAuth scopes, or external-write permission.
- Automatic diagnosis, reproduction, candidate drafting, tests, and evidence assembly
  do not approve themselves. Human candidate, canary, trusted OKF, high-risk external
  publication, and production-promotion gates remain mandatory.

## 4. Sprint implementation ledger

Legend: `[ ]` pending, `[~]` active, `[x]` complete, `[!]` externally blocked.

## Sprint 0 — Baseline, safety, and migration preparation

### Epic 0.1 — Protect current production

- [x] Export current Neon schema and create a backup/restore procedure.
- [x] Record Railway/Vercel deployment identifiers and current public health URLs without writing secrets.
- [x] Capture current latency, failure, RAGAS, token, quota, and Google artifact baselines.
- [x] Inventory current code paths, services, database objects, dashboards, and external artifacts.
- [x] Verify secrets/credentials are ignored and absent from Git history.
- [x] Add feature flags for legacy chat, durable runs, OKF, new RAG, governed improvements, and canary cohorts.
- [x] Preserve legacy `/chat` as a rollback path until the new executor is proven.
- [x] Define migration, deploy, worker, index, prompt, OKF, and dashboard rollback procedures.
- [x] Define zero-duplicate-action/idempotency invariants.

### Epic 0.2 — Golden evaluation set

- [x] Cover Gmail reads/writes, Drive, Sheets, Docs, Calendar/Meet, Chat, Tasks, Contacts, multi-service workflows, ambiguity, misspellings, missing destinations/timezones, quota exhaustion, cold starts, Google 4xx/5xx, cancellation, browser disconnect, Vercel timeout, worker restart, duplicate submission, partial side effects, prompt injection, and cross-user isolation through the planner, resilience, integration, and no-network replay suites.
- [x] Define expected plans, tools, arguments, artifacts, approvals, postconditions, and summaries.
- [x] Replace unsafe mutations with deterministic Google adapter fakes in tests.

Guardrail: capture a reproducible baseline before changing production behavior.

## Sprint 1 — Durable run, step, event, artifact, and approval model

### Epic 1.1 — Runs

- [x] Add `agent_runs`: run/session/user IDs, request/objective, state/phase, timestamps, current step, four completion measures, models/tokens, classification, trace ID, cancellation, idempotency, prompt/OKF/chunker/deployment versions, retention/deletion fields.

### Epic 1.2 — Steps and dependencies

- [x] Add `agent_run_steps`: order, DAG dependencies, service/agent/tool, risk/read-write class, pre/postconditions, weight, retry/timeout/approval policy, inputs/outputs, duration/tokens, artifacts, and failures.

### Epic 1.3 — Append-only events

- [x] Add `agent_run_events` for creation, planning, clarification, model/tool calls, approvals, artifact creation, verification, fallback, quota, heartbeat, retry, cancellation, compensation, and completion.

### Epic 1.4 — Artifacts and attempts

- [x] Add `agent_artifacts`, model-call/tool-attempt records, external IDs/URLs, verification/sharing/cleanup state, safe-delete flag, and lineage.

### Epic 1.5 — Invariants and reporting

- [x] Add foreign keys, state constraints, indexes, idempotency uniqueness, retention fields, migration downgrade/forward repair, and strict cross-user authorization.
- [x] Add reporting schema/views without exposing credentials.

Guardrail: upgrade/downgrade on clean local DB; migration tests and authorization tests pass.

## Sprint 2 — Durable asynchronous execution

### Epic 2.1 — Run API

- [x] Implement `POST /runs`, `GET /runs/{id}`, `GET /runs/{id}/events`, `POST /runs/{id}/cancel`, `/resume`, `/approve`, and session-run history.
- [x] Return queued run IDs immediately instead of holding the Vercel/Railway request open.

### Epic 2.2 — PostgreSQL-backed worker

- [x] Claim work with `FOR UPDATE SKIP LOCKED`, heartbeat/lease, stale-job recovery, bounded retries, and a separate Railway worker using the application image; do not introduce Redis initially.

### Epic 2.3 — Replayable progress

- [x] Persist and stream run/plan/step/progress/approval/heartbeat/final events through SSE; reconnect by run ID and replay missed events.

### Epic 2.4 — Idempotency

- [x] Prevent duplicates from double-clicks, client/proxy retries, reconnects, worker restarts, and lost responses after Google succeeds.

Guardrail: disconnect/restart tests prove the worker continues and writes are not repeated.

## Sprint 3 — Structured planner and plan validation

- [x] Define typed `ExecutionPlan`, `PlanStep`, success criteria, assumptions, clarifications, dependencies, risk, approval, weights, preconditions, postconditions, and estimated budgets.
- [x] Ask only for materially missing information such as ambiguous person/space, timezone/duration, uniqueness, sharing, or destructive cleanup.
- [x] Reject unknown tools, placeholders, missing dependencies/recipients/timezones, impossible arguments, unsupported operations, unsafe parallel writes, and writes before required reads.
- [x] Measure plan tool/order/coverage/necessity/cost/execution quality.

## Sprint 4 — Intent, service, model, and execution policy routing

- [x] Classify read/write, live lookup/semantic recall, single/multi-service, simple/complex, reversible/irreversible, clarification, risk, and parallelism.
- [x] Improve synonyms, misspellings, service detection, entity/date/timezone/recipient/Chat-space extraction; remove unsafe Gmail defaulting.
- [x] Route by complexity, risk, quota, context, tool count, and reliability. Do not silently downgrade a complex mutation from 70B to an unreliable small model.
- [x] Estimate and enforce token/time/tool budgets before execution.
- [x] Implement deterministic high-risk confirmation with explicit opt-out and action-bound approvals.

## Sprint 5 — RAG necessity gate

- [x] Skip RAG for live/latest mutations/lookups; use it for semantic history, conceptual matching, prior context, and cross-document synthesis.
- [x] Begin with deterministic audited rules; later evaluate a small classifier for none/metadata/keyword/vector/hybrid.
- [x] Record whether/why RAG ran, latency, returned/used evidence, and outcome impact.

## Sprint 6 — Source-aware RAG ingestion and chunking

### Epic 6.1 — Source strategies

- [x] Gmail: metadata on every child, clean body, quoted-history/signature detection, thread parent-child, attachment metadata, deduplication.
- [x] Docs/Drive text: title/heading/paragraph/list/table hierarchy; small retrieval children and larger generation parents.
- [x] PDFs: layout/headings, page/bounding provenance, OCR marker, independent table handling, no column corruption.
- [x] Sheets: typed header-aware row groups/ranges/tab/row IDs; structured filtering before vectors.
- [x] Calendar/Meet: structured event/participant/recording metadata; speaker/topic chunks for transcripts; Drive hierarchy for transcript documents.
- [x] Chat: space/thread/sender/time windows/topic boundaries/reply relationships.
- [x] Contacts/Tasks: structured lookup first; generally no chunking for atomic records.
- [x] OKF: Markdown/YAML concept and heading-aware chunks; keep tool schemas, prerequisites, warnings, and parents intact.

### Epic 6.2 — Versioned experiments and lineage

- [x] Implement and run a deterministic no-network comparison of 256/512/768/1024-token policies, overlaps, lineage, duplication, and structured no-chunk cases; keep the production default unchanged.
- [~] Select source-dependent overlap/parent sizes only after at least ten tenant-safe labelled RAG cases exist; synthetic lexical evidence cannot choose a production winner.
- [x] Store source/parent/chunk position/hash, ACL/tenant, embedding/chunker/sync versions, timestamps, tombstones, provenance, and reindex time.
- [x] Incrementally re-embed only changed content/version/metadata.

### Epic 6.3 — Retrieval pipeline

- [x] Query classification -> structured filters -> dense vector + PostgreSQL text search -> rank fusion -> recency/metadata -> dedupe/diversity -> rerank -> threshold/budget -> context/citations.
- [~] Evaluate query normalization/entity/date/acronym/multi-query/HyDE only where measured; never expand precise identifiers/latest lookups unnecessarily.
- [~] Offline policy CI now measures recall@k, precision@k, MRR, nDCG, latency, token size, duplication, evidence presence, and lineage. Production context precision/recall, faithfulness, relevance, citations, cost, and permission-leak comparison remain gated on labelled per-source evidence.

## Sprint 7 — Decouple embedding from live tools

- [x] Return live Google results first and enqueue optional persistence/embedding.
- [x] Batch, dedupe by hash, bound concurrency, time out per item, retry asynchronously, dead-letter failures, and expose embedding health.
- [x] Apply global/per-user/payload admission backpressure to Ollama persistence and monitor cold start, queue, duration, loaded state, errors, input size, overflow retries, and rejection reason.

## Sprint 8 — Dependency-aware durable executor

- [x] Execute independent reads concurrently and dependent/high-risk writes in verified order.
- [x] Bound concurrency per run/user/API/model and avoid unbounded gather.
- [x] Retry transient network/429/5xx/worker failures only; do not retry invalid input, permission denial, invalid timezone, or cancellation blindly.
- [x] Use deterministic idempotency keys and artifact lookup before retrying Google writes.

## Sprint 9 — Verification and deterministic postconditions

- [x] Add tool-specific postconditions and read-after-write for critical Sheets, Drive, Chat, Calendar/Meet, Gmail, Docs, Tasks, and sharing states.
- [x] Require resource IDs, expected content/rows/recipient/timezone/link/sharing state; HTTP 200 alone is not success.
- [x] Prevent the final agent from claiming unverified success.

## Sprint 10 — Recovery, resume, and compensation

- [x] Implement failure taxonomy for user/planning/routing/model/tool/auth/permission/quota/network/database/embedding/verification/cancellation/worker/proxy/security.
- [x] Resume from the first safe incomplete step without recreating verified artifacts.
- [x] Preserve/report, retry population, roll back sharing, cancel incorrect events, or delete only when explicitly approved and safe.
- [x] Surface pending high-risk action approvals in the run state and frontend.

## Sprint 11 — Token, latency, quota, and budget accounting

- [x] Capture per-call input/output/schema tokens where available, model, queue/prompt/completion/tool time, fallback, and rate-limit metadata.
- [x] Attribute to planner/router/executor/verifier/recovery/summarizer and aggregate per step/run/user/model.
- [x] Reserve quality-model quota for complex tasks; avoid spending it on capability questions.
- [x] Stop/replan before runaway loops and present quota-aware defer/simplify choices.

## Sprint 12 — Automatic incident summaries

- [x] Deterministically identify last success, first failure, primary/contributing causes, evidence, artifacts, and cancellation source.
- [x] Generate a short summary only after structured facts exist.
- [x] Calculate technical, functional, user-visible, and side-effect-integrity completion separately.
- [x] Link diagnosis to events, attempts, traces, artifacts, metrics, and external errors with confidence.

## Sprint 13 — Production observability and Grafana

- [x] Deploy lightweight Alloy on Railway; scrape API/worker metrics and forward them to Grafana Cloud with filtering, WAL buffering, privacy, and cardinality controls.
- [x] Add privacy-safe structured request logs to Grafana Cloud Loki with bounded labels and body-level correlation IDs, plus OpenTelemetry FastAPI/HTTPX/asyncpg Tempo traces; keep LangSmith for agent/LLM traces.
- [x] Add Grafana Cloud aggregate dashboards: traffic, latency, errors, tools, quota/fallback, RAG, queue, active/cancelled runs, artifacts, OAuth, DB, Google APIs, and Ollama.
- [x] Add Neon PostgreSQL read-only session/workflow dashboards: task, current step, progress, duration, versions, tokens, heartbeat, breaking point, artifacts, incident, trace links.
- [x] Add 17 evaluated alerts for missing heartbeat, backlog, cancellation, quota, Ollama, Neon/tool failures, orphaned artifacts, RAG latency/quality/sample sufficiency, OAuth, embedding backpressure, and deployment telemetry regression; route notifications to the Grafana organization administrator.
- [x] Provision the same dashboards locally; local Grafana may query production sources but production does not depend on it.

## Sprint 14 — Frontend run and admin experience

- [x] Add live plan/current/completed/pending steps, progress, heartbeat, fallback, clarification, and approval UI.
- [x] Reconnect/resume by run ID and preserve partial verified artifacts.
- [x] Show concise user failure plus authorized detailed administrator diagnosis.
- [x] Add history filters by session/status/user/service/model/failure/time/version.
- [x] Add protected Admin Improvement Center with evidence, diffs, evaluations, risk, privacy, rollback, approve-canary/change/reject/promote actions, expiry, audit, and step-up approval.

## Sprint 15 — Feedback and governed learning dataset

- [x] Capture overall rating and step-specific wrong/missing/slow/tool/data/safety/free-text feedback.
- [x] Include negative and failed runs in evaluation candidates.
- [x] Store sanitized corrected trajectories: original plan/execution/failure/diagnosis/corrected plan/expected result.
- [x] Version datasets; separate train/validation/test, consent, retention, access, deletion, and leakage prevention.

## Sprint 16 — Evaluation and replay

- [x] Build mock Google adapters and safe replay for mutations.
- [~] Compare old/new planner, prompt, OKF, routing, chunking, model, and recovery policies on identical tasks. The replay/comparison engine and promotion gates are complete; statistically meaningful per-policy conclusions remain data-gated.
- [x] Measure task/plan/tool/artifact correctness, latency, tokens, recovery, side effects, satisfaction, and retrieval.
- [x] Block promotion on golden-task, token, cancellation, isolation, verification, safety, or RAG regression.

## Sprint 17 — Prompt optimization and bounded contextual bandits

- [~] Experiment with planner/router/verifier/recovery prompts independently. Versioned isolated experiment infrastructure is complete; selecting winners remains data-gated.
- [x] Allow bandits only among already validated low-risk policies such as RAG gate, prompt variant, read-task model, retrieval, or planner strategy.
- [x] Track completion, correctness, rating, latency, tokens, errors, orphaned artifacts, and unsafe effects separately; do not prematurely collapse reward.

## Sprint 18 — RL readiness, not live RL

- [x] Store state -> decision -> action -> observation -> reward -> next-state trajectories.
- [~] Implement offline policy evaluation and only later offline experiments on verified data. The evaluator, multi-objective regression gates, and reports are complete; the first promotion is correctly blocked until at least 30 verified samples exist.
- [x] Never allow exploratory RL to experiment with real emails, invitations, sharing, deletion, or Chat messages.
- [x] Require separate human approval, consent/data review, stable holdout baseline, rollback, and cost/security evidence before any fine-tuning/RL.

## Sprint 19 — Security, privacy, isolation, and retention

- [x] Scope every run/event/artifact/retrieval/approval to user/tenant.
- [x] Treat Google content as untrusted and defend against prompt injection/data exfiltration.
- [x] Enforce tool allowlists, approval policies, recipients, bulk/destructive limits, and rate/abuse controls.
- [x] Encrypt OAuth credentials, support ordered-key lazy rotation, redact telemetry, audit access, implement tenant export/deletion, and automate approved retention.
- [x] Keep public/private OKF and diagnostic/training consent separate.

## Sprint 20 — DBeaver and reporting database access

- [x] Create `dbeaver_analyst`-style Neon role and curated reporting schema/views with no secret table access.
- [x] Configure production Neon, local Homebrew, and local Docker connections with approved names/colors/folders/read-only settings.
- [x] Provide ER diagram and views for run status, timeline, failure, models/tokens, retrieval, tools, artifacts, prompts, session history, security, improvements, and canaries.
- [x] Keep connection definitions password-free, store the production analyst credential in macOS Keychain, and verify the same credential can read reporting views while PostgreSQL enforces read-only mode and denies the OAuth credential table. DBeaver may still prompt once if the user elects to copy the Keychain secret into DBeaver's own vault.

## Sprint 21 — Deployment, canary, and rollback

- [x] Add feature flags, shadow planning, selected-user cohorts, and old/new side-by-side comparison.
- [x] Expand schema -> dual write/backfill -> switch reads -> remove legacy only after validation.
- [x] Canary approved candidates; pin in-flight versions; auto-rollback guardrail breaches; require final human promotion.
- [x] Preserve in-flight runs during worker/frontend rollback and retain legacy `/chat` until exit criteria pass.
- [!] Roll through internal, 5–10, 20–30, 40–50, and 80–90 user gates (requires real pilot users and measured runs over time).

## Sprint 22 — Documentation and runbooks

- [x] Document architecture, state machine, failure taxonomy, on-call/alerts, quota, Ollama, Neon, OAuth, artifacts, safe deployments, learning governance, privacy/retention, DBeaver, canary, and rollback.

## Sprint 23 — Open Knowledge Format operational knowledge layer

- [x] Implement OKF v0.1-compatible public and protected private bundles with Markdown, YAML frontmatter, stable namespaced concept paths, index/log files, links, provenance, version/owner/tags/timestamps, and default-deny private retrieval.
- [x] Concepts cover capabilities, tools, workflows, policies, schemas, metrics, failures, runbooks, Google API limits, RAG sources, and agent capabilities.
- [x] Generate drafts deterministically from trusted tool registry/OpenAPI/migrations/scopes/metrics, validate links/schema/tool references, scan secrets/PII, and require human publication approval.
- [x] Keep Markdown as source of truth; index structured/heading-aware chunks and graph links in Neon separately from user-content RAG.
- [x] Use OKF for capability discovery, workflow selection, prerequisites/OAuth, validation, recovery, and explanations; never treat retrieved user content as operational authority.
- [x] Record OKF versions/retrievals in runs, LangSmith, Neon, and Grafana; compare versions through replay/canary evaluation.
- [x] Production incidents may create sanitized candidate OKF drafts but never update trusted knowledge directly.

## Sprint 24 — Governed improvement proposal and publication service

- [x] Add proposal/evidence/evaluation/approval/canary/audit/version database models and lifecycle transitions.
- [x] Threshold recurring/severe findings; deduplicate and expire stale proposals.
- [x] Produce exact versioned diffs and GitHub draft PRs for public code/OKF/config; store private revisions in protected storage. Audit on 2026-07-20 found that the analyzer emitted recommendation text rather than executable files; Sprint 26 now requires and publishes concrete candidate files.
- [~] Notify through admin UI, sanitized email, Grafana, and GitHub; never expose private evidence in notifications. Admin/Grafana ledgers and both explicit-action adapters are complete; production email/GitHub delivery remains credential- and confirmation-gated.
- [x] Freeze approved hashes, rerun all gates, deploy selected-user/5–10% canary, compare control/candidate, auto-rollback on guardrail failure, and request final promotion. Candidate/deployment proof was missing from the original approval gate and is now enforced by Sprint 26.
- [x] Retain audit identity/time/version/purpose and invalidate approval after material change.

## Sprint 25 — Original specification compatibility and final guardrails

- [x] Re-run every safe, still-applicable command and acceptance criterion in `PROJECT_SPEC.md` against the upgraded implementation; real Google mutations remain confirmation-gated.
- [x] Run Python formatting/lint/type/security/unit/integration/evaluation/migration tests.
- [x] Run Next.js lint/type/build/tests and Flutter analyze/test/build where toolchains allow.
- [x] Run Docker Compose build/up/health/metrics/Grafana/Prometheus/Ollama/PostgreSQL tests on Docker Desktop.
- [x] Run secret/history scans, authorization/PII/prompt-injection/idempotency/cancellation/canary/rollback tests.
- [x] Run GitHub Actions, Railway, Neon, Vercel, Google OAuth, Grafana Cloud, Alloy, and LangSmith production smoke tests where credentials/account state allow. GitHub backend/web/Flutter CI, Railway API/worker, Neon migration 008, Vercel, OAuth redirect/PKCE/scopes, metrics, logs, traces, alerts, dashboards, authorization, and LangSmith pass. Time/data-dependent pilot conclusions are evidence gates, not an unfinished smoke test, and are recorded in `docs/PILOT_AND_LEARNING_GATE_2026-07-20.md`.
- [x] Document every external blocker with exact user steps; do not mark complete while an in-scope safe action remains.

## Sprint 26 — Truthful implementation candidates and informational-run repair

- [x] Distinguish `diagnosis_only`, `implementation_draft`, `validated_implementation`, and `deployed_canary`; never label a recommendation as a deployable candidate.
- [x] Require concrete changed files, safe paths, content hashes, a base/candidate version, exact diff, rollback plan, and passing command evidence before canary approval.
- [x] Require verified deployment identity and passing smoke evidence before canary activation; bind every decision to the frozen candidate hash.
- [x] Publish actual candidate files in a sanitized draft PR rather than publishing only a proposal Markdown file.
- [x] Require a human note when requesting changes, and visibly disable canary approval for diagnosis-only findings.
- [x] Route identity/capability/help questions through the durable run path using trusted product identity and the registered tool catalog, with no Google API, user RAG, or LLM call.
- [x] Cover combined questions, service-focused questions, common wording, and actionable-command separation with unit, integration, and golden replay tests.

Guardrail: a diagnosis-only proposal cannot create or activate a canary; an informational run completes at 100% with zero model/tool calls.

## Sprint 27 — Guarded conversation routing and complete failure intelligence

### Epic 27.1 — Bounded intent gateway

- [x] Classify every accepted request as `workspace_action`, `workspace_guidance`, `product_information`, `scope_chat`, `ambiguous`, or `out_of_scope` before planning.
- [x] Keep chat limited to this agent and Google Workspace: greetings, clarification, product identity/capabilities, and service guidance answer locally; unrelated general chat is redirected without Google tools, user-content RAG, or global-chat claims.
- [x] Derive capability/guidance text from the trusted tool registry and human-approved OKF capability sources rather than an unbounded conversational prompt.
- [x] Record the intent, confidence basis, detected services, ambiguity, and chosen flow in the durable run and plan events.

### Epic 27.2 — Context-sensitive multi-service planning

- [x] Treat people/senders who emailed the user as Gmail extraction, not an unconditional Contacts request.
- [x] Treat a newly created Sheet URL as its Drive link; do not schedule a redundant Drive lookup.
- [x] Fuse Calendar scheduling with Meet conferencing into one Calendar event when requested; retain standalone Meet-space creation only for explicit instant-space requests.
- [x] Build a data-dependency DAG: reads -> artifact creation/verification -> independent deliveries, with safe concurrency and action-bound approval.
- [x] Ask for Chat space, duration, timezone, uniqueness, or other materially missing inputs before external writes.

### Epic 27.3 — Failure capture at every stage

- [x] Persist pre-execution/admission failures as intake incidents and terminal execution/verification failures as run-linked incidents; backfill any terminal run missed during transient telemetry failure.
- [x] Convert invalid plans into durable structured failures instead of uncaught HTTP 500 responses.
- [x] Redact request excerpts and evidence; store bounded request-shape metadata, failure component/stage, normalized fingerprint, root cause, contributing factors, completion, versions, and evidence links.
- [x] Make incident recording best-effort and non-recursive so telemetry failure cannot conceal or replace the original failure.

### Epic 27.4 — Per-failure analysis and governed aggregation

- [x] Analyze every failure occurrence and place it in the protected portal with exactly two plain-language improvement options, a recommended option with rationale, risk, acceptance tests, and automation eligibility.
- [x] Cluster related incidents by stage/component/service/operation/error template rather than the broad error category alone; preserve every occurrence while avoiding duplicate proposal spam.
- [x] Allow an administrator to choose option A or B, acknowledge, or ignore an incident. Choosing an option creates or updates a diagnosis proposal; it does not create a fake implementation candidate.
- [x] Permit rejected/expired clusters to receive later evidence through a new timestamped revision instead of silently dropping same-day failures.
- [x] Support `manual` and future `auto_draft` analysis modes behind an audited feature flag with an exact confirmation phrase. Human approval remains mandatory for candidates, canaries, trusted OKF, and publication.

### Epic 27.5 — Portal, Grafana, DBeaver, and notifications

- [x] Add a failure inbox with request stage, breaking point, sanitized explanation, occurrence count, two options, recommendation, and review actions.
- [x] Emit immediate internal admin/Grafana notification ledger entries for every incident; external email/GitHub remain separately configured and explicitly confirmed.
- [x] Add bounded-cardinality metrics, alerts, Grafana panels, and read-only Neon/DBeaver reporting views for failure stages, fingerprints, unreviewed incidents, pre-run failures, and notification delivery.

### Epic 27.6 — Regression and rollout safety

- [x] Add the reported Gmail -> Sheet -> Chat + Calendar/Meet request, bare `what?`, Workspace guidance, out-of-scope chat, unknown operations, granular fingerprints, and pre-run persistence to unit/integration/golden/replay suites.
- [x] Verify no Google side effects occur during classification, guidance, failed planning, analysis, or proposal drafting.
- [~] Preserve rollback through the existing deployment path; local migration round-trip, Docker, API/worker, Prometheus, Grafana, PostgreSQL, and frontend gates pass. GitHub CI and Railway/Vercel/Neon/Grafana Cloud production rollout follow the draft PR.

Guardrail: every accepted request has a durable outcome or a separately durable intake incident; every failure occurrence is reviewable, but no diagnosis can approve or deploy itself.

## Sprint 28 — Bounded live-tool results and deterministic structured operations

### Epic 28.1 — Metadata-only Gmail sender extraction

- [x] Add an explicitly registered `list_recent_gmail_senders` operation that lists
  recent message IDs, fetches only required metadata headers, parses names/addresses,
  supports ordered unique/non-unique semantics, and returns a compact bounded schema.
- [x] Route requests such as “last 20 people who mailed me” to this operation without
  reading bodies/HTML, invoking RAG, or asking an LLM to extract deterministic fields.
- [x] Preserve message IDs/date provenance and verify count, order, non-empty names,
  duplicate policy, authorization, and dependency output for downstream Sheets.

### Epic 28.2 — Universal result envelopes and approved projection

- [x] Introduce typed result envelopes carrying compact output, tenant-scoped full-result
  references, item/byte/token counts, projection version, truncation, and continuation.
- [x] Define per-service/operation projection allowlists. Raw Gmail/Drive/Docs/Chat data
  must never be appended directly to an LLM conversation merely because a tool returned it.
- [x] Store necessary full results in bounded private durable storage and supply only the
  projected result/reference to the model; preserve verifier access without public leakage.
- [x] Apply prompt-injection sanitization after structural projection and before any
  remaining untrusted text reaches the model.

### Epic 28.3 — Context-budget manager and safe recovery

- [x] Account before every model call for system/OKF/RAG/tool-schema/history/result and
  reserved-completion tokens using the configured model budget.
- [x] Compact, paginate, defer, or deterministically replan before provider rejection;
  never silently discard required postcondition data.
- [x] Classify context overflow as `model_context_length` with boundary, component,
  service, operation, model, estimated tokens, result sizes, and safe recoverability.
- [x] Record only safe size/count telemetry, not private tool content; distinguish a
  completed read sub-operation from a fully verified workflow step in progress reports.

Guardrail: the reported Gmail -> Sheet -> Chat + Calendar/Meet request reaches the
Gmail dependency result without full email bodies entering Groq, and oversized fake
tool results are bounded before every model call.

## Sprint 29 — Hierarchical failure and policy intelligence

### Epic 29.1 — Occurrences and concrete clusters

- [x] Extend immutable occurrences with failure mechanism, architectural boundary,
  provider code, safe payload-size facts, last verified sub-operation, recoverability,
  affected versions, and reproduction/candidate linkage.
- [x] Version concrete fingerprints from mechanism + boundary + component + service +
  operation + normalized provider code; exclude PII, volatile identifiers, and raw data.
- [x] Track cluster occurrence/version ranges, regression coverage, resolution version,
  reopening, selected strategy, and active candidate without losing individual evidence.

### Epic 29.2 — Cross-cluster policy themes

- [x] Replace the inactive category-only legacy proposal generator with a deterministic
  cross-cluster theme analyzer based on shared mechanism/boundary/component family.
- [x] Require multiple concrete clusters, an evidence threshold, and confidence facts
  before claiming a systemic issue; a broad label such as `execution` is insufficient.
- [x] Give every theme two bounded options (systemic fix and narrower containment),
  acceptance tests, risks, scope, rollback, automation eligibility, and evidence links.
- [x] Version/deduplicate themes; rejected/expired/resolved items must not suppress new
  evidence on a later deployment, while resolved versions do not create proposal spam.

### Epic 29.3 — Portal, reporting, and lifecycle clarity

- [x] Separate Active Failures, Concrete Clusters, Policy Themes, Candidate Pipeline,
  and collapsed History (rejected/expired/rolled-back/published) in the admin portal.
- [x] Make every button state its exact effect: strategy selection is not implementation,
  candidate approval is not deployment, and activation is not promotion.
- [x] Add read-only reporting views, bounded metrics/alerts, Grafana panels, notification
  ledgers, retention, and tenant/admin authorization for all three intelligence levels.
- [x] Remove the legacy category generator from active behavior after reversible migration
  of its historical proposals; retain audit history without presenting it as new evidence.

Guardrail: unrelated `execution` failures never share a code proposal solely because of
their category; a multi-service unbounded-result theme requires at least two specific clusters.

## Sprint 30 — Groq-only governed candidate engineering

### Epic 30.1 — Candidate specification and reproduction

- [x] Convert a human-selected occurrence/cluster/theme option into a sanitized, typed
  implementation specification with scope, invariants, acceptance tests, forbidden
  effects, base version, expiry, and rollback requirements.
- [x] Reproduce using no-network Google adapters, synthetic bounded fixtures, or an
  approved deterministic test; unresolved/private-only failures remain diagnosis-only.
- [x] Never send raw Workspace bodies, OAuth material, production secrets, or unrestricted
  repository/database content to Groq.

### Epic 30.2 — Adaptive single/multi-agent Groq builder

- [x] Implement a token-budgeted Groq coordinator using the existing configured Groq key.
  Use one agent for small candidates and investigator/patch/test/reviewer roles only when
  deterministic complexity/risk thresholds require them.
- [x] Expose least-privilege tools for repository listing/search/read, bounded patch
  proposal/application, diff inspection, allowlisted validation, and candidate rollback.
- [x] Limit files, bytes, iterations, tool calls, elapsed time, and Groq tokens per build;
  pause with truthful evidence instead of degrading to an unsafe or unrelated model.
- [x] Add a tool-extension designer that can draft a registered tool, schemas, adapter,
  OAuth/precondition documentation, tests, and OKF concepts only as an untrusted candidate.
- [x] Require an independent Groq review role for security-sensitive or multi-file changes;
  deterministic validators remain authoritative over model claims.

### Epic 30.3 — Isolated candidate workspace and evidence

- [x] Run candidate builds in disposable GitHub checkouts with no production
  OAuth/database/deployment credentials, allowlisted network, resource/time limits,
  approved path roots, secret/PII scans, and complete sanitized audit events.
- [x] Generate concrete files, content hashes, exact diff, base/candidate commit, validation
  commands/results, security/privacy report, migration compatibility, and rollback manifest.
- [x] Register only reproducible candidates; failed builds remain visible with the exact
  breaking point and cannot advance to canary.

### Epic 30.4 — Trusted GitHub CI and PR handoff

- [x] Create a draft branch/PR only after the separately confirmed external-publication
  action; never publish private evidence or secrets to the public repository.
- [x] Run backend/web/Flutter, migration, security, golden, replay, policy, dashboard, and
  candidate-specific gates in GitHub Actions; bind evidence to immutable commit/artifact IDs.
- [x] Accept validation/deployment evidence only from a trusted CI/deployment identity,
  not a browser-supplied `passed=true`; invalidate approvals after material changes.

Guardrail: a Groq response alone cannot edit trusted main, register a passing candidate,
create a production tool, deploy, approve, or publish.

## Sprint 31 — Real version-pinned canary execution and deployment control

### Epic 31.1 — Stable assignment and version-pinned workers

- [x] Add immutable executor/policy/prompt/OKF/chunker/candidate/cohort versions to each
  run and assign eligible users deterministically with allow/deny overrides and sticky sessions.
- [x] Separate pilot admission from control/candidate routing. A pilot flag must not be
  represented as a version router.
- [x] Make control and candidate workers claim only their assigned executor version so
  competing deployments cannot execute the same run; pin in-flight runs across rollout changes.
- [x] Keep database migrations expand-first/backward-compatible until control retirement.

### Epic 31.2 — Candidate artifact and Railway deployment controller

- [x] Build immutable candidate images/artifacts from trusted CI; record source commit,
  digest, service/deployment IDs, environment profile, health, smoke results, and expiry.
- [x] Add a least-privilege Railway deployment adapter for isolated candidate workers and,
  only when required, candidate API/frontend surfaces; never alter control during preparation.
- [x] Require human approval before production-connected deployment and separate human
  activation before assigning real runs.
- [x] Verify candidate health and exact version from runtime telemetry before activation.

### Epic 31.3 — Measurement, effective rollback, and promotion

- [x] Compare minimum-sample control/candidate completion, correctness, cancellation,
  side-effect integrity, p95 latency, tokens, quota, verification, and incident rates.
- [x] On a safety/quality regression, stop new candidate assignment, route new runs to
  control, reconcile uncertain writes, preserve evidence, and optionally terminate the
  candidate deployment; database status alone is not rollback.
- [x] Require human promotion after a passing measured canary, then deploy/merge the frozen
  candidate broadly and retain the last-known-good rollback path.
- [x] Provide a deterministic dry-run/local dual-worker simulator before Railway mutation.

Guardrail: canary activation demonstrably changes which immutable executor handles an
eligible new run, and automatic rollback demonstrably restores control routing.

## Sprint 32 — Trusted OKF candidates in the improvement lifecycle

- [x] Treat generated OKF as untrusted drafts with provenance, owner, version, source
  proposal/candidate, content hash, visibility, expiry, and publication status.
- [x] Validate OKF v0.1 structure/links, project governance fields, tool references,
  secrets/PII, prompt-injection boundaries, replay behavior, and affected workflows.
- [x] Require human trusted-publication approval; synchronize only the frozen approved
  hash and record the selected OKF version on every run.
- [x] Roll back by stopping new selection of the bad OKF version while preserving prior
  run provenance; a knowledge document cannot add tools/scopes/permissions by itself.
- [x] Allow code candidates to include related OKF drafts, but keep code/tool and trusted
  knowledge approvals explicit and independently auditable.

## Sprint 33 — Evidence-gated dynamic programming candidates

### Epic 33.1 — Quantized context knapsack

- [x] Implement a deterministic quantized 0/1 knapsack candidate after ACL filtering,
  thresholding, parent dedupe, and diversity preprocessing; use true tokenizer costs,
  reconstruct selected chunks, enforce source caps, and retain greedy fallback.
- [x] Bound candidate count, token units, memory, and latency; fall back truthfully when
  constraints or time budgets are exceeded.
- [x] Compare identical labelled cases against greedy for retrieval/answer/citation quality,
  latency, tokens, duplication, source coverage, and permission isolation.

### Epic 33.2 — Later allocation or scheduling experiments

- [x] Define offline-only multiple-choice knapsack experiments for validated model/workflow
  policies under token/latency/risk constraints; do not let optimization weaken write safety.
- [x] Compare DP batch quota allocation against existing admission/reserve/priority
  heuristics before any runtime use; keep heap/queue scheduling for immediate dispatch.
- [x] Promote no DP policy without sufficient labelled evidence, passing regression gates,
  bounded latency, human canary approval, and automatic fallback.

Guardrail: DP is an evidence-gated candidate, not a claimed winner and not a substitute
for deterministic structured Google operations or universal result bounds.

## Sprint 34 — Extended observability, security, operations, and completion audit

- [x] Add safe result-budget, candidate-build, theme, assignment, deployment, rollback,
  OKF-publication, and DP metrics/traces/events without high-cardinality labels or content.
- [x] Extend Grafana, Neon/DBeaver reporting, alerts, history/export/deletion, retention,
  runbooks, architecture/state diagrams, on-call and external-blocker instructions.
- [~] Threat-model Groq prompt injection, malicious candidate patches, sandbox escapes,
  secret exfiltration, poisoned fixtures, approval replay, CI forgery, worker-version races,
  migration incompatibility, and unsafe rollback.
- [~] Run unit/integration/no-network/golden/replay/policy/RAG/DP/candidate/sandbox/migration,
  Python lint/security/dependency, web, Flutter, Compose, secret/history, CI and production
  smoke gates; verify exact deployed versions and rollback evidence.
- [~] Re-audit every Sprint 28–34 story against authoritative files, database state, CI,
  deployments, dashboards, and runtime behavior before declaring completion.

## 5. Required implementation reports

At meaningful checkpoints report:

- Completed sprints/epics and files/migrations changed.
- Verification commands and results.
- Current production/local health.
- Remaining work and dependencies.
- External blockers and exact remediation.
- Data migrations, rollback state, and any artifact risks.

## 6. Teaching phase after working completion

After implementation and verification, teach through this repository:

- Graphs/LangGraph, DAGs/topological order, state machines, queues/heaps, hashing/idempotency, trees/chunking, HNSW, sliding windows, greedy vs dynamic programming context packing, backtracking/replanning, memoization/caching, token buckets, consistent hashing, bandits, MDPs, and offline RL.
- OKF specification, frontmatter, provenance, graph links, knowledge packaging, RAG/database/live API boundaries, and its concrete implementation here.

## 7. Progress log

- 2026-07-19: Discussion completed; Grafana, chunking, external-write, DBeaver, RL-readiness, OKF, human approval, pilot, privacy/retention, and governed improvement decisions approved. Implementation authorized.
- 2026-07-19: Implemented migrations 003–005, durable run/step/event/artifact APIs, PostgreSQL worker leases, action-bound approvals, clarification UI, tenant-safe RAG, async embedding jobs, trusted OKF retrieval, incident summaries, consented trajectories, governed improvement review/canary lifecycle, retention/deletion, local Grafana/Prometheus/Alloy, DBeaver reporting, and operational documentation. Local backend 31/31 and planner golden 20/20 pass; Next.js lint/build pass. Neon was backed up and upgraded to 005; production deployment and remaining partial ledger items continue.
- 2026-07-19: Merged the durable upgrade through public-repository PR #1; main CI and production deploy passed. Neon is at migration 005. Railway API and the new separate durable worker are both successful; the API embedded claimer is disabled. Added tool/argument/result provenance and Google read-after-write verification, partial-run truthfulness, dense+full-text rank fusion with lexical cold-start fallback and citation lineage, in-app governed-improvement decision badges, and GitHub worker deployment. Grafana Cloud Alloy remains externally blocked only by the three absent remote-write credentials; the empty service is intentionally not launched. Follow-up guardrails again pass (31 backend tests, 20/20 golden planner, flake8, Next lint/build).
- 2026-07-19: Added layout-aware PDF/table/OCR lineage, speaker-aware Meet transcript chunking, versioned chunk replacement, reusable retrieval metrics/gate CLI, and run-scoped Google idempotency reconciliation for Gmail, Calendar/Meet, Docs, Sheets, and Chat. Unit coverage is 29/29 for this checkpoint. Production API/worker/frontend from PR #2 are successful and labelled with immutable deployment commit `058f18c`.
- 2026-07-19: Added bounded dependency-aware read concurrency (default 3, hard-capped at 8), preserved serial mutation chains, and made transient batch retry/partial classification consistent. A PostgreSQL integration test proves independent Gmail/Drive steps overlap. This checkpoint passes 30 unit and 5 integration tests plus compile/lint/diff guardrails.
- 2026-07-19: Added deterministic prompt-injection line removal for untrusted retrieved Google content and database-serialized per-user/hour/global durable-run abuse limits. Corrected the concurrency test to assert overlap without assuming scheduler order. The combined hosted-equivalent suite passes 37/37.
- 2026-07-19: Wired database feature flags into durable-run, pilot-cohort, and new-RAG routing; added stable percentage/allow/deny cohorts, protected admin flag APIs, and an unoverrideable live-RL lock. Added app-attributed Groq daily budget/reserve preflight for mutations. The suite passes 38/38 plus Next lint/build.
- 2026-07-19: Added protected browser controls for pilot percentage, explicit pilot allowlist, pilot enable/disable, and new-RAG rollout, with administrator audit identity and a visible live-RL lock. LangSmith read-only verification succeeded with an existing production trace; Flutter analyze/test and Next lint/build pass.
- 2026-07-19: Expanded required GitHub CI into parallel backend, web, and Flutter jobs. It now gates integration tests, flake8, 20-case golden planning, migration downgrade/forward repair, Compose validation, tracked/history secret filenames, Next lint/build, Flutter analyze/test, and a debug Android build.
- 2026-07-19: Captured a sanitized production baseline in `docs/PRODUCTION_BASELINE_2026-07-19.md`: 35 legacy tool attempts, 45.7% tool-error rate, 595 ms mean/1,235 ms p95 tool time, 11 feedback rows, and historical RAGAS-like aggregates. New durable metrics begin at zero and therefore cannot yet justify learning/promotion.
- 2026-07-21: Sprint 28 core is implemented: exact metadata-only Gmail sender extraction, universal model-facing tool-result projection, token measurement/compaction, and typed `model_context_length` failures. Full private raw-result artifact storage remains separate future work; raw results are kept only in the executing process for verification and compact durable output is persisted.
- 2026-07-21: Sprint 29 is operational at occurrence, exact fingerprint-cluster, and cross-cluster theme levels. The inactive category-only generator remains audit-compatible but is not called. Both incident and architectural-theme Option A/B choices now create governed build requests; Active/History, metrics, reporting views, and notification ledgers are present.
- 2026-07-21: Sprint 30 now uses a Groq-only adaptive builder. Because Railway rejected another service at the current plan resource limit, isolated generation was moved to on-demand GitHub Actions with repository/Groq access only—no Neon, OAuth, Railway, or raw Workspace credentials. Generated files stay untrusted until a human publishes the frozen draft PR and trusted no-secret CI attests its exact commit, hashes, commands, and results.
- 2026-07-21: Sprint 31 routing, sticky assignment, executor-version claims, human gates, measured comparison, safety tripwire, queued-run rollback, and trusted deployment evidence are implemented. Live candidate activation is intentionally blocked until Railway can provision `google-connector-candidate-worker`; the current plan returned `Free plan resource provision limit exceeded`. Planner/API-changing candidates are also rejected by the worker-only deploy target until an isolated candidate API/gateway or worker-side planning path exists.
- 2026-07-21: Sprint 32 immutable trusted OKF bundle snapshots, provenance, validation, per-run pinning, and trusted-only retrieval are implemented. Automated OKF draft-to-independent-human-publication remains incomplete and must not be represented as automatic trusted publication.
- 2026-07-21: Sprint 33 quantized knapsack context packing is implemented behind the disabled `dp_context_packing` flag with exact tokenizer costs, bounded candidates, reconstruction, greedy fallback, and offline comparison. One synthetic case improved evidence value by 0.39 and one tied; 28 policy cases are below the 30-case promotion threshold, so DP is not enabled in production. Model/workflow allocation and batch scheduling remain later experiments.
- 2026-07-21: Migration 012 round-trips locally; 97 backend tests, 28/28 planner cases, 4/4 workflow replays, Python compile/flake8/Bandit, Next lint/build, Flutter CI, Compose, secret-history, dashboards, and Docker API/worker/builder image gates pass. GitHub CI and deployment pass at `003cea36fd064bb88d33168f86e728ac3d3abaae`; Railway API/worker and Vercel frontend are healthy. Grafana Cloud dashboard publication remains blocked because `GRAFANA_SERVICE_ACCOUNT_TOKEN` is not present locally or in Railway; OTLP/Alloy ingestion credentials are a different credential and remain configured.
- 2026-07-21: Sprint 28 is completed with metadata-only Gmail sender extraction, universal approved-field result projection, conservative preflight budgeting, and encrypted tenant-scoped private result references with expiry/export/deletion rules. Tokenizer initialization is now image-baked and lazily fail-safe so a first-run CDN interruption cannot crash API or workers; DP falls back to greedy when exact tokenization is unavailable.
- 2026-07-21: Sprint 29 now preserves occurrence -> versioned concrete cluster -> evidence-thresholded architectural theme. Rejected, expired, rolled-back, and failed-build theme candidates release the theme for later evidence; only production attestation resolves it. The category-only generator is retained for historical compatibility but is absent from the live analyzer loop.
- 2026-07-21: Sprint 30 has bounded Groq repository tools, adaptive roles, independent review for multi-role work, tool-extension design surfaces, frozen hashes/diffs, and trusted multi-job CI attestation. Generation runs in an ephemeral credential-minimal GitHub checkout and has no production OAuth, Neon, or Railway credentials; a hardened container-level egress sandbox remains a follow-up defense-in-depth item.
- 2026-07-21: Sprint 31 now has a separate dormant Railway candidate project/service, exact source/digest/runtime readiness attestation, version-specific worker claims, applicability-bounded routing, cleanup scaling, measured rollback, and production API+worker attestation. Worker-compatible code, prompt/config, and OKF candidates are operationally modelled; planner/API/frontend code candidates remain blocked until an isolated candidate API/gateway or worker-side planning path exists. No real candidate was deployed or activated without human approval.
- 2026-07-21: Sprint 32 independent OKF publication approval is implemented for pure and mixed code/knowledge candidates, including immutable overlays, governance/tool/secret/PII/injection validation, canary/trusted/rollback lifecycle, per-run pinning, portal controls, and reporting. Knowledge cannot add executable authority.
- 2026-07-21: Migration 013 round-trips 013 -> 012 -> 013. Current local evidence is 108 total tests, 28/28 planner cases, 4/4 no-network workflow replays, clean compile/flake8/Bandit, 24 valid Prometheus alert rules, valid dashboards/Compose, Next lint/build, Flutter analyze/test/debug APK, no-network API/worker/builder imports, and healthy rebuilt Docker API/worker/Prometheus/Grafana. The dependency audit service timed out against both PyPI and OSV and must be revalidated by trusted CI. DP context packing remains disabled: only two packing cases exist and the 28-case policy sample is below the 30-case promotion floor. Offline multiple-choice workflow and periodic quota DP now pass their bounded risk/fairness fixtures but have no live authority.
- 2026-07-19: Audited the production RAG migration and found 1,152 Gmail plus 13,367 Drive legacy vectors but zero tenant-safe chunks. Added a dry-run/apply/rollback importer that assigns legacy vectors only to an explicit original owner with ACL and lineage; it never makes old single-user data globally searchable.
- 2026-07-19: Applied the reversible legacy import for the documented original owner: 14,521 tenant-scoped chunks (1,152 Gmail, 13,367 Drive, 2 Calendar). A live hybrid query returned owner results while the same query for an unrelated user returned zero, proving cross-user isolation.
- 2026-07-19: Replaced generic service execution steps with explicit validated operations and per-step tool allowlists, while preserving mixed-workflow dependencies and read retry semantics. Added verified Drive trash support. Deploy workflow now labels API and worker with the exact Git commit automatically.
- 2026-07-19: Added a deterministic no-network Google Workspace mutation simulator and versioned replay suite covering idempotency, dependency propagation, retry, partial completion, breaking-point detection, and compensation; wired it into backend CI.
- 2026-07-19: Made the browser restore active durable runs after refresh, resume failed/partial runs from the failed step, and retain/show verified artifact links even when a later workflow step fails.
- 2026-07-19: Strengthened canary evaluation with minimum sample, failure, cancellation, side-effect integrity, p95 latency, and token guardrails; persisted every conclusion, automatically rolled back regressions, and serialized concurrent evaluators so each canary concludes exactly once.
- 2026-07-19: Added migration 006 with complete read-only DBeaver reporting views/ER map and grants, plus a tenant-scoped account export that excludes OAuth ciphertext and vector embeddings; corrected Docker reporting access to explicit IPv4 to avoid Homebrew PostgreSQL collisions.
- 2026-07-19: Expanded the OKF layer to 12 linked capability/workflow/policy/schema/metric/failure/runbook/RAG concepts, made publication status and human approval metadata determine trust, validated registered tool references/links/secrets/public PII, and added deterministic draft generation from runtime tools, OAuth scopes, and metrics without automatic publication.
- 2026-07-19: Added explicit failed-model-call and rate-limit telemetry, fallback transition events, separate latency/usage attribution for primary and fallback calls, and tests proving safe reads may use the approved small fallback while complex/high-risk writes pause instead of silently degrading.
- 2026-07-19: Fixed RL-ready dataset governance so consented trajectories recursively redact requests/plans/incidents/comments before being marked sanitized, retain only structured step/tool metadata, and use stable user-level 80/10/10 splits to prevent session leakage; production mutations remain excluded from exploration.
- 2026-07-19: Added migration 007, workflow/policy evaluation facts, cleanup requests, notification ledger, governed artifact compensation, filtered tenant-safe history, complete live-run UI, plan-quality and multi-objective evaluation, human-activated low-risk Thompson assignment, and sanitized email/GitHub draft-proposal publishers. The final local gate passes 57/57 backend tests, 20/20 golden plans at 1.0 correctness, 4/4 mutation replays, zero policy regressions with promotion correctly blocked below 30 samples, Python lint/audit/Bandit, npm audit/lint/build, Flutter analyze/test/APK, migration 007 round-trip, Docker health, two Prometheus targets, 12 alert rules, two Grafana dashboards, eight upgrade tables, and 14 reporting views.
- 2026-07-19: Merged PR #17 as `ac1ee81`; PR and main CI pass all backend/web/Flutter jobs. Railway API and worker deployments are successful on that immutable version, Vercel and its backend health proxy return HTTP 200, production Neon is at 007 with eight upgrade tables and 14 reporting views, OAuth redirects through Vercel with PKCE and Workspace/Meet scopes, unauthenticated run/admin access returns 401, production metrics are reachable, and LangSmith read access passes. Grafana Cloud/Alloy and optional external improvement notifications remain credential-gated exactly as documented.
- 2026-07-20: Closed the strict completion-audit engineering gaps: atomic embedding admission/backpressure, corrected HTTP/Google failure taxonomy, reconnect/cancellation coverage, Meet routing fixes, privacy-safe structured logs and route-template metrics (including rejected requests), raw access-log suppression, optional safe OTLP tracing, OAuth/RAG/build/admission telemetry, 16 alert rules, 34 Grafana panels, and default-deny mounted private OKF. The exact production image passes 69/69 tests; planner golden coverage is 22/22; migration, Python/web/mobile/security/replay guardrails all pass. Only credential-, GUI-, pilot-, and verified-production-data-dependent conclusions remain open and are enumerated in `docs/COMPLETION_AUDIT_2026-07-20.md`.
- 2026-07-20: Completed Grafana Cloud production observability: Railway Alloy metrics, Tempo API/worker traces, bounded-cardinality sanitized Loki request logs, the 15-panel aggregate dashboard, the restricted 19-panel Neon session dashboard, a dedicated `grafana_reader`, all 19 SQL panels verified, 17 healthy alert rules, and administrator email routing. Added migration 008 so RAG evaluation excludes non-RAG tasks, records metric provenance, gates regression on ten valid samples, and separately alerts on insufficient evidence. Production API/worker are healthy on `778639a`; Neon is at 008; local and production guardrails pass.
- 2026-07-20: Re-audited OKF against Google's official v0.1 draft and corrected reserved-file semantics: root `index.md` now declares `okf_version`, reserved index/log files are not indexed as concepts, minimal `type`-only concepts are consumable, broken links are tolerated, and the stricter ownership/approval profile is enforced only for production synchronization. Expanded the project-grounded DSA/DP/OKF course. Final local evidence is 70/70 exact-image tests, 55/55 host unit tests, 22/22 golden plans, 4/4 mutation replays, migration 008 round-trip, clean lint/Bandit/dependency audits, Next lint/build, Flutter analyze/test/APK, 17 valid alerts, and two healthy Prometheus targets.
- 2026-07-20: GitHub Actions run 29754230916 completed successfully across backend, web, and Flutter, including the Android debug build. Captured the production evidence deficit and exact canary, policy, RAG, pilot, privacy, automatic rollback, and human publication gates in `docs/PILOT_AND_LEARNING_GATE_2026-07-20.md`; no pilot or learning winner is claimed from the current one-user/one-run dataset.
- 2026-07-20: Completed the requested post-upgrade teaching phase through this repository: DAG/topological execution, durable state machines and queues, DP context packing, idempotency, chunk trees/windows, HNSW/hybrid retrieval, memoization, compensation, rate limiting, consistent hashing, bandits/MDPs/offline RL, and practical OKF v0.1 governance. The final strict audit confirms all autonomous engineering work is complete; only secure-vault GUI entry, real pilot evidence, and confirmation-gated external publication remain externally dependent.
- 2026-07-20: Diagnosed the production Improvement portal's vague load failure from bounded route telemetry: all observed admin requests were HTTP 401, so no database query failed. Updated the portal to distinguish expired/missing sessions from non-admin access, clear expired tokens, offer Google reauthentication, stop presenting unauthenticated default rollout values, and hide protected controls until authorization succeeds. Commit `cd03279` deployed successfully to Vercel and Railway; live portal and health endpoints return 200, and web/backend CI pass.
- 2026-07-20: Audited Sprint 24 and corrected an overstated completion claim: recurring-failure recommendations were diagnosis-level text with synthetic candidate labels, not implementation candidates. Migration 009 now marks them `diagnosis_only`, stores concrete candidate files/hashes/manifests/validation/deployment evidence, blocks canary approval and activation without those proofs, publishes real files in governed draft PRs, and requires change-request notes. Fixed durable product-information routing with a trusted registry-derived responder that makes zero Google/RAG/LLM calls. PR #20 merged as `11cd477`; PR and main backend/web/Flutter CI, production deployment, Railway API/worker health, Vercel, and Neon 009 pass. The existing execution proposal is truthfully `diagnosis_only:none` and was not approved or activated.
- 2026-07-20: Added an offline source-aware chunk-policy gate for 256/512/768/1024-token hypotheses with compact source fixtures, token/overlap accounting, retrieval metrics, evidence and lineage validation, and CI coverage. The current synthetic suite passes every policy but explicitly selects no production winner; live Neon still has zero valid `rag_evaluation` samples. Reverified the installed password-free DBeaver definitions and the macOS-Keychain-backed `dbeaver_analyst`: reporting access succeeds, transactions are read-only, and OAuth credential reads are denied.
- 2026-07-21: Merged and deployed Sprint 27 through PR #24 at production commit `b08c8b5`: guarded Workspace conversation routing, contextual Gmail -> Sheet -> Chat + Calendar/Meet DAG planning, durable per-occurrence failure intelligence, two-option human review, guarded auto-draft mode, migration 010, portal inbox, reporting views, metrics, alerts, and Grafana definitions. PR and main backend/web/Flutter CI, Railway API/worker, Vercel, OAuth initiation, Docker Desktop, PostgreSQL migration round-trip, 28/28 goldens, and 18 integration tests pass.
- 2026-07-21: Reverified all three installed DBeaver connections against their real endpoints. Neon uses the Keychain-backed `dbeaver_analyst`, SSL, and server read-only mode; it exposes 17 reporting views including all Sprint-27 failure views and denies OAuth ciphertext. Homebrew and Docker both expose the same 17-view reporting schema. Began the approved teaching phase with project-specific DSA and OKF guides in `docs/`.
- 2026-07-21: Repaired historical failure analysis for PostgreSQL `Decimal` telemetry and JSON/JSONB normalization; backfilled all five recent production failures into the sanitized two-option review inbox with twenty audited notification-ledger entries.
- 2026-07-21: During the project-grounded queue/lease lesson, closed mid-step worker-crash recovery: bounded reads requeue safely, exhausted reads fail recoverably, and ambiguous external writes enter an audited reconciliation state that blocks blind resume and duplicate side effects.
- 2026-07-21: During the RAG lesson audit, corrected the previously overstated parent-child implementation. Migration 011, Gmail/Docs/Drive v3 chunkers, incremental parent tombstones, tenant-scoped expansion, matched-child citations, bounded recency reranking, an 18th DBeaver reporting view, and cross-tenant integration coverage now make small-child retrieval plus larger-parent generation context real.
- 2026-07-21: Detected that a host PostgreSQL process had reclaimed Docker's documented port 5433. Rebound Docker PostgreSQL to loopback-only `127.0.0.1:55432`, updated the installed DBeaver datasource, and live-verified Neon read-only, Homebrew, and Docker at revision 011 with all 18 reporting views.
- 2026-07-21: PRs #29–#35 completed and deployed the Groq-only governed builder, typed draft normalization, isolated API+worker candidate runtime, stable signed candidate routing, active-deployment runtime attestation, deterministic dual-worker rollback simulation, and production migration 013. Production API/worker/Vercel and main CI pass at `7be6548`; 89 unit and 24 database-backed integration tests pass locally. A real reviewed Gmail/durable-worker incident selected option A and queued build `f651b854`; the build is retryable after Groq 70B free-tier quota reset. Candidate Railway deployment remains correctly blocked until a project-scoped `RAILWAY_CANDIDATE_TOKEN` is created in the candidate project's production environment. No implementation candidate, pending OKF overlay, active canary, or promotion currently exists, so none was falsely approved.
- 2026-07-21: Candidate-runner failures now report sanitized stage/type/retry timing through a trusted callback, return retryable failures to `queued`, terminate non-retryable drafts truthfully, and create internal admin/Grafana ledger events. This closes the previously observed stuck-`investigating` state after Groq 429, callback 502, or schema rejection; no model error text, Workspace content, or credential is persisted.
- 2026-07-21: The governed patch builder now keeps `llama-3.3-70b-versatile` as its primary model but may fall back only within candidate generation to Groq-hosted `openai/gpt-oss-120b` after a provider rate limit. Short per-minute limits receive one bounded retry; long daily-limit waits advance immediately. Normal user workflow routing is unchanged, every successfully used builder model is recorded in the durable checkpoint and candidate manifest, and unapproved/unavailable models fail closed.
- 2026-07-21: Groq SDK status failures are now classified by sanitized HTTP status: 429/5xx remain retryable while 4xx fail closed, with no provider body persisted. Explicit reruns may recover the one legacy terminal `APIStatusError` build created before this classification existed, but only when no candidate commit exists; future terminal validation/permission failures remain unavailable.
- 2026-07-21: A read-only production checkpoint identified the fallback failure as HTTP 413. The builder now applies source-shaped projection to repository listings, searches, reads, and diffs; removes staged file bodies from subsequent model history while retaining their hashes; caps each returned tool payload; and compacts oldest tool results under a 50,000-character cumulative request boundary. Repository state remains in the bounded in-memory tool, so generated files are not lost and oversized histories fail before the provider call.
- 2026-07-21: The remaining 413 path was isolated to independent review of a complete multi-file candidate. Reviewers now receive only paths, change types, sizes, hashes, short previews, rollback, validation commands, and a bounded diff preview; a least-privilege `read_staged_candidate_file` tool provides exact on-demand line ranges from in-memory state. Reviewer revisions are re-frozen from that authoritative staged state, preserving independent inspection without bulk prompt duplication.
- 2026-07-21: Groq continued returning HTTP 413 below the earlier 50,000-character boundary, so builder preflight is now adaptive: ordinary accumulated history is capped at 24,000 characters and individual projected results at 4,000; a provider 413 receives exactly one same-model retry after compaction to 12,000 characters and a 2,048-token completion ceiling. A second 413 fails closed. The behavior is candidate-builder-only and covered by a simulated Groq status test.
- 2026-07-21: The adaptive retry removed the 413 but exposed an indistinguishable bounded `RuntimeError`. Candidate failures now publish deterministic sanitized guard codes for history, author/reviewer token budgets, tool-round limits, invalid JSON, review rejection, and unknown bounded runtime failures. Raw model/repository text remains excluded; one explicit compatibility retry is permitted for the legacy generic RuntimeError record created before these codes existed.
- 2026-07-21: The next exact failure was Groq HTTP 400 during local tool generation. Following Groq's documented recovery, the builder detects only the presence of `failed_generation` (never retaining its attempted arguments), retries once at temperature 0 with parallel tool calls disabled, and then fails closed. Other 400 responses are never retried. The local bounded tool registry remains the final authority, so disabling parallel generation does not expand executable tools.
- 2026-07-21: The malformed-tool retry advanced to the exact `tool_token_budget_exhausted` guard. Multi-role candidate builds with an approved fallback chain now receive a 24,000-token effective ceiling while preserving the original stored budget and recording actual usage/models; single-model builds remain at their stored ceiling. No user chat, planner, executor, RAG, or runtime quota policy changed.
- 2026-07-21: After expanding the budget, Groq again rejected a generated tool call with HTTP 400. Only the already-triggered `failed_generation` retry now sets Groq `disable_tool_validation`; initial calls remain provider-validated. This does not widen authority: returned names and JSON arguments still pass through the local six-tool allowlist, path restrictions, size/call/time limits, and in-memory-only handlers, with unknown/malformed calls converted to bounded error results.
- 2026-07-21: A repeated provider-native malformed-tool response now switches the candidate builder to a JSON repository-action protocol instead of terminating the governed patch lifecycle. The same quality model may request one bounded repository action per turn, while all names and arguments still pass through the existing local allowlist, path/read/write/size/time limits, projection, and in-memory-only staging. Provider tools are removed in fallback mode, responses are forced to JSON, staged bodies remain compacted in history, and normal agent/model routing is unchanged.
- 2026-07-21: The first real JSON-protocol run advanced past malformed provider tools and exposed `history_budget_exhausted`. History compaction now recognizes projected repository results by their semantic `tool_result` envelope instead of relying only on the native `tool` role, preserving tool identity/call provenance while removing older result bodies. Explicit reruns may recover that exact pre-fix guard; unrelated terminal failures remain closed.
- 2026-07-21: With semantic compaction active, the real builder advanced to the exact `tool_token_budget_exhausted` guard after iterative repository inspection. The builder-only fallback ceiling is now 48,000 measured input-plus-output tokens so a bounded 12-round author/reviewer workflow can finish; the stored job budget, actual usage, model chain, history cap, time limit, repository-tool limits, and CI/human gates remain durable. No user chat, planner, executor, RAG, or general Groq routing budget changed.
- 2026-07-21: Repeated governed attempts exhausted the separate Llama 70B and GPT-OSS 120B free-model quotas before the 48K run could start. Groq's production `qwen/qwen3-32b` is now the third and final candidate-builder-only fallback because its official model card supports complex coding, 128K context, local tools, and JSON mode. Qwen alone uses the documented 0.6 temperature and hidden reasoning; all successful models remain recorded, while normal application routing is unchanged.
- 2026-07-21: The real fallback returned HTTP 404 because Groq retired `qwen/qwen3-32b` on July 17, 2026. It is replaced by Groq's recommended `qwen/qwen3.6-27b`, whose official card reports flagship agentic coding, 131K context, tool use, JSON mode, and a separate 200K free TPD quota. A model-level 404 now advances only within the configured builder allowlist; the exact pre-fix `NotFoundError` can be explicitly retried, while unrelated terminal failures remain closed.
- 2026-07-21: After the 70B, GPT-OSS 120B, and Qwen 3.6 candidate-only quotas were exhausted during a governed build, `openai/gpt-oss-20b` was added as the final candidate-builder-only fallback. It retains 131K context, reasoning, local tool use, and JSON mode, while all generated files still require deterministic validation and trusted CI before publication. This does not change chat, planner, executor, verifier, or RAG model routing.
- 2026-07-21: Qwen 3.6 ran successfully but consumed all 12 repository turns without finalizing. The builder now reserves its last two turns for JSON-only finalization: repository tools close after round 10, the compact staged-file manifest is supplied, and further tool requests receive a deterministic closed-tools result. This keeps the existing time/round bounds while preventing open-ended investigation; the exact pre-fix `tool_round_limit_exhausted` build can be explicitly retried.
- 2026-07-21: The final Groq fallback completed generation, but its candidate was rejected only at API submission with HTTP 422. Candidate contracts are now preflighted inside the isolated builder with content-free reason codes and one bounded correction turn. Empty or malformed file sets, unsafe paths, oversized content, secret-like assignments, invalid rollback plans, and invalid validation-command lists can no longer reach candidate registration; trusted CI remains the only authority that may attest success.
- 2026-07-21: The exact legacy `submission` + `HTTPStatusError` + sanitized HTTP 422 signature may be leased once more after the preflight deployment. Arbitrary callback failures and locally rejected candidate contracts remain terminal, so the compatibility path cannot become a general retry bypass.
- 2026-07-21: A governed retry reached the outer eight-minute process guard before it could report failure, leaving the durable lease in `investigating`. Author investigation is now bounded to eight rounds and independent review to five, with the last two rounds of each reserved for JSON-only finalization. A nine-minute internal asyncio deadline reports a sanitized retryable timeout before the ten-minute outer process guard, preventing abandoned leases while retaining a hard CI runtime ceiling.
- 2026-07-21: A minimal no-repository probe isolated GPT-OSS 20B's HTTP 400 to `response_format` being combined with local tools. Candidate-builder GPT-OSS tool turns now omit that incompatible field while retaining provider-validated tool schemas; JSON object mode remains mandatory after tools close for final serialization. No normal application model request is changed.
