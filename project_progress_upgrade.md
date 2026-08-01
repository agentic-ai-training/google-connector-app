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
- [x] Preserve rollback through the existing deployment path. Local migration round-trip,
  rebuilt Docker API/worker/PostgreSQL/Ollama/Prometheus/Grafana, web, and mobile gates pass;
  PRs #62–#63, main CI run `29869216872`, deployment run `29869217061`, and exact
  API/worker/frontend attestation pass at `ca5fc5b39f2783ee377ba17239abdada3211e3a9`.

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
- [x] When the independent reviewer rejects an otherwise structurally valid draft, pass
  only bounded/redacted feedback to one remediation-author turn, freeze the corrected
  files, and require a second independent review. A second rejection remains terminal.
- [x] Checkpoint the remediation role before its first provider call, preserve aggregate
  token/tool/read limits across interruption, and allow the final author round to attempt
  its forced correction instead of failing immediately before that turn.
- [x] Allow an administrator to create a separately identified attempt under a newer
  builder policy from a terminal build. Preserve the original row, reuse only sanitized
  evidence, pin the new attempt to the current control version, audit the supersession,
  and continue to require trusted CI and all human publication/deployment/canary gates.

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
- [x] Threat-model Groq prompt injection, malicious candidate patches, sandbox escapes,
  secret exfiltration, poisoned fixtures, approval replay, CI forgery, worker-version races,
  migration incompatibility, and unsafe rollback.
- [x] Run unit/integration/no-network/golden/replay/policy/RAG/DP/candidate/sandbox/migration,
  Python lint/security/dependency, web, Flutter, Compose, secret/history, CI and production
  smoke gates; verify exact deployed versions and rollback evidence.
- [x] Re-audit every Sprint 28–34 story against authoritative files, database state, CI,
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
- 2026-07-21: Sprint 28 core first introduced exact metadata-only Gmail sender extraction, universal model-facing tool-result projection, token measurement/compaction, and typed `model_context_length` failures. The later migration 013 checkpoint below completed encrypted tenant-scoped private result storage; this historical checkpoint no longer describes the final state.
- 2026-07-21: Sprint 29 is operational at occurrence, exact fingerprint-cluster, and cross-cluster theme levels. The inactive category-only generator remains audit-compatible but is not called. Both incident and architectural-theme Option A/B choices now create governed build requests; Active/History, metrics, reporting views, and notification ledgers are present.
- 2026-07-21: Sprint 30 now uses a Groq-only adaptive builder. Because Railway rejected another service at the current plan resource limit, isolated generation was moved to on-demand GitHub Actions with repository/Groq access only—no Neon, OAuth, Railway, or raw Workspace credentials. Generated files stay untrusted until a human publishes the frozen draft PR and trusted no-secret CI attests its exact commit, hashes, commands, and results.
- 2026-07-21: Sprint 31 initially had routing, sticky assignment, executor-version claims, human gates, measured comparison, safety tripwire, queued-run rollback, and trusted deployment evidence, while live candidate capacity and API/frontend surfaces were still blocked. Later checkpoints below supersede those initial runtime limitations.
- 2026-07-21: Sprint 32 first added immutable trusted OKF bundle snapshots, provenance, validation, per-run pinning, and trusted-only retrieval. The later independent publication checkpoint below completed the draft-to-human-publication path; this line remains historical rather than a current blocker.
- 2026-07-21: Sprint 33 quantized knapsack context packing is implemented behind the disabled `dp_context_packing` flag with exact tokenizer costs, bounded candidates, reconstruction, greedy fallback, and offline comparison. One synthetic case improved evidence value by 0.39 and one tied; 28 policy cases are below the 30-case promotion threshold, so DP is not enabled in production. Model/workflow allocation and batch scheduling remain later experiments.
- 2026-07-21: Migration 012 round-trips locally; 97 backend tests, 28/28 planner cases, 4/4 workflow replays, Python compile/flake8/Bandit, Next lint/build, Flutter CI, Compose, secret-history, dashboards, and Docker API/worker/builder image gates pass. GitHub CI and deployment pass at `003cea36fd064bb88d33168f86e728ac3d3abaae`; Railway API/worker and Vercel frontend are healthy. Grafana Cloud dashboard publication remains blocked because `GRAFANA_SERVICE_ACCOUNT_TOKEN` is not present locally or in Railway; OTLP/Alloy ingestion credentials are a different credential and remain configured.
- 2026-07-21: Sprint 28 is completed with metadata-only Gmail sender extraction, universal approved-field result projection, conservative preflight budgeting, and encrypted tenant-scoped private result references with expiry/export/deletion rules. Tokenizer initialization is now image-baked and lazily fail-safe so a first-run CDN interruption cannot crash API or workers; DP falls back to greedy when exact tokenization is unavailable.
- 2026-07-21: Sprint 29 now preserves occurrence -> versioned concrete cluster -> evidence-thresholded architectural theme. Rejected, expired, rolled-back, and failed-build theme candidates release the theme for later evidence; only production attestation resolves it. The category-only generator is retained for historical compatibility but is absent from the live analyzer loop.
- 2026-07-21: Sprint 30 has bounded Groq repository tools, adaptive roles, independent review for multi-role work, tool-extension design surfaces, frozen hashes/diffs, and trusted multi-job CI attestation. Generation runs in an ephemeral credential-minimal GitHub checkout and has no production OAuth, Neon, or Railway credentials. Trusted builder code can reach only the Groq and candidate-callback application paths during generation; generated files are never executed there. Host-level runner egress filtering remains documented defense in depth rather than generated-candidate authority.
- 2026-07-21: Sprint 31 added a separate dormant Railway candidate project/service, exact source/digest/runtime readiness attestation, version-specific worker claims, applicability-bounded routing, cleanup scaling, measured rollback, and production API+worker attestation. Subsequent checkpoints completed planner/API routing and then isolated frontend preview routing. No real candidate was deployed or activated without human approval.
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
- 2026-07-22: Production evidence from two high-risk multi-role builds exposed that reviewer envelopes (`approved`, `reason`, optional `revised_candidate`) were incorrectly subjected to the author file-contract validator. The reviewer now has its own bounded envelope validation and role-correct finalization prompt. An approval without edits preserves valid author files; staged reviewer revisions replace them only when files actually exist. Final author files are revalidated after review. Tool policy v2 permits a narrow retry of `candidate_contract_invalid` builds created under defective v1 without making future invalid candidates generally retryable.
- 2026-07-22: Exact retry `7aa74d5b` passed the repaired reviewer boundary but Groq returned HTTP 400 during generation after 201 seconds. Candidate generation now classifies 400s without persisting provider text, compacts and retries `model_context_length` requests at a 12,000-character/2,048-output-token ceiling, and advances other model-specific 400s only through the approved candidate-builder fallback list. Ordinary chat/planner/executor routing is unchanged. Local evidence: 119 unit tests, 24 database-backed integration tests, flake8, compileall, and diff guardrails pass.
- 2026-07-22: The typed retry exposed `tool_generation_failed` after native-tool recovery had already switched to the local JSON repository-action protocol. JSON-protocol generation now gets one bounded plain-JSON retry without provider `response_format` validation, followed by the approved candidate-only model chain. Local parsing and the same repository action/path/size/call limits remain authoritative; no shell, production credential, Workspace API, or normal agent model policy is added.
- 2026-07-22: The Sprint-30 completion audit found that the Groq builder could stage and diff files but lacked the separately promised model-visible validation and rollback operations. It now exposes deterministic staged-candidate policy/syntax validation, content-free manifests with SHA-256/byte facts, and in-memory discard/rollback. Python is parsed through AST and JSON/YAML/TOML through non-executing parsers; the model still receives no shell, arbitrary command, trusted write, credential, or deployment authority, and trusted CI remains mandatory.
- 2026-07-22: The completion audit also corrected stale documentation that still described planner/API candidates as unsupported. Current code classifies runtime surfaces, deploys `Dockerfile.candidate` in the isolated Railway project, creates a public domain only for API candidates, verifies the exact candidate health/version, and routes eligible creation/resume requests through stable control-side cohort selection. At this checkpoint frontend candidates were still unsupported and worker-only evidence could not attest an API/frontend candidate; PR #62 later superseded that limitation with an isolated preview router.
- 2026-07-22: A working one-token probe on every approved Groq builder model followed by an immediate zero-usage 429 in GitHub isolated the quota defect to oversized per-turn completion reservations, not an invalid key. Tool turns now reserve at most 2,048 tokens, final serialization at most 4,096, and a long-window 429 receives one compact 1,024-token same-model retry before fallback. This preserves the aggregate build budget and quality allowlist while preventing small candidates from failing merely because every exploratory turn requested 6,000 output tokens.
- 2026-07-22: Retryable GitHub candidate builds no longer require manual babysitting after the Railway builder was replaced by isolated Actions. The control improvement loop now claims due queued retries with `FOR UPDATE SKIP LOCKED`, honors Groq's sanitized reset duration (or a conservative 30-minute rate-limit default), records retry count/dispatch state, and dispatches only the opaque build ID. Concurrent/manual dispatch remains safe because the callback input lease transitions only one runner to `investigating`; all human candidate/canary/OKF/promotion gates remain unchanged.
- 2026-07-22: Closed the remaining frontend-candidate runtime gap. Frozen frontend commits now deploy as non-production Vercel previews with project/deployment/metadata/source verification and an exact version-bound frontend health route. The control API returns an attested target only to the stable active cohort; the browser transfers its existing session in a URL fragment, returns to control when routing is removed, and cleanup deletes the retired preview after routing stops. API, worker, frontend, prompt/config, and trusted OKF candidates now all have explicit isolated execution or versioned-registry paths. Added a complete Sprint 30–34 threat model with residual risks and fail-closed recovery.
- 2026-07-22: The first production smoke of the frontend-health route exposed that ordinary Vercel production deploys did not inject the immutable commit even though Railway did. The deploy workflow now pins both build-time and runtime `DEPLOYMENT_VERSION` plus the control role, so production frontend evidence can be matched to the same Git commit as API and worker.
- 2026-07-22: PR #62 (`b4151815`) completed isolated, immutable Vercel-preview execution and stable cohort handoff for frontend candidates; PR #63 (`ca5fc5b`) completed joint production API/worker/frontend attestation. Main CI run `29869216872` and deploy run `29869217061` pass, and both public health surfaces report the exact control commit `ca5fc5b39f2783ee377ba17239abdada3211e3a9`. A clean Docker Desktop rebuild runs API, version-specific worker, PostgreSQL 16/pgvector 0.8.5 at migration 013, Ollama with a live 768-dimensional embedding probe, Prometheus with both targets up and 24 valid alert rules, and both provisioned Grafana dashboards. The dated Sprint 28–34 requirement audit is `docs/COMPLETION_AUDIT_2026-07-22.md`. The selected real candidate build `7aa74d5b` remains safely queued with no files, commit, deployment, canary, or side effects after Groq exhausted every builder-only quality-model quota; durable retries honor the provider reset window automatically.
- 2026-07-22: Operational follow-up found that repeated quota retries were durable only at the build level: every ephemeral runner restarted the multi-role author/reviewer conversation from zero, discarded completed author work, and could consume newly released capacity before a later turn failed. The builder now freezes validated author files and sanitized phase metadata in the existing candidate tables, resumes directly at independent review, progressively reduces only the rejected turn's output reservation from 2,048 to 1,024/512/256 tokens, replaces partial files atomically when the final reviewed draft is stored, and exponentially backs repeated rate-limit retries up to six hours. Checkpoints remain untrusted, contain no Workspace content or production credentials, execute no generated code, and cannot satisfy CI, deployment, canary, promotion, or OKF approval gates. Local evidence: 127 unit tests, 26 PostgreSQL integration tests, all 153 together, complete evaluation/security/client gates, and a database-backed checkpoint-to-final-draft lifecycle test pass.
- 2026-07-22: The first v5 production retry still exhausted quota before the author-complete boundary, proving phase-only durability was insufficient. Tool policy v6 now checkpoints after every accepted author or reviewer turn: bounded compacted conversation, next round, protocol mode, staged files, cumulative token/model use, and non-replenishable tool-call/read-byte counters. Resume continues at the next round and retains the original aggregate token/round/tool limits. Invalid free-form model output is represented only by content length and SHA-256 before persistence. No checkpoint contains Groq credentials, Workspace evidence, trusted test claims, execution access, or approval authority. A simulated ephemeral-runner interruption resumes without replaying its completed model/tool turn, and PostgreSQL coverage proves empty-file investigation state, author completion, and final reviewed freezing remain atomic.
- 2026-07-28: A production-history audit found that all real builder attempts were
  terminal and that the strongest attempt reached independent review but had no
  review-to-repair transition. Builder policies v2/v7 add one bounded remediation-author
  pass followed by mandatory independent re-review, a pre-provider remediation
  checkpoint, and a final-round correction opportunity. The portal also exposes an
  audited new-policy attempt that clones only sanitized evidence into a fresh build while
  keeping the terminal source immutable; it grants no publication, deployment, canary,
  promotion, or OKF authority.
