# Completion audit — 2026-07-20

This audit distinguishes implemented engineering from conclusions that require
external credentials or longitudinal production evidence.

## Newly closed engineering gaps

- Embedding persistence has atomic PostgreSQL admission control with global,
  per-user, and payload-size limits. Rejections are reason-labelled metrics.
- Temporary Google/API 500, 502, 503, and 504 failures are retryable network
  failures; 403 remains permission, 404 remains a non-retryable execution error,
  and 429 remains rate/quota.
- Browser disconnection is tested by closing one application client, reconnecting
  through a fresh client, restoring the durable run, and cancelling it safely.
- Google Meet `space` no longer creates a false Google Chat step. Instant Meet
  creation routes to the registered Meet create/verify tools.
- Structured HTTP logs include only method, route, status, duration, a validated
  correlation ID, and a validated traceparent. Bodies, queries, users, client
  addresses, OAuth data, and Google content are excluded.
- Uvicorn's raw access log is disabled so query strings cannot bypass that
  policy. Authentication rejections are resolved to bounded route templates and
  remain observable without storing resource identifiers.
- Optional OpenTelemetry instruments FastAPI, HTTPX, and asyncpg and exports over
  OTLP/HTTP only when a safe endpoint is configured. Public non-TLS exporters are
  rejected.
- Metrics and alerts now cover OAuth outcomes, rolling offline RAG quality,
  embedding admission rejection, and immutable deployment telemetry.
- A mounted protected OKF directory is validated and namespaced under `private/`.
  Normal runtime retrieval is public-only; protected retrieval requires an
  explicit authorized code path that no current user request enables.
- Grafana Cloud receives Railway API/worker metrics through Alloy, traces through
  Tempo, and bounded-cardinality sanitized request logs through Loki. Both the
  aggregate and restricted Neon session dashboards are installed; all 17 alert
  rules evaluate successfully and route to the Grafana administrator.
- Migration 008 separates runtime prompt telemetry from valid RAG-evaluation
  samples. RAG regression is evaluated only after ten evidence-bearing examples,
  while a separate alert reports insufficient evaluation data.
- The OKF consumer follows the official v0.1 reserved-file rules: `index.md` and
  `log.md` are not concepts, the root index declares `okf_version: "0.1"`, minimal
  concepts require only `type`, and broken links remain consumable. Production
  synchronization separately enforces this project's human-approval profile.
- Sprint 27 adds a guarded six-way Workspace intent gateway, context-sensitive
  multi-service DAG planning, and durable failure intelligence for pre-execution and
  terminal failures. Every incident has a privacy-bounded fingerprint, two review
  strategies, and human-labelled proposal conversion.
- Migration 010 adds three read-only failure reporting views. The protected portal,
  Prometheus metrics/alerts, and version-controlled Grafana dashboards expose the new
  review and notification state without high-cardinality metric labels.

## Direct evaluation evidence

The combined suite covers:

- Gmail, Drive, Docs, Sheets, Calendar, Meet, Chat, Tasks, Contacts, and mixed
  workflows through planner golden cases and tool/verification tests.
- Missing time, duration, timezone, Chat destination, ambiguity, misspelling,
  quota/rate limit, OAuth/permission, 4xx/5xx, and malformed model output.
- Browser/proxy disconnect, cancellation, expired worker lease, duplicate run,
  duplicate Google write, retry, partial side effects, compensation, prompt
  injection, and cross-user isolation.

The authoritative commands are the repository CI jobs, `pytest tests/`,
`scripts/run_golden_evals.py`, `scripts/run_workflow_replays.py`, and
`scripts/run_policy_evals.py`.

The final verified evidence is recorded in the progress log and repository CI.
It includes exact-image and host backend suites, 28 planner golden cases, four
no-network mutation replays, migration downgrade/forward repair through revision
011, Python/web/mobile security and build gates, healthy local and production
services, 17 evaluated Grafana Cloud rules, and 34 installed dashboard panels.

