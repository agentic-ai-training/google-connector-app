# Coding-agent execution architecture

## Scope and non-negotiable boundary

The coding agent has two consumers: authenticated end users asking for repository work,
and the governed improvement system producing a candidate for a diagnosed failure. Both
use one deterministic Rust tool broker. Neither an LLM nor a browser receives a shell,
production OAuth credentials, database owner credentials, deployment credentials, or a
way to assert that validation passed.

The isolated coding/candidate process reads `CODING_GROQ_API_KEY`; ordinary application
services continue their provider-migration phase and must not inherit that credential.
The Rust subprocess receives neither the coding key nor the legacy application key.

Repository ingress has two first-class modes. Hosted repositories use a least-privilege
GitHub App and short-lived installation tokens. Private folders that are not hosted—or
are not Git repositories at all—use `gca-local` on the owner's machine. Both modes feed
the same typed Rust broker and evidence format; local mode is not a bypass around policy.

“AI for tool calling, not code generation” means the model may select a typed operation
and bounded arguments. Arbitrary source bodies, shell programs, SQL mutations and CI
attestations are not model outputs. Novel edits therefore require a trusted deterministic
transformation recipe or a separately approved code-generation policy; pretending that
an unrestricted coding agent can author arbitrary software without producing code would
be contradictory.

## Durable flow

```mermaid
flowchart LR
    U[User coding request] --> C[Context and intent]
    F[Failure intelligence] --> E[Sanitized evidence compiler]
    C --> P[Typed plan]
    E --> P
    P --> A[Risk and approval policy]
    A --> Q[(PostgreSQL durable queue)]
    Q --> W[Version-pinned coding worker]
    W --> R[Rust tool broker]
    R --> S[Ephemeral non-root workspace]
    S --> V[Deterministic validation profiles]
    V --> CI[Trusted no-secret CI]
    CI --> H[Human deployment or canary gate]
```

The durable run records every tool name, policy version, input hash, duration, bounded
result facts, exit status and artifact hash. Raw secrets and unrestricted source dumps do
not enter telemetry or model history. A worker restart resumes from the last verified
checkpoint and never repeats a write without reconciliation.

## Rust broker v0.2

The first broker release provides:

- bounded repository inventory, literal search, line reads and SHA-256 hashing;
- structured Git status and diff inspection;
- fixed validation profiles for Python, web, Flutter, Rust and `git diff --check`;
- canonical root enforcement, parent-traversal rejection and symlink containment;
- credential-path and generated-directory exclusion;
- a one-megabyte JSON request ceiling, bounded file/search/output sizes and timeouts;
- direct process execution with an allowlist, cleared environment and no shell.
- exact single-occurrence text replacement guarded by the complete source-file SHA-256,
  available only to a mutable broker created inside an ephemeral workspace.

It intentionally does not provide arbitrary writes, package installation, raw SQL,
network access, deployment or process termination. Those require separate typed brokers,
approval classes, idempotency contracts and tests. This is a security property, not a
missing hidden terminal. The normal JSON broker executable remains read-only; only its
trusted local orchestrator can construct the ephemeral mutable variant.

## Local private/non-Git runner

Install the CLI from a trusted checkout:

```bash
cargo install --locked --path coding_runtime --bin gca-local
gca-local doctor --workspace /absolute/path/to/private-project
```

The execution input is a typed JSON plan, not shell text. A mutation plan must end with
a fixed validation profile after its final patch:

```json
{
  "actions": [
    {"tool": "read_lines", "path": "app/main.py", "start_line": 1, "end_line": 80},
    {
      "tool": "apply_exact_patch",
      "path": "app/main.py",
      "expected_sha256": "<64 hexadecimal characters>",
      "old": "exact old text",
      "replacement": "exact new text"
    },
    {"tool": "run_validation", "profile": "python_unit", "timeout_seconds": 300}
  ]
}
```

Run it once without approval:

```bash
gca-local execute-plan --workspace /path/to/project --plan plan.json
```

The runner copies allowed regular files into a temporary directory, excluding credentials,
`.git`, dependencies and generated output. It executes and validates there, prints changed
hashes and an approval token, and proves `original_workspace_modified: false`. To apply,
rerun the exact plan with `--approve <plan-sha256>`. The runner reconstructs a fresh
sandbox, repeats validation, rechecks every original preimage hash and atomically replaces
only approved files. If a later replacement fails, it restores already-replaced files from
their retained preimages and reports any rollback failure for manual reconciliation. This
works identically when `.git` does not exist.

