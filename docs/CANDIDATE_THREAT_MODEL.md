# Governed Candidate and Canary Threat Model

Status: implemented and reviewed against the Sprint 30–34 runtime on 2026-07-22.

## Trust boundaries

1. Production Workspace data, OAuth ciphertext, Neon owner credentials, Railway control
   credentials, Grafana credentials, and user prompts remain outside candidate generation.
2. The GitHub builder receives sanitized evidence, the public repository, a Groq key, and a
   route-scoped callback token. It can use only bounded repository operations implemented by
   the trusted base commit; it has no shell or arbitrary network tool.
3. Groq output is untrusted. It may stage candidate files in memory, but it cannot attest,
   publish, deploy, activate, promote, register tools, expand OAuth scopes, or update OKF.
4. Trusted CI and deployment workflows use distinct route-scoped tokens. Human decisions
   bind to the canonical candidate hash and remain separate for PR publication, isolated
   deployment, traffic activation, trusted OKF, and production promotion.
5. Candidate Railway and Vercel deployments are isolated from control. Runs and frontend
   users are assigned only after immutable version/deployment evidence is accepted.

## Threat/control matrix

| Threat | Preventive controls | Detection/evidence | Recovery |
|---|---|---|---|
| Prompt injection in sanitized evidence or repository text | No user bodies; bounded reads; retrieved text has no system authority; local tool allowlist | Candidate diff, staged-file manifest, independent review, prompt-injection and policy tests | Reject/discard in-memory files; request a new strategy |
| Malicious or hallucinated patch | Approved roots; path/symlink/size/file/call/round/token/time limits; syntax/policy validation; no execution during generation | SHA-256 manifest, exact diff, secret/PII scans, trusted multi-suite CI | Candidate cannot advance; delete draft branch/preview |
| Secret exfiltration | No production secrets in builder; exact DNS allowlist in process; secret-like paths/content rejected; callback stores sanitized codes only | Git history/secret scans; credential-minimal workflow definition; notification ledger | Rotate the isolated Groq/callback credential; candidate stays untrusted |
| Sandbox escape or dependency compromise | Ephemeral checkout; no generated-code execution while Groq key exists; resource limits; dependencies installed before model interaction | CI workflow review, dependency/security audits, bounded failure telemetry | Revoke builder secrets and disable candidate builder flag |
| Poisoned reproduction fixture | Synthetic/no-network adapters; fixture paths are reviewed candidate inputs, not authority; expected postconditions are deterministic | Golden/replay diffs and trusted test logs | Reject candidate or replace fixture; no Workspace mutation occurred |
| Approval replay or changed content after approval | Canonical digest covers base, files, diff, validation, rollback, kind/version; stage-specific approval rows; material changes alter hash | Approval identity/time/hash audit and transition constraints | Invalidate approval and return to review |
| CI/deployment forgery | Browser assertions cannot attest code; route-specific constant-time tokens; exact repository/workflow/commit/tree/file hashes/log digest | Trusted identity stored in validation/deployment evidence | Revoke attestation token; deployment cannot activate |
| Worker version race or duplicate execution | Persisted executor version; `FOR UPDATE SKIP LOCKED`; version-specific claims; leases/idempotency; sticky in-flight assignment | Run assignment, attempts, artifact and lease events | Stop new routing; return never-started work to control; reconcile writes |
| API candidate misrouting | Explicit applicability; stable cohort; signed version-bound proxy target; exact HTTPS health | Candidate URL/version/deployment attestation and run cohort fields | Disable routing; control remains available |
| Frontend preview substitution or open redirect | Trusted workflow creates a non-production deployment; Vercel project/ID/metadata/source checks; origin-only `*.vercel.app` validation; production URL rejected | `/api/frontend-health` exact role/version; stored frontend deployment ID and URL | Disable handoff first; remove preview; users return to control |
| Frontend token leakage | Control OAuth return; handoff token only in URL fragment; no query/referrer token; CORS requires bearer auth | Browser flow tests and no-store health/session responses | Disable routing/delete preview; revoke JWT signing key only if evidence requires it |
| Migration incompatibility | Expand-first migrations, downgrade/forward checks, control compatibility before activation | Trusted migration CI and production revision attestation | Forward repair or downgrade; keep control readers/writers online |
| Unsafe rollback or orphaned artifacts | Routing disabled before executor/preview cleanup; started writes stay pinned; compensations are explicit and approval-bound | Artifact integrity, cleanup ledger, canary rollback metrics | Preserve/reconcile uncertain writes; delete only safe approved artifacts |
| OKF authority escalation | Draft status, schema/tool/secret/PII/injection validation, separate trusted-publication decision; knowledge cannot add tools/scopes | Immutable bundle hash, publication state, per-run OKF version | Stop new selection and restore prior trusted bundle |
| Quota-driven unsafe downgrade | Candidate-only model allowlist; complexity/risk policy; bounded retry and durable queue; no fallback changes to user workflows | Model chain, token use, sanitized retry time, build checkpoint | Wait for provider reset; no partial candidate is promoted |

## Residual risks and fail-closed rules

- GitHub-hosted generation uses process-local destination enforcement rather than an
  independently administered network namespace. This is accepted only because generated
  code is never executed in that credentialed process and the model has no shell/network
  operation. A future self-hosted runner may add kernel-level egress filtering as defense in
  depth; its absence does not grant the current model network authority.
- A human-approved frontend candidate necessarily receives a selected pilot user's bearer
  token in its browser origin. Trusted CI, exact preview attestation, a very small cohort,
  short JWT lifetime, and immediate routing rollback bound that risk. Frontend candidates
  must not bypass the same human deployment and activation gates used for backend code.
- No live exploratory RL, tool creation, OAuth expansion, or real external write is permitted
  as an experiment. Learning proposes policies; deterministic guards and humans authorize
  authority changes.

## Required regression evidence

The completion gate must include unit and PostgreSQL-backed integration tests, no-network
workflow replay, golden planner cases, candidate policy/syntax/manifest tests, secret and PII
scans, migration round-trip, web lint/build, Flutter checks, Compose validation, trusted CI,
and exact production health/version evidence. A generated candidate additionally requires its
own frozen validation and, for frontend changes, Vercel project/deployment/health evidence.
