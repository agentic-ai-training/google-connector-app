import time

from app.mlops.metrics import request_count


async def metrics_middleware(request, call_next):
    request_count.labels(request.url.path).inc()
    return await call_next(request)
