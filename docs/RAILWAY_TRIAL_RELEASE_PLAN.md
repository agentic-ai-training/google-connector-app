# Railway trial: capacity, cost and reversible release gates

24 October 2026. [Scoped readback and operator billing evidence](RAILWAY_NEW_WORKSPACE_PREFLIGHT_2026_10_22.md)
establish the intended empty workspace and reported $5 trial balance. No service
has been created or started by this preflight. No paid upgrade is approved.

## Cost arithmetic, not a measured forecast

Using the operator's displayed rates, average aggregate 1 GB RAM costs
`0.000231 × 1440 = $0.33264/day`. Average aggregate 0.5 vCPU costs
`0.000463 × 1440 × 0.5 = $0.33336/day`. Together this illustrative workload costs
**$0.666/day**, so $5 covers approximately **7.5 days**, before egress/other charges.
RAM alone at that average covers about 15 days. Configured limits are not average
usage, GB is not GiB, and neither example promises a month of hosting. API and
worker usage must be added together. Budgets/alerts are not hard resource caps.

## Ordered activation gates

1. Validate [private context exclusions](DEPLOYMENT_CONTEXT_BOUNDARY.md), reviewed
   source and content-secret scan before any upload. The local synthetic Git test
   is passed; on 28 October the real Docker Desktop scratch export also passed
   (30 private/10 public canaries, 14 focused tests) after fixing isolated Buildx
   discovery. Actual Railway upload, current application-image and CI proofs
   remain distinct and pending.
2. Confirm legitimate workspace ownership/access and trial networking. If a
   disposable network probe is needed, first specify exact public image, endpoints,
   maximum duration/cost and automatic stop/cleanup. No production keys/private
   source or model requests belong in that probe. Do not create further accounts
   or bypass verification/trial eligibility.
3. Measure the actual image, API and worker separately against 1-GB container and
   disk limits. Keep durable workers distinct; do not silently embed them in the
   HTTP process merely to fit a trial. A green health endpoint is not load proof.
4. Before a production start, approve an immutable release, Neon backup/restore,
   compatibility/migration sequence, bounded funding term and rollback. The current
   Dockerfile default runs **`alembic upgrade head` on startup**; starting a service
   is therefore not a harmless read-only capacity probe.
5. Transfer only specifically scoped approved variables. Verify Google OAuth,
   Neon, model routing/privacy and outbound access with safe inputs; then verify
   actual durable runs and reconnect/recovery behavior before Vercel cutover.
6. Record deployment IDs, measured usage and stop controls. Roll back new traffic
   without repeating completed writes. Reconcile artifacts/uncertain operations;
   stop trial services at the approved term or budget threshold.

Core production PostgreSQL stays Neon; local PostgreSQL stays local; frontend stays
Vercel. This trial does not authorize placing Kafka, Temporal, Redis, Langfuse or
Ollama on the same small allowance, nor allocating new paid infrastructure. Each
needs its own measured placement/failure-domain and cost approval. The existing
Delhi VM remains stopped with expired hosting authority; GCP Vertex and reserve
budgets are untouched. No migration or live activation has occurred here.
