"""
APScheduler-based in-app scheduler for Railway cron jobs.
Replaces the need for Railway native cron (which requires process to exit).
"""

import asyncio
import logging
from datetime import time as dt_time
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger("mediabot.scheduler")

# Global scheduler instance
_scheduler = None


def get_scheduler() -> AsyncIOScheduler:
    """Get or create the global scheduler instance."""
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone="UTC")
    return _scheduler


async def start_scheduler():
    """Start the scheduler and register all jobs."""
    global _scheduler
    _scheduler = get_scheduler()
    
    if _scheduler.running:
        logger.warning("scheduler_already_running")
        return
    
    # Import job functions here to avoid circular imports
    from youtube_downloader_bot import scheduled_cleanup
    from database import get_database
    
    # Register scheduled jobs
    _register_jobs(scheduled_cleanup, get_database)
    
    _scheduler.start()
    logger.info("scheduler_started", jobs=len(_scheduler.get_jobs()))


async def stop_scheduler():
    """Stop the scheduler gracefully."""
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=True)
        logger.info("scheduler_stopped")


def _register_jobs(cleanup_func, get_db_func):
    """Register all scheduled jobs."""
    global _scheduler
    
    # 1. Daily cleanup at 3 AM UTC (4 AM Iran time)
    _scheduler.add_job(
        cleanup_func,
        CronTrigger(hour=3, minute=0, timezone="UTC"),
        id="daily_cleanup",
        name="Daily file cleanup",
        replace_existing=True,
    )
    
    # 2. Database maintenance at 4 AM UTC (if PostgreSQL)
    async def db_maintenance():
        db = await get_db_func()
        if db:
            try:
                async with db.acquire() as conn:
                    # Vacuum analyze for performance
                    await conn.execute("VACUUM ANALYZE")
                logger.info("db_maintenance_completed")
            except Exception as e:
                logger.error("db_maintenance_failed", error=str(e))
    
    _scheduler.add_job(
        db_maintenance,
        CronTrigger(hour=4, minute=0, timezone="UTC"),
        id="db_maintenance",
        name="Database maintenance",
        replace_existing=True,
    )
    
    # 3. Hourly stats update
    async def hourly_stats():
        try:
            from metrics import set_queue_stats
            from download_queue import get_download_queue
            queue = await get_download_queue()
            if queue:
                stats = await queue.get_stats()
                set_queue_stats(stats)
            logger.info("hourly_stats_updated")
        except Exception as e:
            logger.error("hourly_stats_failed", error=str(e))
    
    _scheduler.add_job(
        hourly_stats,
        IntervalTrigger(hours=1),
        id="hourly_stats",
        name="Hourly queue stats",
        replace_existing=True,
    )
    
    # 4. Daily cookie validation at 5 AM UTC
    async def validate_cookies():
        try:
            from cookie_manager import get_cookie_manager
            manager = get_cookie_manager()
            # Check each platform's cookies
            for platform in ["youtube", "soundcloud", "instagram", "tiktok"]:
                cookies = await manager.get_cookies(platform)
                if cookies:
                    logger.info("cookie_validated", platform=platform)
                else:
                    logger.warning("cookie_missing", platform=platform)
        except Exception as e:
            logger.error("cookie_validation_failed", error=str(e))
    
    _scheduler.add_job(
        validate_cookies,
        CronTrigger(hour=5, minute=0, timezone="UTC"),
        id="cookie_validation",
        name="Daily cookie validation",
        replace_existing=True,
    )
    
    # 5. Weekly stats report at Sunday 6 AM UTC
    async def weekly_report():
        try:
            db = await get_db_func()
            if db:
                stats = await db.get_bot_stats()
                logger.info("weekly_report_generated", stats=stats)
                # Could send to admin here
        except Exception as e:
            logger.error("weekly_report_failed", error=str(e))
    
    _scheduler.add_job(
        weekly_report,
        CronTrigger(day_of_week="sun", hour=6, minute=0, timezone="UTC"),
        id="weekly_report",
        name="Weekly stats report",
        replace_existing=True,
    )


def get_scheduler_jobs():
    """Get list of registered jobs for admin dashboard."""
    global _scheduler
    if _scheduler is None:
        return []
    
    jobs = []
    for job in _scheduler.get_jobs():
        jobs.append({
            "id": job.id,
            "name": job.name,
            "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
            "trigger": str(job.trigger),
        })
    return jobs