"""PostgreSQL database layer with asyncpg for persistent storage."""

import asyncio
import logging
from typing import Optional, List, Tuple, Any
from datetime import datetime
from contextlib import asynccontextmanager

import asyncpg
from config import settings
from logging_config import get_logger

logger = get_logger("database")


class Database:
    """Async PostgreSQL database manager."""

    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool: Optional[asyncpg.Pool] = None

    async def connect(self) -> None:
        """Initialize connection pool."""
        self.pool = await asyncpg.create_pool(
            self.dsn,
            min_size=2,
            max_size=10,
            command_timeout=60,
        )
        logger.info("database_pool_created", dsn=self.dsn.split("@")[-1])

    async def close(self) -> None:
        """Close connection pool."""
        if self.pool:
            await self.pool.close()
            logger.info("database_pool_closed")

    @asynccontextmanager
    async def acquire(self):
        """Acquire a connection from the pool."""
        async with self.pool.acquire() as conn:
            yield conn

    async def init_schema(self) -> None:
        """Create tables if they don't exist."""
        async with self.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS download_history (
                    id SERIAL PRIMARY KEY,
                    url TEXT NOT NULL,
                    title TEXT,
                    artist TEXT,
                    album TEXT,
                    platform TEXT,
                    content_type TEXT,
                    status TEXT,
                    file_path TEXT,
                    duration INTEGER,
                    file_size BIGINT,
                    metadata JSONB DEFAULT '{}',
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_download_history_created_at
                ON download_history(created_at DESC)
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS user_states (
                    user_id BIGINT PRIMARY KEY,
                    state TEXT,
                    data JSONB DEFAULT '{}',
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS download_queue (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    url TEXT NOT NULL,
                    content_type TEXT,
                    quality TEXT,
                    priority INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'pending',
                    progress INTEGER DEFAULT 0,
                    message_id BIGINT,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    started_at TIMESTAMPTZ,
                    completed_at TIMESTAMPTZ,
                    error TEXT,
                    retries INTEGER DEFAULT 0,
                    file_path TEXT,
                    metadata JSONB DEFAULT '{}'
                )
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_download_queue_user_status
                ON download_queue(user_id, status)
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS cookies (
                    id SERIAL PRIMARY KEY,
                    platform TEXT NOT NULL UNIQUE,
                    cookie_data TEXT NOT NULL,
                    user_agent TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS playlists (
                    id TEXT PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT,
                    tracks JSONB DEFAULT '[]',
                    is_public BOOLEAN DEFAULT FALSE,
                    share_token TEXT UNIQUE,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_playlists_user_id
                ON playlists(user_id)
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_playlists_share_token
                ON playlists(share_token)
            """)

    async def add_download_history(
        self,
        url: str,
        title: str = None,
        artist: str = None,
        album: str = None,
        platform: str = None,
        content_type: str = None,
        status: str = "completed",
        file_path: str = None,
        duration: int = None,
        file_size: int = None,
        metadata: dict = None,
    ) -> None:
        """Add a download record to history."""
        async with self.acquire() as conn:
            await conn.execute("""
                INSERT INTO download_history (
                    url, title, artist, album, platform, content_type,
                    status, file_path, duration, file_size, metadata
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            """, url, title, artist, album, platform, content_type,
               status, file_path, duration, file_size,
               metadata or {})

    async def get_download_history(
        self,
        user_id: int = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[dict]:
        """Get download history."""
        async with self.acquire() as conn:
            if user_id:
                rows = await conn.fetch("""
                    SELECT * FROM download_history
                    WHERE metadata->>'user_id' = $1
                    ORDER BY created_at DESC
                    LIMIT $2 OFFSET $3
                """, str(user_id), limit, offset)
            else:
                rows = await conn.fetch("""
                    SELECT * FROM download_history
                    ORDER BY created_at DESC
                    LIMIT $1 OFFSET $2
                """, limit, offset)
            return [dict(r) for r in rows]

    async def set_user_state(self, user_id: int, state: str, data: dict = None) -> None:
        """Set user state."""
        async with self.acquire() as conn:
            await conn.execute("""
                INSERT INTO user_states (user_id, state, data, updated_at)
                VALUES ($1, $2, $3, NOW())
                ON CONFLICT (user_id) DO UPDATE SET
                    state = EXCLUDED.state,
                    data = EXCLUDED.data,
                    updated_at = NOW()
            """, user_id, state, data or {})

    async def get_user_state(self, user_id: int) -> Tuple[str, dict]:
        """Get user state."""
        async with self.acquire() as conn:
            row = await conn.fetchrow("SELECT state, data FROM user_states WHERE user_id = $1", user_id)
            if row:
                return row["state"], row["data"]
            return "idle", {}

    async def clear_user_state(self, user_id: int) -> None:
        """Clear user state."""
        async with self.acquire() as conn:
            await conn.execute("DELETE FROM user_states WHERE user_id = $1", user_id)

    # Playlist methods with lazy import to avoid circular dependency
    async def save_playlist(self, playlist) -> None:
        from playlists import Playlist
        async with self.acquire() as conn:
            await conn.execute("""
                INSERT INTO playlists (id, user_id, name, description, tracks, is_public, share_token, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                ON CONFLICT (id) DO UPDATE SET
                    name = EXCLUDED.name,
                    description = EXCLUDED.description,
                    tracks = EXCLUDED.tracks,
                    is_public = EXCLUDED.is_public,
                    share_token = EXCLUDED.share_token,
                    updated_at = NOW()
            """, playlist.id, playlist.user_id, playlist.name, playlist.description, 
                 playlist.tracks, playlist.is_public, playlist.share_token, 
                 datetime.fromtimestamp(playlist.created_at), datetime.fromtimestamp(playlist.updated_at))

    async def get_playlist(self, playlist_id: str):
        from playlists import Playlist
        async with self.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM playlists WHERE id = $1", playlist_id)
            if row:
                return Playlist(
                    id=row["id"],
                    user_id=row["user_id"],
                    name=row["name"],
                    description=row["description"],
                    tracks=row["tracks"],
                    is_public=row["is_public"],
                    share_token=row["share_token"],
                    created_at=row["created_at"].timestamp(),
                    updated_at=row["updated_at"].timestamp()
                )
        return None

    async def get_playlist_by_share_token(self, share_token: str):
        from playlists import Playlist
        async with self.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM playlists WHERE share_token = $1 AND is_public = TRUE", share_token)
            if row:
                return Playlist(
                    id=row["id"],
                    user_id=row["user_id"],
                    name=row["name"],
                    description=row["description"],
                    tracks=row["tracks"],
                    is_public=row["is_public"],
                    share_token=row["share_token"],
                    created_at=row["created_at"].timestamp(),
                    updated_at=row["updated_at"].timestamp()
                )
        return None

    async def get_user_playlists(self, user_id: int) -> list:
        from playlists import Playlist
        async with self.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM playlists WHERE user_id = $1 ORDER BY updated_at DESC", user_id)
            return [Playlist(
                id=r["id"], user_id=r["user_id"], name=r["name"], description=r["description"],
                tracks=r["tracks"], is_public=r["is_public"], share_token=r["share_token"],
                created_at=r["created_at"].timestamp(), updated_at=r["updated_at"].timestamp()
            ) for r in rows]

    async def update_playlist(self, playlist) -> None:
        from playlists import Playlist
        async with self.acquire() as conn:
            await conn.execute("""
                UPDATE playlists SET
                    name = $1, description = $2, tracks = $3, is_public = $4, share_token = $5, updated_at = NOW()
                WHERE id = $6
            """, playlist.name, playlist.description, playlist.tracks, 
                 playlist.is_public, playlist.share_token, playlist.id)

    async def delete_playlist(self, playlist_id: str) -> None:
        async with self.acquire() as conn:
            await conn.execute("DELETE FROM playlists WHERE id = $1", playlist_id)

    async def search_playlists(self, query: str, user_id: int = None) -> list:
        from playlists import Playlist
        async with self.acquire() as conn:
            sql = "SELECT * FROM playlists WHERE (name ILIKE $1 OR description ILIKE $1)"
            params = [f"%{query}%"]
            if user_id:
                sql += " AND user_id = $2"
                params.append(user_id)
            rows = await conn.fetch(sql, *params)
            return [Playlist(
                id=r["id"], user_id=r["user_id"], name=r["name"], description=r["description"],
                tracks=r["tracks"], is_public=r["is_public"], share_token=r["share_token"],
                created_at=r["created_at"].timestamp(), updated_at=r["updated_at"].timestamp()
            ) for r in rows]

    async def get_public_playlists(self, limit: int = 50, offset: int = 0) -> list:
        from playlists import Playlist
        async with self.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM playlists WHERE is_public = TRUE ORDER BY updated_at DESC LIMIT $1 OFFSET $2", limit, offset)
            return [Playlist(
                id=r["id"], user_id=r["user_id"], name=r["name"], description=r["description"],
                tracks=r["tracks"], is_public=r["is_public"], share_token=r["share_token"],
                created_at=r["created_at"].timestamp(), updated_at=r["updated_at"].timestamp()
            ) for r in rows]


# Global database instance
_database: Optional[Database] = None


async def get_database() -> Database:
    """Get or create global database instance."""
    global _database
    if _database is None:
        dsn = settings.postgres_dsn
        if not dsn:
            raise RuntimeError("POSTGRES_DSN not configured")
        _database = Database(dsn)
        await _database.connect()
        await _database.init_schema()
    return _database


async def close_database() -> None:
    """Close global database."""
    global _database
    if _database:
        await _database.close()
        _database = None