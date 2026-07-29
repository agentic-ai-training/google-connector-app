# Upgrade architecture

## Request lifecycle

1. A deterministic statement analyzer runs for every request. It extracts explicit
   services, delivery channels, recipients, temporal expressions, composition intent,
   and current-turn write authority before classification or planning.
2. Previous messages are not appended unconditionally. A relevance gate projects only
   the bounded session facts needed to resolve anaphora or omitted context; the current
   turn remains the authority for every executable service and external write. Prior
   output can supply referenced content (for example, “send the paragraph above”) but
   nouns inside that output cannot introduce Calendar, Meet, Chat, Gmail, or other
   steps.
3. A guarded router separates Workspace actions, Workspace-scoped product questions,
   and bounded composition (drafts, applications, essays, roadmaps, summaries, and
   pointers intended for a Workspace artifact). It does not provide open-domain chat.
4. `POST /runs` creates a tenant-scoped, idempotent run and a validated service DAG.
5. Materially missing time, timezone, duration, or Chat destination pauses in
   `awaiting_clarification`.
6. High-risk writes pause in `awaiting_approval`; approval is bound to the SHA-256
   hash of the exact plan and expires after 30 minutes.
7. A PostgreSQL worker claims work with `FOR UPDATE SKIP LOCKED`, renews its lease,
   executes dependency-ready steps, and writes append-only events.
8. Tool/model attempts, artifacts, verification, completion, and incident evidence
   are stored separately. Browser disconnection cannot cancel the worker.
9. SSE or polling replays durable events by run ID. Resume resets only failed steps;
   completed steps and verified artifacts remain intact.

The legacy `/chat` route remains available behind `LEGACY_CHAT_ENABLED` for rollback,
but it rejects high-risk mutations so those must use the approved durable path.

## Knowledge boundaries

- Live Google APIs are authoritative for current state and mutations.
- User Google content is untrusted tenant evidence in `rag_chunks`, scoped by user ID.
- The OKF Markdown bundle is trusted operational knowledge. It is loaded, validated,
  versioned, retrieved, and cited separately from user RAG.
- RAG is gated per request. Live/latest Google operations bypass it. When historical
  semantic evidence is needed, source-aware ingestion preserves Gmail thread metadata,
  document heading parents, Sheet row/header structure, Chat threads, Calendar/Meet
  records, ownership/ACL lineage, content hashes, and chunker/embedding versions.
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

## Write execution and recovery contracts

The planner's `allowed_tools` list is a security ceiling: an agent can never call outside
it. A `WriteContract` is a separate completion obligation: its required tools must be a
subset of that ceiling and must succeed in the declared order/mode. A write agent that
returns prose receives exactly one correction with only the missing required tools bound.
A second tool-free answer is `tool_selection`; an attempted failed or uncertain write is
`tool_failure` and is never automatically repeated.

Provider success is followed by operation-specific readback. Sheets compare normalized
values and ranges, Calendar compares time/timezone/attendees/Meet state, Chat compares
resource/destination/text hash/references, and Drive sharing compares the permission
type/role/principal. Mismatch is `postcondition_failure`. Durable evidence contains IDs,
counts, booleans, and hashes—not cell values, messages, titles, or recipients.

Chat sends use an ordered contract:

```text
resolve_chat_destination -> bind verified spaces/... name -> send_chat_message
```

The message text may come from a verified Sheet URL, a completed composition
dependency, or an explicitly referenced prior assistant result. Each source is bound
as typed lineage and sent exactly, without an additional model turn. Combined requests
therefore form `composition -> Chat`; referential requests may form a Chat-only run,
but both retain human approval and the same resolver/send verification contract.

An email destination first uses `spaces.findDirectMessage`. Only a provider-confirmed
404 stating that the direct message does not exist may invoke idempotent `spaces.setup`;
unrelated 404/403 responses are not reclassified. The resolver is itself read back with
`spaces.get`, recorded as an artifact, and the send tool accepts only the verified
`spaces/...` name. The discovery client represents `SetupSpaceRequest.requestId` inside
the `body` alongside `space` and `memberships`; it is not a method keyword argument.
This requires `chat.spaces.create`; users whose encrypted credential
predates that scope must reconnect once. A hostname in an error is never by itself
evidence that the Chat API is disabled.

Google requires a configured Chat app identity for write calls even when the API is
enabled and the request uses user authentication. A provider `Google Chat app not
found` response is classified as incomplete Chat API Configuration, not API
disablement or a missing recipient.

Fully specified Calendar creates are projected by the typed planner into title, start,
duration, timezone, attendees, and Meet intent. The worker normalizes the window and
executes the idempotent Calendar tool directly; model quota is not spent reconstructing
those already-approved arguments.

Once every required tool in an ordered write contract has succeeded, execution returns
directly to deterministic read-after-write verification. It does not spend another
model turn merely to paraphrase success. A post-tool model quota failure therefore
cannot relabel an already-created and subsequently verified Calendar, Chat, Sheet, or
other write as failed.

Resume is an explicit state machine:

```text
failed exact step -> reconciling
  -> safe_to_retry: requeue only that step
  -> already_completed: mark it complete and continue downstream
  -> manual_required: block external repetition and retain evidence
```

