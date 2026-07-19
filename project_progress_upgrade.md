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

## 4. Sprint implementation ledger

Legend: `[ ]` pending, `[~]` active, `[x]` complete, `[!]` externally blocked.

## Sprint 0 — Baseline, safety, and migration preparation

### Epic 0.1 — Protect current production

- [x] Export current Neon schema and create a backup/restore procedure.
- [x] Record Railway/Vercel deployment identifiers and current public health URLs without writing secrets.
- [ ] Capture current latency, failure, RAGAS, token, quota, and Google artifact baselines.
- [x] Inventory current code paths, services, database objects, dashboards, and external artifacts.
- [x] Verify secrets/credentials are ignored and absent from Git history.
- [x] Add feature flags for legacy chat, durable runs, OKF, new RAG, governed improvements, and canary cohorts.
- [x] Preserve legacy `/chat` as a rollback path until the new executor is proven.
- [x] Define migration, deploy, worker, index, prompt, OKF, and dashboard rollback procedures.
- [ ] Define zero-duplicate-action/idempotency invariants.

### Epic 0.2 — Golden evaluation set

- [~] Cover Gmail reads/writes, Drive, Sheets, Docs, Calendar/Meet, Chat, Tasks, Contacts, multi-service workflows, ambiguity, misspellings, missing destinations/timezones, quota exhaustion, cold starts, Google 4xx/5xx, cancellation, browser disconnect, Vercel timeout, worker restart, duplicate submission, partial side effects, prompt injection, and cross-user isolation.
- [ ] Define expected plans, tools, arguments, artifacts, approvals, postconditions, and summaries.
- [ ] Replace unsafe mutations with deterministic Google adapter fakes in tests.

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

- [~] Claim work with `FOR UPDATE SKIP LOCKED`, heartbeat/lease, stale-job recovery, bounded retries, and a separate Railway worker using the application image; do not introduce Redis initially.

### Epic 2.3 — Replayable progress

- [x] Persist and stream run/plan/step/progress/approval/heartbeat/final events through SSE; reconnect by run ID and replay missed events.

### Epic 2.4 — Idempotency

- [~] Prevent duplicates from double-clicks, client/proxy retries, reconnects, worker restarts, and lost responses after Google succeeds.

Guardrail: disconnect/restart tests prove the worker continues and writes are not repeated.

## Sprint 3 — Structured planner and plan validation

- [x] Define typed `ExecutionPlan`, `PlanStep`, success criteria, assumptions, clarifications, dependencies, risk, approval, weights, preconditions, postconditions, and estimated budgets.
- [~] Ask only for materially missing information such as ambiguous person/space, timezone/duration, uniqueness, sharing, or destructive cleanup.
- [~] Reject unknown tools, placeholders, missing dependencies/recipients/timezones, impossible arguments, unsupported operations, unsafe parallel writes, and writes before required reads.
- [ ] Measure plan tool/order/coverage/necessity/cost/execution quality.

## Sprint 4 — Intent, service, model, and execution policy routing

- [ ] Classify read/write, live lookup/semantic recall, single/multi-service, simple/complex, reversible/irreversible, clarification, risk, and parallelism.
- [ ] Improve synonyms, misspellings, service detection, entity/date/timezone/recipient/Chat-space extraction; remove unsafe Gmail defaulting.
- [ ] Route by complexity, risk, quota, context, tool count, and reliability. Do not silently downgrade a complex mutation from 70B to an unreliable small model.
- [ ] Estimate and enforce token/time/tool budgets before execution.
- [ ] Implement deterministic high-risk confirmation with explicit opt-out and action-bound approvals.

## Sprint 5 — RAG necessity gate

- [x] Skip RAG for live/latest mutations/lookups; use it for semantic history, conceptual matching, prior context, and cross-document synthesis.
- [ ] Begin with deterministic audited rules; later evaluate a small classifier for none/metadata/keyword/vector/hybrid.
- [ ] Record whether/why RAG ran, latency, returned/used evidence, and outcome impact.

## Sprint 6 — Source-aware RAG ingestion and chunking

### Epic 6.1 — Source strategies

