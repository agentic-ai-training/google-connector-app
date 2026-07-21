# Sprint 28–34 completion audit — 2026-07-22

This is the authoritative implementation audit for the governed improvement upgrade.
It replaces inference from progress prose with direct source, test, database, CI, and
runtime evidence. It does not convert data-gated experiments or an externally
rate-limited candidate into fabricated successes.

## Scope and decision semantics

The audited outcome is a human-governed improvement lifecycle:

```text
sanitized failure evidence
  -> exact occurrence and fingerprint cluster
  -> optional cross-cluster architectural theme
  -> two bounded strategies and a human selection
  -> Groq-only isolated implementation draft
  -> deterministic validation and trusted CI attestation
  -> human deployment approval
  -> isolated version-pinned candidate runtime
  -> human canary activation
  -> automatic measurement and safety rollback
  -> human promotion
  -> independently approved trusted OKF publication, when present
```

Selecting a strategy is not approving code. Approving a frozen candidate is not
deploying it. Deploying it is not activating traffic. A passing canary is not promotion.
Generated OKF remains untrusted until its independently frozen hash receives human
publication approval.

## Trusted OKF publication

OKF stores curated, provenance-bearing knowledge as Markdown plus validated frontmatter.
In this project it describes stable capabilities, workflow guidance, tool limitations,
approval rules, and recovery knowledge. It does not replace Neon, pgvector, live Google
APIs, executable code, OAuth scopes, or the tool registry.

Candidate code may propose an OKF overlay, but the overlay is staged as a draft with its
source proposal, owner, version, content hash, expiry, visibility, and status. Structural
links, governance fields, tool references, secrets/PII, injection boundaries, and replay
effects are checked before review. A human separately approves the exact OKF hash. New
runs can then pin that trusted bundle version; rollback stops new selection while old
runs retain provenance. Consequently, candidate learning can update reviewed knowledge
without allowing model text to grant itself tools, permissions, or production authority.

Authoritative implementation: `app/okf/candidates.py`, `app/okf/loader.py`,
`app/api/routes/admin.py`, `app/improvements/routing.py`, migration 013, the protected
portal, and the unit/integration OKF lifecycle tests.

## Requirement-by-requirement disposition

| Requirement | Authoritative implementation evidence | Verification evidence | Disposition |
|---|---|---|---|
| 28.1 metadata-only Gmail senders | `app/tools/registry.py`, `app/runs/planner.py`, `app/runs/worker.py` | Unit tests cover metadata projection, ordering, duplicate policy, and the Gmail -> Sheet dependency | Implemented |
| 28.2 universal result envelopes | `app/tools/result_projection.py`, `app/tools/result_store.py`, `app/agents/supervisor.py` | Projection/size/private-reference and tenant-access tests; migration 012/013 storage schema | Implemented |
| 28.3 context budgeting and recovery | `app/agents/supervisor.py`, `app/agents/errors.py` | Oversize fake-result, compaction, safe telemetry, and typed `model_context_length` tests | Implemented |
| 29.1 occurrences and exact clusters | `app/improvements/failure_intelligence.py`, migrations 010/012 | Fingerprint, re-open, per-occurrence, privacy, and pre-run failure tests | Implemented |
| 29.2 cross-cluster themes | `app/improvements/analyzer.py`, migration 012 | Evidence-threshold, unrelated-cluster separation, lifecycle-release, and two-option tests | Implemented |
| 29.3 portal/reporting clarity | `web/src/app/admin/improvements/page.tsx`, `app/api/routes/admin.py`, reporting views, metrics and alerts | Web build, API authorization tests, 24 reporting-schema views, dashboard validation | Implemented |
| 30.1 typed safe candidate input | `app/improvements/failure_intelligence.py`, `app/improvements/builder.py` | Sanitized-input, no-private-evidence, reproducibility, and rejection tests | Implemented |
| 30.2 adaptive Groq-only agent builder | `app/improvements/builder.py`, `app/improvements/builder_tools.py` | Single/multi-role, fallback, bounded-tool, review-envelope, validation, rollback, budget, and failure-code tests | Implemented |
| 30.3 isolated workspace and evidence | `.github/workflows/candidate-builder.yml`, `scripts/run_candidate_builder.py`, candidate schemas | Credential-minimal runner, resource/time bounds, safe-path/secret/PII validation, frozen hashes/diff/rollback tests | Implemented; real build currently quota-waiting |
| 30.4 trusted CI and PR handoff | candidate validation/publish workflows and `app/improvements/publisher.py` | Immutable commit/hash attestation and browser-forgery rejection tests | Implemented |
| 31.1 stable version assignment | `app/improvements/routing.py`, `app/runs/repository.py`, `app/runs/worker.py`, migration 012 | Sticky assignment and dual-worker disjoint-claim/recovery simulation | Implemented |
| 31.2 isolated candidate deployment | candidate deploy workflow, `Dockerfile.candidate`, Railway and Vercel deployment scripts | Source/digest/health/version attestation tests; PR #62 frontend preview isolation | Implemented; dormant until an approved candidate exists |
| 31.3 measurement and rollback | `app/improvements/analyzer.py`, `app/improvements/canary_simulator.py` | Minimum-sample, safety tripwire, queue reroute, cleanup, and rollback simulation | Implemented; no canary truthfully active |
| 32 trusted OKF lifecycle | `app/okf/candidates.py`, `app/okf/loader.py`, admin/routing code, migration 013 | Pure/mixed candidate, independent approval, pinning, validation, and rollback tests | Implemented |
| 33.1 context knapsack | `app/rag/context_packer.py`, supervisor feature gate | Exact-cost reconstruction, bounds, ACL/source-cap, timeout, and greedy-comparison tests | Implemented behind disabled evidence gate |
| 33.2 allocation experiments | `app/improvements/dp_allocation.py`, offline evaluation scripts | Workflow/quota fixtures pass without acquiring live authority | Implemented offline only, as specified |
| 34 observability/security/operations | metrics/collector, alerts, two dashboards, reporting views, runbooks, `docs/CANDIDATE_THREAT_MODEL.md` | Metrics scrape, Prometheus rule validation, dashboard validation, retention/auth/security tests | Implemented |

