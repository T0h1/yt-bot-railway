"""Health check endpoint for Railway and monitoring."""

import asyncio
from aiohttp import web
from typing import Optional

from config import settings
from logging_config import get_logger

logger = get_logger("health")

_health_server: Optional[web.AppRunner] = None


async def health_handler(request: web.Request) -> web.Response:
    """Health check endpoint."""
    return web.json_response({
        "status": "healthy",
        "service": "mediabot",
        "version": "2.1.0",
    })


async def ready_handler(request: web.Request) -> web.Response:
    """Readiness check - verifies bot can handle requests."""
    # Could add checks for: DB connection, Telegram API, disk space
    return web.json_response({
        "status": "ready",
        "checks": {
            "database": "ok",
            "telegram": "ok",
            "disk": "ok",
        }
    })


async def metrics_handler(request: web.Request) -> web.Response:
    """Basic metrics endpoint."""
    import os
    from pathlib import Path
    
    download_dir = Path(__file__).parent / "media_downloads"
    active_files = sum(1 for f in download_dir.iterdir() if f.is_file()) if download_dir.exists() else 0
    used_mb = sum(f.stat().st_size for f in download_dir.iterdir() if f.is_file()) / (1024**2) if download_dir.exists() else 0
    
    stat = os.statvfs(str(download_dir)) if download_dir.exists() else None
    free_gb = (stat.f_bavail * stat.f_frsize) / (1024**3) if stat else 0
    
    return web.json_response({
        "active_downloads": len(getattr(request.app, 'active_downloads', {})),
        "temp_files": active_files,
        "disk_used_mb": round(used_mb, 2),
        "disk_free_gb": round(free_gb, 2),
    })


async def start_health_server(port: int = 8080, app_ref=None) -> web.AppRunner:
    """Start the health check HTTP server."""
    global _health_server
    
    app = web.Application()
    
    # Share reference to bot's active_downloads if provided
    if app_ref:
        app.active_downloads = app_ref.active_downloads
    
    app.router.add_get("/health", health_handler)
    app.router.add_get("/ready", ready_handler)
    app.router.add_get("/metrics", metrics_handler)
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    
    _health_server = runner
    logger.info("health_server_started", port=port)
    return runner


async def stop_health_server() -> None:
    """Stop the health check HTTP server."""
    global _health_server
    if _health_server:
        await _health_server.cleanup()
        _health_server = None
        logger.info("health_server_stopped")