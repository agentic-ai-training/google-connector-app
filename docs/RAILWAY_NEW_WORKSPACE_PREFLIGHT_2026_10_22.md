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

## Funding and eligibility are not verified

The operator's free-trial statement has not yet been verified through the intended
workspace or billing screen. [Railway's trial documentation](https://docs.railway.com/pricing/free-trial)
describes a one-time trial grant, automated GitHub verification and outbound-network
restrictions on email-only/unverified Limited Trial accounts. These restrictions may
prevent the core app from reaching Google, Neon or model services; do not claim
this account can host the required production services merely because API access works.

[Railway account rules](https://docs.railway.com/access/accounts) permit one account
per person. Additional personal accounts are not an approved repeat-trial recovery
strategy. Establish a compliant ownership/funding path, involving Railway support
if consolidation is needed, before any migration. Do not bypass verification,
create further accounts or silently upgrade billing.

Next missing non-secret input: the intended workspace's Settings-page URL or copied
workspace ID. After scope verification, inspect actual plan/trial status, credits,
capacity and existing resources before proposing a costed deployment/rollback plan.
No GraphQL mutation, project/service creation, migration, variable copy, deploy,
account creation, payment, model call or cloud spend was performed in this preflight.
