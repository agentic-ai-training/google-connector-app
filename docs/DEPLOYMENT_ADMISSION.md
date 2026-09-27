# Durable deployment admission

Status: local implementation extended on 27 September 2026. Migrations 017–018 are
unapplied to production. No generated workload or deployment write was performed
by these tests. This is a controller component, not the completed deployment service.

## Trust boundary

`app/coding/deployment_admission.py` is internal trusted service code. It must not
be exposed as an arbitrary model tool. An operator registers a target after verifying
the Coolify team/server/project/environment and secret ownership. Target identity,
owner, funding project, endpoint and credential references are immutable; changes
require a new target ID. No secret plaintext is stored in target records.

The authenticated user-service approval handler must validate the actual release,
Compose policy and trusted validation attestation before calling `record_user_grant`.
This persistence function does not itself verify an attestation's signature or run
tests. Hash presence is not proof. That handler integration is still pending; there
is deliberately no public grant/deploy endpoint yet. Core-platform candidates cannot
enter this lane: they retain separate CI, canary and promotion authority.

## Durable sequence

```text
Operator-verified target identity / tenant / funded project
                            |
Verified immutable release + Compose + validation attestation
                            |
Authenticated exact-manifest user authority (expiring grant)
                            |
              enqueue transaction
              ├─ serialize user idempotency key across projects
              ├─ lock funding account, then target and grant
              ├─ reserve incremental estimated cost
              └─ record deployment and audit event atomically
                            |
              claim transaction
              ├─ recheck target, grant expiry/revocation, credit and cap
              └─ cost dispatch + deployment dispatch intent + events
                            |
         ONE caller receives the committed claim
                            |
             provider write outside DB transaction
                  /                      \
         known resource             ambiguous outcome
           submitted                   uncertain
               |                          |
               |              query provider identity; never blind retry
               +<-------------------------+
               |
       independent behavioral verification (pending integration)
```

An unchanged replay does not grant a second dispatch. One grant is bound to one
deployment; another idempotency key cannot reuse it. A worker crash after committing
intent may have happened before or after the external write, so it must reconcile
instead of guessing. This is not a claim of exactly-once external delivery.

Ambiguous provider outcomes retain the financial reservation. Reconciliation remains
possible after target/grant revocation, without allowing new writes. A submitted
resource ID cannot be replaced on replay. Submission is not health, functional
verification, or completion. No function here marks a deployment verified.

Database composite foreign keys bind the same user, target and funded project to
the authorization, deployment and cost reservation. Advisory locking covers the
user's idempotency namespace even when two competing requests name different funding
projects. Ordinary admission/claim uses a consistent account-first lock order after
that idempotency lock.

## Tests and remaining integration

Real local PostgreSQL tests cover duplicate enqueue/dispatch, cross-tenant denial,
cross-project idempotency, changed manifests, one-grant reuse prevention, expired and
revoked authority, stale credit, transaction fault rollback, ambiguous-write recovery,
submission identity, database ownership constraints and migration downgrade/re-upgrade.
The migration round-trip is confined to a disposable local schema, not production.

Before deployment activation, complete:

1. Audited enrollment/import of the already-running VM's obligation and fresh,
   attributable credit/cost evidence. Do not enroll its account as unused.
2. Activate and enforce host capacity admission. Migration 018 and
   `app/coding/host_capacity.py` now allocate CPU/RAM/storage against one existing
   hosting obligation. The immutable target binds the permitted pool; aliases cannot
   register the same funded host twice. Requests contend atomically for capacity and
   cannot exceed its lifetime. Ambiguous deployments retain capacity until trusted
   cleanup evidence releases it. Verify actual runtime limits and usable host capacity
   before registering a live pool: a stored enforcement hash alone proves nothing.
   A zero incremental cost needs trusted host coverage; variable costs beyond that
   coverage must still be reserved separately.
3. Authenticated approval/standing-authority integration and real attestation checks.
4. Scoped Coolify configuration/deployment credentials and immutable artifact loading.
5. Typed provider operations, stable resource lookup, bounded polling and ambiguity
   reconciliation; do not equate an accepted request with a healthy deployment.
6. Secure execution/network/storage limits, generated Compose policy, TLS/domain,
   real application security/persistence tests and exact authorized Chat delivery.

Budget ceilings are not provider hard caps; existing VM costs continue independently
of this unactivated controller. See [cost ledger](CLOUD_COST_LEDGER.md) and
[Delhi hosting](GCP_DELHI_HOSTING.md).

```text
Host bill reservation (one per funded host)
                  |
         verified usable capacity pool
                  |
       +----------+-----------+
       |                      |
 App A CPU/RAM/storage   App B CPU/RAM/storage
       |                      |
       +--> sum <= pool limit <+

Timeout/uncertainty -> keep allocation
Verified removal   -> release capacity
Host costs accrue  -> move reserved money to spent, without charging it twice
```

The complete local coding suite passes 103 tests with real PostgreSQL enabled,
including capacity races, independent resource limits, shared funding, uncertainty,
revocation, accrual and database migration checks. Runtime isolation, quota enforcement
and a real generated-app deployment still need their own live evidence.
The complete Alembic chain was also exercised in a new disposable local database:
upgrade to 018, downgrade to 002, and upgrade back to 018. The helper is
`python3 -m scripts.verify_cloud_migrations_local` with the loopback-only
`COST_LEDGER_TEST_DSN` set. It removes only the database it creates for that check.
CI now opts into the PostgreSQL cost/deployment tests and compares the applied
migration version with the repository head rather than a stale hard-coded revision.