- [ ] Gmail: metadata on every child, clean body, quoted-history/signature detection, thread parent-child, attachment metadata, deduplication.
- [ ] Docs/Drive text: title/heading/paragraph/list/table hierarchy; small retrieval children and larger generation parents.
- [ ] PDFs: layout/headings, page/bounding provenance, OCR marker, independent table handling, no column corruption.
- [ ] Sheets: typed header-aware row groups/ranges/tab/row IDs; structured filtering before vectors.
- [ ] Calendar/Meet: structured event/participant/recording metadata; speaker/topic chunks for transcripts; Drive hierarchy for transcript documents.
- [ ] Chat: space/thread/sender/time windows/topic boundaries/reply relationships.
- [ ] Contacts/Tasks: structured lookup first; generally no chunking for atomic records.
- [ ] OKF: Markdown/YAML concept and heading-aware chunks; keep tool schemas, prerequisites, warnings, and parents intact.

### Epic 6.2 — Versioned experiments and lineage

- [ ] Evaluate 256/512/768/1024-token and source-dependent policies, overlap, parent sizes, and no-chunk cases.
- [ ] Store source/parent/chunk position/hash, ACL/tenant, embedding/chunker/sync versions, timestamps, tombstones, provenance, and reindex time.
- [ ] Incrementally re-embed only changed content/version/metadata.

### Epic 6.3 — Retrieval pipeline

- [ ] Query classification -> structured filters -> dense vector + PostgreSQL text search -> rank fusion -> recency/metadata -> dedupe/diversity -> rerank -> threshold/budget -> context/citations.
- [ ] Evaluate query normalization/entity/date/acronym/multi-query/HyDE only where measured; never expand precise identifiers/latest lookups unnecessarily.
- [ ] Evaluate recall@k, precision@k, MRR, nDCG, context precision/recall, faithfulness, relevance, citation correctness, latency, tokens, duplication, cost, and permission leaks per source.

## Sprint 7 — Decouple embedding from live tools

- [x] Return live Google results first and enqueue optional persistence/embedding.
- [x] Batch, dedupe by hash, bound concurrency, time out per item, retry asynchronously, dead-letter failures, and expose embedding health.
- [~] Apply backpressure to Ollama and monitor cold start, queue, duration, loaded state, errors, input size, and overflow retries.

## Sprint 8 — Dependency-aware durable executor

- [ ] Execute independent reads concurrently and dependent/high-risk writes in verified order.
- [ ] Bound concurrency per run/user/API/model and avoid unbounded gather.
- [ ] Retry transient network/429/5xx/worker failures only; do not retry invalid input, permission denial, invalid timezone, or cancellation blindly.
- [ ] Use deterministic idempotency keys and artifact lookup before retrying Google writes.

## Sprint 9 — Verification and deterministic postconditions

- [ ] Add tool-specific postconditions and read-after-write for critical Sheets, Drive, Chat, Calendar/Meet, Gmail, Docs, Tasks, and sharing states.
- [ ] Require resource IDs, expected content/rows/recipient/timezone/link/sharing state; HTTP 200 alone is not success.
- [ ] Prevent the final agent from claiming unverified success.

## Sprint 10 — Recovery, resume, and compensation

- [x] Implement failure taxonomy for user/planning/routing/model/tool/auth/permission/quota/network/database/embedding/verification/cancellation/worker/proxy/security.
- [x] Resume from the first safe incomplete step without recreating verified artifacts.
- [ ] Preserve/report, retry population, roll back sharing, cancel incorrect events, or delete only when explicitly approved and safe.
- [ ] Surface pending high-risk action approvals in the run state and frontend.

## Sprint 11 — Token, latency, quota, and budget accounting

- [ ] Capture per-call input/output/schema tokens where available, model, queue/prompt/completion/tool time, fallback, and rate-limit metadata.
- [ ] Attribute to planner/router/executor/verifier/recovery/summarizer and aggregate per step/run/user/model.
- [ ] Reserve quality-model quota for complex tasks; avoid spending it on capability questions.
- [ ] Stop/replan before runaway loops and present quota-aware defer/simplify choices.

## Sprint 12 — Automatic incident summaries

