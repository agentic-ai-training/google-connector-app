# Pilot and learning evidence gate — 2026-07-20

This document separates implemented capability from conclusions that require real,
consented production evidence. The system must not invent pilot success, retrieval
quality, or a learning-policy winner when these gates are not satisfied.

## Current production evidence

The production Neon audit on 2026-07-20 found:

| Evidence | Current value | Consequence |
| --- | ---: | --- |
| Distinct durable-run users | 1 | No multi-user pilot conclusion is possible. |
| Durable runs | 1 failed | Reliability and latency comparisons are not representative. |
| Feedback | 6 positive, 5 negative | Useful incident evidence, but not enough controlled trajectories. |
| Scored prompt metrics | 13 runtime samples | These are operational observations, not RAG relevance judgments. |
| Valid `rag_evaluation` samples | 0 | Chunker/retriever regression and promotion remain blocked. |
| Workflow evaluations | 1 | No workflow-policy winner can be selected. |
| Policy evaluation reports | 0 | No candidate policy has passed the governed promotion gate. |
| Improvement proposals | 0 | There is currently nothing awaiting approval or publication. |

## Implemented gates

### Deployment canary

Each control and candidate version needs at least five measured runs. A candidate is
rolled back automatically if it regresses failure rate, side-effect integrity,
cancellation rate, p95 latency, or average tokens. Passing this small canary proves only
that the candidate may proceed to the next controlled stage; it is not sufficient for a
learning or fine-tuning conclusion.

### Offline policy comparison

Promotion requires at least 30 verified examples and no regression in any independently
tracked objective: task success, plan correctness, tool correctness, artifact
correctness, recovery, side-effect integrity, satisfaction, retrieval quality, latency,
or tokens. Latency and token increases count as regressions; decreases in the other
objectives count as regressions.

### RAG and chunking

RAG regression evaluation begins only after ten valid `rag_evaluation` examples. Runtime
metrics from requests that did not use RAG cannot satisfy this gate. Each example must
have a provenance-bearing judgment such as retrieval relevance, context precision,
context recall, faithfulness, or citation correctness. A sample-deficit alert reports
when this evidence has not accumulated; it does not substitute synthetic success.

### Pilot rollout

The approved sequence is internal verification, then approximately 5–10, 20–30,
40–50, and 80–90 consenting users. The feature flag stores rollout checkpoints as
10/30/50/90 percent and also supports explicit allow/deny lists. At every checkpoint,
review task/artifact correctness, side effects, OAuth health, latency, quota, failure
categories, orphaned artifacts, and privacy before increasing exposure.

## What happens automatically

- Persist sanitized run, step, event, artifact, token, latency, failure, and retrieval
  facts.
- Detect recurring failure categories and draft a sanitized, hash-bound improvement
  proposal when its configured threshold is reached.
- Run deterministic replay, regression, security, and multi-objective evaluation gates.
- Start only a previously human-approved low-risk canary.
- Stop and roll back a candidate automatically when a guardrail regresses.
- Notify through the protected admin view and monitoring ledger.
- Keep `live_rl` disabled and locked. Production mutations are never exploration.

## What always requires a human

- Approving the exact frozen proposal hash for a canary.
- Approving final production publication after the canary passes.
- Publishing a draft GitHub change or sending an external notification; this also needs
  the relevant repository-scoped credential or mail configuration.
- Publishing or trusting a changed OKF concept. Automated analysis may create only a
  draft; it cannot update trusted operational knowledge.
- High-risk Google writes unless the user explicitly waived that confirmation for the
  stated action and scope.
- Enabling later fine-tuning/offline RL after enough governed evidence exists.

The browser Admin Improvement Center is the approval surface. It displays the proposal,
evidence, exact diff, risk, evaluation results, and immutable candidate hash. Approval
moves the proposal through:

`awaiting_review -> approved_for_canary -> canary_active -> awaiting_promotion -> approved_for_publication`

Any material edit changes the hash and invalidates prior approval. A failed guardrail
moves the proposal to `rolled_back`.

## How the remaining evidence is obtained

1. Keep `live_rl` locked and retain the legacy route as rollback protection.
2. Add consenting Google test users and enable `pilot_cohorts` first through an explicit
   allowlist, not a broad percentage.
3. Exercise the golden mix: Gmail/Drive reads, Sheet creation, Calendar/Meet, Chat,
   ambiguous requests, quota failures, reconnects, retries, and partial side effects.
4. Ask users for step-specific feedback and consent before a trajectory enters the
   learning dataset.
5. Label RAG cases with relevance, context, faithfulness, and citation judgments; keep
   non-RAG runtime samples separate.
6. Compare control and candidate on matched tasks. Advance only when sample and
   multi-objective gates pass and a human approves the next stage.
7. Preserve the approved retention rules: raw payload telemetry 14 days, structured
   workflow metadata 90 days, sanitized aggregates/audit facts 12 months, and consented
   evaluation examples until removed or superseded.

Until these real observations exist, the correct system outcome is **data-gated**, not
failed and not complete. The implementation needed to collect and evaluate the evidence
is deployed; the conclusions themselves require real users and elapsed operation.
