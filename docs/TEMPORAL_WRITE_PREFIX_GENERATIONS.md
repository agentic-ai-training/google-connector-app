# Temporal verified-prefix write generations

Local schema-034 checkpoint, 26–27 October 2026. This extends schema 033's actual
authenticated Resume and shared executor, rather than creating another agent
execution loop. Temporal admission/transport remain default-off in production.

## Executing flow

```mermaid
sequenceDiagram
    participant U as Authenticated user
    participant A as Resume API
    participant P as Private PostgreSQL ledger
    participant T as Temporal workflow
    participant W as Version-pinned activity worker
    U->>A: Resume exact failed step
    A->>P: Snapshot plan, receipts and source
    A->>A: Provider readback proves the successful ordered prefix
    A->>P: Lock/recheck source, authority and prior activity quiescence
    A->>P: Commit prefix grant + generation permit + pending step + event
    P-->>T: Existing transactional refresh hint
    T->>W: New reference-only generation/activity identity
    W->>P: Validate immutable permit; SQL independently admits new journal
    W->>P: Consume single-use grant under new execution nonce
    P-->>W: Exact saved prefix result; no repeated provider call
    W->>W: Invoke only never-attempted suffix with write-ahead receipt
    W->>P: Full readback + atomic step/artifact/event/journal completion
    T->>P: Finalize truthful business result
```

The existing ordered contracts currently cover Sheet create/populate and Chat
resolve/send. This is not a general permission to retry arbitrary writes or
replace a failed plan with another plan.

## Two independent classes of authority

Read retries still use schema 032's original SQL trigger/function and
`temporal_read_retry_permits`. The function is retained byte-for-byte as
`admit_temporal_read_generation_v032`, invoked only when no write-prefix permit is
present. A new, separate trigger validates write-prefix generations. SQL
constraints forbid using both permit classes or pretending a write continuation
is a read retry.

New write activities record `execution_policy=prefix-safe-v1`. Old writes labelled
`legacy-v1` or `read-safe-v1` cannot retroactively gain write-generation authority.
They still require the existing explicit full readback reconciliation or manual
handling. Policy labels alone never establish a verified prefix.

`temporal_prefix_generations` links the shared prefix grant to the exact prior
journal token, new execution token, first workflow execution, generation, actor
tenant, readback evidence hash and source snapshot. It contains no task/body/tool
result content. Generation/expiry/token/source fields are immutable. Hard erasure
removes private grants/results and clears parent/actor links when those parents
are deleted; the existing opaque journal audit may remain. There is no claim here
that unrelated retained audit tables have a complete pruning policy.

## Admission invariants

- The prior activity must positively record completion. A timeout or expired
  lease alone is not proof that its process or remote write stopped. Every prior
  activity for this step must be quiescent and have compatible write-safe
  provenance. The old uncertain journal row is never overwritten/reset.
- The current typed write plan, semantic authority, actor, approval, executor and
  dependency evidence must match. The observation is fresh at issuance; source
  changes during readback prevent issuance. Verified prefix artifacts are saved
  before freezing the final Temporal source hash.
- Only a complete, fresh, decryptable **proper prefix** can qualify. Any attempted
  suffix, unknown/dispatched write receipt, missing evidence or unaccounted
  historical write blocks continuation. The independent SQL guard also requires
  returned, unexpired private receipts with the expected count and live grant.
- At most three shared grants (including renewals) and three successor generations
  are allowed. Attempt counts increase monotonically and are not reset. Permits expire within 15 minutes of
  the original observation and bind a fresh execution token. Current approval,
  source, grant binding and expiry are rechecked at use time and per tool call.
- An expired, unused grant can be replaced only after another authenticated
  readback and all source/authority/quiescence checks. A still-valid matching grant
  is reused rather than spending another grant. A stale Temporal source snapshot
  also requires a new proof/permit, never an in-place edit. Old permit deliveries
  return `superseded`; expiry before admission does not fabricate an execution or
  increment/reset its attempt count. Renewal remains within the same lifetime cap.
- Activity IDs include generation and opaque permit ID. A stale delivery returns
  `superseded` without failing/completing the newer step. Initial activity IDs and
  reference-only workflow contracts retain their existing defaults.
