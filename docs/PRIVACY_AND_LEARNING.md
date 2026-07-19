# Privacy, retention, and learning governance

- Raw conversation/tool/embedding payload telemetry: 14 days.
- Structured run/event/retrieval metadata: 90 days.
- Aggregate operational/audit data: 12 months.
- OAuth credentials: encrypted and retained until disconnect/account-data deletion.
- Full email bodies, OAuth tokens, and secrets are excluded from public metrics and
  improvement notifications.

Feedback is user-scoped. A trajectory is added to the learning dataset only when the
user explicitly consents; email addresses and common secret forms are redacted first.
Dataset split begins as `unassigned` so train/validation/test assignment can be done
once with leakage checks. Production mutations are never exploration actions.

Automatic analysis groups recurring sanitized failure categories, creates evidence
links and a frozen candidate hash, then exposes it in the protected Admin Improvement
Center. The lifecycle is:

`awaiting_review -> approved_for_canary -> canary_active -> awaiting_promotion -> approved_for_publication`

A guardrail breach produces `rolled_back`. Any material candidate change changes the
hash and invalidates the earlier approval.
