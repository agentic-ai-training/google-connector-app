# Operations and recovery

## Local service URLs

- API health: `http://localhost:8000/health`
- API metrics: `http://localhost:8000/monitoring/metrics`
- Prometheus: `http://localhost:9090`
- Grafana: `http://localhost:3001`
- Ollama: `http://localhost:11434/api/tags`
- Docker PostgreSQL: `127.0.0.1:55432`

Start with `docker compose up -d --build`; inspect with `docker compose ps -a` and
`docker compose logs --tail=200 api worker`. The API runs Alembic before serving and
the worker starts only after the API health check passes.

## State and failure recovery

- Expired worker leases are reclaimed automatically.
- Safe reads retry transient network/rate-limit/worker failures up to three attempts.
- Writes do not retry blindly. Resume keeps completed steps and starts from the first
  failed step.
- Complex/high-risk tasks pause on quality-model quota instead of silently using the
  small fallback model.
- Embeddings are outside the live-tool path. Failed jobs retry with backoff and move
  to `dead_letter` after exhaustion.
- The incident summary records last success, breaking point, pending steps, cause,
  evidence IDs, and four distinct completion measures.
- Supported writes are read back from Google before completion is claimed. A run with
  verified earlier artifacts and a later failure is recorded as `partial`, not failed.

## Governed improvement decisions

Administrators see an in-app badge (refreshed every 30 seconds) when a proposal needs
canary review, activation, or final promotion. Open `/admin/improvements` to inspect
the sanitized evidence, exact candidate diff, privacy/security reports, rollback plan,
and content hash. Material changes invalidate the reviewed hash. A measured passing
canary is mandatory before the second human publication decision. No email or Chat
notification is sent automatically because those are external writes.

Every proposal and canary conclusion also creates a durable notification ledger.
The protected Admin UI and Grafana channels are marked delivered immediately because
they are internal views over the same sanitized database facts. Email and GitHub are
marked skipped until their narrowly scoped credentials exist and the administrator
approves that external publication path. Approval never edits trusted OKF or runtime
policy directly: it authorizes a selected-user canary; only the later promotion
approval can publish the already hash-frozen candidate.

Current governed sequence:

1. Review an exact occurrence/cluster/theme and choose option A or B.
2. The credential-minimal builder generates a bounded untrusted draft and regression tests.
3. Explicitly approve public draft-PR creation; trusted CI validates every application surface.
4. Review the frozen hash. Separately approve any bundled OKF overlay.
5. Approve isolated candidate deployment, then separately activate selected-user traffic.
6. Wait for minimum labelled control/candidate evidence. Safety regressions roll back assignment.
7. Approve promotion. Production remains `production_pending` until both Railway API and
   worker attest the exact merged commit and smoke tests.

Rejected, expired, rolled-back, and failed-build policy themes return to `active` so later
evidence can reopen them. Only attested production publication resolves a theme. API/planner
candidates require the isolated HTTPS candidate API plus exact health/version attestation;
frontend candidates remain blocked until a separate preview router exists. Do not represent
worker-only deployment evidence as proof for an API or frontend surface.

The isolated candidate Railway project should remain scaled to zero until the deployment
gate is approved. It needs no public domain. `candidate-infra-check.yml` validates access
without deploying; `candidate-cleanup.yml` returns its worker region to zero after rollback,
rejection, or promotion.

## Tokenizer and bounded-result recovery

The exact `cl100k_base` tokenizer is downloaded during image build into
`/opt/tiktoken-cache`. Runtime modules load it lazily. If the cache is unexpectedly absent
and the network is unavailable, model-result budgeting uses a conservative UTF-8 byte bound,
DP falls back to greedy packing, and the worker remains available. Rebuild the immutable
image to restore exact-tokenizer eligibility; never solve this by allowing unbounded results.

After final promotion approval, the **Publish sanitized draft PR** button creates a
new branch containing only `.improvement-proposals/<key>.md` and opens a draft PR;
it never auto-merges. It requires `GITHUB_PROPOSAL_REPOSITORY` and a short-lived
GitHub App installation token in `GITHUB_PROPOSAL_TOKEN` with Contents and Pull
requests write permission only for this repository. **Send sanitized review email**
is a separate confirmation and requires `ADMIN_NOTIFICATION_EMAIL` plus the
administrator's connected Google account.

