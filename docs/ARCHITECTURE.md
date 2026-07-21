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

## Failure-to-improvement lifecycle

Each failed intake or durable run creates an immutable sanitized occurrence. Versioned
fingerprints group only the same mechanism and architectural boundary; an analyzer may
then form a systemic theme from multiple concrete clusters. Every occurrence and theme
offers exactly two reviewable strategies. A selection queues a Groq-built untrusted
candidate—it does not itself change runtime behavior.

The builder receives bounded repository tools and sanitized evidence, but no Workspace
content, OAuth token, production database, or deployment credential. Trusted CI must bind
the resulting files, commit, tree, hashes, validation commands, rollback manifest, and
privacy/security results. Human gates remain separate for draft PR publication, candidate
deployment, real-user canary activation, trusted OKF publication, and promotion.

Worker-compatible code candidates run in a separate Railway project and claim only runs
pinned to their immutable executor version. API/planner candidates use the same isolated
candidate image with an HTTPS domain created only for the `api` runtime surface; the
control API performs bounded applicability/cohort selection and proxies creation/resume
to that exact attested version. Prompt/config/OKF candidates use versioned registries.
Frontend candidates deploy as immutable non-production Vercel previews from the frozen
commit. Trusted CI verifies project/deployment identity and a version-bound health route;
the authenticated control frontend then hands only stable approved-cohort users to the
preview through a URL fragment. A preview sends expired users back through control OAuth.
Safety regression stops assignment, returns never-started queued runs to control, and
scales down the candidate and removes any attested preview through the cleanup controller.

## Result and optimization boundaries

Live tools return an approved-field compact envelope to model history. A necessary full
result may be encrypted in tenant-scoped, expiring private storage and referenced by an
opaque identifier; exports exclude it. Exact structured operations such as recent Gmail
sender extraction bypass message bodies, RAG, and LLM extraction.

DP context packing, workflow choice, and periodic quota allocation are offline or
feature-flagged candidate policies. They filter ACL/risk-invalid choices first, stay
within hard resource bounds, and fall back to deterministic greedy/queue policies. They
cannot relax write approval, OAuth, verification, or side-effect constraints.
