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
