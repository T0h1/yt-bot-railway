"""Rate limiter with Redis backend, in-memory fallback, and daily quota support."""

import time
import asyncio
from typing import Optional
from abc import ABC, abstractmethod

from config import settings, RATE_LIMIT_PER_MINUTE, RATE_LIMIT_PER_HOUR, MAX_DOWNLOADS_PER_USER_PER_DAY
from logging_config import get_logger

logger = get_logger("rate_limiter")


class RateLimiter(ABC):
    @abstractmethod
    async def check(self, user_id: int, limit: int = 5, window: int = 60) -> bool:
        pass

    @abstractmethod
    async def get_remaining(self, user_id: int, limit: int = 5, window: int = 60) -> int:
        pass


class MemoryRateLimiter(RateLimiter):
    def __init__(self):
        self._store: dict[int, list[float]] = {}
        self._daily: dict[int, list[float]] = {}
        self._lock = asyncio.Lock()

    async def check(self, user_id: int, limit: int = 5, window: int = 60) -> bool:
        async with self._lock:
            now = time.time()
            if user_id not in self._store:
                self._store[user_id] = []
            self._store[user_id] = [t for t in self._store[user_id] if now - t < window]
            if len(self._store[user_id]) >= limit:
                return False
            self._store[user_id].append(now)
            return True

    async def get_remaining(self, user_id: int, limit: int = 5, window: int = 60) -> int:
        async with self._lock:
            now = time.time()
            if user_id not in self._store:
                return limit
            self._store[user_id] = [t for t in self._store[user_id] if now - t < window]
            return max(0, limit - len(self._store[user_id]))

    async def check_daily(self, user_id: int, limit: int = 50) -> tuple[bool, int]:
        """Check daily quota. Returns (allowed, remaining)."""
        async with self._lock:
            now = time.time()
            day_start = now - 86400
            if user_id not in self._daily:
                self._daily[user_id] = []
            self._daily[user_id] = [t for t in self._daily[user_id] if t > day_start]
            count = len(self._daily[user_id])
            remaining = max(0, limit - count)
            if count >= limit:
                return False, 0
            self._daily[user_id].append(now)
            return True, remaining - 1


class RedisRateLimiter(RateLimiter):
    def __init__(self, redis_url: str):
        self._redis_url = redis_url
        self._redis: Optional["redis.asyncio.Redis"] = None

    async def _get_redis(self) -> "redis.asyncio.Redis":
        if self._redis is None:
            import redis.asyncio as redis
            self._redis = redis.from_url(self._redis_url, decode_responses=True)
        return self._redis

    async def check(self, user_id: int, limit: int = 5, window: int = 60) -> bool:
        try:
            r = await self._get_redis()
            key = f"ratelimit:{user_id}"
            now = time.time()
            pipe = r.pipeline()
            pipe.zremrangebyscore(key, 0, now - window)
            pipe.zcard(key)
            pipe.zadd(key, {str(now): now})
            pipe.expire(key, window)
            results = await pipe.execute()
            current_count = results[1]
            return current_count < limit
        except Exception as e:
            logger.error("redis_rate_limit_error", error=str(e), user_id=user_id)
            return True

    async def get_remaining(self, user_id: int, limit: int = 5, window: int = 60) -> int:
        try:
            r = await self._get_redis()
            key = f"ratelimit:{user_id}"
            now = time.time()
            await r.zremrangebyscore(key, 0, now - window)
            count = await r.zcard(key)
            return max(0, limit - count)
        except Exception:
            return limit

    async def check_daily(self, user_id: int, limit: int = 50) -> tuple[bool, int]:
        """Check daily quota via Redis. Returns (allowed, remaining)."""
        try:
            r = await self._get_redis()
            today = time.strftime("%Y-%m-%d")
            key = f"daily:{user_id}:{today}"
            count = int(await r.get(key) or 0)
            remaining = max(0, limit - count)
            if count >= limit:
                return False, 0
            pipe = r.pipeline()
            pipe.incr(key)
            pipe.expire(key, 86400)
            await pipe.execute()
            return True, remaining - 1
        except Exception as e:
            logger.error("redis_daily_quota_error", error=str(e), user_id=user_id)
            return True, limit


_rate_limiter: Optional[RateLimiter] = None


async def get_rate_limiter() -> RateLimiter:
    global _rate_limiter
    if _rate_limiter is None:
        if settings.redis_url:
            try:
                _rate_limiter = RedisRateLimiter(settings.redis_url)
                logger.info("rate_limiter_initialized", type="redis")
            except Exception as e:
                logger.warning("redis_rate_limiter_failed_fallback", error=str(e))
                _rate_limiter = MemoryRateLimiter()
        else:
            _rate_limiter = MemoryRateLimiter()
            logger.info("rate_limiter_initialized", type="memory")
    return _rate_limiter


async def check_rate_limit(user_id: int, limit: int = None, window: int = None) -> bool:
    """Check per-minute rate limit. Uses config defaults if not specified."""
    if limit is None:
        limit = RATE_LIMIT_PER_MINUTE
    if window is None:
        window = 60
    limiter = await get_rate_limiter()
    return await limiter.check(user_id, limit, window)


async def check_rate_limit_hourly(user_id: int) -> bool:
    """Check per-hour rate limit."""
    limiter = await get_rate_limiter()
    return await limiter.check(user_id, RATE_LIMIT_PER_HOUR, 3600)


async def check_daily_quota(user_id: int, daily_limit: int = None) -> tuple[bool, int]:
    """Check daily download quota. Returns (allowed, remaining)."""
    if daily_limit is None:
        daily_limit = MAX_DOWNLOADS_PER_USER_PER_DAY
    limiter = await get_rate_limiter()
    return await limiter.check_daily(user_id, daily_limit)


async def get_remaining_requests(user_id: int, limit: int = None, window: int = 60) -> int:
    if limit is None:
        limit = RATE_LIMIT_PER_MINUTE
    limiter = await get_rate_limiter()
    return await limiter.get_remaining(user_id, limit, window)
