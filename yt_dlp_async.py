import asyncio
from pathlib import Path
from typing import Optional, Dict, Any, List
from concurrent.futures import ThreadPoolExecutor

import yt_dlp

from config import settings
from logging_config import get_logger
from cookie_manager import get_cookie_manager

logger = get_logger("yt_dlp_async")

BASE_DIR = Path(__file__).resolve().parent

# Global executor (initialized in main bot file)
_ytdlp_executor: Optional[ThreadPoolExecutor] = None


def _find_cookie_file(platform: str = "") -> Optional[str]:
    """Find cookie file for a platform. Checks multiple locations in order.
    
    Priority (uploaded cookies always win over defaults):
    1. COOKIE_FILE env var (settings.cookie_file) — manual override
    2. cookie_data/{platform}_cookies.txt — uploaded via Telegram /cookie command
    3. /tmp/cookies_{platform}.txt — temp files from cookie_manager
    4. youtube_downloads/cookies.txt — built-in default (non-authenticated)
    """
    # 1. Manual override via env var
    if settings.cookie_file and Path(settings.cookie_file).exists():
        logger.info("cookie_file_found", source="settings", path=settings.cookie_file)
        return str(settings.cookie_file)
    
    # 2. Uploaded cookies via /cookie command (highest priority for user cookies)
    if platform:
        cookie_data_file = BASE_DIR / "cookie_data" / f"{platform}_cookies.txt"
        if cookie_data_file.exists():
            logger.info("cookie_file_found", source="uploaded", path=str(cookie_data_file))
            return str(cookie_data_file)
        else:
            logger.info("cookie_file_not_in_cookie_data", platform=platform, expected_path=str(cookie_data_file))
    
    # 3. Temp files from cookie_manager
    if platform:
        tmp_cookie = Path(f"/tmp/cookies_{platform}.txt")
        if tmp_cookie.exists():
            logger.info("cookie_file_found", source="tmp", path=str(tmp_cookie))
            return str(tmp_cookie)
    
    # 4. Built-in default (non-authenticated, last resort)
    default_cookie = BASE_DIR / "youtube_downloads" / "cookies.txt"
    if default_cookie.exists():
        logger.info("cookie_file_found", source="default_fallback", path=str(default_cookie))
        return str(default_cookie)
    
    logger.warning("cookie_file_not_found", platform=platform, base_dir=str(BASE_DIR))
    return None


def set_executor(executor: ThreadPoolExecutor) -> None:
    """Set the global yt-dlp executor."""
    global _ytdlp_executor
    _ytdlp_executor = executor


def get_executor() -> ThreadPoolExecutor:
    """Get the global yt-dlp executor."""
    if _ytdlp_executor is None:
        raise RuntimeError("YT-DLP executor not initialized. Call set_executor() first.")
    return _ytdlp_executor


async def _get_cookie_file(platform: str = "") -> Optional[str]:
    """Get cookie file path for a platform via cookie manager."""
    cookie_manager = get_cookie_manager()
    return await cookie_manager.get_cookie_file_path(platform)


def _extract_info_sync(url: str, extra_opts: Optional[Dict] = None) -> Optional[Dict[str, Any]]:
    """Synchronous yt-dlp extraction."""
    # Detect platform from URL for cookie selection
    platform = ""
    if "youtube.com" in url or "youtu.be" in url:
        platform = "youtube"
    elif "soundcloud.com" in url:
        platform = "soundcloud"
    elif "instagram.com" in url:
        platform = "instagram"
    elif "tiktok.com" in url:
        platform = "tiktok"

    cookie_file = _find_cookie_file(platform)

    from yt_dlp.networking.impersonate import ImpersonateTarget
    opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'noplaylist': True,
        'cookiefile': cookie_file,
        'remote_components': {'ejs': 'github'},
        'impersonate': ImpersonateTarget.from_str('chrome'),
    }
    if extra_opts:
        opts.update(extra_opts)

    import time
    for attempt in range(3):
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)
        except Exception as e:
            if '403' in str(e) and attempt < 2:
                time.sleep(3 + attempt * 2)
                continue
            raise
    return None


