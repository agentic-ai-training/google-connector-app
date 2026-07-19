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
- [x] Capture current latency, failure, RAGAS, token, quota, and Google artifact baselines.
- [x] Inventory current code paths, services, database objects, dashboards, and external artifacts.
- [x] Verify secrets/credentials are ignored and absent from Git history.
- [x] Add feature flags for legacy chat, durable runs, OKF, new RAG, governed improvements, and canary cohorts.
- [x] Preserve legacy `/chat` as a rollback path until the new executor is proven.
- [x] Define migration, deploy, worker, index, prompt, OKF, and dashboard rollback procedures.
- [x] Define zero-duplicate-action/idempotency invariants.

### Epic 0.2 — Golden evaluation set

- [~] Cover Gmail reads/writes, Drive, Sheets, Docs, Calendar/Meet, Chat, Tasks, Contacts, multi-service workflows, ambiguity, misspellings, missing destinations/timezones, quota exhaustion, cold starts, Google 4xx/5xx, cancellation, browser disconnect, Vercel timeout, worker restart, duplicate submission, partial side effects, prompt injection, and cross-user isolation.
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

- [~] Evaluate 256/512/768/1024-token and source-dependent policies, overlap, parent sizes, and no-chunk cases.
- [x] Store source/parent/chunk position/hash, ACL/tenant, embedding/chunker/sync versions, timestamps, tombstones, provenance, and reindex time.
- [x] Incrementally re-embed only changed content/version/metadata.

### Epic 6.3 — Retrieval pipeline

- [x] Query classification -> structured filters -> dense vector + PostgreSQL text search -> rank fusion -> recency/metadata -> dedupe/diversity -> rerank -> threshold/budget -> context/citations.
- [~] Evaluate query normalization/entity/date/acronym/multi-query/HyDE only where measured; never expand precise identifiers/latest lookups unnecessarily.
- [~] Evaluate recall@k, precision@k, MRR, nDCG, context precision/recall, faithfulness, relevance, citation correctness, latency, tokens, duplication, cost, and permission leaks per source.

## Sprint 7 — Decouple embedding from live tools

- [x] Return live Google results first and enqueue optional persistence/embedding.
- [x] Batch, dedupe by hash, bound concurrency, time out per item, retry asynchronously, dead-letter failures, and expose embedding health.
- [~] Apply backpressure to Ollama and monitor cold start, queue, duration, loaded state, errors, input size, and overflow retries.

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

- [!] Deploy lightweight Alloy on Railway; scrape metrics and forward to Grafana Cloud with filtering, WAL buffering, privacy, and cardinality controls (blocked only by absent Grafana Cloud remote-write credentials).
- [~] Add structured logs and OpenTelemetry HTTP/DB/worker traces progressively; keep LangSmith for agent/LLM traces.
- [~] Add Grafana Cloud aggregate dashboards: traffic, latency, errors, tools, quota/fallback, RAG, queue, active/cancelled runs, artifacts, OAuth, DB, Google APIs, Ollama.
- [x] Add Neon PostgreSQL read-only session/workflow dashboards: task, current step, progress, duration, versions, tokens, heartbeat, breaking point, artifacts, incident, trace links.
- [~] Add alerts for missing heartbeat, backlog, cancellation, quota, Ollama, Neon, tools, orphaned artifacts, RAG latency/quality, OAuth, and deployment regression.
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
- [~] Encrypt OAuth credentials, rotate keys, redact telemetry, audit access, implement export/deletion, and automate approved retention.
- [x] Keep public/private OKF and diagnostic/training consent separate.

## Sprint 20 — DBeaver and reporting database access

- [x] Create `dbeaver_analyst`-style Neon role and curated reporting schema/views with no secret table access.
- [x] Configure production Neon, local Homebrew, and local Docker connections with approved names/colors/folders/read-only settings.
- [x] Provide ER diagram and views for run status, timeline, failure, models/tokens, retrieval, tools, artifacts, prompts, session history, security, improvements, and canaries.
- [!] Store credentials only in DBeaver secure storage; final master-password/GUI confirmation remains a user-local interaction.

