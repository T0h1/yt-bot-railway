"""Persistent download queue and worker with Redis."""

import asyncio
import json
import time
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, asdict
from enum import Enum

import redis.asyncio as redis
from config import settings
from logging_config import get_logger

logger = get_logger("download_queue")


class DownloadStatus(str, Enum):
    PENDING = "pending"
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class DownloadTask:
    """Represents a download task in the queue."""
    id: str
    user_id: int
    chat_id: int
    url: str
    title: str = ""
    artist: str = ""
    platform: str = ""
    content_type: str = "audio"
    quality: str = "best"
    priority: int = 0  # Higher = more urgent
    status: DownloadStatus = DownloadStatus.PENDING
    progress: float = 0.0
    error: str = ""
    file_path: str = ""
    metadata: Dict[str, Any] = None
    created_at: float = None
    updated_at: float = None
    attempts: int = 0
    max_attempts: int = 3

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = time.time()
        if self.updated_at is None:
            self.updated_at = time.time()
        if self.metadata is None:
            self.metadata = {}

    def to_json(self) -> str:
        data = asdict(self)
        data["status"] = self.status.value
        return json.dumps(data)

    @classmethod
    def from_json(cls, data: str) -> "DownloadTask":
        d = json.loads(data)
        d["status"] = DownloadStatus(d["status"])
        return cls(**d)


