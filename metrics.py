"""Prometheus metrics for monitoring."""

import time
from typing import Dict
from functools import wraps

from prometheus_client import Counter, Histogram, Gauge, CollectorRegistry, generate_latest, CONTENT_TYPE_LATEST
from aiohttp import web

from config import settings
from logging_config import get_logger

logger = get_logger("metrics")

# Custom registry to avoid conflicts
REGISTRY = CollectorRegistry()

# Counters
downloads_total = Counter(
    "mediabot_downloads_total",
    "Total number of downloads",
    ["platform", "content_type", "status"],
    registry=REGISTRY
)

download_duration_seconds = Histogram(
    "mediabot_download_duration_seconds",
    "Download duration in seconds",
    ["platform", "content_type"],
    buckets=[1, 5, 10, 30, 60, 120, 300, 600],
    registry=REGISTRY
)

download_errors_total = Counter(
    "mediabot_download_errors_total",
    "Total number of download errors",
    ["platform", "error_type"],
    registry=REGISTRY
)

active_downloads_gauge = Gauge(
    "mediabot_active_downloads",
    "Number of currently active downloads",
    registry=REGISTRY
)

queue_size_gauge = Gauge(
    "mediabot_queue_size",
    "Number of tasks in download queue",
    ["status"],  # pending, processing
    registry=REGISTRY
)

rate_limit_hits_total = Counter(
    "mediabot_rate_limit_hits_total",
    "Total number of rate limit hits",
    ["user_id"],
    registry=REGISTRY
)

api_requests_total = Counter(
    "mediabot_api_requests_total",
    "Total number of API requests",
    ["endpoint", "method", "status"],
    registry=REGISTRY
)

api_request_duration_seconds = Histogram(
    "mediabot_api_request_duration_seconds",
    "API request duration in seconds",
    ["endpoint", "method"],
    buckets=[0.01, 0.05, 0.1, 0.5, 1, 5],
    registry=REGISTRY
)

db_queries_total = Counter(
    "mediabot_db_queries_total",
    "Total number of database queries",
    ["operation", "status"],
    registry=REGISTRY
)

db_query_duration_seconds = Histogram(
    "mediabot_db_query_duration_seconds",
    "Database query duration in seconds",
    ["operation"],
    buckets=[0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1],
    registry=REGISTRY
)

redis_operations_total = Counter(
    "mediabot_redis_operations_total",
    "Total number of Redis operations",
    ["operation", "status"],
    registry=REGISTRY
)

# Info metric
build_info = Gauge(
    "mediabot_build_info",
    "Build information",
    ["version", "python_version", "railway_mode"],
    registry=REGISTRY
)


def init_metrics(version: str = "2.0") -> None:
    """Initialize build info metric."""
    import sys
    build_info.labels(
        version=version,
        python_version=f"{sys.version_info.major}.{sys.version_info.minor}",
        railway_mode=str(settings.railway_mode).lower()
    ).set(1)


def record_download(platform: str, content_type: str, status: str, duration: float) -> None:
    """Record a download metric."""
    downloads_total.labels(platform=platform, content_type=content_type, status=status).inc()
    download_duration_seconds.labels(platform=platform, content_type=content_type).observe(duration)


def record_error(platform: str, error_type: str) -> None:
    """Record a download error."""
    download_errors_total.labels(platform=platform, error_type=error_type).inc()


def set_active_downloads(count: int) -> None:
    """Set active downloads gauge."""
    active_downloads_gauge.set(count)


def set_queue_stats(pending: int, processing: int) -> None:
    """Set queue size gauges."""
    queue_size_gauge.labels(status="pending").set(pending)
    queue_size_gauge.labels(status="processing").set(processing)


def record_rate_limit(user_id: int) -> None:
    """Record rate limit hit."""
    rate_limit_hits_total.labels(user_id=str(user_id)).inc()


def record_api_request(endpoint: str, method: str, status: int, duration: float) -> None:
    """Record API request metric."""
    api_requests_total.labels(endpoint=endpoint, method=method, status=str(status)).inc()
    api_request_duration_seconds.labels(endpoint=endpoint, method=method).observe(duration)


def record_db_query(operation: str, status: str, duration: float) -> None:
    """Record database query metric."""
    db_queries_total.labels(operation=operation, status=status).inc()
    db_query_duration_seconds.labels(operation=operation).observe(duration)


def record_redis_operation(operation: str, status: str) -> None:
    """Record Redis operation metric."""
    redis_operations_total.labels(operation=operation, status=status).inc()


# Decorator for timing
def time_it(metric_func, *labels):
    """Decorator to time a function and record metric."""
    def decorator(func):
        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            start = time.time()
            try:
                result = await func(*args, **kwargs)
                metric_func(*labels, "success", time.time() - start)
                return result
            except Exception as e:
                metric_func(*labels, "error", time.time() - start)
                raise
        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            start = time.time()
            try:
                result = func(*args, **kwargs)
                metric_func(*labels, "success", time.time() - start)
                return result
            except Exception as e:
                metric_func(*labels, "error", time.time() - start)
                raise
        import asyncio
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper
    return decorator


# Health check for metrics endpoint
async def metrics_handler(request: web.Request) -> web.Response:
    """Prometheus metrics endpoint."""
    start = time.time()
    try:
        data = generate_latest(REGISTRY)
        record_api_request("metrics", "GET", 200, time.time() - start)
        return web.Response(body=data, content_type=CONTENT_TYPE_LATEST)
    except Exception as e:
        record_api_request("metrics", "GET", 500, time.time() - start)
        logger.error("metrics_error", error=str(e))
        return web.Response(text="Error generating metrics", status=500)


def setup_metrics_app() -> web.Application:
    """Create aiohttp app with metrics endpoint."""
    app = web.Application()
    app.router.add_get("/metrics", metrics_handler)
    app.router.add_get("/health", lambda r: web.Response(text="OK"))
    return app