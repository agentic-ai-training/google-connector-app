# Learning agentic systems through this repository

This guide connects the project's implementation to the data structures and
algorithms that make durable agents reliable. It is intentionally grounded in
the code in this repository rather than isolated textbook exercises.

Use each lesson in the same order: understand the invariant, locate its
implementation, trace one request, calculate complexity, then change a fixture
and predict the result before running it. This turns DSA into an engineering
tool rather than a collection of interview tricks.

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

The durable hierarchy is split across `rag_chunks` and `rag_parent_sections`.
Only small children receive embeddings and participate in precise matching. A selected
child's `(tenant, source, source ID, parent ID, chunker version)` expands to one larger
generation parent. The citation keeps the matched child ID, while a reporting view
exposes lineage counts and hashes without exposing parent text.

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

## 11. Dynamic programming: context as a knapsack

Suppose retrieval returns `n` candidate chunks. Chunk `i` has token cost `w[i]`,
estimated value `v[i]`, source and parent metadata, and a hard total budget `B`.
The simplest exact formulation is zero/one knapsack:

```text
dp[i][b] = best value using the first i chunks with at most b tokens

dp[i][b] = dp[i-1][b]                              if w[i] > b
dp[i][b] = max(dp[i-1][b], dp[i-1][b-w[i]]+v[i]) otherwise
```

Time is `O(nB)` and the full table uses `O(nB)` memory. If only the score is
needed, iterate `b` backwards and use `O(B)` memory. Retain parent pointers to
reconstruct which chunks were selected.

The production packer in `app/rag/context_packer.py` is greedy because token
budgets are large, relevance is uncertain, and diversity, parent expansion,
recency, and duplicate penalties make value context-dependent. The robust design
is to enforce ACL/safety first, deduplicate, use fast greedy packing online, and
compare it offline with an exact or beam-search oracle on labelled examples.

Exercise: use costs `[120, 200, 280, 350, 500, 700]`, values
`[0.30, 0.48, 0.60, 0.72, 0.82, 0.90]`, and budget `900`. Compare value sorting,
value-per-token sorting, and exact DP. Then allow at most two Gmail chunks. The
state becomes `dp[i][b][gmail_count]`, demonstrating how constraints expand DP.

## 12. Graphs: planning and topological execution

In `app/runs/planner.py`, every `PlanStep` is a vertex and every dependency is a
directed edge. `validate_plan` checks predecessor existence and ordering. In
`app/runs/worker.py`, `_claim_step` selects only a pending step whose dependencies
are complete.

Kahn's topological-sort model is:

```text
indegree[v] = unfinished prerequisites of v
ready = vertices whose indegree is zero

while ready:
    v = ready.pop()
    execute v
    for each edge v -> u:
        indegree[u] -= 1
        if indegree[u] == 0: ready.push(u)
```

This is `O(V+E)`. If fewer than `V` vertices are removed, the remainder contains
a cycle. The worker stores graph state in PostgreSQL so a process restart does
not erase progress. Several ready reads may execute concurrently; dependent or
unsafe mutations remain ordered.

Exercise: draw the graph for “find the last twenty Gmail senders, create a Sheet,
then send its verified link in Chat and create a Calendar event with a Meet link.”
Mark reads, writes, verification, and the first point where two branches can run
concurrently.

## 13. Queues, leases, and distributed state machines

`claim_run` and `_claim_step` use `FOR UPDATE SKIP LOCKED`. A database row is a
durable queue node and the row lock is an atomic claim. Two workers can scan the
same queue but cannot claim the same item.

```text
claim: owner = worker_id, lease_expires_at = now + lease_duration
heartbeat: extend only if owner still matches
recovery: expired running rows become claimable again
```

A retry must preserve the idempotency key, completed-step ledger, and external
artifacts. Otherwise recovery could send the same email twice. Queue performance
depends on partial indexes over runnable states; fairness additionally requires
per-user limits and an age/risk/deadline policy.

The lease-recovery policy deliberately distinguishes computation from side effects:

- an interrupted read is returned to `pending` only while its bounded retry budget
  remains;
- an exhausted read becomes a normal recoverable worker failure;
- an interrupted write is never retried blindly, because the Google API may have
  committed immediately before the worker died;
- that ambiguous write becomes `worker_reconciliation`, records a portal incident,
  sets side-effect integrity to unknown/unsafe, and blocks ordinary resume until the
  external resource has been reconciled.

This is a practical distributed-systems invariant: absence of a local acknowledgement
does not prove absence of a remote side effect.

## 14. Hash maps, sets, and effectively-once effects

`app/runs/repository.py` stores request idempotency keys. The planner binds an
approval to an action hash. Google mutation tools derive deterministic request
IDs and record returned artifact IDs.

Exactly-once computation is generally unavailable across arbitrary networks.
Effectively-once external effects are achieved through idempotency,
reconciliation, read-after-write verification, and durable evidence. Hash maps
provide expected `O(1)` lookup from logical action to artifact; sets deduplicate
senders, tool names, OAuth scopes, and content hashes.

Collision resistance is not authorization. Every lookup still includes the
tenant boundary, and a recovered artifact must match the intended recipient,
file, event, or Chat space.

## 15. Search structures: full text, vectors, and HNSW

`app/rag/retriever.py` combines PostgreSQL lexical ranking and pgvector neighbors
with reciprocal-rank fusion. An item at rank `r` contributes approximately
`1/(k+r)`, avoiding the false assumption that lexical and vector scores share a
numeric scale.

