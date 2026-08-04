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
                CREATE TABLE IF NOT EXISTS user_reactions (
                    id SERIAL PRIMARY KEY,
                    history_id INTEGER REFERENCES download_history(id) ON DELETE CASCADE,
                    user_id BIGINT NOT NULL,
                    action TEXT NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS active_downloads (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    chat_id BIGINT NOT NULL,
                    url TEXT NOT NULL,
                    title TEXT,
                    artist TEXT,
                    platform TEXT,
                    content_type TEXT,
                    status TEXT DEFAULT 'pending',
                    progress REAL DEFAULT 0,
                    file_path TEXT,
                    error TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS cookies (
                    id SERIAL PRIMARY KEY,
                    platform TEXT NOT NULL UNIQUE,
                    cookie_data TEXT NOT NULL,
                    expires_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id BIGINT PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    is_admin BOOLEAN DEFAULT FALSE,
                    is_banned BOOLEAN DEFAULT FALSE,
                    daily_download_limit INTEGER DEFAULT 50,
                    daily_downloads_used INTEGER DEFAULT 0,
                    quota_reset_date DATE DEFAULT CURRENT_DATE,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)

            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_users_quota_reset
                ON users(quota_reset_date)
            """)

            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_download_history_created_at
                ON download_history(created_at DESC)
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_active_downloads_user_id
                ON active_downloads(user_id)
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_active_downloads_status
                ON active_downloads(status)
            """)

            logger.info("database_schema_initialized")

    # --- Download History ---
    async def log_download(
        self,
        url: str,
        title: str,
        artist: str,
        album: str,
        platform: str,
        content_type: str,
        status: str,
        file_path: str = "",
        duration: int = 0,
        file_size: int = 0,
        metadata: Optional[dict] = None,
    ) -> int:
        """Log a download and return history ID."""
        async with self.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO download_history
                (url, title, artist, album, platform, content_type, status, file_path, duration, file_size, metadata)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                RETURNING id
            """, url, title, artist, album, platform, content_type, status, file_path, duration, file_size, metadata or {})
            return row["id"]

    async def get_history(self, limit: int = 10) -> List[dict]:
        async with self.acquire() as conn:
            rows = await conn.fetch("""
                SELECT id, title, artist, platform, content_type, status, created_at
                FROM download_history
                ORDER BY id DESC
                LIMIT $1
            """, limit)
            return [dict(r) for r in rows]

    async def get_favorites(self, limit: int = 20) -> List[dict]:
        async with self.acquire() as conn:
            rows = await conn.fetch("""
                SELECT dh.id, dh.title, dh.artist, dh.url, dh.created_at
                FROM download_history dh
                JOIN user_reactions ur ON dh.id = ur.history_id
                WHERE ur.action = 'like'
                ORDER BY ur.id DESC
                LIMIT $1
            """, limit)
            return [dict(r) for r in rows]

    async def get_albums(self, limit: int = 20) -> List[dict]:
        async with self.acquire() as conn:
            rows = await conn.fetch("""
                SELECT artist, album, COUNT(*) as cnt
                FROM download_history
                WHERE artist != '' AND album != ''
                GROUP BY artist, album
                ORDER BY cnt DESC
                LIMIT $1
            """, limit)
            return [dict(r) for r in rows]

    async def get_logs(self, limit: int = 15) -> List[dict]:
        async with self.acquire() as conn:
            rows = await conn.fetch("""
                SELECT title, platform, status, created_at
                FROM download_history
                ORDER BY id DESC
                LIMIT $1
            """, limit)
            return [dict(r) for r in rows]

    async def get_download_file(self, history_id: int) -> Optional[dict]:
        async with self.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT file_path, title, url
                FROM download_history
                WHERE id = $1
            """, history_id)
            return dict(row) if row else None

    async def get_stats(self) -> dict:
        async with self.acquire() as conn:
            total = await conn.fetchval("SELECT COUNT(*) FROM download_history")
            success = await conn.fetchval("SELECT COUNT(*) FROM download_history WHERE status='success'")
            likes = await conn.fetchval("SELECT COUNT(*) FROM user_reactions WHERE action='like'")
            return {"total": total, "success": success, "likes": likes}

    # --- Reactions ---
    async def log_reaction(self, history_id: int, user_id: int, action: str) -> None:
        async with self.acquire() as conn:
            await conn.execute("""
                INSERT INTO user_reactions (history_id, user_id, action)
                VALUES ($1, $2, $3)
            """, history_id, user_id, action)

    # --- Active Downloads (Persistent Queue) ---
    async def create_active_download(
        self,
        user_id: int,
        chat_id: int,
        url: str,
        title: str = "",
        artist: str = "",
        platform: str = "",
        content_type: str = "",
    ) -> int:
        async with self.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO active_downloads
                (user_id, chat_id, url, title, artist, platform, content_type, status)
                VALUES ($1, $2, $3, $4, $5, $6, $7, 'pending')
                RETURNING id
            """, user_id, chat_id, url, title, artist, platform, content_type)
            return row["id"]

    async def update_active_download(self, download_id: int, **kwargs) -> None:
        """Update active download fields. Allowed: status, progress, file_path, error, title, artist."""
        allowed = {"status", "progress", "file_path", "error", "title", "artist"}
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        if not fields:
            return
        set_clause = ", ".join(f"{k} = ${i+2}" for i, k in enumerate(fields.keys()))
        values = list(fields.values())
        async with self.acquire() as conn:
            await conn.execute(f"""
                UPDATE active_downloads
                SET {set_clause}, updated_at = NOW()
                WHERE id = $1
            """, download_id, *values)

    async def get_active_downloads(self, user_id: Optional[int] = None) -> List[dict]:
        async with self.acquire() as conn:
            if user_id:
                rows = await conn.fetch("""
                    SELECT * FROM active_downloads
                    WHERE user_id = $1 AND status IN ('pending', 'downloading', 'processing')
                    ORDER BY created_at
                """, user_id)
            else:
                rows = await conn.fetch("""
                    SELECT * FROM active_downloads
                    WHERE status IN ('pending', 'downloading', 'processing')
                    ORDER BY created_at
                """)
            return [dict(r) for r in rows]

    async def get_pending_downloads(self, limit: int = 10) -> List[dict]:
        async with self.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM active_downloads
                WHERE status = 'pending'
                ORDER BY created_at
                LIMIT $1
            """, limit)
            return [dict(r) for r in rows]

    async def cleanup_stale_downloads(self, max_age_hours: int = 2) -> int:
        """Mark stale downloads as failed."""
        async with self.acquire() as conn:
            result = await conn.execute("""
                UPDATE active_downloads
                SET status = 'failed', error = 'stale_timeout', updated_at = NOW()
                WHERE status IN ('pending', 'downloading', 'processing')
                AND updated_at < NOW() - INTERVAL '%s hours'
            """ % max_age_hours)
            return int(result.split()[-1]) if result else 0

    # --- Cookies ---
    async def save_cookies(self, platform: str, cookie_data: str, expires_at: Optional[datetime] = None) -> None:
        async with self.acquire() as conn:
            await conn.execute("""
                INSERT INTO cookies (platform, cookie_data, expires_at, updated_at)
                VALUES ($1, $2, $3, NOW())
                ON CONFLICT (platform) DO UPDATE SET
                    cookie_data = EXCLUDED.cookie_data,
                    expires_at = EXCLUDED.expires_at,
                    updated_at = NOW()
            """, platform, cookie_data, expires_at)

    async def get_cookies(self, platform: str) -> Optional[str]:
        async with self.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT cookie_data FROM cookies
                WHERE platform = $1 AND (expires_at IS NULL OR expires_at > NOW())
            """, platform)
            return row["cookie_data"] if row else None

    async def list_cookies(self) -> List[dict]:
        async with self.acquire() as conn:
            rows = await conn.fetch("""
                SELECT platform, expires_at, created_at, updated_at
                FROM cookies
                ORDER BY platform
            """)
            return [dict(r) for r in rows]

    # --- Users (Multi-user with quotas) ---
    async def get_or_create_user(self, user_id: int, username: str = "", first_name: str = "", last_name: str = "") -> dict:
        """Get user or create if not exists."""
        async with self.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
            if row:
                return dict(row)
            # Create new user
            await conn.execute("""
                INSERT INTO users (id, username, first_name, last_name)
                VALUES ($1, $2, $3, $4)
            """, user_id, username, first_name, last_name)
            return await conn.fetchrow("SELECT * FROM users WHERE id = $1", user_id)

    async def check_user_quota(self, user_id: int) -> tuple[bool, int]:
        """Check if user has quota remaining. Returns (allowed, remaining)."""
        async with self.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
            if not row:
                # Create user with default quota
                await conn.execute("""
                    INSERT INTO users (id, daily_download_limit)
                    VALUES ($1, 50)
                """, user_id)
                return True, 50
            
            # Check if quota reset date has passed
            if row["quota_reset_date"] < datetime.now().date():
                await conn.execute("""
                    UPDATE users SET daily_downloads_used = 0, quota_reset_date = CURRENT_DATE
                    WHERE id = $1
                """, user_id)
                return True, row["daily_download_limit"]
            
            remaining = row["daily_download_limit"] - row["daily_downloads_used"]
            return remaining > 0, max(0, remaining)

    async def increment_user_downloads(self, user_id: int) -> None:
        """Increment user's daily download count."""
        async with self.acquire() as conn:
            await conn.execute("""
                UPDATE users SET daily_downloads_used = daily_downloads_used + 1, updated_at = NOW()
                WHERE id = $1
            """, user_id)

    async def set_user_admin(self, user_id: int, is_admin: bool = True) -> None:
        """Set user admin status."""
        async with self.acquire() as conn:
            await conn.execute("""
                UPDATE users SET is_admin = $1, updated_at = NOW() WHERE id = $2
            """, is_admin, user_id)

    async def set_user_ban(self, user_id: int, is_banned: bool = True) -> None:
        """Ban/unban user."""
        async with self.acquire() as conn:
            await conn.execute("""
                UPDATE users SET is_banned = $1, updated_at = NOW() WHERE id = $2
            """, is_banned, user_id)

    async def set_user_quota(self, user_id: int, limit: int) -> None:
        """Set user's daily download limit."""
        async with self.acquire() as conn:
            await conn.execute("""
                UPDATE users SET daily_download_limit = $1, updated_at = NOW() WHERE id = $2
            """, limit, user_id)

    async def get_all_users(self) -> List[dict]:
        """Get all users for admin dashboard."""
        async with self.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM users ORDER BY created_at DESC")
            return [dict(r) for r in rows]

    async def save_playlist(self, playlist: Playlist) -> None:
        """Save or update a playlist."""
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

    async def get_playlist(self, playlist_id: str) -> Optional[Playlist]:
        """Get playlist by ID."""
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

    async def get_playlist_by_share_token(self, share_token: str) -> Optional[Playlist]:
        """Get public playlist by share token."""
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

    async def get_user_playlists(self, user_id: int) -> List[Playlist]:
        """Get all playlists for a user."""
        async with self.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM playlists WHERE user_id = $1 ORDER BY updated_at DESC", user_id)
            return [Playlist(
                id=r["id"], user_id=r["user_id"], name=r["name"], description=r["description"],
                tracks=r["tracks"], is_public=r["is_public"], share_token=r["share_token"],
                created_at=r["created_at"].timestamp(), updated_at=r["updated_at"].timestamp()
            ) for r in rows]

    async def update_playlist(self, playlist: Playlist) -> None:
        """Update existing playlist."""
        async with self.acquire() as conn:
            await conn.execute("""
                UPDATE playlists SET
                    name = $1, description = $2, tracks = $3, is_public = $4, share_token = $5, updated_at = NOW()
                WHERE id = $6
            """, playlist.name, playlist.description, playlist.tracks, 
                 playlist.is_public, playlist.share_token, playlist.id)

    async def delete_playlist(self, playlist_id: str) -> None:
        """Delete a playlist."""
        async with self.acquire() as conn:
            await conn.execute("DELETE FROM playlists WHERE id = $1", playlist_id)

    async def search_playlists(self, query: str, user_id: Optional[int] = None) -> List[Playlist]:
        """Search playlists."""
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

    async def get_public_playlists(self, limit: int = 50, offset: int = 0) -> List[Playlist]:
        """Get public playlists."""
        async with self.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM playlists WHERE is_public = TRUE ORDER BY updated_at DESC LIMIT $1 OFFSET $2", limit, offset)
            return [Playlist(
                id=r["id"], user_id=r["user_id"], name=r["name"], description=r["description"],
                tracks=r["tracks"], is_public=r["is_public"], share_token=r["share_token"],
                created_at=r["created_at"].timestamp(), updated_at=r["updated_at"].timestamp()
            ) for r in rows]


