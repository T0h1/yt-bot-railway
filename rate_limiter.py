"""Rate limiter with Redis backend and in-memory fallback."""

import time
import asyncio
from typing import Optional
from abc import ABC, abstractmethod

from config import settings
from logging_config import get_logger

logger = get_logger("rate_limiter")


class RateLimiter(ABC):
    """Abstract rate limiter interface."""
    
    @abstractmethod
    async def check(self, user_id: int, limit: int = 5, window: int = 60) -> bool:
        """Check if user is within rate limit. Returns True if allowed."""
        pass
    
    @abstractmethod
    async def get_remaining(self, user_id: int, limit: int = 5, window: int = 60) -> int:
        """Get remaining requests for user."""
        pass


class MemoryRateLimiter(RateLimiter):
    """In-memory rate limiter (fallback)."""
    
    def __init__(self):
        self._store: dict[int, list[float]] = {}
        self._lock = asyncio.Lock()
    
    async def check(self, user_id: int, limit: int = 5, window: int = 60) -> bool:
        async with self._lock:
            now = time.time()
            if user_id not in self._store:
                self._store[user_id] = []
            # Clean old entries
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


class RedisRateLimiter(RateLimiter):
    """Redis-backed rate limiter for multi-instance deployments."""
    
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
            # Fail open - allow request if Redis is down
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


# Global rate limiter instance
_rate_limiter: Optional[RateLimiter] = None


async def get_rate_limiter() -> RateLimiter:
    """Get or create the global rate limiter."""
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


async def check_rate_limit(user_id: int, limit: int = 5, window: int = 60) -> bool:
    """Convenience function to check rate limit."""
    limiter = await get_rate_limiter()
    return await limiter.check(user_id, limit, window)


async def get_remaining_requests(user_id: int, limit: int = 5, window: int = 60) -> int:
    """Convenience function to get remaining requests."""
    limiter = await get_rate_limiter()
    return await limiter.get_remaining(user_id, limit, window)