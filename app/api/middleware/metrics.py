import json
import logging
import re
import sys
import time
import uuid

from opentelemetry import trace
from opentelemetry.context import Context, attach, detach
from opentelemetry.propagators.textmap import Getter
from opentelemetry.trace import SpanKind
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from starlette.routing import Match

from app.mlops.metrics import request_count, request_latency

logger = logging.getLogger("google_connector.requests")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")


class _HeaderGetter(Getter):
    def get(self, carrier, key):
        value = carrier.get(key)
        return [value] if value else None

    def keys(self, carrier):
        return list(carrier.keys())


HEADER_GETTER = _HeaderGetter()


def _correlation_id(request) -> str:
    supplied = request.headers.get("x-request-id", "")
    return supplied if SAFE_REQUEST_ID.fullmatch(supplied) else uuid.uuid4().hex


def _route_template(request) -> str:
    route = request.scope.get("route")
    if getattr(route, "path", None):
        return route.path
    # Authentication can reject a request before Starlette's router attaches
    # the matched route to the scope. Resolve against route definitions so the
    # metric remains useful without falling back to the high-cardinality URL.
    app = getattr(request, "app", None)
    for candidate in getattr(app, "routes", []):
        original_router = getattr(candidate, "original_router", None)
        if original_router is not None:
            for included in original_router.routes:
                match, _ = included.matches(request.scope)
                if match is Match.FULL:
                    return getattr(included, "path", None) or "unmatched"
            continue
        match, _ = candidate.matches(request.scope)
        if match is Match.FULL:
            return getattr(candidate, "path", None) or "unmatched"
    return "unmatched"


async def metrics_middleware(request, call_next):
    started = time.perf_counter()
    request_id = _correlation_id(request)
    request.state.request_id = request_id
    status = 500
    parent = TraceContextTextMapPropagator().extract(request.headers, getter=HEADER_GETTER)
    tracer = trace.get_tracer("google_connector.http")
    with tracer.start_as_current_span(
        "HTTP request", context=parent, kind=SpanKind.SERVER
    ) as span:
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            duration = time.perf_counter() - started
            route = _route_template(request)
            request_count.labels(route).inc()
            request_latency.labels(
                route, request.method, str(status)
            ).observe(duration)
            span.update_name(f"{request.method} {route}")
            span.set_attribute("http.request.method", request.method)
            span.set_attribute("http.route", route)
            span.set_attribute("http.response.status_code", status)
            context = span.get_span_context()
            trace_id = f"{context.trace_id:032x}" if context.is_valid else ""
            # Deliberately exclude query strings, bodies, identities, tokens, and
            # client addresses. Route names and correlation IDs are operational data.
            # Keep correlation IDs in the JSON body, not in the OTLP LogRecord
            # context. Grafana/Loki can otherwise promote per-request trace and
            # span IDs to stream labels, causing unbounded label cardinality.
            log_context = attach(Context())
            try:
                logger.info(json.dumps({
                    "event": "http_request",
                    "request_id": request_id,
                    "trace_id": trace_id,
                    "method": request.method,
                    "path": route,
                    "status": status,
                    "duration_ms": round(duration * 1000, 2),
                }, separators=(",", ":")))
            finally:
                detach(log_context)
