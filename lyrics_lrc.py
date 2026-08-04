"""Synced lyrics (LRC) fetching and embedding."""

import re
import asyncio
from typing import Optional, List, Tuple
from dataclasses import dataclass

import requests

from config import settings
from logging_config import get_logger

logger = get_logger("lyrics_lrc")


@dataclass
class SyncedLyrics:
    """Container for synced lyrics."""
    lines: List[Tuple[int, str]]  # (timestamp_ms, text)
    raw_lrc: str
    
    def to_lrc_string(self) -> str:
        """Convert to LRC format string."""
        return self.raw_lrc
    
    def to_sylt_format(self) -> List[Tuple[int, str]]:
        """Format for mutagen SYLT frame: list of (text, timestamp_ms)."""
        return [(text, ts) for ts, text in self.lines]


def parse_lrc(lrc_text: str) -> List[Tuple[int, str]]:
    """
    Parse LRC format lyrics.
    Returns list of (timestamp_ms, text).
    """
    lines = []
    # Pattern: [mm:ss.xx]text or [mm:ss:xx]text
    # Group 1: minutes, Group 2: seconds, Group 3: centiseconds, Group 4: text
    pattern = re.compile(r'\[(\d{1,2}):(\d{2})[.:](\d{1,3})\](.*)')

    for line in lrc_text.strip().split('\n'):
        line = line.strip()
        if not line:
            continue

        # Find all timestamp matches in this line
        matches = list(pattern.finditer(line))
        if matches:
            for match in matches:
                minutes = int(match.group(1))
                seconds = int(match.group(2))
                centiseconds = match.group(3)
                text = match.group(4).strip()
                # Normalize to milliseconds
                if len(centiseconds) == 2:
                    ms = int(centiseconds) * 10
                elif len(centiseconds) == 3:
                    ms = int(centiseconds)
                else:
                    ms = int(centiseconds) * 10
                timestamp_ms = (minutes * 60 + seconds) * 1000 + ms
                if text:
                    lines.append((timestamp_ms, text))
        else:
            # Line without timestamp (metadata like [ti:Title], [ar:Artist])
            pass

    # Sort by timestamp
    lines.sort(key=lambda x: x[0])
    return lines


def fetch_lrc_from_lrclib(artist: str, title: str, duration: Optional[int] = None) -> Optional[SyncedLyrics]:
    """
    Fetch synced lyrics from lrclib.net (free, no API key needed).
    """
    try:
        # Clean artist/title for search
        artist_clean = re.sub(r'[，。、；：！？（）【】「」《》""''…—]', ' ', artist).strip()
        artist_clean = re.sub(r'\s+', ' ', artist_clean)
        title_clean = re.sub(r'[，。、；：！？（）【】「」《》""''…—]', ' ', title).strip()
        title_clean = re.sub(r'\s+', ' ', title_clean)
        
        # Remove common suffixes
        for suffix in ['Official Video', 'Official Audio', 'Lyrics', 'Music Video',
                       'Official Music Video', 'Audio', 'Video', 'HD', '4K',
                       '(Official Video)', '(Official Audio)', '(Lyrics)',
                       '[Official Video]', '[Official Audio]', '[Lyrics]']:
            title_clean = title_clean.replace(suffix, '').strip()
        
        params = {
            'artist_name': artist_clean,
            'track_name': title_clean,
        }
        if duration:
            params['duration'] = duration
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json',
        }
        
        resp = requests.get(
            'https://lrclib.net/api/get',
            params=params,
            headers=headers,
            timeout=15
        )
        
        if resp.status_code == 200:
            data = resp.json()
            synced_lyrics = data.get('syncedLyrics')
            if synced_lyrics:
                parsed = parse_lrc(synced_lyrics)
                if parsed:
                    logger.info("lrc_fetched_lrclib", artist=artist_clean, title=title_clean, lines=len(parsed))
                    return SyncedLyrics(lines=parsed, raw_lrc=synced_lyrics)
        
        elif resp.status_code == 404:
            logger.debug("lrc_not_found_lrclib", artist=artist_clean, title=title_clean)
        
    except Exception as e:
        logger.debug("lrclib_fetch_error", error=str(e))
    
    return None


