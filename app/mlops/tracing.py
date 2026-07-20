import os
from urllib.parse import unquote, urlparse

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from app.config.settings import get_settings

_configured = False


def _headers(value: str) -> dict[str, str]:
    parsed = {}
    for item in value.split(","):
        if not item.strip():
            continue
        key, separator, content = item.partition("=")
        if not separator or not key.strip() or not content.strip():
            raise ValueError("OTLP headers must use comma-separated key=value entries")
        # OTEL_EXPORTER_OTLP_HEADERS uses URL-encoded header values. Grafana's
        # setup wizard therefore emits ``Authorization=Basic%20...``.
        parsed[unquote(key.strip())] = unquote(content.strip())
    return parsed


def _trace_endpoint(value: str) -> str:
    endpoint = value.strip().rstrip("/")
    if not endpoint:
        return ""
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1"}:
        raise ValueError("OTLP endpoint must use HTTPS outside local development")
    return endpoint if endpoint.endswith("/v1/traces") else f"{endpoint}/v1/traces"


def configure_tracing(app=None) -> bool:
    """Instrument safe metadata only; never attach request bodies or query strings."""
    global _configured
    settings = get_settings()
    if not settings.otel_enabled or _configured:
        return False
    service_name = (
        settings.otel_service_name.strip()
        or os.getenv("RAILWAY_SERVICE_NAME", "").strip()
        or "google-connector-app"
    )
    provider = TracerProvider(resource=Resource.create({
        "service.name": service_name,
        "service.version": settings.deployment_version,
        "deployment.environment": "production"
        if settings.deployment_version != "local" else "local",
    }))
    endpoint = _trace_endpoint(settings.otel_exporter_otlp_endpoint)
    if endpoint:
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(
            endpoint=endpoint,
            headers=_headers(settings.otel_exporter_otlp_headers),
        )))
    trace.set_tracer_provider(provider)
    AsyncPGInstrumentor().instrument(tracer_provider=provider)
    HTTPXClientInstrumentor().instrument(tracer_provider=provider)
    _configured = True
    return True