def _extract_artist_tracks_sync(url: str, max_retries: int = 3) -> Optional[Dict[str, Any]]:
    """Synchronous artist/playlist track extraction with retry on 403."""
    from yt_dlp.networking.impersonate import ImpersonateTarget
    # Detect platform
    platform = ""
    if "youtube.com" in url or "youtu.be" in url:
        platform = "youtube"
    elif "soundcloud.com" in url:
        platform = "soundcloud"

    cookie_file = _find_cookie_file(platform)

    opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'noplaylist': True,
        'cookiefile': cookie_file,
        'remote_components': {'ejs': 'github'},
        'impersonate': ImpersonateTarget.from_str('chrome'),
    }
    import time
    info = None
    for attempt in range(max_retries):
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                break
        except Exception as e:
            if '403' in str(e) and attempt < max_retries - 1:
                time.sleep(3 + attempt * 2)
                continue
            raise
    if not info:
        return None
        entries = info.get('entries', [])
        tracks = []
        for i, entry in enumerate(entries):
            if entry:
                track_url = entry.get('url') or entry.get('webpage_url')
                if not track_url and entry.get('id'):
                    ie = entry.get('ie_key', '')
                    if 'Soundcloud' in ie:
                        track_url = f"https://soundcloud.com/{entry.get('uploader_id', '')}/{entry.get('id', '')}"
                    elif 'Youtube' in ie:
                        track_url = f"https://www.youtube.com/watch?v={entry.get('id', '')}"
                    else:
                        track_url = entry.get('id', '')
                tracks.append({
                    'title': entry.get('title', f'Track {i+1}'),
                    'url': track_url,
                    'duration': entry.get('duration', 0),
                    'id': entry.get('id', ''),
                    'uploader': entry.get('uploader', ''),
                    'thumbnail': entry.get('thumbnail', ''),
                })
        return {
            'title': info.get('title', 'Unknown'),
            'thumbnail': info.get('thumbnail'),
            'description': info.get('description', ''),
            'uploader': info.get('uploader', ''),
            'tracks': tracks,
        }


def _search_sync(query: str, max_results: int = 5) -> List[Dict[str, Any]]:
    """Synchronous search."""
    opts = {'quiet': True, 'no_warnings': True, 'skip_download': True, 'extract_flat': True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"scsearch{max_results}:{query}", download=False)
            if not info or not info.get('entries'):
                return []
            results = []
            for e in info['entries']:
                if e:
                    results.append({
                        'title': e.get('title', 'Unknown'),
                        'url': e.get('url') or e.get('webpage_url', ''),
                        'uploader': e.get('uploader', ''),
                        'thumbnail': e.get('thumbnail', ''),
                        'duration': e.get('duration', 0),
                    })
            return results
    except Exception as e:
        logger.error("search_failed", query=query, error=str(e))
        return []


def _download_audio_sync(url: str, output_dir: str) -> Optional[str]:
    """Synchronous audio download."""
    import os

    # Detect platform
    platform = ""
    if "youtube.com" in url or "youtu.be" in url:
        platform = "youtube"
    elif "soundcloud.com" in url:
        platform = "soundcloud"

    cookie_file = _find_cookie_file(platform)

    from yt_dlp.networking.impersonate import ImpersonateTarget
    ydl_opts = {
        'outtmpl': os.path.join(output_dir, 'temp_audio.%(ext)s'),
        'format': 'bestaudio/best',
        'quiet': True,
        'noplaylist': True,
        'cookiefile': cookie_file,
        'remote_components': {'ejs': 'github'},
        'impersonate': ImpersonateTarget.from_str('chrome'),
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
    
    # Find downloaded file
    for f in os.listdir(output_dir):
        if f.startswith('temp_audio.') and os.path.isfile(os.path.join(output_dir, f)):
            return os.path.join(output_dir, f)
    return None


def _download_video_sync(url: str, output_dir: str, format_str: str) -> Optional[str]:
    """Synchronous video download. Returns file path or None."""
    import os
    # Detect platform
    platform = ""
    if "youtube.com" in url or "youtu.be" in url:
        platform = "youtube"
    elif "soundcloud.com" in url:
        platform = "soundcloud"

    cookie_file = _find_cookie_file(platform)

    from yt_dlp.networking.impersonate import ImpersonateTarget
    ydl_opts = {
        'outtmpl': os.path.join(output_dir, 'temp_video.%(ext)s'),
        'format': format_str,
        'merge_output_format': 'mp4',
        'remote_components': {'ejs': 'github'},
        'impersonate': ImpersonateTarget.from_str('chrome'),
        'quiet': True,
        'cookiefile': cookie_file,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    # Find downloaded file
    for f in os.listdir(output_dir):
        if f.startswith('temp_video.') and os.path.isfile(os.path.join(output_dir, f)):
            return os.path.join(output_dir, f)
    return None


# Async wrappers
async def extract_info_async(url: str, extra_opts: Optional[Dict] = None) -> Optional[Dict[str, Any]]:
    """Async wrapper for extract_info."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(get_executor(), _extract_info_sync, url, extra_opts)


async def extract_artist_tracks_async(url: str) -> Optional[Dict[str, Any]]:
    """Async wrapper for extract_artist_tracks."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(get_executor(), _extract_artist_tracks_sync, url)


async def search_async(query: str, max_results: int = 5) -> List[Dict[str, Any]]:
    """Async wrapper for search."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(get_executor(), _search_sync, query, max_results)


async def download_audio_async(url: str, output_dir: str) -> Optional[str]:
    """Async wrapper for audio download."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(get_executor(), _download_audio_sync, url, output_dir)


async def download_video_async(url: str, output_dir: str, format_str: str) -> Optional[str]:
    """Async wrapper for video download. Returns file path or None."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(get_executor(), _download_video_sync, url, output_dir, format_str)