HNSW is a layered proximity graph. Sparse upper layers navigate quickly toward a
query neighborhood; the dense bottom layer refines candidates. It trades exact
nearest-neighbor guarantees for practical latency, with graph degree and search
breadth trading memory/build time for recall.

The complete retrieval sequence is:

```text
classify -> tenant/source/date filters -> lexical + vector retrieval
-> rank fusion -> deduplicate -> diversify -> optional rerank -> pack -> cite
```

Chunking is source-specific in `app/rag/chunking.py`: Gmail thread lineage, Docs
headings, Sheet row groups, Chat threads, PDF layout/table/OCR boundaries, Meet
speakers/topics, and structured records that need no ordinary splitting.
Chunk-size winners remain empirical; versioned chunks allow controlled replay.

## 16. Bandits, MDPs, and why this is not live RL

A contextual bandit observes context, selects one already validated action, and
receives a reward without modelling a long transition sequence. It fits bounded
choices such as prompt A/B, RAG gate, safe-read model, or retrieval policy.

An MDP adds transitions:

```text
(state, action) -> observation/reward -> next_state
```

The governed trajectory dataset stores that shape, but real Google writes are
not an exploration environment. Offline replay uses mock adapters, stable
holdouts, and separately reported completion, correctness, safety, latency, and
token outcomes. Most observed failures—timeouts, quota, missing dependencies,
bad retries, absent verification—are systems problems that fine-tuning cannot fix.

## 17. OKF v0.1 precisely, and this project's profile

Google introduced OKF v0.1 as a vendor-neutral format, not a hosted service or
Python library. A bundle is a directory of UTF-8 Markdown files with YAML
frontmatter. A concept's identity is its bundle-relative path without `.md`.
Only `type` is required; `title`, `description`, `resource`, `tags`, and an ISO
timestamp are recommended. `index.md` and `log.md` are reserved navigation and
history files, not concepts. Markdown links form directed untyped graph edges,
and consumers must tolerate broken links.

Official sources:

- [Google Cloud introduction](https://cloud.google.com/blog/products/data-analytics/how-the-open-knowledge-format-can-improve-data-sharing/)
- [Official OKF v0.1 specification](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md)
- [Reference implementation and samples](https://github.com/GoogleCloudPlatform/knowledge-catalog/tree/main/okf)

This repository layers a stricter operational profile on that minimal format.
Concepts additionally carry owner, version, visibility, publication status, and
approval evidence. The generic loader consumes a minimal v0.1 concept, but
production synchronization refuses to trust it until the governance fields and
human approval are valid. Those are project rules, not OKF requirements.

OKF does not replace RAG, Neon/pgvector, live Google APIs, MCP/tool protocols, or
prompts. It represents portable curated knowledge. RAG may retrieve it; databases
may index it; APIs provide current facts and actions; protocols expose tools.

## 18. Hands-on sequence

1. Run the planner golden set and draw one execution DAG.
2. Add a cyclic plan fixture and predict the validation error.
3. Compare greedy context packing with a small exact knapsack implementation.
4. Change one Gmail chunk and verify only its content hash/version needs reindexing.
5. Simulate a crash after verified Sheet creation and prove resume does not
   recreate the Sheet.
6. Add a minimal OKF concept containing only `type`; confirm it is consumable but
   not trusted as production policy.
7. Add approval metadata, synchronize, and inspect its section chunks.
8. Compare two retrieval policies with the replay suite on identical cases.
9. Use Grafana's run, step, token, retrieval, and incident panels to connect
   algorithmic choices to production outcomes.
10. After enough reviewed examples exist, evaluate a bounded bandit offline;
    never enable live exploratory RL for Google writes.

## 19. Classification and guarded dispatch

Sprint 27 adds a deterministic intent gateway before the action planner. The output
set is finite: Workspace action, Workspace guidance, product information, bounded
scope chat, ambiguous, or out of scope. This is a classification-and-dispatch
algorithm, not an invitation to global conversation.

A useful implementation model is a decision tree whose early branches protect
security boundaries:

```text
product identity/capability pattern? -> trusted registry answer
bounded greeting/clarification?       -> local scope answer
Workspace entity + guidance wording? -> approved registry/OKF guidance
Workspace entity + action verb?       -> typed action planner
Workspace entity only?               -> precise clarification
otherwise                            -> polite scope redirect
```

The fast path is linear in request length for the bounded pattern set. The important
invariant is not the asymptotic complexity; it is that a conversational classification
cannot silently acquire Google tools, tenant RAG, or model authority.

## 20. Failure fingerprints, clustering, and streaming evidence

A broad label such as `execution` is too lossy for learning. Sprint 27 hashes a
normalized tuple:

```text
(stage, category, component, service, operation, normalized_error_template)
```

This is analogous to choosing the key for a hash map. Too broad a key merges unrelated
bugs; raw error text creates excessive cardinality and privacy risk. Normalization
removes direct identifiers and variable numbers before hashing, while every occurrence
remains a separate durable incident.

The portal is a human-labelled stream processor: each incident receives two bounded
strategies; a reviewer selects A/B, acknowledges, or ignores it; selected incidents
join a cluster proposal; rejected or expired proposals do not suppress later evidence.
Those labels become evaluation data, but never self-approve a candidate or live policy.
