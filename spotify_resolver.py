"""Spotify resolution service - converts Spotify URLs to playable media URLs."""

import re
import asyncio
from typing import Optional, List, Dict, Any
from urllib.parse import urlparse

from config import settings
from logging_config import get_logger
from yt_dlp_async import search_async

logger = get_logger("spotify")


class SpotifyResolver:
    """Resolve Spotify URLs to YouTube/SoundCloud playable URLs."""
    
    def __init__(self):
        self._sp = None
        self._enabled = bool(settings.spotify_client_id and settings.spotify_client_secret)
    
    def _get_client(self):
        """Lazy initialize Spotify client."""
        if self._sp is None and self._enabled:
            try:
                import spotipy
                from spotipy.oauth2 import SpotifyClientCredentials
                
                auth = SpotifyClientCredentials(
                    client_id=settings.spotify_client_id,
                    client_secret=settings.spotify_client_secret
                )
                self._sp = spotipy.Spotify(auth_manager=auth)
                logger.info("spotify_client_initialized")
            except Exception as e:
                logger.error("spotify_client_init_failed", error=str(e))
                self._enabled = False
        return self._sp
    
    def is_spotify_url(self, url: str) -> bool:
        """Check if URL is a Spotify URL."""
        parsed = urlparse(url)
        return parsed.netloc in ('open.spotify.com', 'spotify.link', 'spoti.fi')
    
    def get_spotify_type(self, url: str) -> Optional[str]:
        """Get Spotify content type: track, album, playlist, artist."""
        if 'spotify.link' in url or 'spoti.fi' in url:
            # Short URLs - need to resolve first
            return 'unknown'
        
        parsed = urlparse(url)
        path = parsed.path
        
        if '/track/' in path:
            return 'track'
        elif '/album/' in path:
            return 'album'
        elif '/playlist/' in path:
            return 'playlist'
        elif '/artist/' in path:
            return 'artist'
        return None
    
    def extract_spotify_id(self, url: str) -> Optional[str]:
        """Extract Spotify ID from URL."""
        # Handle short URLs
        if 'spotify.link' in url or 'spoti.fi' in url:
            return None  # Need to resolve first
        
        patterns = {
            'track': r'/track/([a-zA-Z0-9]+)',
            'album': r'/album/([a-zA-Z0-9]+)',
            'playlist': r'/playlist/([a-zA-Z0-9]+)',
            'artist': r'/artist/([a-zA-Z0-9]+)',
        }
        
        for _, pattern in patterns.items():
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        return None
    
    async def resolve_short_url(self, url: str) -> str:
        """Resolve short Spotify URL to full URL."""
        if 'spotify.link' not in url and 'spoti.fi' not in url:
            return url
        
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.head(url, allow_redirects=True, timeout=10) as resp:
                    return str(resp.url)
        except Exception as e:
            logger.error("spotify_short_url_resolve_failed", url=url, error=str(e))
            return url
    
    async def get_track_info(self, track_id: str) -> Optional[Dict[str, Any]]:
        """Get track metadata from Spotify."""
        sp = self._get_client()
        if not sp:
            return None
        
        try:
            track = await asyncio.get_event_loop().run_in_executor(
                None, lambda: sp.track(track_id)
            )
            if not track:
                return None
            
            artists = ', '.join(a['name'] for a in track['artists'])
            return {
                'type': 'track',
                'title': track['name'],
                'artist': artists,
                'album': track['album']['name'],
                'duration_ms': track['duration_ms'],
                'isrc': track.get('external_ids', {}).get('isrc'),
                'spotify_url': track['external_urls']['spotify'],
            }
        except Exception as e:
            logger.error("spotify_track_fetch_failed", track_id=track_id, error=str(e))
            return None
    
    async def get_album_tracks(self, album_id: str) -> List[Dict[str, Any]]:
        """Get all tracks from a Spotify album."""
        sp = self._get_client()
        if not sp:
            return []
        
        tracks = []
        try:
            results = await asyncio.get_event_loop().run_in_executor(
                None, lambda: sp.album_tracks(album_id)
            )
            
            while results:
                for item in results['items']:
                    artists = ', '.join(a['name'] for a in item['artists'])
                    tracks.append({
                        'title': item['name'],
                        'artist': artists,
                        'duration_ms': item['duration_ms'],
                        'spotify_url': item['external_urls']['spotify'],
                    })
                if results['next']:
                    results = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: sp.next(results)
                    )
                else:
                    break
        except Exception as e:
            logger.error("spotify_album_fetch_failed", album_id=album_id, error=str(e))
        
        return tracks
    
    async def get_playlist_tracks(self, playlist_id: str) -> List[Dict[str, Any]]:
        """Get all tracks from a Spotify playlist."""
        sp = self._get_client()
        if not sp:
            return []
        
        tracks = []
        try:
            results = await asyncio.get_event_loop().run_in_executor(
                None, lambda: sp.playlist_tracks(playlist_id, fields='items(track(name,artists,album,duration_ms,external_urls)),next')
            )
            
            while results:
                for item in results['items']:
                    track = item.get('track')
                    if track and track['type'] == 'track':
                        artists = ', '.join(a['name'] for a in track['artists'])
                        tracks.append({
                            'title': track['name'],
                            'artist': artists,
                            'album': track['album']['name'],
                            'duration_ms': track['duration_ms'],
                            'spotify_url': track['external_urls']['spotify'],
                        })
                if results['next']:
                    results = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: sp.next(results)
                    )
                else:
                    break
        except Exception as e:
            logger.error("spotify_playlist_fetch_failed", playlist_id=playlist_id, error=str(e))
        
        return tracks
    
    async def get_artist_top_tracks(self, artist_id: str, country: str = 'US') -> List[Dict[str, Any]]:
        """Get artist's top tracks from Spotify."""
        sp = self._get_client()
        if not sp:
            return []
        
        try:
            results = await asyncio.get_event_loop().run_in_executor(
                None, lambda: sp.artist_top_tracks(artist_id, country=country)
            )
            
            tracks = []
            for track in results['tracks']:
                artists = ', '.join(a['name'] for a in track['artists'])
                tracks.append({
                    'title': track['name'],
                    'artist': artists,
                    'album': track['album']['name'],
                    'duration_ms': track['duration_ms'],
                    'spotify_url': track['external_urls']['spotify'],
                })
            return tracks
        except Exception as e:
            logger.error("spotify_artist_tracks_failed", artist_id=artist_id, error=str(e))
            return []
    
    async def search_and_resolve(self, query: str, max_results: int = 5) -> List[Dict[str, Any]]:
        """Search YouTube/SoundCloud for a track and return playable URLs."""
        # Use yt-dlp search
        results = await search_async(query, max_results)
        return results
    
    async def resolve_to_playable(self, url: str) -> List[Dict[str, Any]]:
        """
        Main entry point: Convert Spotify URL to list of playable media info.
        Returns list of dicts with title, artist, and search query for yt-dlp.
        """
        # Resolve short URLs first
        url = await self.resolve_short_url(url)
        
        content_type = self.get_spotify_type(url)
        spotify_id = self.extract_spotify_id(url)
        
        if not content_type or not spotify_id:
            logger.warning("spotify_url_parse_failed", url=url)
            return []
        
        tracks = []
        
        if content_type == 'track':
            info = await self.get_track_info(spotify_id)
            if info:
                tracks.append(info)
        
        elif content_type == 'album':
            tracks = await self.get_album_tracks(spotify_id)
        
        elif content_type == 'playlist':
            tracks = await self.get_playlist_tracks(spotify_id)
        
        elif content_type == 'artist':
            tracks = await self.get_artist_top_tracks(spotify_id)
        
        # Add search query for each track
        for track in tracks:
            track['search_query'] = f"{track['artist']} {track['title']}"
            if track.get('album'):
                track['search_query'] += f" {track['album']}"
        
        logger.info("spotify_resolved", url=url, type=content_type, track_count=len(tracks))
        return tracks


# Global resolver instance
_spotify_resolver: Optional[SpotifyResolver] = None


def get_spotify_resolver() -> SpotifyResolver:
    """Get global Spotify resolver instance."""
    global _spotify_resolver
    if _spotify_resolver is None:
        _spotify_resolver = SpotifyResolver()
    return _spotify_resolver