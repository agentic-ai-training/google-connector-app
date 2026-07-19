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
