# Durable deployment admission

Status: local implementation extended on 29 September 2026. Migrations 017–020 are
unapplied to production. No generated workload or deployment write was performed
by these tests. This is a controller component, not the completed deployment service.

## Trust boundary

`app/coding/deployment_admission.py` is internal trusted service code. It must not
be exposed as an arbitrary model tool. An operator registers a target after verifying
the Coolify team/server/project/environment and secret ownership. Target identity,
owner, funding project, endpoint and credential references are immutable; changes
require a new target ID. No secret plaintext is stored in target records.
The operator binds a verified destination UUID plus its provider numeric ID and
type in the immutable target. The create adapter always sends the UUID, never
chooses a destination from provider ordering, and recovery compares the returned
ID/type. The public destination API does not expose numeric IDs, so live enrollment
requires a separately reviewed trusted verification source; no live target has been
registered on an assumed mapping.
The installed VM's read-only database query on 2026-09-29 found its one server and
one standalone Docker destination at ID `0`; nonnegative ID validation is required.
No Coolify project, environment or API token exists there yet, so this remains
enrollment evidence only, not a deployable target.

The authenticated user-service approval handler must validate the actual release,
Compose policy and trusted validation attestation before calling `record_user_grant`.
This persistence function does not itself verify an attestation's signature or run
tests. Hash presence is not proof. That handler integration is still pending; there
is deliberately no public grant/deploy endpoint yet. Core-platform candidates cannot
enter this lane: they retain separate CI, canary and promotion authority.

The new `app/coding/coolify_service.py` path is an internal trusted controller step,
not a user/model tool. Before consuming a dispatch claim, it matches the exact
Compose bytes to the approved SHA-256, checks a bounded generated-Compose policy,
requires a real unexpired `coding_validation_claims` pass for the same user, run,
target and frozen source, resolves an operator-owned credential reference, and
checks the Coolify team. Dispatch checks validation again in the ledger transaction.
The create path now also reads the registered server, project, environment and
destination from installed-version API routes before consuming the one-time claim.
It checks UUID relationships, standalone/swarm type and an exact approved
`gca-app-*` destination-network name; the VM's existing `coolify` network is
explicitly ineligible for generated apps. The same network check applies to
uncertain-write reconciliation. This is only a network-identity gate, not proof
that the Docker network is unique, isolated, or unable to reach core services;
network creation, uniqueness, firewall/east-west tests and runtime security remain
required.
Coolify's public
destination response omits the numeric ID; the operator's registered ID still
needs a trusted out-of-band mapping. Missing or mismatched targets leave the
deployment reserved and make no Service POST.
The local PostgreSQL/provider-mock test exercises one concurrent POST and denies
replay, false/expired validation, changed source, cross-tenant access and wrong team.
Read-only recovery of an uncertain create now checks the deterministic Service
name, a submitted Compose SHA-256 marker in its description, the registered
server/project/environment identities and the parsed Compose document. Installed
Coolify 4.3.23 re-dumps YAML before saving it, so its bytes need not match the
approved bytes. A missing, duplicate, wrong-host or semantically different result
never becomes submitted and never triggers a second POST. The installed API hides
`docker_compose_raw` from ordinary read tokens; recovery fails closed with
`sensitive_read_required` unless a token with `read:sensitive` (or broader root)
authority is supplied. The target now requires a distinct recovery credential
reference. Recovery lists resource metadata with the ordinary inventory credential,
rejects an overscoped inventory response that includes raw Compose, and reads only
the one matching Service with the sensitive recovery credential. The adapter resolves
that reference only after metadata identifies one candidate. The
operator-owned secret store, token issuance/rotation and limited lifetime remain
to be integrated and reviewed before activation; this scope exposes sensitive
provider fields. The hash marker alone is not sufficient to adopt a resource.
The credential's scope and readback payload still need an authenticated live test.
On 29 September 2026, read-only unauthenticated requests through pinned IAP/SSH to
the installed instance returned 401 for `/api/v1/services`, `/api/v1/servers/{uuid}`,
`/api/v1/projects/{uuid}` and the project-environment route; a deliberately absent
API path returned 404. This distinguishes route presence from a blanket 401, but
does **not** prove authenticated payload schemas, credential scope, or write behavior.
The container's Artisan route-list command failed, so it is not counted as proof.
Bounded source inspection of the installed `ServicesController` independently
confirmed the create endpoint accepts base64 Compose and defaults
`instant_deploy` to false; its custom-Compose branch calls `StartService::dispatch`
only when true. This is source-level evidence, not a live authenticated write.

The provider step follows the [Coolify Service creation API](https://coolify.io/docs/api/endpoints/services/create-service)
using `POST /api/v1/services` with base64 Compose and `instant_deploy=false`.
It records a returned UUID as **submitted**, not started, healthy or functionally
complete. A lost response or provider error is **uncertain** and never blindly
reposted. The installed Coolify 4.3.23 payload still needs authenticated
version-specific verification before activation; the official public reference
is not a payload probe of this VM.

`app/coding/compose_policy.py` currently rejects unpinned images, host binds,
published host ports, privileged/network escapes, labels, replica multipliers,
literal secret-like environment values, unbounded CPU/RAM and malformed or duplicate
YAML. This is a baseline static gate, **not** build provenance, image scanning,
runtime CPU/storage enforcement, secret provisioning or a claim that arbitrary
generated code is safe. Named volumes still need enforceable disk quotas and backup
policy before any service is started.

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
5. Verify this API and inventory contract against the pinned installed Coolify
   version, create least-privilege configuration/deployment credentials, connect
   the mock-tested identity reconciliation to a trusted worker, and add durable secret,
   start, bounded-poll, health, backup and behavioral-verification steps. Do not
   equate a submitted Service UUID with a healthy deployment.
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

The complete local coding suite passes 279 tests with real PostgreSQL enabled as of
30 September 2026, including capacity races, imported-VM funding, single Service
submission, false validation denial, uncertainty, revocation, accrual and database
migration checks. The Coolify provider is mocked in these tests. Runtime isolation,
image-to-source provenance, quota enforcement and a real generated-app deployment
still need their own live evidence.
The complete Alembic chain was also exercised in a new disposable local database:
upgrade to 018, downgrade to 002, and upgrade back to 018. The helper is
`python3 -m scripts.verify_cloud_migrations_local` with the loopback-only
`COST_LEDGER_TEST_DSN` set. It removes only the database it creates for that check.
CI now opts into the PostgreSQL cost/deployment tests and compares the applied
migration version with the repository head rather than a stale hard-coded revision.
