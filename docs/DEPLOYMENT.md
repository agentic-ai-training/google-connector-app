# Safe deployment

## Order

1. Export the production Neon schema/data required for recovery.
2. Record the current Railway/Vercel deployment IDs and health responses.
3. Apply additive Alembic migrations.
4. Deploy the API image; wait for `/health` and migration head.
5. Deploy one worker service from the same image with
   `python -m app.runs.worker_entry`.
6. Deploy Alloy only after Grafana Cloud remote-write credentials exist.
7. Deploy Vercel with the Railway public API URL.
8. Run OAuth, read-only, approval, reconnect, session isolation, metrics, and dashboard
   smoke tests before enabling a pilot cohort.

Set `DEPLOYMENT_VERSION` to the immutable commit/deployment version. Canary evaluation
uses that label and refuses to pass without at least five measured control and five
candidate runs.

## Pilot gates

Use internal users first, then 5–10, 20–30, 40–50, and 80–90 users. At every gate,
review task correctness, external side effects, OAuth health, latency, quota, failure
categories, orphaned artifacts, and privacy. Automatic rollback may disable a candidate;
production promotion always needs the second human approval.

## Railway service layout

- `google-connector-app`: API, `Dockerfile`, public port 8080, and
  `EMBEDDED_WORKER_ENABLED=false`.
- `google-connector-worker`: durable worker, `Dockerfile.worker`, private metrics
  port 8001, and the same application/database/OAuth variables as the API.
- `google-connector-alloy`: `Dockerfile.alloy`; keep it undeployed until all three
  Grafana Cloud remote-write variables are configured.

The GitHub deploy workflow updates both the API and worker from the reviewed `main`
commit. Alloy requires `GRAFANA_CLOUD_PROMETHEUS_URL`,
`GRAFANA_CLOUD_PROMETHEUS_USERNAME`, and `GRAFANA_CLOUD_API_KEY`, plus private targets
`google-connector-app.railway.internal:8080` and
`google-connector-worker.railway.internal:8001`.
