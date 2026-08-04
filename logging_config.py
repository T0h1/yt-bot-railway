"""Structured logging with structlog and correlation IDs."""

import logging
import sys
import uuid
from contextvars import ContextVar
from typing import Any, Optional

import structlog

# Context variable for correlation ID
correlation_id_var: ContextVar[Optional[str]] = ContextVar("correlation_id", default=None)


def get_correlation_id() -> str:
    """Get or generate correlation ID for current context."""
    cid = correlation_id_var.get()
    if cid is None:
        cid = str(uuid.uuid4())[:8]
        correlation_id_var.set(cid)
    return cid


def set_correlation_id(cid: str) -> None:
    """Set correlation ID for current context."""
    correlation_id_var.set(cid)


def clear_correlation_id() -> None:
    """Clear correlation ID."""
    correlation_id_var.set(None)


def add_correlation_id(logger: Any, method_name: str, event_dict: dict) -> dict:
    """Add correlation ID to log event."""
    cid = correlation_id_var.get()
    if cid:
        event_dict["correlation_id"] = cid
    return event_dict


def setup_logging(
    log_level: str = "INFO",
    json_output: bool = True,
) -> None:
    """Configure structlog with JSON output and correlation IDs."""
    
    # Standard library logging setup
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, log_level.upper(), logging.INFO),
    )
    
    # Shared processors
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        add_correlation_id,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.dev.set_exc_info,
    ]
    
    if json_output:
        # Production: JSON output
        processors = shared_processors + [
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ]
    else:
        # Development: Pretty console output
        processors = shared_processors + [
            structlog.dev.ConsoleRenderer(colors=True),
        ]
    
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, log_level.upper(), logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "mediabot") -> structlog.BoundLogger:
    """Get a structured logger instance."""
    return structlog.get_logger(name)


# Convenience functions for common log patterns
def log_download_started(logger: structlog.BoundLogger, url: str, user_id: int, platform: str) -> None:
    logger.info("download_started", url=url, user_id=user_id, platform=platform)


def log_download_completed(
    logger: structlog.BoundLogger,
    url: str,
    user_id: int,
    title: str,
    file_size_mb: float,
    duration_sec: float,
) -> None:
    logger.info(
        "download_completed",
        url=url,
        user_id=user_id,
        title=title,
        file_size_mb=round(file_size_mb, 2),
        duration_sec=round(duration_sec, 2),
    )


def log_download_failed(
    logger: structlog.BoundLogger,
    url: str,
    user_id: int,
    error: str,
    platform: str = "",
) -> None:
    logger.error("download_failed", url=url, user_id=user_id, error=error, platform=platform)


def log_rate_limited(logger: structlog.BoundLogger, user_id: int, limit: int) -> None:
    logger.warning("rate_limited", user_id=user_id, limit=limit)


def log_cleanup(logger: structlog.BoundLogger, files_removed: int, freed_mb: float) -> None:
    logger.info("cleanup_completed", files_removed=files_removed, freed_mb=round(freed_mb, 2))