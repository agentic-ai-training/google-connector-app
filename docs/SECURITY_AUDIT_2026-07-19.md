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