## Embedding backpressure

Embedding persistence is outside the user-response critical path. Admission is
bounded by `MAX_EMBEDDING_JOBS_GLOBAL`, `MAX_EMBEDDING_JOBS_PER_USER`, and
`MAX_EMBEDDING_PAYLOAD_CHARS`. A rejected persistence job does not invalidate the
live Google result; it increments `agent_embedding_admission_rejections_total`
with a bounded reason label. Investigate sustained rejections before increasing
limits: first inspect Ollama health, dead letters, input size, and queue age.

## Structured logs and OTLP traces

HTTP logs and spans intentionally contain only a validated request ID, trace ID,
method, route template, response status, and duration. They never contain raw
paths, run IDs, query strings, bodies, users, OAuth tokens, or Google content.

Set `OTEL_EXPORTER_OTLP_ENDPOINT` and optional comma-separated
`OTEL_EXPORTER_OTLP_HEADERS` only in the deployment secret manager. Non-local
endpoints must use HTTPS. The API and worker derive distinct service names from
`RAILWAY_SERVICE_NAME`; local Compose sets them explicitly. With no endpoint the
instrumentation remains local/inert and does not attempt an export.

## Grafana Cloud dashboard synchronization

Dashboard JSON is version-controlled under `monitoring/grafana/dashboards`. Validate
both dashboards without credentials or writes:

```bash
python scripts/sync_grafana_dashboards.py
```

Publishing is a separately authorized external write. Create a Grafana service-account
token with dashboard write access, keep it out of Git and shell history, then run:

```bash
export GRAFANA_URL=https://pluckypanther2969.grafana.net
export GRAFANA_SERVICE_ACCOUNT_TOKEN=... # enter locally; never paste into chat
python scripts/sync_grafana_dashboards.py --apply \
  --confirmation 'SYNC GRAFANA DASHBOARDS'
unset GRAFANA_SERVICE_ACCOUNT_TOKEN
```

The synchronizer validates required dashboard fields, requires HTTPS, overwrites only
the two stable dashboard UIDs, and never prints the token. Without `--apply` it performs
no network request.

## Rollback

1. Stop the worker so no new step is claimed.
2. Preserve in-flight run/event/artifact rows.
3. Set `DURABLE_RUNS_ENABLED=false` and retain `LEGACY_CHAT_ENABLED=true` if the new
   API must be disabled.
4. Roll the application image back before downgrading schema. Migrations are additive;
   only downgrade after confirming no newer process is running and after a backup.
5. Restore dashboards/config independently; telemetry rollback must not change runs.

## Quota and OAuth

For Groq 429 errors, safe simple reads may use the configured small model. Complex or
mutating workflows remain resumable and wait for quality quota. For OAuth failures,
check `/auth/me` missing scopes, reconnect once, and verify the exact production
callback URI in Google Cloud. Never log access/refresh tokens.

## Artifact cleanup

Created artifacts are retained and reported by default. Delete, revoke sharing, or
cancel a Calendar event only through an explicit approved action. The system never
interprets a failed later step as permission to delete an earlier verified artifact.
The browser first calls `cleanup-request`; preserve completes without an external
write, while delete/cancel/revoke/retry returns an action hash. Only a matching,
unexpired `cleanup-decision` executes it. Deletion is limited to resources created by
that run and marked safe; legacy sharing records without a verified permission ID are
reported as `manual_required` rather than guessing which permission to revoke.

## Legacy tenant-safe RAG import

The old source tables predate multi-user ownership. Never expose them directly to the
new retriever. For a known original owner, first run a count-only preview and then the
reversible import:

```bash
NEON_DATABASE_URL=... python scripts/backfill_legacy_rag.py --user-id owner@example.com
NEON_DATABASE_URL=... python scripts/backfill_legacy_rag.py --user-id owner@example.com --apply
```

This reuses existing vectors, assigns an explicit ACL owner, and labels rows
`legacy-import-<source>-v1`. As each source is refreshed, the v2 source-aware ingester
tombstones the legacy chunk. Roll back only this import with `--rollback`.
