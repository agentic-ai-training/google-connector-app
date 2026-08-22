---
type: workflow
title: Durable least-privilege coding agent
description: Governs repository planning, sandbox mutation, validation, publication, and recovery.
owner: project-admin
version: 1
visibility: public
publication_status: approved
approved_by: project-admin
approved_at: 2026-08-22T00:00:00Z
tags: [coding, candidate, builder, source, tests, ci, rollback]
timestamp: 2026-08-22T00:00:00Z
---
# Authority boundaries

The planning model may choose only registered typed investigation and transformation
operations. It never receives a shell, database credential, deployment credential,
production Google OAuth token, merge authority, deployment authority, or the ability to
claim validation passed. Source egress requires explicit consent.

# Source-grounded sequence

1. Inventory the bounded project and localize real symbols and nearby tests.
2. Read only the source required to establish the existing invariant.
3. Prefer the smallest hash-guarded edit; create a file only when no adopted file fits.
4. Preview every mutation in an ephemeral workspace.
5. Require a fixed validation profile after the final mutation.
6. Bind human approval to the exact frozen plan hash.
7. Recreate a fresh keyless sandbox, re-run validation, and publish only a draft PR.
8. Accept CI truth only from the trusted no-secret workflow identity.

# Recovery and release

Leases, idempotency keys, append-only events, encrypted checkpoints, preimage hashes, and
artifact lineage make interruption resumable. A partially published branch or PR is a
reconciliation incident, not permission to repeat publication. Merge, production-connected
deployment, real-user canary traffic, trusted OKF publication, and promotion remain separate
human-governed actions.
