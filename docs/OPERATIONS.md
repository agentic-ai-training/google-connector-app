# Operations and recovery

## Local service URLs

- API health: `http://localhost:8000/health`
- API metrics: `http://localhost:8000/monitoring/metrics`
- Prometheus: `http://localhost:9090`
- Grafana: `http://localhost:3001`
- Ollama: `http://localhost:11434/api/tags`
- Docker PostgreSQL: `localhost:5433`

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