- A resumed activity must consume the exact grant named by its journal, using
  its new execution nonce. The shared tool wrapper compares exact prefix
  arguments and enforces output-to-input resource lineage. Reuse is separately
  audited with `provider_called:false`; it is not a fake provider attempt.
- `temporal_prefix_superseded` excludes only the specifically authorized old
  journal token after its successor was admitted. New executing/uncertain work
  still blocks completion. Full business-step verification remains necessary.

## Unknown writes and post-response failures

The shared durable tool boundary now raises `WriteReconciliationRequired` for a
write exception after dispatch, including projection/logging errors after a
response was already persisted. It does not feed a misleading tool-error message
back to the model and invite a duplicate write. Saved response evidence can still
support later deterministic readback; a lost response remains unknown.

Temporal finalization also consults unresolved private write-receipt markers, not
only activity status. An unknown/dispatched write keeps side-effect integrity at
zero and prevents a false successful run, even if another layer recorded a failed
step. Cancellation is not proof that the remote write did not happen.

## Evidence and rollout limits

`tests/coding/test_temporal_prefix_retry.py` exercises the actual SDK activity
bridge, typed Chat executor, shared receipt/reuse boundary and full migrated SQL.
It covers successful continuation, old deliveries, bounded generations, source /
expiry / approval / receipt rejection, raw-SQL token/read substitution, immutable
metadata, erasure and downgrade refusal. Actual Sheet activity coverage also
checks reuse of the original Sheet and exact population from its completed Gmail
sender dependency; no separate model call copies resource IDs or rows.
Additional receipt tests cover lost
write responses and post-response logging failures without model-directed retry.

Two new real loopback server scenarios demonstrate:

1. Stop after a synthetic resolver effect; stop/restart the worker; use actual
   authenticated Resume and refresh transport; let an unused permit expire,
   prove no new attempt occurred, authorize its bounded replacement, reuse the
   resolver result and send exactly one synthetic message with requested content.
2. Perform one synthetic send then lose its response; block Resume, preserve
   uncertainty, cancel without repeating the send, and replay its history.

All seven local server cases passed, including the earlier five read/info/erase/
restart cases, and all seven histories replayed offline in a fresh process.
The final server run passed in 8.66 seconds; artifacts are ignored under
`.gca-local/temporal-proof/20261027T092756Z-93c980d2/`. It emitted transient queue-reader
`shard status unknown` errors during rapid development-server startup despite all
scenario/replay assertions passing. That is recorded evidence, not clean startup
logs or a production-availability claim. The observation helper now waits for
both workflow-query and business-ledger state to agree, avoiding a stale-query
race during renewal.

Final coding/unit regression: **2,345 passed without skips in 104.39 seconds**,
including 22 new Temporal-prefix and two new durable-write error cases. Thirteen
existing SDK/multithreaded-fork warnings remain. Full disposable upgrade-to-034 /
downgrade-to-002 / upgrade-to-034 and retention, targeted lint,
medium-confidence/medium-severity security and whitespace checks passed.
All 44 golden plans and 16 no-network workflow replay fixtures also passed.
These are real Temporal/SQL tests with **synthetic providers**, not real Google
delivery, abrupt process/server crash, HA, load, production activation, general
write retries, compensation or a complete coding workflow. Existing CI invokes
the same bounded isolated proof runner; no remote CI result is claimed.

Apply schema 034 before running the new activity worker, quiesce old workers and
use reviewed immutable executor versions. Downgrade refuses retained new-policy
write journals/generations or multiple historical grants for the same attempt,
rather than erasing audit records to make rollback
appear successful. Disabling future intake does not transfer existing runs to
PostgreSQL. No VM restart or new cloud/model spending was authorized or performed.

See [shared prefix recovery](VERIFIED_PREFIX_CONTINUATION.md),
[read-only generations](TEMPORAL_READ_RETRY_GENERATIONS.md),
[real server proof](LOCAL_TEMPORAL_SERVER_PROOF.md) and
[single-owner migration](WORKFLOW_OWNERSHIP.md).