The production failure audit also found five recent terminal runs that predated the
failure inbox. Its first backfill exposed a JSON serialization defect: PostgreSQL
completion percentages arrived as `Decimal` values and were silently skipped. The
analyzer now converts telemetry to JSON-safe numbers, normalizes JSON/JSONB objects,
and logs only the run ID plus exception type when a historical row cannot be processed.
The corrected backfill created five sanitized incidents, five two-option reviews, and
twenty channel-ledger rows. Admin/Grafana delivery is internal; email/GitHub remain
skipped behind explicit configuration and approval.

The teaching-phase lease audit then closed a narrower crash-recovery gap. Expired
leases now requeue only bounded read-only work. An exhausted read becomes an ordinary
worker failure, while a write interrupted before durable acknowledgement becomes a
`worker_reconciliation` incident and cannot be resumed blindly. Integration tests
cover replacement-worker execution, interrupted-read recovery, retry exhaustion, the
uncertain-write terminal state, portal incident creation, and resume rejection.

The subsequent RAG lesson corrected an earlier overstatement: child chunks carried
lineage identifiers, but larger generation parents were not durably stored or expanded.
Migration 011 adds tenant-scoped, versioned parent sections and a content-free reporting
lineage view. Gmail and Docs/Drive v3 chunkers now bind precise children to larger
parents; ingestion incrementally hashes and tombstones both levels; hybrid retrieval
matches on children, expands a deduplicated parent under the same tenant, preserves the
matched-child citation, and applies only a bounded recency tie-breaker. Tests cover
parent expansion and cross-tenant denial.

The DBeaver verification found that a separate host PostgreSQL process had reclaimed
port 5433 ahead of Docker. The Docker database is now bound only to
`127.0.0.1:55432`, the installed DBeaver definition was updated, and Neon read-only,
Homebrew, and Docker were each live-queried at revision 011 with 18 reporting views.

## Correctly unresolved conclusions

- Chunk-size, overlap, parent-size, query transformation, HyDE, reranker, and
  source-specific retrieval winners require labelled relevance judgments.
- Prompt/model/routing/OKF policy winners and offline RL require at least the
  approved verified sample minimum and stable train/validation/test splits.
- Pilot expansion requires real consenting users and elapsed production evidence.
- DBeaver connection definitions and production credential storage are complete: the
  definitions are password-free, the analyst credential is in macOS Keychain, and the
  role's reporting access/read-only/OAuth-table denial were verified. Copying that
  secret into DBeaver's separate encrypted vault is optional convenience, not a blocker.
- External proposal email/GitHub delivery requires a chosen recipient or scoped
  publisher token and an explicit publication confirmation.

These are not replaced with fabricated synthetic production claims.

## Teaching deliverable

The post-upgrade teaching curriculum is implemented and grounded in the repository rather
than generic examples. It covers execution DAGs and topological order, durable state
machines, PostgreSQL queues and leases, idempotency and effectively-once effects,
source-aware chunk trees, sliding windows, HNSW and reciprocal-rank fusion, greedy
versus dynamic-programming context packing, memoization and incremental embeddings,
bounded backtracking and compensation, admission/rate limiting, consistent hashing,
contextual bandits, MDP trajectories, offline-RL safety boundaries, and OKF v0.1 plus
the project's stricter publication profile. The durable course is
`docs/TEACHING_AGENTIC_DSA_OKF.md`, with a focused OKF companion in
`docs/OKF_PROJECT_GUIDE.md`. Interactive lessons proceed after the working engineering
upgrade, beginning with dynamic programming and guarded policy selection.

## Final requirement disposition

- Approved sprint/epic/story plan and detailed implementation ledger: complete.
- Source-aware RAG/chunking and evaluation infrastructure: complete; empirical winners
  remain correctly evidence-gated.
- Production, local Homebrew, and local Docker DBeaver definitions: installed. The
  production connection uses `dbeaver_analyst`, SSL, a read-only server role, and
  `save-password: false`; the credential is stored in macOS Keychain and was verified
  against Neon without exposing it.
- Engineering implementation, deployment, observability, documentation, and tests:
  complete and verified. The durable teaching material is complete; interactive
  teaching remains an ongoing user-facing phase.
- Longitudinal pilot expansion, policy/RAG winners, and real external proposal delivery:
  cannot be completed without real users, elapsed labelled evidence, or the expressly
  required human publication decision.