def fetch_lrc_from_musixmatch(artist: str, title: str) -> Optional[SyncedLyrics]:
    """
    Fetch synced lyrics from MusixMatch (unofficial, may be rate limited).
    """
    try:
        artist_clean = re.sub(r'[，。、；：！？（）【】「」《》""''…—]', ' ', artist).strip()
        artist_clean = re.sub(r'\s+', ' ', artist_clean)
        title_clean = re.sub(r'[，。、；：！？（）【】「」《》""''…—]', ' ', title).strip()
        title_clean = re.sub(r'\s+', ' ', title_clean)
        
        for suffix in ['Official Video', 'Official Audio', 'Lyrics', 'Music Video',
                       'Official Music Video', 'Audio', 'Video', 'HD', '4K',
                       '(Official Video)', '(Official Audio)', '(Lyrics)',
                       '[Official Video]', '[Official Audio]', '[Lyrics]']:
            title_clean = title_clean.replace(suffix, '').strip()
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json',
        }
        
        # Search for track
        search_url = 'https://www.musixmatch.com/ws/1.1/matcher.track.get'
        params = {
            'q_track': title_clean,
            'q_artist': artist_clean,
            'format': 'json',
        }
        
        resp = requests.get(search_url, params=params, headers=headers, timeout=10)
        if resp.status_code != 200:
            return None
        
        data = resp.json()
        track = data.get('message', {}).get('body', {}).get('track')
        if not track:
            return None
        
        track_id = track.get('track_id')
        if not track_id:
            return None
        
        # Get subtitle (LRC)
        subtitle_url = 'https://www.musixmatch.com/ws/1.1/track.subtitle.get'
        params = {
            'track_id': track_id,
            'format': 'json',
            'subtitle_format': 'lrc',
        }
        
        resp2 = requests.get(subtitle_url, params=params, headers=headers, timeout=10)
        if resp2.status_code != 200:
            return None
        
        data2 = resp2.json()
        subtitle = data2.get('message', {}).get('body', {}).get('subtitle', {})
        lrc_text = subtitle.get('subtitle_body')
        
        if lrc_text:
            parsed = parse_lrc(lrc_text)
            if parsed:
                logger.info("lrc_fetched_musixmatch", artist=artist_clean, title=title_clean, lines=len(parsed))
                return SyncedLyrics(lines=parsed, raw_lrc=lrc_text)
        
    except Exception as e:
        logger.debug("musixmatch_fetch_error", error=str(e))
    
    return None


async def fetch_synced_lyrics(artist: str, title: str, duration: Optional[int] = None) -> Optional[SyncedLyrics]:
    """
    Fetch synced lyrics from multiple sources with fallback.
    Returns SyncedLyrics object or None.
    """
    # Source 1: lrclib.net (best free source)
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None, fetch_lrc_from_lrclib, artist, title, duration
    )
    if result:
        return result
    
    # Source 2: MusixMatch
    result = await loop.run_in_executor(
        None, fetch_lrc_from_musixmatch, artist, title
    )
    if result:
        return result
    
    logger.warning("no_synced_lyrics_found", artist=artist, title=title)
    return None


def embed_synced_lyrics_mp3(file_path: str, synced_lyrics: SyncedLyrics) -> bool:
    """
    Embed synced lyrics (SYLT frame) into MP3 file using mutagen.
    """
    try:
        from mutagen.mp3 import MP3
        from mutagen.id3 import SYLT, ID3
        
        audio = MP3(file_path, ID3=ID3)
        if audio.tags is None:
            audio.add_tags()
        
        # Remove existing SYLT frames
        for key in list(audio.tags.keys()):
            if key.startswith('SYLT'):
                del audio.tags[key]
        
        # Add new SYLT frame
        # Format: 2 = milliseconds, Type: 1 = lyrics, Content: 0 = other
        audio.tags.add(SYLT(
            encoding=3,  # UTF-8
            lang='eng',
            format=2,    # Milliseconds
            type=1,      # Lyrics
            desc='',
            text=synced_lyrics.to_sylt_format()
        ))
        
        audio.save(v2_version=3)
        logger.info("synced_lyrics_embedded", file=file_path, lines=len(synced_lyrics.lines))
        return True
        
    except Exception as e:
        logger.error("synced_lyrics_embed_failed", file=file_path, error=str(e))
        return False