# Privacy, retention, and learning governance

- Raw conversation/tool/embedding payload telemetry: 14 days.
- Structured run/event/retrieval metadata: 90 days.
- Aggregate operational/audit data: 12 months.
- OAuth credentials: encrypted and retained until disconnect/account-data deletion.
- Full email bodies, OAuth tokens, and secrets are excluded from public metrics and
  improvement notifications.

Feedback is user-scoped. A trajectory is added to the learning dataset only when the
user explicitly consents; email addresses and common secret forms are redacted first.
Legacy trajectories began as `unassigned`; they cannot enter an evaluation dataset until
the governed sanitizer/backfill assigns them. Production mutations are never exploration
actions.

New consented trajectories are recursively sanitized before insertion and assigned by
a stable user-level hash to `train` (80%), `validation` (10%), or `test` (10%). Keeping
all runs from the same user in one split prevents near-duplicate session leakage. Existing
legacy `unassigned` rows must pass the same sanitizer before a governed backfill.

Automatic analysis groups recurring sanitized failure categories, creates evidence
links and a frozen candidate hash, then exposes it in the protected Admin Improvement
Center. The lifecycle is:

`awaiting_review -> approved_for_canary -> canary_active -> awaiting_promotion -> approved_for_publication`

A guardrail breach produces `rolled_back`. Any material candidate change changes the
hash and invalidates the earlier approval.

Users can download their tenant-scoped runs, events, verified artifacts,
conversations, feedback, RAG text/lineage, and consented trajectories from
`GET /auth/account-data/export`. OAuth ciphertext and vector embeddings are always
excluded. `POST /auth/account-data/delete` remains separately confirmation-gated.
