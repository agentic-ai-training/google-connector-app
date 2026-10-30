# Railway production access and observed service state — 13 October 2026

The operator saved a separate `RAILWAY_TOKEN_17407` in ignored `.env.local`.
Its value is not recorded here or in command output. The existing `RAILWAY_TOKEN`
was neither overwritten nor revoked; local configuration does not automatically
update GitHub Actions secrets or Railway variables.

## Actual read-only identity proof

The Railway `projectToken` query, using `Project-Access-Token`, matched both:

- Existing project: `db6865d1-da2c-4640-ab95-090d8b73bf98` (`joyful-achievement`).
- Existing production environment: `568a6180-3144-482a-a02a-8a4091f1ab05`.

The initial Python-default client request returned HTTP 403. A bounded read-only
retry using the explicit operator client User-Agent returned HTTP 200 without
GraphQL errors and proved both identities. That sequence is evidence of a
client/request-dependent rejection, not proof of an invalid token, disabled
account or changed permissions. Do not request another token on that evidence.

[Railway's official API contract](https://docs.railway.com/integrations/api#using-a-project-token)
distinguishes this header from the account/workspace bearer-token header. Scope
is one project environment; account-wide authorization was not requested.

## Observed production state

The project service inventory succeeded. The `serviceInstance.latestDeployment`
queries returned:

| Service | Service ID | Latest deployment observation |
| --- | --- | --- |
| API | `d1c5532a-59c1-475c-86fa-91ce51605cff` | `edf719ce-ced7-4a70-9135-791a51129336`, FAILED, created 22 Aug 2026 14:01:11 UTC |
| Worker | `57b79285-61de-4e76-81e2-46cb169e6ecf` | `a9ff8d07-c1e7-4246-9486-8eb0fa40e978`, FAILED, created 22 Aug 2026 14:01:11 UTC |
| Candidate | `8eb93049-3bed-4ed4-8c6e-2eeb05943563` | `fb866446-e6ef-4d86-8e3b-900ce6969f74`, FAILED, created 22 Aug 2026 14:01:12 UTC |
| Alloy | `3815999b-fbca-4086-a566-83121134fbae` | Latest deployment null |
| Ollama | `d207e73c-829a-4bc4-88d7-77def06cf0ad` | Latest deployment null |

The ten most recent entries from the separate deployment-history connection were
all REMOVED, also dated 22 August. History entries and the service instance's
latest-deployment pointer are different observations, not interchangeable.
The authorized API runtime-log request returned zero retained lines. The subsequent
build-log query did retain evidence: at the bounded 120-line request, the provider
returned 121 records, including the failed `openssl-sys v0.9.117` build. It could not
locate an OpenSSL installation, and `cargo build --release --locked` exited 101.
The failed deployment identifies source commit
`ac3340b6a5a4abc9ba4cbc279eea1bbf199a6695`. This is an established build-dependency
failure, not evidence that the new token, GCP credit or Neon database caused it.
The current repository Dockerfile already installs `pkg-config libssl-dev` in its
Rust build stage; do not add a redundant patch. A compatible verified release/build
and actual deployment are still required, rather than assuming the old failed
deployment automatically received that later source correction. Raw logs and full
deployment metadata were never emitted; only bounded sanitized error evidence.

Railway returned the existing API service domain
`google-connector-app-production-9a03.up.railway.app`. A real request to its
`/health` endpoint returned **HTTP 404**, not a verified healthy application.
The endpoint matches the application's existing `app/api/main.py` health route.
No custom domain was returned. These facts do not prove frontend connectivity,
Neon database health, current model access or successful Workspace actions.

No GraphQL mutation, service creation, redeployment, secret export, migration,
payment/billing upgrade, model call or Google Workspace write occurred. Production
recovery still needs compatible release/backup and deployment prerequisites, and
Railway's actual spending/capacity eligibility—not only a working token. Preserve
the separate GCP budget/host deadline and local PostgreSQL boundary.

## Subsequent operator billing confirmation

The operator explicitly reported that the achintya17407 workspace shows **Free
trial expired**. This is user-console evidence, not a billing-field inference from
the token or the old Rust build failure. No paid upgrade, another trial/account,
restart or topology migration was performed. The Railway runtime branch needs a
separately approved funding path; moving core services onto the existing GCP VM
would also require explicit topology/capacity/budget/rollback review rather than
silently consuming the protected Vertex or future-reserve envelopes. Approved
isolated GCP implementation can continue independently while that choice is pending.

## Older 17408 workspace check

At the operator's request, the preserved `RAILWAY_TOKEN` successfully authorized
read-only access to project `8db2f70a-b9d6-477a-9223-01faa776b8b6`, environment
`cb017d58-9b9a-440d-b23b-385c0cf999bb`. The API returned workspace
**achintya17408's Projects**, plan **HOBBY**, and project **comfortable-intuition**,
created 16 July 2026, with project `subscriptionType: trial`. API, worker, Alloy
and Ollama service-instance latest-deployment pointers were null. These observations
do not prove an unexpired trial, remaining credit, paid subscription eligibility,
or application health. Billing balance/expiry still require authorized account-level
evidence or an operator console observation. No old token was replaced or revoked.

[Railway account rules](https://docs.railway.com/access/accounts) permit one account
per person. Creating another personal account with another Gmail is not the approved
recovery strategy. The documented one-time trial is $5/up to 30 days; afterward the
Free plan provides $1/month. Hobby is normally $5/month including $5 resource usage,
not a renewed trial. No restart, upgrade, new account or migration was performed.