After an explicitly selected failed step resumes, the worker also rechecks any failed
sibling that already contains successful write evidence. A sibling is marked complete
only when deterministic provider readback still passes; the worker never retries that
sibling automatically. If any sibling remains failed, the run stays `partial` and can
never be falsely finalized as `completed`.

Completed dependencies and verified artifacts never reset. Reconciliation performs no
external call while holding a long database transaction.

Candidate generation uses the same durable-authority principle. Every accepted turn
checkpoints its role, next round, counters, content-free contract errors, staged-file
count, and separate base/effective/current-role budgets. Early, restricted, hard-file,
and finalization gates prevent investigation-only loops. The runner may suggest a retry,
but the server recomputes eligibility from the checkpoint, remaining round/token
authority, terminal policy codes, and absence of a commit/deployment. A valid reviewer
checkpoint resumes at its persisted round with frozen author files.

The active candidate policies are `adaptive-roles-v3-model-chain-evidence` and
`bounded-repo-tools-v9-runtime-adoption-gate`. The portal reports the configured primary
and every actually used Groq-hosted fallback. Finalization rejects files whose declared
create/replace/delete operation disagrees with the base tree and rejects new modules
that are not adopted by an existing runtime path.
## Durable hybrid execution

Every durable step passes through one execution boundary. The worker first checks
whether the selected operation has one registered tool/contract and whether the
persisted arguments validate against that tool's Pydantic schema. Complete arguments
use the typed deterministic adapter; specialized ordered workflows such as Chat
resolution/send and Sheet creation/population retain explicit output-to-input lineage.
Incomplete, ambiguous, or multi-tool preflight is handed to the existing bounded
service agent with only the step's allowlisted tools.

This is a pre-attempt fallback, not a blind retry mechanism. Once any tool call has
been attempted—especially an external write—a provider failure, uncertain result, or
verification failure cannot fall through to a model. It is persisted for deterministic
readback, reconciliation, or explicit resume. Both paths share immutable approval
hashes, idempotency, tool-result projection, verification, artifacts, incidents, and
durable run events. Steps persist `execution_path` and `fallback_reason`, and emit
`typed_execution_selected` or `guarded_agent_fallback_selected` without sensitive
arguments.

A failed step that reconciliation proves safe to retry is re-pinned to the currently
deployed immutable executor version before it is queued. This permits a production
repair to resume an older run. A write with uncertain acceptance is never re-pinned or
queued automatically.

## Active-session accounting

`GET /runs/{run_id}` derives total elapsed time from the durable queued/completed
timestamps and recorded step time from step duration evidence. It also groups the
append-only `agent_model_calls` ledger by actual model, returning calls plus
input/output token totals. The session UI uses these fields for live and terminal
progress; configured model names are never treated as proof that a model was called.

## Exact same-service lineage and private RAG readiness

One service may contribute more than one operation to a run. The planner expands only
an explicit current-turn read-then-write instruction into separate durable steps. For
example, copying the latest sent Gmail message is:

```text
search sent Gmail -> fetch exact source message -> encrypted result reference
                  -> send exact subject/body -> verify ID, recipient, subject, body
```

The write step depends on the read step. It does not ask a model to remember the body,
and it does not expose the body in ordinary step/event evidence. The complete provider
result is encrypted in expiring, tenant-scoped private storage; downstream code resolves
that opaque reference only for the same user and run. A missing or truncated source
blocks the send. The source message and newly sent message are distinct artifacts.

Current-turn lineage is also distinct from conversation context. “Fetch a message and
send the same message” resolves inside the current DAG and does not load an unrelated
prior assistant answer. “Send the paragraph above” may load a bounded exact prior
assistant result. Neither mechanism is knowledge RAG.

Knowledge RAG is opt-in per user and is never implied by OAuth consent. The authenticated
index action creates a durable `rag_source_sync_jobs` record and returns immediately.
A leased worker reads a bounded set of that user's Gmail, Drive, and Calendar resources,
enqueues source-aware embedding jobs, survives browser disconnects/restarts, and records
completion or sanitized failure. Duplicate active syncs are suppressed per tenant.
Timestamp strings from Google are normalized to timezone-aware datetimes before asyncpg
writes; only dead letters matching the historical timestamp defect are automatically
requeued.

The UI reports three independent facts:

```text
Conversation context: used / not used
Knowledge RAG: not requested / hybrid with returned-and-used evidence
Private index: sources, chunker versions, pending/dead-letter jobs, latest sync
```

`RAG: none` is therefore correct for live reads and writes. A semantic/historical request
may report hybrid retrieval only when a tenant-owned indexed corpus exists and a durable
retrieval event records what was returned and used. Another user's legacy chunks are
never evidence that RAG is ready for the current user.

Dense query embedding has its own latency bound. If the production Ollama service
cannot return a query vector within that budget, PostgreSQL full-text retrieval still
runs instead of being cancelled with the dense channel. The durable event and run
diagnostics distinguish requested mode (`hybrid`) from effective mode (`keyword`),
and record dense availability/error type, embedding duration, and dense/lexical
candidate counts. This may reduce recall during an Ollama slowdown, but it cannot
masquerade as successful hybrid retrieval or discard usable lexical evidence.
For semantic request wrappers with an explicit `about`, `concerning`, or `regarding`
topic, the keyword channel searches that topic rather than requiring every instruction
word to occur in the indexed content. The transformation strategy and token count are
recorded without copying the query into high-cardinality telemetry.