- 2026-07-22: Candidate retry visibility is now first-class in the protected improvement portal. Its API projects only sanitized operational fields—retry count/type/stage, next eligibility, dispatch state, durable role phase/round, token use, and frozen-file count—while excluding the checkpoint conversation and sanitized builder input. The browser renders the local eligibility time and refreshes it with the existing 30-second poll. Full local backend evidence is 130 unit plus 26 PostgreSQL integration tests (156 total); web lint and production build pass.
- 2026-07-22: The new visibility exposed a stale retry-dispatch marker that remained `dispatching` after GitHub accepted a workflow and after its runner returned to quota wait. The durable dispatch state machine now records `dispatched`, `runner_leased`, `waiting_for_retry`, `completed`, `terminal`, or dispatch `failed` at the authoritative transition. These markers never contain generated content or credentials and do not alter the independently calculated retry deadline. The full 156-test backend suite and web lint/build pass.

## Sprint 35 — Write-contract, verification, reconciliation, and builder reliability audit

The 2026-07-27 reliability blueprint is implemented without a schema migration. Existing
step output, artifact, checkpoint, event, and notification fields are sufficient.

| Requirement | Implementation | Regression evidence | Status | Migration / rollout and rollback |
|---|---|---|---|---|
| Allowed-tool ceiling separated from required-write obligation | `app/tools/contracts.py`, planner, state, worker | `tests/unit/test_core.py` write-contract cases | Implemented | None; disable the new executor path or revert code |
| One missing-tool-only correction; no prose success | `app/agents/supervisor.py` | tool-free correction and second-answer tests | Implemented | None; revert supervisor policy |
| Distinct selection, execution, and postcondition failures | errors, worker, verifier | unit verifier and service-agent tests | Implemented | None; historical `verification` remains compatible |
| Pre-tool model quota remains distinct from verification | agent errors/supervisor, worker, incident completion | high-risk quota and zero-side-effect tests | Implemented | None; resume reconciles the exact step after quota recovery |
| Typed service failures survive the LangGraph state boundary | `app/agents/state.py`, supervisor graph | compiled-graph structured-error propagation test | Implemented | None; state-schema declaration only |
| Sheets exact range/value readback | `app/runs/verifier.py` | Sheet create/write/append/mismatch tests | Implemented | None; verifier rollback only |
| Calendar time/timezone/attendee/Meet readback | verifier and Calendar registry | Calendar match/mismatch tests | Implemented | None |
| Chat destination/text-hash/reference readback | verifier | Chat match/mismatch tests | Implemented | None |
| Drive permission type/role/principal readback and idempotency | verifier and registry | Drive match/mismatch tests | Implemented | None |
| Content-free evidence persistence | failure-intelligence sanitizer | redaction/hash unit test | Implemented | None; stricter sanitizer is fail-closed |
| Exact-step resume and three-way reconciliation | reconciliation service, runs API/schema | unit decisions and PostgreSQL exact-resume test | Implemented | None; uncertain writes stay blocked |
| Candidate budget/checkpoint diagnostics | builder, schemas, admin API/UI | budget and sanitized-view tests | Implemented | None; old checkpoints use bounded defaults |
| Native/JSON candidate history integrity | builder history fitter | atomic protocol-exchange, summary-consolidation, and counter-bound tests | Implemented | None; old bounded checkpoints remain resumable |
| Staged-source provenance integrity | builder tools and mandatory author preflight | projected-body overwrite and syntax-correction tests | Implemented | None; unrecoverable legacy placeholder checkpoints stop safely |
| Early/restricted/hard file and finalization gates | builder | typed `files_required` and finalization tests | Implemented | None; revert gate constants |
| Typed builder failures and shared retry authority | builder, retry service, runner/admin | retry eligibility and terminal-policy tests | Implemented | None; server remains authoritative |
| Same checkpoint callback in both builder paths | builder and Actions runner | checkpoint lifecycle tests | Implemented | None |
| Granular intelligence and cross-cluster write themes | failure intelligence and analyzer | sanitizer/analyzer unit coverage | Implemented | None |
| Metrics, alerts, Grafana, and safe portal fields | metrics/collector, monitoring files, admin page | dashboard validation and client build | Implemented; final validation pending | Publish dashboards only through confirmed Grafana sync |
| No-network lost-response, mismatch, resume, and uncertainty replay | replay engine and 13 fixtures | workflow replay script | Implemented, 13/13 local | No production side effects |
| CI compilation before tests and full trusted validation | CI and candidate-validation workflows | workflow inspection plus trusted PR/main runs | Implemented and operational | Revert workflow-only change |

