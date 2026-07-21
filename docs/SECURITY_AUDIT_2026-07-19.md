# Security and dependency audit — 2026-07-19

## Outcome

- `pip-audit -r requirements.txt`: no known vulnerabilities.
- `npm audit --audit-level=high`: zero vulnerabilities.
- Bandit: no high-severity findings; medium findings were removed or constrained.
- Tracked-file and full-history sensitive-filename scans: no matches.
- Repository secret-pattern scan: no matches.
- OAuth credentials remain encrypted and user-scoped in PostgreSQL.

## Remediation performed

The audit initially identified three packages without patched releases in the
installed dependency set:

- `diskcache` unsafe pickle deserialization;
- `ecdsa` timing leakage in private-key signing/ECDH operations;
- `ragas` SSRF in its multimodal collection utilities.

The application no longer installs any of these packages. JWT signing and
verification use `PyJWT` with the existing configured algorithm. The obsolete
local `token.pkl` deserialization fallback was removed; production and pilot
requests use encrypted per-user OAuth credentials, with an optional JSON-only
local fallback.

The weekly evaluation keeps the stable script/workflow interface but now uses a
constrained, no-tools Groq judge to calculate bounded faithfulness,
answer-relevancy, and context-recall metrics. Retrieved text is explicitly
delimited as untrusted evidence. Negative examples use their human-corrected
`expected_result`, never the known-bad answer as ground truth.

Generated Flutter and Next.js output is excluded from Docker build contexts.
This prevents unreviewed generated binaries and multi-gigabyte local build
artifacts from entering the Python production image.

## Accepted low-severity static-analysis findings

- A non-cryptographic random draw assigns prompt experiment arms; it is not used
  for credentials, authorization, or identifiers.
- Optional RAG and Drive ingestion errors are isolated so a background indexing
  failure cannot turn a successful live Google operation into a false failure.
- Bandit's token-name heuristics flag normal OAuth response fields such as
  `token_type: bearer`; these values are protocol constants, not passwords.

## Operational follow-up

Run `pip-audit`, Bandit medium/high scanning, npm audit, and secret/history scans
in release guardrails. New vulnerable evaluation dependencies must not be added
to the API/worker image merely to preserve a particular benchmark library.

## Governed candidate threat model — 2026-07-21

- Prompt injection in failure evidence or repository text cannot invoke a shell or arbitrary
  network tool. The builder exposes bounded list/search/read/in-memory-stage/diff operations.
- Generated patches are never executed in the generation process. They remain untrusted until
  secret/PII/path checks and trusted CI finish on the immutable commit.
- Symlink/path traversal and credential-like paths are rejected; calls, files, bytes, output,
  tokens, rounds, and elapsed time have hard limits.
- The GitHub builder job receives only Groq and one narrowly scoped callback credential—not
  Google OAuth, production PostgreSQL, Railway, Grafana, or user Workspace content.
- Browser-supplied success cannot attest CI or deployment. Tokens are route-specific, hashes
  bind approvals to content, and material changes invalidate review.
- Candidate/control workers claim mutually exclusive executor versions. Candidate routing is
  applicability- and cohort-bounded, and never-started queued work is reassigned on rollback.
- OKF is a non-executable knowledge layer. It has an independent human publication decision
  and cannot register tools, add scopes, or grant permissions.
- Private full tool results are encrypted, tenant-scoped, size/retention bounded, excluded from
  account exports, and removed with run/user retention.

Remaining defense-in-depth: GitHub-hosted generation is an ephemeral credential-minimal
checkout, but not a network-namespace sandbox with destination-level egress enforcement.
Frontend candidates use an immutable, non-production Vercel preview and an authenticated,
stable-cohort handoff. The bearer token is placed only in the URL fragment, never a query
parameter or request URL; rollback stops routing before preview deletion. Planner/API
candidates use the separately attested Railway candidate API and stable control-side proxy
routing; neither surface replaces the control deployment during canary preparation.