class DownloadQueue:
    """Redis-backed download queue with priority support."""

    QUEUE_KEY = "download_queue:pending"
    PROCESSING_KEY = "download_queue:processing"
    TASK_PREFIX = "download_task:"
    WORKER_HEARTBEAT_KEY = "download_worker:heartbeat"

    def __init__(self, redis_url: str):
        self.redis_url = redis_url
        self._redis: Optional[redis.Redis] = None
        self._worker_id = f"worker-{time.time()}"

    async def _get_redis(self) -> redis.Redis:
        if self._redis is None:
            self._redis = redis.from_url(self.redis_url, decode_responses=True)
        return self._redis

    async def close(self) -> None:
        if self._redis:
            await self._redis.close()
            self._redis = None

    async def enqueue(self, task: DownloadTask) -> str:
        """Add a task to the queue with priority."""
        r = await self._get_redis()
        task_key = f"{self.TASK_PREFIX}{task.id}"

        # Store task data
        await r.set(task_key, task.to_json())

        # Add to sorted set with negative priority for max-heap behavior
        await r.zadd(self.QUEUE_KEY, {task.id: -task.priority})

        logger.info("task_enqueued", task_id=task.id, user_id=task.user_id, priority=task.priority)
        return task.id

    async def dequeue(self, worker_id: str, count: int = 1) -> List[DownloadTask]:
        """Atomically move tasks from pending to processing."""
        r = await self._get_redis()
        tasks = []

        for _ in range(count):
            # Lua script for atomic pop
            lua_script = """
            local task_id = redis.call('ZPOPMIN', KEYS[1])
            if task_id then
                redis.call('ZADD', KEYS[2], ARGV[1], task_id[1])
                return task_id[1]
            end
            return nil
            """
            script = r.register_script(lua_script)
            task_id = await script(keys=[self.QUEUE_KEY, self.PROCESSING_KEY], args=[worker_id])

            if task_id:
                task_key = f"{self.TASK_PREFIX}{task_id}"
                task_data = await r.get(task_key)
                if task_data:
                    task = DownloadTask.from_json(task_data)
                    task.status = DownloadStatus.DOWNLOADING
                    task.updated_at = time.time()
                    await r.set(task_key, task.to_json())
                    tasks.append(task)
                else:
                    # Orphaned task_id in processing set, remove it
                    await r.zrem(self.PROCESSING_KEY, task_id)

        return tasks

    async def update_task(self, task: DownloadTask) -> None:
        """Update task status and progress."""
        r = await self._get_redis()
        task.updated_at = time.time()
        task_key = f"{self.TASK_PREFIX}{task.id}"
        await r.set(task_key, task.to_json())

        # If completed/failed, move from processing
        if task.status in (DownloadStatus.COMPLETED, DownloadStatus.FAILED, DownloadStatus.CANCELLED):
            await r.zrem(self.PROCESSING_KEY, task.id)
            # Keep task data for 24h for result retrieval
            await r.expire(task_key, 86400)

    async def get_task(self, task_id: str) -> Optional[DownloadTask]:
        """Get task by ID."""
        r = await self._get_redis()
        task_key = f"{self.TASK_PREFIX}{task_id}"
        task_data = await r.get(task_key)
        if task_data:
            return DownloadTask.from_json(task_data)
        return None

    async def get_queue_stats(self) -> Dict[str, int]:
        """Get queue statistics."""
        r = await self._get_redis()
        pending = await r.zcard(self.QUEUE_KEY)
        processing = await r.zcard(self.PROCESSING_KEY)
        return {"pending": pending, "processing": processing}

    async def requeue_stale_tasks(self, max_age_seconds: int = 300) -> int:
        """Requeue tasks that have been processing too long."""
        r = await self._get_redis()
        now = time.time()
        stale_tasks = await r.zrangebyscore(self.PROCESSING_KEY, 0, now - max_age_seconds)

        count = 0
        for task_id in stale_tasks:
            task = await self.get_task(task_id)
            if task and task.attempts < task.max_attempts:
                task.attempts += 1
                task.status = DownloadStatus.PENDING
                task.error = f"Requeued after timeout (attempt {task.attempts})"
                await self.update_task(task)
                await self.enqueue(task)
                count += 1
            elif task:
                task.status = DownloadStatus.FAILED
                task.error = "Max attempts exceeded"
                await self.update_task(task)

        if count > 0:
            logger.info("stale_tasks_requeued", count=count)

        return count

    async def worker_heartbeat(self) -> None:
        """Register worker heartbeat."""
        r = await self._get_redis()
        await r.hset(self.WORKER_HEARTBEAT_KEY, self._worker_id, str(time.time()))
        await r.expire(self.WORKER_HEARTBEAT_KEY, 60)

    async def get_active_workers(self) -> List[str]:
        """Get list of active workers."""
        r = await self._get_redis()
        now = time.time()
        workers = await r.hgetall(self.WORKER_HEARTBEAT_KEY)
        return [w for w, ts in workers.items() if now - float(ts) < 30]

    async def get_processing_tasks(self) -> List[DownloadTask]:
        """Get all tasks currently in processing."""
        r = await self._get_redis()
        task_ids = await r.zrange(self.PROCESSING_KEY, 0, -1)
        tasks = []
        for task_id in task_ids:
            task = await self.get_task(task_id)
            if task:
                tasks.append(task)
        return tasks

    async def get_pending_tasks(self, limit: int = 50) -> List[DownloadTask]:
        """Get pending tasks from queue."""
        r = await self._get_redis()
        task_ids = await r.zrange(self.QUEUE_KEY, 0, limit - 1)
        tasks = []
        for task_id in task_ids:
            task = await self.get_task(task_id)
            if task:
                tasks.append(task)
        return tasks

    async def start_worker(self, process_task_func) -> None:
        """Start the background worker to process queued tasks."""
        self._worker_task = asyncio.create_task(self._worker_loop(process_task_func))
        logger.info("download_worker_started", worker_id=self._worker_id)

    async def stop_worker(self) -> None:
        """Stop the background worker."""
        if hasattr(self, '_worker_task') and self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        logger.info("download_worker_stopped", worker_id=self._worker_id)

    async def _worker_loop(self, process_task_func) -> None:
        """Main worker loop - processes tasks from the queue."""
        while True:
            try:
                # Heartbeat
                await self.worker_heartbeat()
                
                # Requeue stale tasks
                await self.requeue_stale_tasks(300)
                
                # Dequeue tasks (max 1 at a time per worker)
                tasks = await self.dequeue(self._worker_id, count=1)
                
                for task in tasks:
                    logger.info("worker_processing_task", task_id=task.id, url=task.url)
                    try:
                        # Process the task
                        await process_task_func(task)
                        task.status = DownloadStatus.COMPLETED
                        task.progress = 100.0
                    except Exception as e:
                        task.attempts += 1
                        task.error = str(e)
                        if task.attempts >= task.max_attempts:
                            task.status = DownloadStatus.FAILED
                            logger.error("task_failed_max_attempts", task_id=task.id, error=str(e))
                        else:
                            task.status = DownloadStatus.PENDING
                            logger.warning("task_failed_requeuing", task_id=task.id, attempt=task.attempts, error=str(e))
                    finally:
                        await self.update_task(task)
                
                # Small delay to prevent tight loop
                await asyncio.sleep(2)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("worker_loop_error", error=str(e))
                await asyncio.sleep(5)


# Global queue instance
_download_queue: Optional[DownloadQueue] = None


async def get_download_queue() -> DownloadQueue:
    global _download_queue
    if _download_queue is None:
        if not settings.redis_url:
            raise RuntimeError("REDIS_URL not configured")
        _download_queue = DownloadQueue(settings.redis_url)
    return _download_queue


async def close_download_queue() -> None:
    global _download_queue
    if _download_queue:
        await _download_queue.close()
        _download_queue = None