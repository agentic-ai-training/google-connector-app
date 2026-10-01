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
requires a separately reviewed trusted verification source. The installed
server-detail API likewise omits its numeric ID, while Service readback includes
`server_id`. The target now pins an operator-verified nonnegative `server_id`
alongside its UUID; an API-supplied ID, if present, must match. No live target
has been registered on an assumed mapping.
The installed VM's read-only database query on 2026-09-29 found its one server and
one standalone Docker destination at ID `0`; nonnegative ID validation is required.
No Coolify project, environment or API token existed at that 29 September
checkpoint, so this was enrollment evidence only, not a deployable target.
On 2026-09-30, separate limited-lifetime inventory, configuration, deployment
and recovery tokens were issued under the existing owner (no `root` token).
Inventory, configuration and recovery each passed a read-only team/server probe
through pinned IAP; the deploy-only token has not been exercised. There is still
no private generated-app destination. An operator-only API call subsequently
created project `mnv84tsssicw2y7dtogvbmwr` and environment
`pi3o2fpjo2ituxrppav8izj4`; readback verified both without a second write.
An app-specific pilot destination was subsequently created on the same server:
UUID `2rxr0cpp0x91g47o9flwcbzk`, network `gca-app-login-pilot-20260930`,
trusted database ID `1` (server ID `0`). Docker reported zero containers on its
attachable bridge. This is target-enrollment evidence, not proof that future
Service containers are isolated, healthy or safe to start. No target is
deployable yet because cost-ledger import, trusted build, secret and runtime
gates remain. See [Delhi hosting](GCP_DELHI_HOSTING.md) for identities and scope.

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
The manifest now has two typed release profiles. A single-service release retains
one passing claim. A `react_spring_postgres` release requires four distinct,
profile-labelled passing claims (`react_test`, `react_build`, `spring_test`,
`spring_package`) and a canonical hash of the complete proof set. Each claim must
belong to the same tenant/run/target/frozen source and remain unexpired at provider
admission and dispatch. A Python-only pass, missing profile, duplicate claim, altered
source/profile, failed result or expired result cannot authorize the stack. Compose
must match the declared profile: the generated stack contains `frontend`, `backend`
and `database`, with an internal named database volume and bounded resources;
a multi-service
stack cannot be labelled `single_service`. This is a local admission gate, not live
toolchain proof. The React/Spring profiles are not yet activated on the host.
For this typed stack, static Compose admission now also requires the Postgres data
volume at `/var/lib/postgresql/data`, exactly `POSTGRES_DB`, `POSTGRES_USER`, and a
secret-reference `POSTGRES_PASSWORD` on the database service, and matching
`SPRING_DATASOURCE_URL`, username, and password on the backend. Database command,
entrypoint, working-directory and authentication-mode overrides are denied. This
prevents a release from claiming a connected private PostgreSQL stack while its
declared credentials or destination contradict one another. It does not prove
that secrets have been provisioned, Spring actually connects, or the database
persists; those require later live behavioral and restart tests.
The low-level dispatch function now requires passing validation by default. The
explicit unvalidated option is used only by financial-ledger tests; the Coolify
adapter keeps the gate enabled. An absent validation claim leaves both the provider
claim and cost reservation untouched.
The Compose policy now recomputes `release_hash` from a domain-separated canonical
statement containing that validated source hash, the exact Compose hash, and every
service's digest-pinned image reference. A mismatched release, source or image is
rejected before credential resolution or a provider request. This closes an
unused-manifest-field gap. A separate local verifier now requires an Ed25519-signed
build statement for every controller-led Service create. Its public key is
operator-pinned in the immutable target; the signed statement binds run, tenant,
target, frozen source,
release identity, exact Compose/image references, builder job identity, evidence hash
and expiry. A generated stack must assert `frontend` and `backend` as built images;
the `database` image is explicitly external. Missing, expired, altered or unsigned
statements fail before credentials or provider access, and the signature is checked
again in the one-time dispatch transaction. This is **signature verification**, not
proof that a trusted keyless builder actually measured and signed those images.
For the generated stack, the external PostgreSQL image must also match an exact
digest-pinned image in the immutable operator-registered target. A generated
manifest cannot choose or replace that database image. Pinning is not an image
vulnerability scan or a proof of upstream image provenance.
After dispatch, read-only provider reconciliation still verifies signature and
release identity even if the statement has expired; expiry prevents another
write claim, not investigation of an uncertain earlier write.
No production signer/key has been enrolled. The external PostgreSQL image also needs
approved digest provenance/scanning before any Service may be started.
The create path now also reads the registered server, project, environment and
destination from installed-version API routes before consuming the one-time claim.
It checks UUID relationships, standalone/swarm type and an exact approved
`gca-app-*` destination-network name; the VM's existing `coolify` network is
explicitly ineligible for generated apps. The same network check applies to
uncertain-write reconciliation. This is only a network-identity gate, not proof
that the Docker network is unique, isolated, or unable to reach core services;
network creation, uniqueness, firewall/east-west tests and runtime security remain
required.
Installed Coolify 4.3.23 source confirms that Services use a resource-specific
Compose network and join the selected predefined network only when
`connect_to_docker_network` is true. The controller now sends `false` explicitly;
sensitive readback rejects true or missing values before adopting an uncertain
create. The shared Coolify proxy still connects to routed Service networks, so
this static gate is **not** live cross-app or core-network isolation proof.
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
that reference only after metadata identifies one candidate. This scope exposes
sensitive provider fields. The hash marker alone is not sufficient to adopt a
resource. The recovery credential's sensitive readback payload still needs an
authenticated live test after an approved Service exists.

`app/coding/operator_secrets.py` now provides a callable operator resolver for
explicitly registered reference-to-file or reference-to-environment bindings.
Private-file reads use an owner-only directory, an owner-only regular file,
`O_NOFOLLOW`, a bounded single assignment, strict single-quote parsing for the
existing bootstrap files and a fixed expiry; unknown references and expired
credentials fail closed without logging values. On 2026-10-01 the read-only pilot
target probe used this resolver and authenticated inventory token to verify the
Coolify team, server, project, environment and destination over IAP. This does
not yet bind the four references to a production deployment worker, prove token
rotation, or authorize Service creation/start.
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
YAML. It also binds the release fingerprint to the validated source, exact Compose
bytes and each pinned image reference. This is a baseline static gate, **not**
build execution, image scanning,
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
4. Connect the now-issued scoped Coolify credentials to an authenticated controller
   and immutable artifact loading. The deploy-only token has no live proof yet.
5. Verify this API and inventory contract against the pinned installed Coolify
   version, verify and rotate least-privilege configuration/deployment credentials, connect
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

The complete local coding suite passed 320 tests with real PostgreSQL enabled after
the Coolify network-attachment readback change. This run covered capacity races,
imported-VM funding, single Service submission, false validation denial, uncertainty,
revocation, accrual and database migration checks. The targeted Compose and
Coolify-Service suite also covers the four-profile generated stack, including a
provider-access denial for an approved manifest containing an unrelated release hash.
The Coolify provider is mocked in these tests. Runtime isolation,
image-to-source provenance, quota enforcement and a real generated-app deployment
still need their own live evidence.
The complete Alembic chain was also exercised in a new disposable local database:
upgrade to 018, downgrade to 002, and upgrade back to 018. The helper is
`python3 -m scripts.verify_cloud_migrations_local` with the loopback-only
`COST_LEDGER_TEST_DSN` set. It removes only the database it creates for that check.
CI now opts into the PostgreSQL cost/deployment tests and compares the applied
migration version with the repository head rather than a stale hard-coded revision.
