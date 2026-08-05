"""Personal playlists and sharing functionality."""

import os
import json
import asyncio
import logging
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

from logging_config import get_logger

logger = get_logger("playlists")


@dataclass
class Playlist:
    """User playlist."""
    id: str
    user_id: int
    name: str
    description: str = ""
    tracks: List[Dict[str, Any]] = None
    is_public: bool = False
    share_token: str = ""
    created_at: float = 0
    updated_at: float = 0

    def __post_init__(self):
        if self.tracks is None:
            self.tracks = []
        if self.created_at == 0:
            self.created_at = datetime.now().timestamp()
        if self.updated_at == 0:
            self.updated_at = datetime.now().timestamp()


@dataclass
class PlaylistTrack:
    """Track in a playlist."""
    title: str
    artist: str
    url: str
    platform: str = ""
    duration: int = 0
    thumbnail: str = ""
    added_at: float = 0
    track_id: str = ""

    def __post_init__(self):
        if self.added_at == 0:
            self.added_at = datetime.now().timestamp()
        if not self.track_id:
            self.track_id = f"{self.platform}_{hash(self.url) % 1000000}"


class PlaylistManager:
    """Manage user playlists with sharing."""
    
    def __init__(self):
        self.db = None
    
    async def _get_db(self):
        if self.db is None:
            from database import get_database
            self.db = await get_database()
        return self.db
    
    def _require_db(self):
        if self.db is None:
            raise RuntimeError("Playlist features require PostgreSQL database (POSTGRES_DSN not configured)")
        return self.db
    
    async def _get_db_required(self):
        db = await self._get_db_required()
        return self._require_db()
    
    async def create_playlist(
        self,
        user_id: int,
        name: str,
        description: str = "",
        is_public: bool = False
    ) -> Optional[Playlist]:
        """Create a new playlist."""
        db = await self._get_db_required()
        
        import secrets
        playlist_id = f"pl_{user_id}_{secrets.token_urlsafe(8)}"
        share_token = secrets.token_urlsafe(16) if is_public else ""
        
        playlist = Playlist(
            id=playlist_id,
            user_id=user_id,
            name=name,
            description=description,
            is_public=is_public,
            share_token=share_token
        )
        
        try:
            await db.save_playlist(playlist)
            logger.info("playlist_created", playlist_id=playlist_id, user_id=user_id, name=name)
            return playlist
        except Exception as e:
            logger.error("create_playlist_failed", user_id=user_id, error=str(e))
            return None
    
    async def get_playlist(self, playlist_id: str) -> Optional[Playlist]:
        """Get playlist by ID."""
        db = await self._get_db_required()
        return await db.get_playlist(playlist_id)
    
    async def get_playlist_by_share_token(self, share_token: str) -> Optional[Playlist]:
        """Get public playlist by share token."""
        db = await self._get_db_required()
        return await db.get_playlist_by_share_token(share_token)
    
    async def get_user_playlists(self, user_id: int) -> List[Playlist]:
        """Get all playlists for a user."""
        db = await self._get_db_required()
        return await db.get_user_playlists(user_id)
    
    async def update_playlist(
        self,
        playlist_id: str,
        user_id: int,
        name: Optional[str] = None,
        description: Optional[str] = None,
        is_public: Optional[bool] = None
    ) -> bool:
        """Update playlist metadata."""
        db = await self._get_db_required()
        playlist = await db.get_playlist(playlist_id)
        
        if not playlist or playlist.user_id != user_id:
            return False
        
        if name is not None:
            playlist.name = name
        if description is not None:
            playlist.description = description
        if is_public is not None:
            playlist.is_public = is_public
            if is_public and not playlist.share_token:
                import secrets
                playlist.share_token = secrets.token_urlsafe(16)
            elif not is_public:
                playlist.share_token = ""
        
        playlist.updated_at = datetime.now().timestamp()
        
        try:
            await db.update_playlist(playlist)
            logger.info("playlist_updated", playlist_id=playlist_id)
            return True
        except Exception as e:
            logger.error("update_playlist_failed", playlist_id=playlist_id, error=str(e))
            return False
    
    async def delete_playlist(self, playlist_id: str, user_id: int) -> bool:
        """Delete a playlist."""
        db = await self._get_db_required()
        playlist = await db.get_playlist(playlist_id)
        
        if not playlist or playlist.user_id != user_id:
            return False
        
        try:
            await db.delete_playlist(playlist_id)
            logger.info("playlist_deleted", playlist_id=playlist_id)
            return True
        except Exception as e:
            logger.error("delete_playlist_failed", playlist_id=playlist_id, error=str(e))
            return False
    
    async def add_track_to_playlist(
        self,
        playlist_id: str,
        user_id: int,
        title: str,
        artist: str,
        url: str,
        platform: str = "",
        duration: int = 0,
        thumbnail: str = ""
    ) -> bool:
        """Add a track to playlist."""
        db = await self._get_db_required()
        playlist = await db.get_playlist(playlist_id)
        
        if not playlist or playlist.user_id != user_id:
            return False
        
        track = PlaylistTrack(
            title=title,
            artist=artist,
            url=url,
            platform=platform,
            duration=duration,
            thumbnail=thumbnail
        )
        
        # Check for duplicates
        for existing in playlist.tracks:
            if existing.get("url") == url:
                logger.info("track_already_in_playlist", playlist_id=playlist_id, url=url)
                return True  # Already exists, treat as success
        
        playlist.tracks.append(asdict(track))
        playlist.updated_at = datetime.now().timestamp()
        
        try:
            await db.update_playlist(playlist)
            logger.info("track_added_to_playlist", playlist_id=playlist_id, title=title)
            return True
        except Exception as e:
            logger.error("add_track_failed", playlist_id=playlist_id, error=str(e))
            return False
    
    async def remove_track_from_playlist(
        self,
        playlist_id: str,
        user_id: int,
        track_id: str
    ) -> bool:
        """Remove a track from playlist."""
        db = await self._get_db_required()
        playlist = await db.get_playlist(playlist_id)
        
        if not playlist or playlist.user_id != user_id:
            return False
        
        original_count = len(playlist.tracks)
        playlist.tracks = [t for t in playlist.tracks if t.get("track_id") != track_id]
        
        if len(playlist.tracks) == original_count:
            return False  # Track not found
        
        playlist.updated_at = datetime.now().timestamp()
        
        try:
            await db.update_playlist(playlist)
            logger.info("track_removed_from_playlist", playlist_id=playlist_id, track_id=track_id)
            return True
        except Exception as e:
            logger.error("remove_track_failed", playlist_id=playlist_id, error=str(e))
            return False
    
    async def reorder_tracks(
        self,
        playlist_id: str,
        user_id: int,
        track_ids: List[str]
    ) -> bool:
        """Reorder tracks in playlist."""
        db = await self._get_db_required()
        playlist = await db.get_playlist(playlist_id)
        
        if not playlist or playlist.user_id != user_id:
            return False
        
        # Create new ordered list
        track_map = {t.get("track_id"): t for t in playlist.tracks}
        new_tracks = []
        for tid in track_ids:
            if tid in track_map:
                new_tracks.append(track_map[tid])
        
        # Add any tracks not in the new order (at the end)
        for tid, track in track_map.items():
            if tid not in track_ids:
                new_tracks.append(track)
        
        playlist.tracks = new_tracks
        playlist.updated_at = datetime.now().timestamp()
        
        try:
            await db.update_playlist(playlist)
            return True
        except Exception as e:
            logger.error("reorder_tracks_failed", playlist_id=playlist_id, error=str(e))
            return False
    
    async def get_share_url(self, playlist_id: str, base_url: str = "") -> Optional[str]:
        """Get shareable URL for a public playlist."""
        db = await self._get_db_required()
        playlist = await db.get_playlist(playlist_id)
        
        if not playlist or not playlist.is_public or not playlist.share_token:
            return None
        
        if not base_url:
            from config import settings
            base_url = f"https://{settings.railway_public_domain}" if settings.railway_public_domain else ""
        
        return f"{base_url}/playlist/{playlist.share_token}" if base_url else f"/playlist/{playlist.share_token}"
    
    async def export_playlist(self, playlist_id: str, user_id: int) -> Optional[Dict[str, Any]]:
        """Export playlist as JSON."""
        playlist = await self.get_playlist(playlist_id)
        
        if not playlist or playlist.user_id != user_id:
            return None
        
        return {
            "name": playlist.name,
            "description": playlist.description,
            "tracks": playlist.tracks,
            "exported_at": datetime.now().isoformat(),
            "track_count": len(playlist.tracks)
        }
    
    async def import_playlist(
        self,
        user_id: int,
        data: Dict[str, Any],
        name: Optional[str] = None
    ) -> Optional[Playlist]:
        """Import playlist from JSON."""
        playlist_name = name or data.get("name", "Imported Playlist")
        
        playlist = await self.create_playlist(
            user_id=user_id,
            name=playlist_name,
            description=data.get("description", ""),
            is_public=False
        )
        
        if not playlist:
            return None
        
        tracks = data.get("tracks", [])
        for track_data in tracks:
            await self.add_track_to_playlist(
                playlist_id=playlist.id,
                user_id=user_id,
                title=track_data.get("title", ""),
                artist=track_data.get("artist", ""),
                url=track_data.get("url", ""),
                platform=track_data.get("platform", ""),
                duration=track_data.get("duration", 0),
                thumbnail=track_data.get("thumbnail", "")
            )
        
        # Refresh playlist
        return await self.get_playlist(playlist.id)
    
    async def search_playlists(self, query: str, user_id: int = None) -> List[Playlist]:
        """Search playlists by name/description."""
        db = await self._get_db_required()
        return await db.search_playlists(query, user_id)
    
    async def get_public_playlists(self, limit: int = 50, offset: int = 0) -> List[Playlist]:
        """Get public playlists for discovery."""
        db = await self._get_db_required()
        return await db.get_public_playlists(limit, offset)


# Global instance
_playlist_manager: Optional[PlaylistManager] = None


def get_playlist_manager() -> PlaylistManager:
    global _playlist_manager
    if _playlist_manager is None:
        _playlist_manager = PlaylistManager()
    return _playlist_manager