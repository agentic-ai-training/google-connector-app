---
type: runbook
title: Google OAuth recovery
owner: project-admin
version: 2
timestamp: 2026-07-29T00:00:00Z
visibility: public
publication_status: approved
approved_by: project-admin
approved_at: 2026-07-19T00:00:00Z
tags: [oauth, scopes, recovery]
---
# Diagnosis

Distinguish an expired/used authorization code, missing newly added scope, revoked token,
testing-mode tester denial, redirect mismatch, and provider outage. Never retry an OAuth
code. Restart login with PKCE and require consent when the stored scope set is incomplete.

# Scope expansion

`/auth/me` is the source of truth for `missing_scopes`. A newly required scope does not
require a new OAuth client or downloaded JSON; it requires adding the scope to the same
Google Auth Platform Data Access configuration when necessary, then one fresh consent.

Google Chat direct-message creation requires
`https://www.googleapis.com/auth/chat.spaces.create`. Existing users must reconnect once
after deployment. The runtime may call `spaces.setup` only after
`spaces.findDirectMessage` returns the specific “direct message doesn't exist” 404.
Other 403/404 responses retain their original category.