- [x] Deterministically identify last success, first failure, primary/contributing causes, evidence, artifacts, and cancellation source.
- [ ] Generate a short summary only after structured facts exist.
- [x] Calculate technical, functional, user-visible, and side-effect-integrity completion separately.
- [ ] Link diagnosis to events, attempts, traces, artifacts, metrics, and external errors with confidence.

## Sprint 13 — Production observability and Grafana

- [ ] Deploy lightweight Alloy on Railway; scrape metrics and forward to Grafana Cloud with filtering, WAL buffering, privacy, and cardinality controls.
- [ ] Add structured logs and OpenTelemetry HTTP/DB/worker traces progressively; keep LangSmith for agent/LLM traces.
- [ ] Add Grafana Cloud aggregate dashboards: traffic, latency, errors, tools, quota/fallback, RAG, queue, active/cancelled runs, artifacts, OAuth, DB, Google APIs, Ollama.
- [ ] Add Neon PostgreSQL read-only session/workflow dashboards: task, current step, progress, duration, versions, tokens, heartbeat, breaking point, artifacts, incident, trace links.
- [ ] Add alerts for missing heartbeat, backlog, cancellation, quota, Ollama, Neon, tools, orphaned artifacts, RAG latency/quality, OAuth, and deployment regression.
- [ ] Provision the same dashboards locally; local Grafana may query production sources but production does not depend on it.

## Sprint 14 — Frontend run and admin experience

- [ ] Add live plan/current/completed/pending steps, progress, heartbeat, fallback, clarification, and approval UI.
- [ ] Reconnect/resume by run ID and preserve partial verified artifacts.
- [ ] Show concise user failure plus authorized detailed administrator diagnosis.
- [ ] Add history filters by session/status/user/service/model/failure/time/version.
- [~] Add protected Admin Improvement Center with evidence, diffs, evaluations, risk, privacy, rollback, approve-canary/change/reject/promote actions, expiry, audit, and step-up approval.

## Sprint 15 — Feedback and governed learning dataset

- [x] Capture overall rating and step-specific wrong/missing/slow/tool/data/safety/free-text feedback.
- [x] Include negative and failed runs in evaluation candidates.
- [x] Store sanitized corrected trajectories: original plan/execution/failure/diagnosis/corrected plan/expected result.
- [ ] Version datasets; separate train/validation/test, consent, retention, access, deletion, and leakage prevention.

## Sprint 16 — Evaluation and replay

- [ ] Build mock Google adapters and safe replay for mutations.
- [ ] Compare old/new planner, prompt, OKF, routing, chunking, model, and recovery policies on identical tasks.
- [ ] Measure task/plan/tool/artifact correctness, latency, tokens, recovery, side effects, satisfaction, and retrieval.
- [ ] Block promotion on golden-task, token, cancellation, isolation, verification, safety, or RAG regression.

## Sprint 17 — Prompt optimization and bounded contextual bandits

- [ ] Experiment with planner/router/verifier/recovery prompts independently.
- [ ] Allow bandits only among already validated low-risk policies such as RAG gate, prompt variant, read-task model, retrieval, or planner strategy.
- [ ] Track completion, correctness, rating, latency, tokens, errors, orphaned artifacts, and unsafe effects separately; do not prematurely collapse reward.

## Sprint 18 — RL readiness, not live RL

- [x] Store state -> decision -> action -> observation -> reward -> next-state trajectories.
- [ ] Implement offline policy evaluation and only later offline experiments on verified data.
- [ ] Never allow exploratory RL to experiment with real emails, invitations, sharing, deletion, or Chat messages.
- [ ] Require separate human approval, consent/data review, stable holdout baseline, rollback, and cost/security evidence before any fine-tuning/RL.

## Sprint 19 — Security, privacy, isolation, and retention

- [ ] Scope every run/event/artifact/retrieval/approval to user/tenant.
- [ ] Treat Google content as untrusted and defend against prompt injection/data exfiltration.
- [ ] Enforce tool allowlists, approval policies, recipients, bulk/destructive limits, and rate/abuse controls.
- [ ] Encrypt OAuth credentials, rotate keys, redact telemetry, audit access, implement export/deletion, and automate approved retention.
- [ ] Keep public/private OKF and diagnostic/training consent separate.

