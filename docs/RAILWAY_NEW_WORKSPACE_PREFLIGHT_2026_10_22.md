# Operator-provided Railway token: read-only preflight

22 October 2026. The operator reports a newly created Gmail-based Railway account
with a free trial and saved its token in ignored `.env.local` under the separate
`RAILWAY_API_TOKEN_MAHTO` name. No token value is recorded here. Existing Railway
account/project tokens, CLI login and project links are unchanged. The suffix
prevents automatic replacement of standard Railway CLI credentials.

## Observed API evidence

Bounded HTTPS requests to the official GraphQL endpoint used this explicit token
only, with bearer authentication and an operator User-Agent:

- The documented `me { name email }` query returned authorization denial. The
  intended account identity/email therefore has **not** been verified.
- The documented `projects { edges { node { id name } } }` query succeeded without
  GraphQL errors and returned **zero projects**. This proves resource-query access,
  not trial funding, unrestricted networking or the operator's intended identity.
- Read-only schema inspection succeeded and confirmed the workspace query requires
  a non-null `workspaceId`. No workspace listing field was found in the selected
  query-root names. A workspace ID/Settings URL is the next missing scope input.

One initial response parser raised `AttributeError` on a null GraphQL data object;
the corrected bounded query distinguished authorization errors from transport or
token-read failure. That local parsing issue was not evidence of a revoked token.
No secret/raw GraphQL error response was printed and no token rotation was requested.

The [official API contract](https://docs.railway.com/integrations/api) distinguishes
account tokens from workspace tokens. A workspace-selected token cannot run the
personal `me` query; the observed result is compatible with that scope but does not
independently prove the token type. Prefer checking the intended workspace with
this token before asking for broader account access. No GitHub connection is needed
for the bearer-token API authentication itself.

## Superseding scope and billing observation — 24 October

The later read-only `apiToken { workspaces { id name } }` query resolved one
workspace: `24e7c6af-7588-410b-a6df-8fa2a5fc1208`, named
`hansraj mahto's Projects`. An explicit workspace-scoped projects query returned
zero projects. The earlier missing-workspace conclusion above is historical and
resolved; do not request the URL or another token for that reason. Personal `me`
authorization remains distinct from workspace resource authority.

The operator subsequently confirmed the billing screen shows **Trial Plan,
$5.00 available, $0.00 used**, 1 GB RAM, 2 vCPU and 1 GB disk, with no project
usage. Its displayed Oct 22–Oct 23 range is a usage interval, **not a trial expiry**.
This is accepted operator billing evidence, not an API balance measurement. API
metadata reports plan `HOBBY` and included usage of $5; neither field independently
proves a paid subscription or remaining balance. Do not ask the operator to repeat
the supplied billing evidence.

Capacity metadata limits each container to 1,000,000,000 memory bytes, two vCPUs
and 1,000,000,000 disk bytes. A project permits five services, two service domains
and a 4-GB maximum image. These are ceilings, not measured application fit.
No outbound Google/Neon/model network proof or production-memory measurement has
yet been obtained. See [capacity and release gates](RAILWAY_TRIAL_RELEASE_PLAN.md).
No mutations, services, variable copies, migrations or spend were authorized by
these read-only checks. Saved credentials and older accounts remain unchanged.

## Trial eligibility and technical activation remain separate

The trial statement is now confirmed by the operator's billing-screen observation.
[Railway's trial documentation](https://docs.railway.com/pricing/free-trial)
describes a one-time trial grant, automated GitHub verification and outbound-network
restrictions on email-only/unverified Limited Trial accounts. These restrictions may
prevent the core app from reaching Google, Neon or model services; do not claim
this account can host the required production services merely because API access works.

[Railway account rules](https://docs.railway.com/access/accounts) permit one account
per person. Additional personal accounts are not an approved repeat-trial recovery
strategy. Establish a compliant ownership/funding path, involving Railway support
if consolidation is needed, before any migration. Do not bypass verification,
create further accounts or silently upgrade billing.

The workspace ID and operator billing observation are no longer missing. Technical
networking, capacity, compliant ownership and a safe release/rollback plan remain
activation gates; a trial label alone does not satisfy those gates.
No GraphQL mutation, project/service creation, migration, variable copy, deploy,
account creation, payment, model call or cloud spend was performed in this preflight.