## Sprint 21 — Deployment, canary, and rollback

- [x] Add feature flags, shadow planning, selected-user cohorts, and old/new side-by-side comparison.
- [x] Expand schema -> dual write/backfill -> switch reads -> remove legacy only after validation.
- [x] Canary approved candidates; pin in-flight versions; auto-rollback guardrail breaches; require final human promotion.
- [x] Preserve in-flight runs during worker/frontend rollback and retain legacy `/chat` until exit criteria pass.
- [!] Roll through internal, 5–10, 20–30, 40–50, and 80–90 user gates (requires real pilot users and measured runs over time).

## Sprint 22 — Documentation and runbooks

- [x] Document architecture, state machine, failure taxonomy, on-call/alerts, quota, Ollama, Neon, OAuth, artifacts, safe deployments, learning governance, privacy/retention, DBeaver, canary, and rollback.

## Sprint 23 — Open Knowledge Format operational knowledge layer

- [~] Implement OKF v0.1-compatible public and private bundles with Markdown, YAML frontmatter, stable concept paths, index/log files, links, provenance, version/owner/tags/timestamps.
- [x] Concepts cover capabilities, tools, workflows, policies, schemas, metrics, failures, runbooks, Google API limits, RAG sources, and agent capabilities.
- [x] Generate drafts deterministically from trusted tool registry/OpenAPI/migrations/scopes/metrics, validate links/schema/tool references, scan secrets/PII, and require human publication approval.
- [x] Keep Markdown as source of truth; index structured/heading-aware chunks and graph links in Neon separately from user-content RAG.
- [x] Use OKF for capability discovery, workflow selection, prerequisites/OAuth, validation, recovery, and explanations; never treat retrieved user content as operational authority.
- [x] Record OKF versions/retrievals in runs, LangSmith, Neon, and Grafana; compare versions through replay/canary evaluation.
- [x] Production incidents may create sanitized candidate OKF drafts but never update trusted knowledge directly.

## Sprint 24 — Governed improvement proposal and publication service

- [x] Add proposal/evidence/evaluation/approval/canary/audit/version database models and lifecycle transitions.
- [x] Threshold recurring/severe findings; deduplicate and expire stale proposals.
- [x] Produce exact versioned diffs and GitHub draft PRs for public code/OKF/config; store private revisions in protected storage. Publication is hash-bound, sanitized, draft-only, and requires an explicit administrator confirmation plus a repository-scoped credential.
- [~] Notify through admin UI, sanitized email, Grafana, and GitHub; never expose private evidence in notifications. Admin/Grafana ledgers and both explicit-action adapters are complete; production email/GitHub delivery remains credential- and confirmation-gated.
- [x] Freeze approved hashes, rerun all gates, deploy selected-user/5–10% canary, compare control/candidate, auto-rollback on guardrail failure, and request final promotion.
- [x] Retain audit identity/time/version/purpose and invalidate approval after material change.

## Sprint 25 — Original specification compatibility and final guardrails

- [x] Re-run every safe, still-applicable command and acceptance criterion in `PROJECT_SPEC.md` against the upgraded implementation; real Google mutations remain confirmation-gated.
- [x] Run Python formatting/lint/type/security/unit/integration/evaluation/migration tests.
- [x] Run Next.js lint/type/build/tests and Flutter analyze/test/build where toolchains allow.
- [x] Run Docker Compose build/up/health/metrics/Grafana/Prometheus/Ollama/PostgreSQL tests on Docker Desktop.
- [x] Run secret/history scans, authorization/PII/prompt-injection/idempotency/cancellation/canary/rollback tests.
- [~] Run GitHub Actions, Railway, Neon, Vercel, Google OAuth, Grafana Cloud, Alloy, and LangSmith production smoke tests where credentials/account state allow. GitHub, Railway API/worker, Neon migration 007, Vercel, OAuth redirect/PKCE/scopes, metrics, authorization, and LangSmith pass; Grafana Cloud/Alloy remains externally blocked by its three absent remote-write values.
- [x] Document every external blocker with exact user steps; do not mark complete while an in-scope safe action remains.

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