The current CLI is the deterministic local execution boundary. The natural-language
planner remains a separately versioned component: it may request these typed actions but
cannot call a shell, read excluded files, mint approval tokens or claim validation passed.

## Hosted GitHub App boundary

`GITHUB_CODING_APP_ID`, `GITHUB_CODING_APP_INSTALLATION_ID`, and
`GITHUB_CODING_APP_PRIVATE_KEY` configure hosted repository access. The server signs a
GitHub App JWT, asks GitHub for a short-lived token restricted to the configured repository,
and uses that token for candidate Actions, draft PRs and governed promotion. Partial App
configuration fails closed. `GITHUB_PROPOSAL_TOKEN` is retained only for migration and
should be removed after the App path is verified.

## Planned deterministic tool families

| Family | Safe operations | Additional gate for mutation |
|---|---|---|
| Repository | inventory, symbols, references, bounded reads, hashes | transformation recipe, expected hash, reversible patch |
| Validation | compile, lint, unit/integration tests, diff inspection | none; read-only execution profile |
| Processes | list owned processes, health, bounded logs | approved service identity; no arbitrary PID kill |
| Database | schema/plan/read-only query, sanitized dump metadata | dedicated role, migration allowlist, backup and approval |
| DevOps | manifest render, image/SBOM scan, deployment status | signed artifact and human production gate |
| Conversion | AST/IR parse and language feature inventory | verified converter recipe plus differential tests |
| Algorithms | complexity inventory, invariant and data-flow checks | benchmark/evaluation evidence before replacement |

## Candidate-builder integration

The improvement system must admit only a concrete, automation-eligible strategy with
specific occurrence or cross-cluster evidence. A deterministic evidence compiler derives
service, operation and write risk from the registered runtime tools, localizes real source
and nearby tests, and produces the initial broker calls. Finalization is impossible before
real implementation and test files were read, a non-empty integrated patch exists, syntax
passes and the trusted CI identity supplies validation evidence.

Old terminal/fileless builds remain immutable and are labelled superseded. They are never
resumed as if a safe checkpoint existed.

Candidate policy v19 uses the broker for generic repository inventory, literal search and
bounded source reads whenever the packaged binary exists. Python keeps AST/symbol analysis
and in-memory candidate staging until equivalent typed Rust transformations exist. A
broker denial is terminal for that tool call; it cannot trigger an invisible Python
bypass. Source-only unit environments may use the deterministic Python reader explicitly.

## Token discipline and planning principles

The system applies a compact evidence-first sequence:

1. Verify that a change is needed.
2. Reuse an existing runtime path, registry or recipe.
3. Localize symbols and tests deterministically.
4. Read only the bounded neighborhood required for the invariant.
5. Select the smallest reversible transformation.
6. Validate locally, inspect the diff, then use trusted CI.

Review roles do not run before a compilable candidate exists. A model never repeatedly
searches once deterministic localization has produced valid paths. This follows the
project's Karpathy-style rules (think, simplify, make surgical changes, verify) and the
Ponytail ladder (question existence, reuse, standard/platform/library mechanisms, then
minimum custom code), without deleting required security or correctness controls.

## Dynamic cache policy

Cache behavior is selected by data semantics, not a global TTL:

- immutable commit/file hashes: content-addressed and long lived;
- repository inventory: invalidated by worktree/commit identity;
- source excerpts: keyed by path plus file hash;
- process health/log tails: seconds and never reused as proof of current health;
- database schema: keyed by migration revision;
- validation evidence: immutable for the exact tree, toolchain and command profile;
- OAuth, secrets and raw private content: never cached in the broker or model history.

Every cache entry records producer version, source version, ACL/tenant, creation time,
expiry/invalidation rule and content hash. A cache hit cannot satisfy a live-write
postcondition or production-health claim.

## Scaling decisions

The existing PostgreSQL lease queue is sufficient until measurements demonstrate a need
for another orchestrator. Kafka or Temporal must not be introduced merely for prestige:
they become candidates only when measured throughput, event fan-out, very long workflow
history or cross-service scheduling exceeds the current durable worker design. The same
rule applies to additional vector databases and caching products.