## Guardrail evidence

The final local and trusted-CI checks cover the promised surfaces rather than a narrow
smoke subset:

- Backend: 124 unit/database-independent tests passed; 25 integration tests passed
  against PostgreSQL when explicitly enabled.
- Planner and execution: 28/28 deterministic planner cases and 4/4 no-network Google
  workflow replays passed.
- RAG/policy/DP: source-aware chunk-policy validation passed without inventing a winner;
  offline policy evaluation correctly remained ineligible at 28/30 samples; context DP,
  workflow-allocation DP, quota-allocation DP, and dual-worker rollback fixtures passed.
- Python: compilation, Flake8, Bandit, and `pip-audit` passed; no known dependency
  vulnerability was reported.
- Web: lint, production build, and npm audit passed with zero known vulnerabilities.
- Flutter: dependency resolution, analyze, tests, and debug APK build passed.
- Database: in an isolated database with administrator-precreated `vector`/`pgcrypto`,
  migration 001 -> 013 -> 002 -> 013 passed. Production Neon and local Docker both report
  revision 013. The extension must be installed by a database owner before migrations;
  the application role correctly cannot elevate itself.
- Secrets and workflows: Actionlint, tracked-sensitive-filename, Git-history filename,
  secret-pattern, and clean-worktree gates passed.
- Docker Desktop: a clean rebuilt stack runs the API, separate control worker, PostgreSQL,
  Ollama, Prometheus, and Grafana. PostgreSQL is healthy on loopback port 55432 with
  pgvector 0.8.5 and 24 reporting views. Ollama returned one 768-dimensional
  `nomic-embed-text` vector. Prometheus reports API and worker targets `up`, validates 24
  alert rules, and Grafana provisions both repository dashboards.

Trusted GitHub evidence for production commit
`ca5fc5b39f2783ee377ba17239abdada3211e3a9`:

- CI: <https://github.com/agentic-ai-training/google-connector-app/actions/runs/29869216872>
- Deploy and joint attestation:
  <https://github.com/agentic-ai-training/google-connector-app/actions/runs/29869217061>
- Frontend candidate isolation: PR #62, merge `b4151815`
- Exact frontend/control attestation: PR #63, merge `ca5fc5b`

Production health at audit time:

- Railway API `/health`: status `ok`, role `control`, exact deployment commit `ca5fc5b...`.
- Vercel `/api/frontend-health`: status `ok`, role `control`, the same exact commit.
- Neon: migration 013, 24 protected reporting views, no active canary.

## Real candidate status and retry evidence

Build `7aa74d5b-cf30-4efc-9517-883d0999edd4` is the real high-risk multi-role candidate
for the selected verification strategy. The original reviewer-contract defect is fixed.
Manual retry run `29871180682` and scheduler retry run `29871442047` both reached the
isolated generation step, where Groq returned `RateLimitError` across the approved
builder-only model chain. The durable row is `queued`, accepted tokens are zero, and no
candidate files, commit, deployment, canary, or external side effects exist. Its sanitized
provider delay is honored by the automatic retry loop. This is an external capacity gate,
not evidence of a generated or validated candidate.

## Intentionally open evidence and human gates

The following are not missing engineering implementation:

- Source-specific chunk-size, overlap, parent-size, query transformation, reranker, and
  retrieval winners require tenant-safe labelled relevance judgments.
- Prompt, planner, routing, model, OKF, and DP promotion require the approved verified
  sample minimum, regression gates, and human canary decisions.
- Pilot expansion requires consenting real users and elapsed control/candidate evidence.
- Email/GitHub notifications require separately configured recipients/credentials and an
  explicit external-publication confirmation. Admin/Grafana internal ledgers are active.
- Grafana Cloud telemetry credentials are separate from dashboard-administration
  credentials. Local dashboard definitions and production telemetry paths are implemented;
  a live cloud-dashboard inventory cannot be re-read from this workstation without a
  currently available service-account token.
- The selected real candidate requires Groq quota/capacity before it can produce code;
  retries are durable and automatic.

None of these gates may be bypassed by synthetic data, automatic approval, unsafe model
fallback, or a database status edit.

## Audit conclusion

The engineering mechanisms and settled governance decisions in Sprints 28–34 are
implemented, tested, and deployed. Their safety gates are also functioning: no candidate
or empirical policy is falsely promoted without code, trusted evidence, sufficient data,
or the required human decision. Operational outcomes that inherently require future data,
real pilot traffic, explicit publication, or restored Groq capacity remain visibly gated.
