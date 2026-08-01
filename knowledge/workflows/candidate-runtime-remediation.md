---
type: workflow
title: Runtime-integrated candidate remediation
description: Builds small source-grounded candidates without executing generated code beside provider credentials.
owner: project-admin
version: 1
visibility: public
publication_status: approved
approved_by: project-admin
approved_at: 2026-08-01T00:00:00Z
tags: [candidate, builder, source, tests, ci, canary, rollback]
timestamp: 2026-08-01T00:00:00Z
---
# Build plane

1. Consume only sanitized incident facts and a selected strategy.
2. Localize the existing runtime boundary and neighboring tests.
3. Read the exact implementation before staging application code.
4. Patch an adopted runtime path and add a failing-before/passing-after behavioral test.
5. Reject placeholder bodies, assertion-free tests, disconnected modules, invalid syntax,
   missing rollback, or a candidate that never read source.
6. Freeze files, hashes, model-chain evidence, and validation commands.

# Validation and release plane

Generated code never runs while provider credentials are present. Trusted no-secret CI
checks syntax, tests, security, migrations, replay, and integration. Human approval is
required before publishing a PR, deploying a production-connected candidate, activating
real-user canary traffic, publishing trusted OKF, or promoting production. Rejected or
expired proposals cancel unfinished builds.

# Token discipline

Prefer ranked localization, symbol reads, references, bounded patches, and deterministic
contract errors. Stop roles at their independent budgets. Repeated broad investigation
without source reads or a staged behavioral fix is not progress.

