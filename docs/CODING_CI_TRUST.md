# CI trust, exact-attempt evidence and isolated reporting

Local checkpoint, 28 October 2026; schema **036**. No production policy is seeded,
no GitHub workflow is dispatched and no canary/promotion authority is granted.
That statement describes the original local checkpoint. The subsequent real PR
checks and their discovered failures are recorded below; neither run constitutes
production trust-policy enrollment or a candidate lifecycle proof.

## First real PR checks: false-green diagnosis and correction

PR #139 at `126f1cd477b60b1c7ef39850027c8ce86c4dd66a` produced main
[CI run 40715706533](https://github.com/agentic-ai-training/google-connector-app/actions/runs/40715706533)
and [validation run 40715706489](https://github.com/agentic-ai-training/google-connector-app/actions/runs/40715706489).
Web, mobile and Rust jobs passed in both. The main backend failed: 18 failed,
2,507 passed, seven skipped. Candidate validation incorrectly reported success
despite 18 failed, 2,462 passed and 52 skipped tests. Its brace group piped through
`tee` lacked `pipefail`: Bash exited the group at pytest, but successful `tee`
masked that exit. The later evaluations and Docker builds never ran.
**That green result is not valid release evidence.**

Backend/web/mobile validation gates now explicitly use Bash and `set -euo
pipefail`. Inert shell-execution tests inject a failure at every command position
and prove later commands never execute, the failure survives `tee`, and logs are
retained. Candidate backend enables the same integration suite as main CI.
Unraisable subprocess exceptions are fatal in both pytest invocations.

The 18 failures came from a test fixture using Linux's world-writable `/tmp` for
archives that correctly require protected ancestry. The fixture now uses an
isolated private directory beneath the operator's home, as existing custody tests
already do; the production guard is unchanged. A new negative test proves writable
ancestors remain rejected. Oversized diagnostic test IDs now have short stable
names, so verbose logs remain inspectable instead of embedding thousands of
parameter characters. A separate real Linux warning revealed pipe transports left
open after output-limit/cancellation cleanup; the owned-process lifecycle now
joins a public subprocess transport's closure as well as process exit. See
[execution fences](CODING_EXECUTION_FENCES.md).

Corrected native CI must pass at its own exact source SHA. These observations do
not authorize merge, deployment, production policy enrollment or canary traffic.

## What changed

The former hosted worker accepted any nonempty collection of successful,
`neutral` or `skipped` check runs. It did not prove that a reviewed workflow or
the required tests ran. The candidate callback also accepted a bearer-authorized
`passed: true` report without independent Actions read-back. A callback token
authenticates a reporting caller; it cannot establish the claimed test outcome.

```mermaid
flowchart TD
    H[Human reviews immutable workflow definition and required job/step names] --> P[Append expiring repository CI policy]
    D[Verified draft publication] --> W[Candidate checkout: no production/provider/callback secrets]
    W --> J[Required backend, web, mobile and Rust jobs]
    J --> R[Completed workflow triggers separate reporter]
    R --> T[Checkout trusted default branch; no candidate execution or artifact download]
    T --> M[Read bounded inert GitHub metadata/files and send a claimed result]
    M --> G[Server independently reads exact latest Actions run/attempt]
    P --> G
    G --> V[Match source, workflow digest, current PR, required jobs/steps and frozen files]
    V --> F[Run/source locks + unchanged-policy advisory fence]
    F --> C[Atomic CI evidence and completion]
    V -->|Missing/unsafe evidence| B[Blocked, never a false success]
    H --> X[Append explicit revocation]
    X --> F
```

## Trust contract

- `coding_ci_policies` is append-only. A human administrator registers the exact
  repository, workflow ID/path, trusted definition commit, SHA-256 of its bytes,
  and required **display job names → display step names**. Policies expire after
  at most 30 days; newest disabled/expired trust cannot fall back to an older row.
  Updates/deletes are rejected, and retained policies prevent blind downgrade.
- CI obtains a separate short-lived GitHub App installation token scoped to the
  selected repository and read-only `actions`, `contents`, `pull_requests` (plus
  metadata). Returned scopes are checked. No write-capable legacy token fallback
  is allowed in this verifier. The publication controller keeps its separate API.
- The verifier reads only fixed GitHub API URLs with bounded responses and an
  overall deadline. It selects the latest run for the configured workflow and
  exact candidate SHA, accepts only same-repository `pull_request` execution,
  validates the current PR head/base and workflow bytes at both candidate and
  current base, then reads the **specific run attempt's** jobs. A stale successful
  run cannot mask a newer pending/failing run or a rerun.
- Every required job and named step must exist exactly once, be completed and
  conclude **success**. `neutral`, `skipped`, missing steps, wrong heads, duplicate
  identities and incomplete inventories do not pass. Non-required check names
  cannot substitute for required gates. Complete inventories are capped at 100
  runs/jobs; unsupported larger inventories block rather than silently truncate.
- Candidate file SHA-256s are calculated from actual bounded GitHub file content,
  not from submitted hashes alone. Commit/tree identity and the final run/attempt
  are read back. Per-file content is capped at 1 MB and aggregate content at 8 MiB;
  source paths must be canonical. Supporting larger files requires a separately
  bounded Git-blob path, not bypassing verification.
- Candidate success and remediation callbacks both use observed Actions evidence.
  Failure-job identities come from required gates, not arbitrary submitted labels.
  A failure outside reviewed required gates does not automatically generate a code
  patch. Posted commands, diagnostics and metadata are reports; the independently
  observed run/attempt/job/step record is stored separately as `github_observed`.
- Network reads happen **outside** SQL completion locks. Inside the transaction,
  run/tenant/source/publication pointers, candidate files, proposal identity and
  current policy are checked again. Parent/file/proposal locks stop source races.
  A per-repository transaction advisory lock serializes policy publication/revoke
  against final acceptance; observing the old policy before taking this fence
  does not permit completion after a revocation wins.
- Hosted CI's timeout begins at the recorded PR publication artifact, not an old
  request timestamp. Transient reads can retry within that existing deadline;
  missing App permissions, policy or invalid evidence require action, not blind
  provider retries or model calls.

## Keyless validation versus privileged reporting

`candidate-validation.yml` checks out the exact PR head, disables persisted Git
credentials, installs the optional orchestration dependencies, sets only synthetic
test database/auth values, and validates backend/web/mobile/Rust. Its obsolete
`015 (head)` assertion is replaced by the existing disposable migration-chain proof.
It no longer contains callback secrets or privileged reporting jobs.

`candidate-ci-report.yml` uses a completed `workflow_run` and checks out
`${{ github.sha }}`: the trusted default-branch revision for that event, **not** the
candidate SHA. Its small isolated Python reporter imports no project modules,
executes no candidate scripts and downloads no candidate archives/log artifacts.
Candidate manifests and content are inert bounded data fetched from GitHub.
GitHub and callback credentials use separate clients; neither client follows
redirects. The reported `log_digest` is explicitly a digest of **job metadata**,
not a claim that raw logs were downloaded or inspected.

GitHub warns that privileged `workflow_run` chains must not execute untrusted
code or poisoned artifacts. See the [event security warning](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_run)
and [GitHub's workflow security analysis](https://github.blog/security/application-security/how-to-secure-your-github-actions-workflows-with-codeql/).
The API facts used here are documented in [workflow runs](https://docs.github.com/en/rest/actions/workflow-runs),
[specific-attempt jobs](https://docs.github.com/en/rest/actions/workflow-jobs#list-jobs-for-a-workflow-run-attempt)
and [workflow identity](https://docs.github.com/en/rest/actions/workflows#get-a-workflow).

## Explicit human activation, not performed by this checkpoint

1. Publish/review the new workflow and reporter through the ordinary PR/CI process.
   The reporter must exist on the trusted default branch before its trigger works.
2. Confirm the scoped GitHub App installation has the three required read
   permissions. Keep App keys and callback tokens in their existing secret stores.
3. Review the immutable workflow bytes and obtain its GitHub workflow ID, trusted
   commit and SHA-256. Review both the commands and their referenced dependencies;
   a matching digest alone cannot establish test quality or supply-chain safety.
4. An authenticated human administrator may `POST /admin/coding-ci-policies` with:

   ```json
   {
     "repository": "OWNER/REPOSITORY",
     "workflow_id": 123,
     "workflow_path": ".github/workflows/candidate-validation.yml",
     "trusted_commit": "<40 hex characters>",
     "workflow_sha256": "<64 hex characters>",
     "required_jobs": {
       "backend": ["Validate candidate with no production credentials"],
       "web": ["Validate candidate web"],
       "flutter": ["Validate candidate mobile"],
       "coding-runtime": ["Validate candidate Rust broker"]
     },
     "ttl_hours": 24,
     "confirmation": "APPROVE_CI_TRUST_POLICY"
   }
   ```

   These placeholders are explanatory, not valid API hash values. The route
   verifies workflow ID/path and exact trusted-commit bytes before enrollment.
   Callback/builder identities cannot approve trust. No model or test fixture
   supplies production approval.
5. To withdraw trust immediately, a human administrator can `POST
   /admin/coding-ci-policies/revoke` with repository, current `policy_id` and
   `confirmation: "REVOKE_CI_TRUST_POLICY"`. This appends disabled trust and
   preserves history. An old policy ID cannot revoke a newly reviewed replacement.
6. A run blocked specifically for CI evidence may request `POST
   /coding/runs/{id}/recheck-ci` with its existing `action_hash`. Tenant/repository/
   feature gates, current policy and retained returned publication evidence are
   required. This queues **CI read-back only**, never execution/publication,
   cancelled-run revival, owner transfer or approval renewal. The frontend action
   and policy-management UI are still pending; these are API operations.

## Evidence boundary and remaining work

Real disposable PostgreSQL tests cover policies, advisory-lock races, actual
worker completion, candidate callbacks, revoked/replaced sources and truthful
outcomes. GitHub transport, token minting and reporter inputs are synthetic.
Reporter tests exercise its actual main function, inert malicious-looking source,
credential separation, malformed triggers and static no-secret workflow boundaries.
No real CI attempt, signature, runner isolation or production policy was proved.

Final verification on this worktree: **2,487 coding/unit tests passed without
skips in 85.34 seconds**; thirteen existing SDK and multithreaded-fork warnings
remain. All 115 focused CI/reporting/publication/fence cases also passed at the
preceding focused checkpoint (overlapping, not additional unique coverage).
Migration 036 passed upgrade → downgrade 002 → upgrade and empty full-schema
retention. Targeted Flake8, medium-severity/medium-confidence Bandit and whitespace
checks passed; pre-existing archive `nosec` annotation warnings remain visible.
No model call, live CI dispatch, production migration, policy enrollment, account
change, VM restart or paid deployment was performed by this checkpoint.

The human-reviewed definition/gates must still be evaluated for test sufficiency.
Mutable action/dependency pinning, an independent base-owned regression harness,
OIDC/signed provenance, workflow supply-chain review, log diagnostics in a separate
credential-free analyzer and live GitHub failure/recovery proof remain unfinished.
Policy revocation blocks **new CI acceptance**; retroactive re-evaluation of already
validated candidates and consuming current trust again at deployment/promotion
gates are separate remaining integration work, not guaranteed by these APIs.
No observed CI result authorizes merge, canary traffic, promotion, paid hosting or
private data egress. Coding Temporal integration and the full fresh candidate →
isolated canary → measurement → promotion/rollback proof remain open.