## Sprint 20 — DBeaver and reporting database access

- [x] Create `dbeaver_analyst`-style Neon role and curated reporting schema/views with no secret table access.
- [x] Configure production Neon, local Homebrew, and local Docker connections with approved names/colors/folders/read-only settings.
- [ ] Provide ER diagram and views for run status, timeline, failure, models/tokens, retrieval, tools, artifacts, prompts, session history, security, improvements, and canaries.
- [ ] Store credentials only in DBeaver secure storage; stop only for unavoidable GUI/master-password interaction.

## Sprint 21 — Deployment, canary, and rollback

- [ ] Add feature flags, shadow planning, selected-user cohorts, and old/new side-by-side comparison.
- [ ] Expand schema -> dual write/backfill -> switch reads -> remove legacy only after validation.
- [ ] Canary approved candidates; pin in-flight versions; auto-rollback guardrail breaches; require final human promotion.
- [ ] Preserve in-flight runs during worker/frontend rollback and retain legacy `/chat` until exit criteria pass.
- [ ] Roll through internal, 5–10, 20–30, 40–50, and 80–90 user gates.

## Sprint 22 — Documentation and runbooks

- [x] Document architecture, state machine, failure taxonomy, on-call/alerts, quota, Ollama, Neon, OAuth, artifacts, safe deployments, learning governance, privacy/retention, DBeaver, canary, and rollback.

## Sprint 23 — Open Knowledge Format operational knowledge layer

- [~] Implement OKF v0.1-compatible public and private bundles with Markdown, YAML frontmatter, stable concept paths, index/log files, links, provenance, version/owner/tags/timestamps.
- [ ] Concepts cover capabilities, tools, workflows, policies, schemas, metrics, failures, runbooks, Google API limits, RAG sources, and agent capabilities.
- [ ] Generate drafts deterministically from trusted tool registry/OpenAPI/migrations/scopes/metrics, validate links/schema/tool references, scan secrets/PII, and require human publication approval.
- [ ] Keep Markdown as source of truth; index structured/heading-aware chunks and graph links in Neon separately from user-content RAG.
- [ ] Use OKF for capability discovery, workflow selection, prerequisites/OAuth, validation, recovery, and explanations; never treat retrieved user content as operational authority.
- [ ] Record OKF versions/retrievals in runs, LangSmith, Neon, and Grafana; compare versions through replay/canary evaluation.
- [ ] Production incidents may create sanitized candidate OKF drafts but never update trusted knowledge directly.

## Sprint 24 — Governed improvement proposal and publication service

- [x] Add proposal/evidence/evaluation/approval/canary/audit/version database models and lifecycle transitions.
- [ ] Threshold recurring/severe findings; deduplicate and expire stale proposals.
- [ ] Produce exact versioned diffs and GitHub draft PRs for public code/OKF/config; store private revisions in protected storage.
- [ ] Notify through admin UI, sanitized email, Grafana, and GitHub; never expose private evidence in notifications.
- [ ] Freeze approved hashes, rerun all gates, deploy selected-user/5–10% canary, compare control/candidate, auto-rollback on guardrail failure, and request final promotion.
- [ ] Retain audit identity/time/version/purpose and invalidate approval after material change.

## Sprint 25 — Original specification compatibility and final guardrails

- [ ] Re-run every still-applicable command and acceptance criterion in `PROJECT_SPEC.md` against the upgraded implementation.
- [ ] Run Python formatting/lint/type/security/unit/integration/RAGAS/migration tests.
- [ ] Run Next.js lint/type/build/tests and Flutter analyze/test/build where toolchains allow.
- [ ] Run Docker Compose build/up/health/metrics/Grafana/Prometheus/Ollama/PostgreSQL tests on Docker Desktop.
- [ ] Run secret/history scans, authorization/PII/prompt-injection/idempotency/cancellation/canary/rollback tests.
- [ ] Run GitHub Actions, Railway, Neon, Vercel, Google OAuth, Grafana Cloud, Alloy, and LangSmith production smoke tests where credentials/account state allow.
- [ ] Document every external blocker with exact user steps; do not mark complete while an in-scope safe action remains.

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
