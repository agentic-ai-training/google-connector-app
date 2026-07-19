# Upgrade architecture

## Request lifecycle

1. `POST /runs` creates a tenant-scoped, idempotent run and a validated service DAG.
2. Materially missing time, timezone, duration, or Chat destination pauses in
   `awaiting_clarification`.
3. High-risk writes pause in `awaiting_approval`; approval is bound to the SHA-256
   hash of the exact plan and expires after 30 minutes.
4. A PostgreSQL worker claims work with `FOR UPDATE SKIP LOCKED`, renews its lease,
   executes dependency-ready steps, and writes append-only events.
5. Tool/model attempts, artifacts, verification, completion, and incident evidence
   are stored separately. Browser disconnection cannot cancel the worker.
6. SSE or polling replays durable events by run ID. Resume resets only failed steps;
   completed steps and verified artifacts remain intact.

The legacy `/chat` route remains available behind `LEGACY_CHAT_ENABLED` for rollback,
but it rejects high-risk mutations so those must use the approved durable path.

## Knowledge boundaries

- Live Google APIs are authoritative for current state and mutations.
- User Google content is untrusted tenant evidence in `rag_chunks`, scoped by user ID.
- The OKF Markdown bundle is trusted operational knowledge. It is loaded, validated,
  versioned, retrieved, and cited separately from user RAG.
- Neon/PostgreSQL stores durable workflow facts and high-cardinality reporting data.
- Prometheus/Grafana stores aggregate metrics; LangSmith stores agent/LLM traces.

## Learning boundary

Feedback and failures can create sanitized, consented trajectories and improvement
proposals. Analysis may draft a versioned diff, but cannot publish it. A human must
approve the frozen hash for canary, measured canary guardrails must pass, and a human
must approve publication. Live exploratory RL and automatic trusted-OKF edits are
locked off.
