import time

from app.mlops.metrics import request_count, request_latency


async def metrics_middleware(request, call_next):
    request_count.labels(request.url.path).inc()
    started = time.perf_counter()
    response = await call_next(request)
    request_latency.labels(
        request.url.path, request.method, str(response.status_code)
    ).observe(time.perf_counter() - started)
    return response