"""Helper methods for Database to support Playlists."""

import asyncio
from typing import Optional, List, Any
from datetime import datetime
from playlists import Playlist

# These methods should be added to the Database class in database.py
# using the patch tool to insert them before the Global database instance definition.

async def save_playlist(self, playlist: Playlist) -> None:
    """Save or update a playlist."""
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

async def get_playlist(self, playlist_id: str) -> Optional[Playlist]:
    """Get playlist by ID."""
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

async def get_playlist_by_share_token(self, share_token: str) -> Optional[Playlist]:
    """Get public playlist by share token."""
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

async def get_user_playlists(self, user_id: int) -> List[Playlist]:
    """Get all playlists for a user."""
    async with self.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM playlists WHERE user_id = $1 ORDER BY updated_at DESC", user_id)
        return [Playlist(
            id=r["id"], user_id=r["user_id"], name=r["name"], description=r["description"],
            tracks=r["tracks"], is_public=r["is_public"], share_token=r["share_token"],
            created_at=r["created_at"].timestamp(), updated_at=r["updated_at"].timestamp()
        ) for r in rows]

async def update_playlist(self, playlist: Playlist) -> None:
    """Update existing playlist."""
    async with self.acquire() as conn:
        await conn.execute("""
            UPDATE playlists SET
                name = $1, description = $2, tracks = $3, is_public = $4, share_token = $5, updated_at = NOW()
            WHERE id = $6
        """, playlist.name, playlist.description, playlist.tracks, 
             playlist.is_public, playlist.share_token, playlist.id)

async def delete_playlist(self, playlist_id: str) -> None:
    """Delete a playlist."""
    async with self.acquire() as conn:
        await conn.execute("DELETE FROM playlists WHERE id = $1", playlist_id)

async def search_playlists(self, query: str, user_id: Optional[int] = None) -> List[Playlist]:
    """Search playlists."""
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

async def get_public_playlists(self, limit: int = 50, offset: int = 0) -> List[Playlist]:
    """Get public playlists."""
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