# Transactional Temporal startup delivery

Local checkpoint, 24 October 2026. Migration **027** adds start intents to the
actual schema-026 Workspace/coding business inserts. Transport is independently
**off by default**. PostgreSQL-owned inserts create no intent. Temporal admission
also remains off, and normal intake remains PostgreSQL. No Temporal server, worker,
workflow, production migration or deployment has been started by this checkpoint.

```mermaid
sequenceDiagram
    participant DB as Business database
    participant C as Trusted start controller
    participant T as Temporal service
    DB->>DB: Commit Temporal-owned run and reference-only intent together
    C->>DB: Claim with nonce, lease and write-ahead uncertainty
    C->>T: Describe immutable workflow ID
    alt Existing matching execution
        T-->>C: Bound memo, queue, type and first run ID
    else Missing and current start authority
        C->>T: Start same ID, reject duplicate and conflicting execution
        C->>T: Describe and verify receipt
    else Missing but paused, deleted, cancelled or expired
        C->>DB: Block; no new start and no PostgreSQL fallback
    end
    C->>DB: Nonce-fenced acknowledgement
    Note over C,T: Lost response reuses and reconciles the same identity
```

## Durable database side

The run and start intent commit or roll back together. The intent stores only kind,
run UUID, namespace, queue, versions, exact bounded schema bytes and their SHA-256.
It contains no request, conversation, email address, repository source, credentials
or tool result. References are still linkable metadata and require private access.

Admission serializes a 2,000-unfinished-intent ceiling; exhaustion rolls back the
business insert rather than silently dropping dispatch. One 90-second fenced lease
owns a start attempt. Database claim acquisition/lock/statement waits are bounded.
Claim records `may_have_started` **before** a possible RPC. That bit cannot be
cleared, attempt count cannot decrease, and identity/payload/deadline cannot change.
Ten attempts exhaust automatic recovery and leave a blocked reconciliation record.
Blocked records are not deleted or treated as successfully completed.

Before allowing a new start, the controller rereads the current business parent,
checks owner, queued state, deletion and all version/binding fields, and checks
transport/deadline. Current cancellation, missing parent, paused transport or expiry
allows describe-only reconciliation, not a fresh start. Disabling transport cannot
recall an RPC already admitted/in flight; that identity must be reconciled.

Parent FKs become null on hard deletion, preserving reference-only uncertainty
without preventing private user-data erasure. They cannot be rebound to a different
parent. No run body is reconstructed from that record, and missing-parent work
cannot gain new execution authority. External workflow cancellation and history
retention/erasure still require the future operational reconciliation controller.

## Actual optional SDK adapter

`requirements-orchestration.txt` pins `temporalio==1.34.0` with its small required
dependencies. It does not add Google/OpenAI/agent-framework SDK extras or switch
inference providers. The local SDK imports and public signatures were checked;
CI installs the same optional adapter set for contract tests. Core requirements and
the core Docker runtime are not silently replaced with an orchestration service.

`TemporalStartTransport` requires a caller-supplied, explicitly configured client
whose namespace matches the intent. Fixed workflow names are `gca.workspace.v1`
and `gca.coding.v1`, not model-selected code. It describes first, and only starts
when missing and currently admitted. Requests use `REJECT_DUPLICATE` reuse policy,
`FAIL` conflict policy, bounded RPC timeouts, a 24-hour execution timeout, no workflow
retry policy, no cron, no automatic signal and no terminate-existing behavior.

After either a positive start or an already-started conflict, it describes again
and checks namespace, exact ID, workflow type, queue, contract/digest memo and the
service's first execution run ID. A matching ID alone is insufficient. Changed
memo/binding fails closed. Transport errors retain uncertainty with bounded delay;
cancellation leaves the write-ahead claim for lease-based recovery. Diagnostics
persist only fixed categories, never raw provider errors or connection credentials.
Acknowledgement requires the current unexpired nonce; it cannot turn a stale RPC
response into current success. `dispatch_one` returning true means an **attempt**
was processed, not that the request or its business tools completed.

## Evidence and activation gates

Thirteen local tests exercise real loopback PostgreSQL transactions, capacity-bound
payload rollback, shared UUIDs across workflow kinds, disjoint claims, stale ACKs,
lost start responses, cancellation, parent erasure, immutable uncertainty, bounded
attempts and downgrade refusal. SDK service doubles use actual installed enums,
exception types and public adapter arguments. They are **not real service starts**.
The fresh disposable schema upgrades to 027, downgrades to 002, upgrades to 027 and
passes empty-data retention checks. Transport policy changes are audited in the
existing ownership audit and survive schema rollback.

Final local coding regression: **1,906 tests passed without skips** in 52.49 seconds
(one existing Google SDK deprecation warning). Targeted lint, medium/high security
and whitespace checks passed. This includes the optional SDK contract tests but
does not convert their service doubles into live Temporal evidence.

Before activation, still required:

- Deterministic business workflows and typed activities reusing existing tools,
  postconditions, approvals, lineage and private ledger—not a toy parallel executor.
- Version-pinned compatible workers and task queues, approved server placement,
  TLS/namespace credentials, provider privacy checks and measured capacity/cost.
- Real server start/lost-ACK/duplicate/history proof, replay, crash recovery, signals,
  cancellation and uncertain-write reconciliation. SDK sandboxing is not execution
  security isolation.
- An operator service/CLI with governed transport activation, receipt visibility,
  history/privacy retention and explicit reconciliation. No background dispatcher
  or authority-granting portal button was added here.

Downgrade refuses enabled transport or **any** retained intent, including started
and blocked records. It does not automatically delete evidence, cancel workflows,
switch owners or prove external drain. The broader [single-owner rollout contract](WORKFLOW_OWNERSHIP.md)
and all unchecked Sprint 62 obligations remain in force.

The next schema-028 source checkpoint adds the first actual Workspace business
workflow/activities through the [existing executor bridge](WORKSPACE_TEMPORAL_EXECUTION.md).
This supersedes only the historical absence of that Workspace code—not the live
server, replay, coding-workflow, signal delivery, reconciliation or activation gates.

Sources: [official Python client](https://docs.temporal.io/develop/python/client/temporal-client),
[retry/error handling](https://docs.temporal.io/develop/python/best-practices/error-handling),
[official SDK distribution](https://pypi.org/project/temporalio/).
