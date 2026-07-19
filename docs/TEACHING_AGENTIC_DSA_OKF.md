# Learning agentic systems through this repository

This guide connects the project's implementation to the data structures and
algorithms that make durable agents reliable. It is intentionally grounded in
the code in this repository rather than isolated textbook exercises.

## 1. Graphs, DAGs, and topological execution

An execution plan is a directed graph. A step is a vertex and a dependency is a
directed edge. The planner creates the graph; the worker executes only vertices
whose incoming dependencies are complete. Independent read vertices may run in
parallel, while mutation chains remain ordered.

Example: `fetch Gmail -> create Sheet -> verify link -> {send Chat, create Meet}`.
Chat and Meet are parallel only after the verified Sheet URL exists. A cycle
means the plan is invalid. Topological ordering, cycle detection, bounded
concurrency, and dependency-failure propagation live conceptually in
`app/runs/planner.py` and `app/runs/worker.py`.

## 2. State machines and durable runs

`agent_runs`, `agent_run_steps`, and append-only `agent_run_events` form a state
machine. Valid transitions prevent a run from jumping from queued directly to
success, preserve evidence after a crash, and allow a restarted worker to resume
from the first safe incomplete step. Leases and heartbeats distinguish a slow
run from an abandoned worker.

This is the same idea as a distributed transaction log: current state is useful,
but the event history explains how that state was reached.

## 3. Queues, heaps, and scheduling

The PostgreSQL queue uses row locks and `FOR UPDATE SKIP LOCKED` so multiple
workers claim different jobs safely. A future priority scheduler could use a
heap ordered by risk, age, deadline, and estimated cost. Retry scheduling is a
delayed queue problem, while per-user and global limits are fairness constraints.

## 4. Hash maps, sets, and idempotency

A deterministic idempotency key maps one logical external write to one durable
result. Before retrying Gmail, Calendar, Sheets, Docs, or Chat, the executor checks
for a recorded or reconcilable artifact. Hash maps provide constant-time lookup;
sets deduplicate senders, document hashes, tool names, and OAuth scopes.

Content hashes also make RAG indexing incremental: unchanged chunks do not need
new embeddings.

## 5. Trees, chunking, and retrieval indexes

Google Docs have a heading tree. A small child chunk supports precise matching;
its parent section restores broader context. Gmail uses thread/message lineage,
Sheets use header-aware row groups, PDFs use layout/table/OCR boundaries, and
Meet transcripts use speaker turns and topics. There is no universally correct
chunk size because each source has a different structure and query distribution.

pgvector's HNSW index is itself a proximity graph. Hybrid retrieval combines
vector neighbors with PostgreSQL full-text results, then fuses ranks, removes
duplicates, preserves permissions, and packs context under a token budget.

## 6. Sliding windows, greedy packing, and dynamic programming

Token-aware overlapping windows are useful only when a source has no better
semantic boundary. Context packing is currently a constrained selection problem:
maximize relevant, diverse evidence without exceeding a token budget. A greedy
score-per-token method is fast; dynamic programming can find a better knapsack
solution when exact optimization is worth its cost.

Model and workflow selection is another constrained optimization problem across
correctness, latency, token quota, risk, and tool count.

## 7. Backtracking, replanning, and compensation

When a postcondition fails, the recovery path resembles bounded backtracking:
retain the verified prefix, reject the bad branch, and choose a safe alternative.
External writes cannot simply be undone in memory, so compensation is explicit:
report and preserve a useful artifact, retry population, roll back sharing, or
delete/cancel only with the required approval.

## 8. Memoization, caches, and rate limiting

Embedding hashes, verified artifact records, and tool results are memoized work.
They reduce latency and protect quotas. Token-bucket or leaky-bucket algorithms
fit per-user Google/API and model limits. Bounded concurrency prevents one run
from exhausting the entire service.

Consistent hashing becomes useful if sessions or tenants later need stable
placement across multiple worker/cache partitions.

## 9. Bandits, MDPs, and later reinforcement learning

A contextual bandit may choose only among already validated, low-risk policies:
RAG/no-RAG, prompt variants, safe-read models, or retrieval strategies. Reward
components stay separate: completion, correctness, latency, tokens, user rating,
tool errors, orphaned artifacts, and unsafe effects.

The stored trajectory schema—state, decision, action, observation, reward, next
state—makes offline policy evaluation possible. Live exploratory RL is locked.
It must never experiment with real email, Chat, invitations, sharing, or deletion.
Most current reliability gains come from orchestration, verification, and
idempotency rather than model fine-tuning.

## 10. OKF in this project

The `knowledge/` bundle is curated operational knowledge expressed as Markdown
with validated YAML frontmatter. It contains capabilities, workflows, policies,
schemas, metrics, failure guidance, RAG source rules, and runbooks.

OKF has four important boundaries:

- Live Google APIs answer current facts and perform actions.
- PostgreSQL stores durable runs, artifacts, telemetry, and structured facts.
- User-content RAG retrieves tenant-scoped evidence from email and documents.
- OKF supplies trusted operational rules and explanations.

Retrieved email or document content is untrusted evidence; it can never override
OKF policy or system authority. Deterministic generation may create an OKF draft,
but only human-approved documents become trusted. Each document records ownership,
version, publication state, provenance, and links. Runtime retrieval records the
OKF version so a failed run can be replayed against a candidate version.

The safe improvement loop is therefore:

`incident -> sanitized evidence -> draft -> validation -> offline replay -> human
canary approval -> bounded canary -> automatic rollback or human promotion`.

That loop improves the system while keeping the public repository, private user
content, and production authority cleanly separated.