Current local checkpoint: 156 unit/API tests, 28 PostgreSQL integration tests (184
combined), and all
13 workflow replays pass. Python compilation and Flake8, dependency/security checks,
migration 013→002→013 round-trip, Compose, secret/history checks, Next.js lint/build,
Flutter analyze/test/debug APK, Docker API/worker/Prometheus/Grafana health, and two
Grafana dashboard definitions pass. PRs #70–#74 passed the trusted CI/candidate
attestation matrix and their exact merge commits passed joint Railway API/worker and
Vercel frontend deployment attestation. The final staged-provenance preflight hardening
is published only after its own trusted matrix passes.

- 2026-07-27: PR #70 implemented the complete reliability-audit blueprint. Production
  retries then found and closed three independent legacy-resume defects without
  weakening budgets: the callback's misplaced deployment-column lookup (PR #71), a
  tool counter that advanced after rejected calls (PR #72), and native/JSON protocol
  history that was not compacted as one atomic exchange (PRs #73–#74).
- 2026-07-27: Historical no-file builds now terminate as `files_required`; the preserved
  reviewer checkpoint resumes without restarting its author; and build `7aa74d5b`
  progressed from author round 6 through independent review. Its reviewer correctly
  rejected two invalid one-line placeholder files. The follow-up integrity guard rejects
  content-free staged-body projections before they can overwrite source and requires
  deterministic syntax/policy validation before review.
- 2026-07-28: A live multi-service run on the exact `92de67ec` deployment proved the
  Gmail metadata path, clarification, approval, and write-contract planning, then met
  Groq's quality-model quota before any Sheet tool ran. The runtime correctly refused a
  small-model downgrade for the high-risk workflow, but its caught exception was
  mislabeled as generic verification. Pre-tool quota exhaustion is now a typed,
  recoverable `rate_limit` at the model-router boundary; a write with zero tool attempts
  retains 100% side-effect integrity and does not claim that artifact review is needed.
- 2026-07-28: Production run `abb47286` on the PR #76 deployment proved the typed
  exception existed inside the service agent but exposed a second boundary defect:
  LangGraph discarded undeclared structured error fields before returning the compiled
  graph result. The graph state now declares category, component, boundary, and sanitized
  evidence explicitly, and a compiled-graph regression test proves all four survive.
  Future high-risk quota pauses therefore remain `rate_limit/model_router` incidents
  instead of degrading to a generic no-tool-result verification diagnosis.
- 2026-07-28: PR #77 deployed the graph-state correction as `23e106a8`. Authenticated
  production run `4086b5a8` then proved the complete boundary: Gmail metadata extraction
  completed, a pre-tool Sheets quota pause retained `rate_limit/model_router`, the
  incident was marked recoverable, and side-effect integrity remained 100%. The Grafana
  dashboard publisher now also consumes the previously documented untracked
  `.env.local` management credential without overriding explicit process variables;
  publication still requires the exact confirmation phrase and never prints the token.

## Sprint 36 — Deterministic Gmail counts and contextual service clarification

Production runs `48a30383`, `2eaf933d`, and `44b52dc4` exposed a routing gap: “how
many senders sent me promotional mails today?” was persisted as an ambiguous,
successfully completed guarded-chat run, so Gmail was never called. The action
classifier recognized search/list/read verbs but not count/how-many intent.

The correction is a general, bounded Gmail count path rather than a hard-coded answer:

- `count` and `how many` are recognized as operational intent, while sender counting is
  selected only when the user asks about senders/people rather than every Gmail count.
- `sender_count` uses `count_gmail_senders`, a metadata-only deterministic tool. It
  projects Gmail category names, computes exact local-day epoch bounds from an IANA
  timezone, reads only the `From` header, and deduplicates normalized addresses.
- The scan is capped at 500 messages by the plan and 2,000 by the tool. Exhausted scans
  return an exact count; capped scans truthfully return a lower bound rather than a
  false exact result.
- The browser supplies its IANA timezone. Non-browser clients that request “today”
  without a timezone receive a material clarification instead of a server-time guess.
- No LLM, Groq token, RAG lookup, body fetch, or embedding is used for this operation.
- A service-only follow-up such as `gmail` may resolve one recent unresolved request,
  but only within the same authenticated user and session, within 15 minutes, and only
  if the combined request becomes a valid action involving that service. Cross-user and
  cross-session context reuse remains impossible.
- The exact production wording is now in the golden planner set. Unit coverage proves
  routing, category/timezone arguments, metadata-only access, and deduplication;
  PostgreSQL API coverage proves contextual resolution and tenant isolation.

Local evidence at this checkpoint: 160 non-database tests pass (29 integration tests
skipped), all 29 PostgreSQL integration tests pass, the 29-case golden planner suite and
13/13 workflow replays pass, Python compilation and Flake8 pass, all offline
chunking/policy/DP/dual-worker evaluations pass, and Next.js lint/production build pass.

## Sprint 37 — Relevance-gated conversation context and compositional work

The durable `/runs` path must support natural follow-ups and bounded Workspace writing
without becoming a general-purpose chat product or silently inheriting old authority.
This sprint adds two explicit planning concerns.

### Epic 37.1 — Analyze every current statement before classification

- A dedicated request-statement analysis component runs for every durable request. It
  analyzes the current statement only and emits normalized text, explicit service cues,
  composition intent, contextual-reference cues, service-only clarification, email
  recipients, and current-turn external-write authority.
- The classifier is required to consume this structured contract and passes the
  resulting intent, services, risk, ambiguities, and action boundaries to the typed
  planner. This component is distinct from conversation-history retrieval.
- A deterministic implementation is the auditable control. A future model-assisted
  analyzer may compete only behind replay, evaluation, canary, and rollback gates.
- A separate `request_analyzed` event and content-free planning diagnostics prove that
  the component ran without copying message text into metric labels or event payloads.

### Epic 37.2 — Include prior conversation context only when relevant

- A separate deterministic conversation-context resolver runs after current-statement
  analysis and before classification for every durable request.
- It first evaluates the current message for a service-only clarification, a referential
  action such as `make it shorter` or `send it`, or an explicit follow-up phrase.
- Only a relevant request may load one recent prior turn, and only from the same
  authenticated user and exact session. Unrelated, cross-session, cross-user, deleted,
  or stale context is never supplied to the classifier, planner, RAG, or model.
- Self-contained requests proceed with their current text alone; this keeps latency,
  prompt size, token cost, and privacy exposure bounded.
- Reused context is reference material only. The current message is the sole authority
  for external writes, approval bypass language, service selection, recipients, risk,
  and other consequential actions. A past `send`, `share`, or `without asking` cannot
  authorize the new turn.
- The raw current request remains the durable audit record. Planning diagnostics and a
  `context_analyzed` event record only content-free relevance facts, source run IDs,
  bounded character counts, and whether prior context was included.
- The analyzer is a required intake component, not another unconstrained LLM agent.
  A learned relevance classifier may later compete behind evaluation and canary gates,
  but the deterministic privacy and authority boundary remains mandatory.

### Epic 37.3 — Typed composition inside Workspace workflows

- Add `composition/compose` as a first-class, tool-free plan step for drafting,
  rewriting, summarizing, outlining, brainstorming, applications, essays, roadmaps,
  pointers, and similar bounded writing related to a Google Workspace outcome.
- Draft-only requests complete through composition without calling Google APIs.
- Combined requests form an explicit dependency DAG, for example:
  `compose application → Gmail send`, or
  `compose roadmap → create Google Doc`.
- Downstream service steps receive the verified, compact dependency output. They must
  not regenerate the content or claim that a prior write occurred.
- Content nouns do not invent service actions: “draft an email asking for a meeting”
  is composition, not a Calendar mutation. Conversely, “schedule the meeting” is a
  Calendar action.
- Writing remains within Google Workspace use cases; unrelated global conversation is
  rejected by the existing guarded conversation classifier.

### Epic 37.4 — Model, safety, and approval policy

- Complex composition routes to the configured Groq reasoning model; ordinary short
  composition may use the configured quality model.
- The small Groq fallback is allowed only for low-risk, single-step composition when
  its existing safety and context limits pass. Complex or mutating workflows do not
  silently downgrade.
- Composition itself has no Google tools and is read-only. Creating a Doc or Sheet is
  a normal reversible Workspace write; sending email/Chat, inviting attendees,
  sharing, deleting, publishing, or otherwise high-risk writing requires the existing
  human approval unless the current request explicitly invokes an allowed opt-out.
- The planner preserves explicit preconditions, postconditions, token budgets,
  dependency outputs, verification, incident capture, and durable resume semantics.

### Epic 37.5 — Acceptance, telemetry, and rollback

- Golden and unit cases cover standalone drafts, combined compose-and-send/create
  flows, referential rewrites, contextual sends, ambiguous service replies, typo
  variants, authority non-inheritance, and cross-tenant/session isolation.
- Empty composition output is a postcondition failure and enters granular failure
  intelligence rather than producing false success.
- Context diagnostics make it possible to measure relevance decisions without storing
  duplicated message bodies in events or Prometheus labels.
- Rollback removes the context analyzer call and composition routing while retaining
  the raw durable requests, events, and legacy control path; no schema downgrade is
  required.

Local evidence at this checkpoint: 169 unit tests and 30 PostgreSQL integration tests
pass; all Python sources compile; Flake8, Bandit, and `pip-audit` pass; the expanded
32-case golden planner set, 13 workflow replays, chunking, policy, greedy/DP packing,
DP allocation, dual-worker, and Grafana-definition checks pass; the migration
downgrade/forward-repair guardrail returns to revision 013; secret-history and Compose
configuration guardrails pass; Next.js lint/build and Flutter analyze/test/debug APK
build pass; and Docker Desktop rebuilds healthy API and ready worker images.

## Sprint 38 — Runtime lineage, delivery-intent, and partial-write safety

Production runs `54b0c2bb` and `58f895d1` on deployment `7347b96` proved that
write contracts alone did not establish resource lineage or semantic delivery intent.
The first run created a Sheet but passed its title as the subsequent spreadsheet ID.
The second asked for Google Chat, but an address domain invented a Gmail step that sent
an unintended email; stale clarification keys then invented a Calendar step.

### Epic 38.1 — Current-turn delivery authority

- [x] Remove recipient email addresses before scanning service nouns so `@gmail.com`
  cannot authorize Gmail.
- [x] Represent explicit Gmail and Google Chat delivery channels separately and prohibit
  one delivery channel from being inferred when the current turn explicitly selects the
  other; multi-channel requests remain possible only when both are stated.
- [x] Add the exact Chat-only email-recipient request and mixed Gmail/Sheet/Chat/Calendar
  request to unit, golden, and replay coverage.

### Epic 38.2 — Run-bound clarification integrity

- [x] Accept only answer keys present in the exact run's current clarification-question
  set; reject stale, cross-run, extra, or changed keys with a structured 422.
- [x] Clear browser clarification state whenever the run or question set changes.
- [x] Do not allow clarification question text to create new services unless that
  question was issued and answered for the same run.

### Epic 38.3 — Verified Chat destination resolution

- [x] Accept an already validated `spaces/...` resource, resolve a direct-message email
  through Google Chat, or require one unambiguous accessible display name.
- [x] Bind message idempotency and read-after-write verification to the resolved space,
  while retaining the user-approved destination in the durable action.
- [x] Return a sanitized actionable destination error without exposing provider payloads.

### Epic 38.4 — Deterministic Sheet composition and generic ordered lineage

- [x] For recent-Gmail-sender workflows, construct table rows deterministically, create
  the Sheet, bind the returned `spreadsheetId`, populate it, and read it back without
  spending Groq tokens or asking a model to copy an identifier.
- [x] At the general service-agent boundary, enforce the next allowed tool in an ordered
  contract and bind downstream identifier fields from successful upstream results.
- [x] Record lineage source, target, field, and binding evidence in the execution ledger.

### Epic 38.5 — Partial side effects, incidents, and reconciliation

- [x] Preserve successful and failed tool executions when a service agent exits through
  a typed exception so created artifacts are not lost from the ledger.
- [x] Include a sanitized operation-specific cause beside the no-blind-retry message.
- [x] Reduce side-effect integrity when a failed step contains successful/attempted writes,
  and expose the artifact for preserve/cleanup/retry-population decisions.
- [x] Block ordinary resume when legacy tool-attempt evidence proves a write occurred but
  its result/artifact lineage was not retained; require explicit reconciliation.

### Epic 38.6 — Candidate-builder and OKF governance compatibility

- [x] Retain the Sprint-30 Groq-only adaptive coding-agent architecture and its bounded
  repository read/search/stage/diff/validation/rollback tools; no other model credential
  is introduced.
- [x] Retain tool-extension drafting as an untrusted candidate capability rather than
  dynamic runtime authority.
- [x] Retain independent human gates for candidate publication, production-connected
  deployment, canary activation, promotion, and trusted OKF publication; approved OKF
  remains immutable, hash-pinned per run, separately measured, and independently rolled back.

Guardrail: a Chat-only request cannot execute Gmail or Calendar; a Sheet population
cannot use a title/placeholder as the created resource ID; stale clarification keys are
rejected; and every successful write remains visible even when a later write fails.

Local implementation evidence on 2026-07-29: 182 default backend tests and all 32
PostgreSQL integration tests pass; planner golden cases pass 33/33 and workflow replays
pass 14/14. Python compile and Flake8, Bandit, `pip-audit`, source-aware chunking,
policy, context-packing, DP-allocation, dual-worker, Grafana-definition, Compose,
secret-history, and migration downgrade/forward-repair guardrails pass. Next.js lint
and production build pass; Flutter analyze, test, and debug APK build pass. Production
deployment and post-deployment smoke evidence are recorded after the reviewed branch
is merged.

## Sprint 39 — Production multi-service completion and candidate reliability

Production run `6e72e786` on deployment `3652da8` exposed three independent
boundaries in one Gmail → Sheets → Chat/Calendar DAG: generic dependency projection
removed structured Gmail sender records, Chat returned an API-disabled 403, and
Calendar received natural-language date/time arguments with `India` instead of an
IANA timezone. Candidate builds `failure-2827094a7e87` and
`failure-c4e36d93d227` additionally exposed null tool arguments, exhausted cumulative
tool authority, misleading retry controls, and incomplete model-chain reporting.

### Epic 39.1 — Structured sender lineage and exact Sheet verification

- [x] Preserve the bounded `list_recent_gmail_senders` schema through dependency
  projection instead of recursively replacing sender records with omitted placeholders.
- [x] Populate header plus every returned sender row using the verified created Sheet
  ID, and fail closed when Google reports a different updated-row count.
- [x] Retain exact range/content read-after-write comparison, expected/observed shapes,
  and content hashes in verification evidence.

### Epic 39.2 — Calendar normalization and guided timezone input

- [x] Normalize accepted aliases such as India/Indian/IST to `Asia/Kolkata`, validate
  IANA zones, convert supported relative times to offset-bearing RFC3339, and reject
  invalid/end-before-start values before calling Google.
- [x] Apply the same normalization at the Calendar tool boundary for every planner or
  fallback path, not only the reported workflow.
- [x] Render timezone clarifications as a populated browser-aware dropdown rather than
  a blank text field, while retaining common global IANA choices.

### Epic 39.3 — Chat diagnosis, recipient safety, and side-effect truthfulness

- [x] Detect a disabled `chat.googleapis.com` response and return an actionable
  sanitized cause without project IDs, provider payloads, or private console URLs.
- [x] Reject malformed Chat destinations and near-match recipient typos with a
  structured correction before approval/execution; never silently change recipients.
- [x] Keep side-effect integrity at 100% for explicit failed provider writes that
  created nothing, while retaining uncertainty for missing results, successful partial
  writes, and lost-worker boundaries.
- [x] Google Chat API enablement is confirmed by the production
  `spaces.findDirectMessage` response. A provider-confirmed missing-DM 404 proves the
  API is reachable and must not be classified as `SERVICE_DISABLED`.

### Epic 39.4 — Candidate-builder resumability and transparent evidence

- [x] Accept null arguments only as an empty object for empty-schema bounded tools and
  reject every other non-object argument deterministically.
- [x] Expand bounded repository tool-call authority for the full author/reviewer/
  remediation lifecycle while retaining byte, file, elapsed-time, token, path, secret,
  no-network, and trusted-CI limits.
- [x] Version the corrected model/tool policies so older terminal builds become
  eligible for an explicit current-policy clone instead of a misleading same-policy
  retry.
- [x] Display the complete observed builder model chain, including Groq-hosted
  fallbacks, separately from the configured primary model.
- [x] Hide the retry action when the backend says a terminal build has no safe/current
  retry path.
- [x] Reject disconnected code candidates that create an isolated application module
  and tests without changing an existing runtime path that adopts the new module;
  reject create/replace/delete declarations that disagree with the frozen base tree.

### Epic 39.5 — Remaining-roadmap truthfulness

- [x] Re-audit every roadmap item that does not require future labelled evidence,
  production sample sizes, real pilot users, or separately approved external
  publication credentials.
- [~] Keep source chunk-policy winners, query-transformation winners, production RAG
  quality, policy/prompt winners, and the first offline-policy promotion data-gated;
  their evaluation infrastructure is complete and fabricating evidence is prohibited.
- [!] Keep staged 5–90-user pilot expansion blocked on real consenting users and
  measured traffic.
- [~] Keep external email/GitHub improvement notifications credential- and explicit-
  publication-approval-gated; admin and Grafana notification ledgers remain complete.

Guardrail: no failed provider call is reported as an uncertain side effect without
evidence; no typo is silently substituted; no Calendar write reaches Google with an
unvalidated timezone/window; no sender row can disappear through generic dependency
projection; and no candidate portal can imply that only the primary model ran.

Local implementation evidence on 2026-07-29: 187 unit tests and 33 PostgreSQL
integration tests pass; the focused Sprint-39 suite passes; planner golden cases pass
33/33 and workflow replays pass 14/14. Python compilation and Flake8 pass; Bandit has
no medium/high findings; `pip-audit` and production `npm audit` report no known
vulnerabilities. Source-aware chunking, policy, greedy/DP packing, DP allocation,
dual-worker, Grafana-definition, Compose, secret-history, and migration
downgrade/forward-repair guardrails pass. Next.js lint and production build pass.
Docker Desktop builds the API and worker images successfully after explicitly placing
Docker Desktop's credential helper on the non-login command path. Flutter analyze and
tests pass, and the debug APK builds successfully. GitHub CI and production deployment
evidence are recorded after publication.

## Sprint 40 — Idempotent Google Chat direct-message setup

Production run `2df8b140` proved that the Chat API was enabled but no direct-message
space yet existed for the requested recipient. The former classifier treated any error
containing the Chat API hostname as disabled, while the send tool could not create the
missing DM.

### Epic 40.1 — Exact Chat failure taxonomy

- [x] Recognize API disablement only from genuine `SERVICE_DISABLED`,
  `accessNotConfigured`, or the provider's explicit “API has not been used/disabled”
  evidence; the hostname alone is never sufficient.
- [x] Preserve provider-confirmed missing-DM, insufficient-scope, invalid-destination,
  recipient-ineligible, and ordinary permission failures as distinct sanitized causes.
- [x] Add regression coverage for a Chat-hostname 404, genuine disabled 403, missing
  scope, malformed destination, and unrelated 404.

### Epic 40.2 — Ordered DM resolution and send

- [x] Expand OAuth authority with `chat.spaces.create`; existing users whose stored
  scope set is incomplete are disconnected safely and asked to consent once.
- [x] Resolve an email with `spaces.findDirectMessage`; only its exact missing-DM 404
  may invoke idempotent `spaces.setup` with a deterministic request ID and one human
  membership.
- [x] Split resolution and send into the ordered contract
  `resolve_chat_destination → send_chat_message`; bind the verified returned
  `spaces/...` name at the executor boundary and forbid hidden resolution/setup inside
  the send tool.
- [x] Bind the current-turn analyzed recipient into the resolver as a trusted planner
  argument, retain near-match rejection, and never let model output substitute it.
- [x] Read back the space and message separately, persist resolution/artifact lineage,
  and preserve no-blind-retry behavior for a failed/uncertain message send.

### Epic 40.3 — Documentation and rollout

- [x] Update architecture, deployment, operations, OAuth recovery, capability catalog,
  threat model, original specification addendum, and this upgrade ledger.
- [x] Preserve dated completion/security audit reports as immutable historical
  snapshots rather than rewriting their earlier evidence.
- [x] PR #87 passed normal and governed-candidate CI, merged as runtime commit
  `b371edd3`, and deployed successfully to Railway API/worker and Vercel. Production
  attestation, direct health, frontend proxy health, HTTP 200, unauthenticated 401,
  OAuth PKCE redirect, `prompt=consent`, and requested `chat.spaces.create` all pass.
- [!] Each existing pilot user must reconnect once and approve
  `chat.spaces.create`; this user consent cannot be performed by the service.

Guardrail: a missing DM may create or reuse exactly one human-to-human direct-message
space, but it may not send until the verified `spaces/...` output is lineage-bound.
No unrelated 403/404 may trigger space creation, and no retry may duplicate an
uncertain message.

Local implementation evidence on 2026-07-29: 197 unit tests and 33 Docker
PostgreSQL-backed integration tests pass. Planner goldens pass 33/33 and no-network
workflow replays pass 14/14. Python compilation/Flake8, Bandit, `pip-audit`,
source-aware chunking, policy, context-packing, DP allocation, dual-worker,
Grafana-definition, Docker Compose, secret-history, and isolated migration
downgrade/forward-repair guardrails pass. Next.js lint/build/audit and Flutter
analyze/test/debug APK pass. Docker Desktop builds both the API and worker images.

## Sprint 41 — Production Chat setup request and verified-write completion repair

Production run `f07b9ddd` reached the new resolver but proved that the generated
discovery client accepts `requestId` only inside `SetupSpaceRequest.body`, matching the
REST schema. Passing it as a Python method keyword raised locally before any setup HTTP
request. The same run created and read-back-verified its Calendar event, then incorrectly
marked that step failed when an unnecessary post-tool quality-model turn hit quota.

- [x] Move the deterministic Chat setup request ID into the request body and enforce the
  real discovery-client call signature in regression tests.
- [x] Stop model execution immediately when the ordered write contract is satisfied;
  use the existing deterministic verifier, not an extra model response, to decide
  success.
- [x] Preserve pre-tool quota protection: a complex/high-risk write still pauses if the
  quality model is unavailable before the required tool contract completes.
- [x] Preserve artifact verification, confirmation, idempotency, lineage, and
  no-blind-retry rules.
- [x] Permit exact-step resume after an explicitly failed idempotent Chat resolver,
  while keeping an uncertain message send blocked.
- [x] Reconcile verified failed sibling writes without retry after the selected step
  resumes, and forbid final `completed` status while any durable step remains failed,
  pending, running, skipped, or otherwise unresolved.
- [x] Update architecture, operations, the original specification addendum, and this
  upgrade ledger with the production evidence and corrected boundary.

Guardrail: model quota may prevent a write before its contract begins, but it cannot
override successful tool evidence plus deterministic readback after the contract has
completed.

## Sprint 42 — Chat app configuration diagnosis and deterministic Calendar create

Production run `f6cb9e9d` reached Google after the setup-request repair. Google
returned `404 Google Chat app not found`, proving that the API was enabled and the
scope was granted but the project-level Chat app identity had not been configured.
The same run's Calendar step made no Google call because quality-model quota was
unavailable even though the approved clarifications fully specified the event.

- [x] Classify `Google Chat app not found` as incomplete Chat API Configuration and
  preserve a precise sanitized recovery message instead of the generic write failure.
- [x] Document that Chat writes require a saved app name, HTTPS avatar, description,
  and disabled interactive features in addition to API enablement and OAuth scopes.
- [x] Project fully specified Calendar create arguments in the typed planner,
  including the common `tommorow` misspelling, duration, IANA timezone, attendee, and
  Meet intent.
- [x] Execute those Calendar creates through the idempotent tool and deterministic
  verifier without spending quality-model quota to reconstruct approved arguments.
- [x] Recover typed Calendar arguments from a legacy failed step's persisted approved
  request so run `f6cb9e9d` can use the repair when explicitly resumed.
- [x] Bind an unambiguous email recipient into the Chat resolver even when it arrives
  through clarification text rather than the initial Chat-specific analyzer field.
- [x] Add focused and exact-request regression coverage.

External requirement: the Cloud project owner must save the Chat API Configuration
once. The application cannot truthfully report Chat write readiness before Google
accepts that project-level configuration.

Guardrail: the deterministic Calendar path activates only when every required typed
argument is present. Otherwise, the existing guarded planner path remains in control.

## Sprint 43 — Service-wide durable hybrid execution

The typed Calendar repair is generalized without turning the product into a brittle
collection of natural-language special cases.
Production run `2bffbd6d` then proved the distinction: Calendar completed with zero
model tokens, while the already-complete ordered Chat inputs still entered the quality
model and paused on quota before any Chat call. The ordered adapter below closes that
gap without weakening write safety.

- [x] Add a pure typed preflight that validates persisted arguments against the
  registered tool schema for every eligible single-tool read and write contract.
- [x] Preserve explicit composite handlers and ordered lineage for Sheet and Chat
  workflows; do not pretend an incomplete multi-tool operation is deterministic.
- [x] Execute a fully bound Chat composite deterministically when the approved
  recipient and a verified dependency Sheet URL are both present: resolve/create the
  DM, lineage-bind the exact `spaces/...` result, then send the verified URL without a
  quality-model round trip.
- [x] Select the bounded allowlisted service agent when typed preflight is incomplete,
  invalid, ambiguous, or multi-tool, and record a sanitized reason.
- [x] Forbid model fallback after any tool attempt. Provider failures, uncertain
  writes, and postcondition failures enter verification, reconciliation, or explicit
  resume so an external action cannot be duplicated.
- [x] Persist the execution path and fallback reason and emit append-only selection
  events with the pre-fallback external-attempt count.
- [x] Generate a sanitized immutable approval preview with service, operation, tools,
  recipients/resources/times, and content presence/size; render it before approval.
- [x] Retain the same approval hash, semantic authority, idempotency, bounded tool
  allowlist, projection, verification, artifacts, incidents, and tenant isolation on
  both execution paths.
- [x] Add regression tests for exact write/read selection, incomplete-argument
  fallback, ordered-contract fallback, and content-safe approval previews.
- [x] Re-pin only reconciliation-proven safe resumes to the current immutable executor
  so an older failed run can consume a deployed repair; uncertain writes remain pinned
  and blocked.

Guardrail: fallback is available for planning before the first external call, not as
an alternate writer after deterministic execution fails.

Local evidence on 2026-07-29: 213 unit tests and 33 PostgreSQL-backed integration
tests pass; planner goldens pass 33/33 and no-network workflow replays pass 14/14.
Python compilation and Flake8 pass, Bandit reports no medium/high findings, and the
Next.js lint, production build, and critical-severity audit gate pass.

## Sprint 44 — Live duration and actual per-model usage

- [x] Show total elapsed request time on the main active-session progress card,
  updating while a durable run is in progress and freezing at its completion time.
- [x] Show recorded step execution time separately so queue, approval, and
  clarification waiting time are not confused with completed/running step work.
- [x] Aggregate the immutable `agent_model_calls` ledger by actual model, including
  fallback models, call count, input tokens, output tokens, and total tokens.
- [x] Expose zero-token deterministic runs explicitly rather than hiding the usage
  section.
- [x] Preserve a labelled aggregate-only fallback for historical runs whose old data
  does not have per-call allocation.

Guardrail: token attribution comes from server-side model-call evidence. The browser
does not infer which configured model probably handled a call.

Local evidence on 2026-07-29: 213 unit tests and 34 PostgreSQL-backed integration
tests pass; planner goldens pass 33/33 and no-network workflow replays pass 14/14.
Python compilation and Flake8 pass, Bandit reports no medium/high findings, the
version-controlled Grafana dashboards validate, and Next.js lint, production build,
and the critical-severity audit gate pass.

## Sprint 45 — Context-authorized composition delivery and safe API errors

Production session `de7c74d0-fef3-4595-b69a-76d53e871340` exposed two connected
failures. A combined composition-and-Chat request containing `sendchat` was completed
as composition-only, and referential retries inherited the word `meeting` from the
paragraph as an unauthorized Meet service. The resulting structured API error was
rendered by the browser as `[object Object]`.

- [x] Recognize joined and natural Chat delivery wording such as `sendchat`,
  `send chat`, and `send on Google Chat`.
- [x] Stop treating the ordinary noun `space` as sufficient Chat intent; retain
  explicit Chat resource and channel recognition.
- [x] Make the current request the sole authority for service selection and external
  writes. Prior same-session output may resolve referenced content but cannot add a
  Gmail, Chat, Calendar, Meet, or other operation.
- [x] Keep bounded prior output outside content-free diagnostics and bind it only to
  the typed step authorized by the current request.
- [x] Plan combined writing-and-delivery as an ordered `composition -> Chat` DAG with
  the existing high-risk human approval.
- [x] Send the exact completed composition dependency or referenced prior assistant
  output through the deterministic Chat resolver/send contract without another model
  planning turn.
- [x] Preserve Chat destination resolution, idempotency, verification, artifact
  evidence, no-blind-retry behavior, and approval hashing.
- [x] Decode nested API `message`, `reason`, and `detail` objects into safe readable
  frontend errors so no user path displays `[object Object]`.
- [x] Add exact production-wording, contextual service-authority, content-lineage,
  deterministic Chat, and durable persistence regression tests.

Guardrail: conversation history supplies data only when a current-turn reference
requires it. It never supplies permission or silently expands the current action set.

Local evidence on 2026-07-29: 218 unit tests and 35 PostgreSQL-backed integration
tests pass; planner goldens pass 34/34 and no-network workflow replays pass 14/14.
Python compilation and Flake8 pass, Bandit reports no medium/high findings, all
offline chunking/policy/DP and dual-worker gates pass, Grafana dashboards validate,
and the Next.js lint, production build, and critical-severity audit gate pass.

## Sprint 46 — Same-service exact-copy DAG and tenant RAG activation

Production run `6ad2c09b` proved that a single Gmail service may still require multiple
ordered operations. The planner searched and fetched the source message but had excluded
`send_gmail`, then reported a generic tool failure. Production also contained 14,521
embedded legacy chunks owned by another tenant, zero retrieval events, and two
dead-letter Gmail embedding jobs for the affected user because an ISO timestamp string
reached an asyncpg timestamp column.

- [x] Expand explicit same-service read-then-write requests into separate durable DAG
  steps without allowing prior conversation text to add an operation.
- [x] Implement exact Gmail-copy lineage: sent-mail lookup, exact source fetch, encrypted
  tenant/run-scoped result reference, recipient-bound send, and no model-memory transfer.
- [x] Keep the Gmail write behind the high-risk approval policy and prevent a missing,
  truncated, or tenant-mismatched source from reaching `send_gmail`.
- [x] Verify the new Gmail message ID, exact recipient, subject, and body through
  read-after-write; store hashes and identifiers rather than private contents.
- [x] Preserve sanitized underlying tool/provider evidence instead of collapsing every
  failure into “at least one tool failed.”
- [x] Distinguish a current-request antecedent from a prior-turn reference so “fetch and
  send the same mail” does not load an unrelated paragraph.
- [x] Normalize source timestamps from ISO strings to timezone-aware datetimes before
  source-aware parent/chunk persistence.
- [x] Add explicit authenticated per-user indexing consent and a durable leased
  `rag_source_sync_jobs` queue that survives browser/API disconnects.
- [x] Bound Gmail/Drive/Calendar collection, enqueue only tenant-owned source-aware
  chunks, suppress duplicate active syncs, and retry only the known timestamp dead
  letters automatically.
- [x] Make run/UI telemetry distinguish conversation context, knowledge-RAG gating and
  returned/used evidence, and private index readiness/sync status.
- [x] Add the exact production request to planner goldens and no-network replay, add
  endpoint/worker/verification isolation tests, and run every repository guardrail.
- [x] Bound dense query embedding independently from PostgreSQL retrieval so a slow
  Railway Ollama request degrades to explicitly labelled keyword evidence rather than
  cancelling the whole RAG node after 20 seconds.
- [x] Record requested versus effective retrieval mode, dense availability/error type,
  query-embedding duration, and dense/lexical candidate counts in run diagnostics.
- [x] Deploy migration, API/worker, and web changes; enqueue the consenting production
  user's bounded backfill; verify source-aware chunks and retrieval evidence in
  production, and verify exact-copy completion/actionable errors with the no-network
  replay suite without duplicating the already-sent artifact.

Guardrails: OAuth access alone is not training/indexing consent; indexed content remains
tenant-scoped. Knowledge RAG is not used to locate current Gmail state or execute writes.
The source and destination messages are separate artifacts, and a failed write is never
blindly retried.

Local evidence on 2026-07-29: 230 unit tests and 36 PostgreSQL-backed integration
tests pass; planner goldens pass 36/36 and no-network workflow replays pass 15/15.
Migration 014 downgrades to 013 and repairs forward to 014. Python compilation,
Flake8, Bandit, and dependency audit pass; offline chunking/policy/context-packing/DP
and dual-worker gates pass; Grafana dashboards and Compose validate; Next.js
lint/build/audit pass; Flutter analyze/test/debug APK pass; and the API and worker
Docker images build successfully.

Production evidence on 2026-07-29:

- Reliability changes were merged through PRs
  [#99](https://github.com/agentic-ai-training/google-connector-app/pull/99),
  [#100](https://github.com/agentic-ai-training/google-connector-app/pull/100),
  [#101](https://github.com/agentic-ai-training/google-connector-app/pull/101),
  [#102](https://github.com/agentic-ai-training/google-connector-app/pull/102), and
  [#103](https://github.com/agentic-ai-training/google-connector-app/pull/103).
- Migration 014, API, durable worker, and frontend were deployed. Deployment workflow
  `30446030283` attested the exact immutable merge
  `58ef28cf15ef46aa867ea0b618846eb9817a9f8a` on Railway API/worker and Vercel.
- Consented sync job `c04f9d7a-e876-41ce-9708-636a2b3f470f` collected 25 Gmail,
  11 Drive, and 4 Calendar records. All 42 embedding jobs completed (the total includes
  two repaired and requeued timestamp dead letters), leaving zero pending, failed, or
  dead jobs and 39 active source-aware chunks linked to 33 parent sections.
- Read-only production run `289e873a-7cc9-4527-abb2-44a7e0f68789` completed in
  22.174 seconds. Bounded dense embedding degraded explicitly to PostgreSQL keyword
  retrieval, returned and used five tenant-owned Gmail/Drive chunks, produced a cited
  evidence answer, created zero external artifacts, and executed a single
  `llama-3.3-70b-versatile` call (2,064 input and 338 output tokens).
- Exact-copy read/write lineage, recipient/subject/body verification, sanitized failure
  evidence, and no-blind-retry behavior passed the integration and no-network replay
  gates. No additional Gmail message was sent while verifying the already-completed
  user workflow.

## Sprint 47 — Deferred write authority and send-first Gmail copy regression

Production runs `c42918b4` and `b952849a` exposed two remaining statement-analysis
defects. A request to prepare content and wait for a later delivery command was
incorrectly marked as current write authority and loaded an unrelated prior run. A
self-contained “send the last mail you sent to A, send it to B” request also loaded
that paragraph, collapsed to one Gmail search step, and accepted model refusal prose
as a successful live read despite recording zero tool attempts.

- [x] Treat “wait/hold until my next command or instruction” as deferred delivery:
  compose now, grant no current external-write authority, require no write approval,
  and do not load prior conversation output merely because the sentence contains `it`.
- [x] Recognize latest/last sent-mail copying as a self-contained current-request
  antecedent with two recipient roles even when natural verb order begins with `send`.
- [x] Build the immutable typed Gmail `search -> send` DAG with source query
  `to:<source> in:sent`, exact subject/body lineage, destination binding, high-risk
  approval, and the existing read-after-write verification.
- [x] Keep live Gmail state on live Google APIs; do not use conversation history or
  knowledge RAG to locate the source message.
- [x] Require actual allowlisted tool evidence for a live read. Reject provider/model
  refusal prose and generic text as postcondition failures instead of allowing false
  100% completion.
- [x] Preserve tool-free composition and guarded informational answers; the live-tool
  invariant applies only when the durable step declares non-empty `allowed_tools`.
- [x] Add exact production wording to current-turn context, deferred authorization,
  planner, postcondition, PostgreSQL persistence, golden-task, and workflow-replay
  regression coverage.
- [x] Publish, deploy, and verify the immutable release without approving or executing
  a real Gmail-copy run; production verification must stop at approval/cancellation.

Guardrails: the current statement alone grants a new external write. Pronouns first
resolve against explicit resources in that statement; only unresolved references may
load one bounded same-session prior result. A live-operation step cannot complete from
model prose without provider tool evidence.

Local evidence on 2026-07-29: 235 unit tests and 37 PostgreSQL-backed integration
tests pass; planner goldens pass 38/38 and no-network workflow replays pass 16/16.
Python compilation, Flake8, Bandit, and dependency audit pass; offline
chunking/policy/context-packing/DP and dual-worker gates pass; Grafana dashboards
validate; Next.js lint/build and the critical-severity audit gate pass; Flutter
analyze/test/debug APK pass.

Production evidence on 2026-07-29:

- PR [#105](https://github.com/agentic-ai-training/google-connector-app/pull/105)
  passed two independent CI/attestation runs and merged as immutable commit
  `eb801a68c58b9b449fc3a3004452d299c9668f57`.
- Deployment workflow
  [30448969879](https://github.com/agentic-ai-training/google-connector-app/actions/runs/30448969879)
  deployed and attested that exact commit on the Railway API/worker and Vercel
  frontend.
- Approval-gated production proof run
  `32f8d46f-682e-4bea-be66-b7732725d3a0` stored standalone context and the typed
  Gmail `search -> send` DAG with the exact source query and destination.
  It recorded zero tool attempts and zero artifacts and was cancelled before
  approval; no Gmail message was read or sent.

## Sprint 48 — Collection-valued read verification

Production run `63308852` proved that the Sprint 47 Gmail DAG and deterministic
source lookup ran, but exposed a verifier contract mismatch: `search_gmail`
correctly returned a list of messages, while the shared verifier treated every
non-mapping result as explicit failure evidence. The fetched source artifact
`19faa92cd9c37ff8` was retained, the Gmail send step never started, and no
destination message was attempted.

- [x] Accept collection-valued results from read tools as valid evidence.
- [x] Continue rejecting absent results and mappings containing explicit
  `error` or `success: false` evidence.
- [x] Continue requiring write tools to return structured mappings before
  tool-specific postcondition and read-after-write verification.
- [x] Add regression coverage for a Gmail search list followed by an exact
  Gmail message mapping.
- [x] Add negative coverage proving explicit read errors and non-mapping write
  results still fail closed.
- [x] Publish, deploy, and attest the immutable correction.

Guardrail: result shape alone is not failure evidence for an allowlisted read.
Read failure must be absent or explicit; external writes retain the stricter
structured-result and postcondition contracts.

Local evidence on 2026-07-29: 237 unit tests and 37 PostgreSQL-backed integration
tests pass; planner goldens pass 38/38 and no-network workflow replays pass 16/16.
Python compilation, Flake8, Bandit, and dependency audit pass.

Production evidence on 2026-07-29: PR
[#107](https://github.com/agentic-ai-training/google-connector-app/pull/107)
passed two independent CI runs and the governed candidate attestation, then merged
as immutable commit `c6e74f02a986a2f7189f5740c474e2b13fe5dddf`. Deployment
workflow
[30451036207](https://github.com/agentic-ai-training/google-connector-app/actions/runs/30451036207)
deployed and attested that exact version on the Railway API/worker and Vercel
frontend. Production verification performed no Gmail write.

## Sprint 49 — Dynamic content contracts and semantic lineage

Production incidents `7c75e091` and `263ca2fb` exposed two systemic boundaries rather
than two phrases to hard-code. A request for a future conversation was resolved to an
old paragraph, and a many-language word-gloss request exhausted a fixed reasoning-model
completion allowance without producing visible content.

- [x] Analyze every current statement into a provider-independent content contract:
  artifact kind, interaction mode, languages, translation granularity, minimum visible
  content, complexity, output allowance, deferred delivery, future-artifact state, and
  required clarifications.
- [x] Permit bounded creation/transformation tasks without requiring an immediate
  Workspace mutation, while keeping unrestricted factual/open-domain chat out of scope.
- [x] Detect content that does not exist yet. Never resolve it to an older session
  result; clarify whether to generate a sample now or capture a later real interaction.
- [x] Normalize language aliases as vocabulary and preserve request order.
- [x] Clarify combinatorially large or ambiguous word-gloss requests before spending
  model quota.
- [x] Route composition from structured complexity rather than topic words such as
  `plan`; use a content-specific visible-output budget.
- [x] If a tool-free composition returns no visible content, retry exactly once through
  the approved quality route. Never use this fallback after an external tool attempt.
- [x] Persist content lineage containing source run/step, artifact kind, languages,
  future-artifact state, and SHA-256 content hash.
- [x] Verify composition from the persisted content contract.
- [x] Deploy and prove the release with safe tool-free and clarification-only requests.

Guardrails: deterministic execution is selected from typed completeness and risk, not
from a growing phrase table. Deterministic failure after an external attempt never falls
through to an LLM. Delivery consumes only a compatible completed artifact or explicit
bounded prior reference.

## Sprint 50 — Compiler-style candidate builder and trusted CI remediation

Whole-file generation, broad reads, and repeated provider turns consumed large token
budgets while still producing incomplete candidates. The target is a two-plane coding
system, not an unrestricted shell attached to the Groq key.

- [x] Add bounded Python symbol indexing/reads, reference lookup, and
  implementation/test-neighborhood discovery.
- [x] Add bounded in-memory line-range patching so one symbol can change without
  re-emitting an entire source file.
- [x] Retain whole-file staging for new files/refactors, structural validation, and
  manifest/diff inspection.
- [x] Activate `bounded-repo-tools-v10-symbol-patch-sandbox`.
- [x] Keep generation free of shell, network, production data, OAuth, deployment
  credentials, and executable candidate code.
- [x] Keep test execution in trusted CI with no Groq key or production credentials.
- [x] Capture backend/web/mobile failure logs, reduce them to bounded diagnostics, and
  redact identities and secret-like assignments.
- [x] Submit hash-bound trusted failure evidence to a dedicated endpoint.
- [x] Clone the immutable failed candidate into one governed remediation build, preserve
  its files, attach sanitized CI evidence, and require fresh independent review.
- [x] Deduplicate remediation per source build and preserve failed history.
- [x] Put a high-contrast `Required human actions` ledger directly below the builder for
  retries, PR publication, canary approval/activation, and promotion.
- [ ] Exercise the complete hosted fail-remediate-pass-canary lifecycle with a real
  candidate and its human gates.

Guardrails: trusted CI failure may create another untrusted draft, never an approval or
deployment. Arbitrary generated code cannot be guaranteed to pass; bounded tools,
validation, CI feedback, retry ceilings, and human gates prevent unsafe claims and loops.

## Sprint 51 — Operational OKF selection and evidence

OKF already loads at API/worker startup, is version-pinned per run, and is supplied to
the guarded agent. Its previous raw-query lexical lookup was insufficient.

- [x] Select operational knowledge from structured service, operation, risk, read/write,
  tool, and content-kind tags in addition to lexical relevance.
- [x] Prefer tag-compatible documents before lexical-only matches.
- [x] Persist selected document IDs, versions, query hash, structured tags, and
  selection-policy version as durable evidence.
- [x] Keep OKF separate from tenant RAG and live Google state.
- [x] Keep executable schemas, tool allowlists, approvals, OAuth, idempotency, and
  verifier postconditions in code; OKF cannot weaken them.
- [x] Add selected OKF IDs/version/reason to run detail and Grafana session views.
- [ ] Measure structured selection against lexical-only lookup before canary promotion.

Practical role: OKF is the versioned, human-readable operational knowledge plane for
capabilities, workflows, policies, runbooks, recovery guidance, and candidate context.
It is not a replacement for PostgreSQL, pgvector, Google APIs, or hard enforcement.

Local evidence on 2026-07-30: 242 unit tests and 37 PostgreSQL-backed integration
tests pass; planner goldens, source-aware chunking evaluation, 16/16 no-network
workflow replays, policy/context-packing/DP evaluation, and dual-worker isolation
pass. Python compilation, Flake8, Bandit, and dependency audit pass. Migration 014
downgrades to 002 and repairs forward to 014. Grafana dashboards, GitHub workflow
YAML, Docker Compose, and secret-history guardrails pass. Next.js lint/build and the
production critical audit pass. Flutter analyze/test/debug APK pass. API, worker,
candidate API, and candidate-builder Docker images build successfully.

Production evidence on 2026-07-30:

- PR [#109](https://github.com/agentic-ai-training/google-connector-app/pull/109)
  passed both CI and governed-candidate validation, then merged as immutable commit
  `04d3fcd0476592e4de9d9bb2bb7a4cfd9864e8c7`. Deployment workflow
  [30495920194](https://github.com/agentic-ai-training/google-connector-app/actions/runs/30495920194)
  deployed and attested that exact API, worker, and frontend version.
- Tool-free production run `ca2d687b-7435-4ff7-8dc7-e61e1dcbdd3e` completed
  deterministically with the bounded-writing capability catalog, no model call, and
  no external artifact.
- The first multilingual proof exposed an equivalent translation-grammar form before
  any external action. PR
  [#110](https://github.com/agentic-ai-training/google-connector-app/pull/110)
  generalized quantifier/word/translation-stem order, passed both hosted suites, and
  deployed through attested workflow
  [30496553978](https://github.com/agentic-ai-training/google-connector-app/actions/runs/30496553978)
  as `564c1bd5b9bbd355ecdfa53e04a14860a18e4cbc`.
- Final production run `d91c528a-5428-47cf-b3ce-39019b372959` stopped at
  `awaiting_clarification`, asked separately for passage layout and gloss language,
  and recorded zero model calls and zero external artifacts.
- The revised session dashboard is version-controlled and validated, but Grafana Cloud
  publication is pending because `GRAFANA_SERVICE_ACCOUNT_TOKEN` is absent from both
  the local untracked vault and the linked Railway service. This does not affect
  runtime OKF evidence, which is already returned by the API and rendered by the
  deployed frontend.

## Sprint 52 — Semantic-frame runtime and source-grounded candidate hardening

The July 30 production history and candidate `25927029-08f8-4485-a5b6-d1e8f330fb02`
showed a shared failure pattern: lexical nouns could displace the requested operation,
provider prose could be accepted without evidence, and a candidate could spend 32,155
tokens while reading zero source bytes and staging placeholder code.

- [x] Expand action morphology without adding sentence-specific answers; canonicalize
  Gmail sender language, Calendar create synonyms, bounded composition, and explicit
  delivery channels before plan construction.
- [x] Add canonical plan-coverage validation after service collapse so incidental nouns
  cannot invent Contacts/Docs steps and requested canonical services cannot disappear.
- [x] Add metadata-only Gmail message counts and category/local-day aware sender lists;
  do not retrieve bodies for count/name questions.
- [x] Recover provider-visible composition content from the final graph message before
  declaring an empty-output postcondition failure.
- [x] Require an exact URL clarification when a contextual “send the link” antecedent
  contains no URL.
- [x] Add Calendar recurrence fields and verification; compare equivalent ISO offsets as
  instants rather than strings.
- [x] Classify persistent Chat project/configuration blocks as manual reconciliation so
  resume cannot repeat an externally blocked operation.
- [x] Reject sexual-image/video delivery with unknown adult status, consent, or ownership
  before planning, model selection, RAG, or Google API access; record `policy_refusal`.
- [x] Cancel unfinished candidate builds when their proposal is rejected, expired, or
  rolled back, and reject new dispatch for an ineligible proposal. Cancel old-policy
  checkpoints instead of resuming them under incompatible role/token limits; a retry
  must clone the evidence into a fresh current-policy build.
- [x] Activate `bounded-repo-tools-v11-runtime-read-noop-gates`: ranked runtime
  localization, mandatory exact source read before application staging, adopted-runtime
  integration, placeholder-body rejection, assertion-bearing behavioral tests, syntax,
  rollback, and frozen-hash evidence.
- [x] Bound author, remediation, and reviewer roles separately so repeated review cannot
  consume the entire 48k authority without a concrete patch.
- [x] Add approved OKF concepts for the delivery boundary and source-grounded candidate
  remediation; preserve code as the hard enforcement plane.
- [x] Update architecture and the deep teaching/dictionary guide with the implemented
  compiler-style flow and its DSA foundations.
- [ ] Publish through trusted CI, deploy the immutable release, verify safe production
  replays, and observe at least one real candidate through source-read and no-op gates.

No future labelled-data dependency is required for the checked items. RAG, DP, prompt,
and policy winners still require measured consenting traffic before promotion; that
evidence requirement is not unfinished implementation.

Local release evidence on 2026-08-01: 249 unit tests pass; 286 tests pass with the
isolated PostgreSQL integration suite enabled; 42/42 planner goldens and 16/16 workflow
replays pass. Python compilation, Flake8, Bandit medium/high checks, dependency audit,
web lint/build/audit, Flutter analyze/test/debug APK, migration downgrade/forward repair,
Compose validation, Grafana dashboard validation, secret-history checks, and all four
Docker image builds pass.
