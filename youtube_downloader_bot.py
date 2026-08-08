import os
import io
import re
import time
import json
import shutil
import asyncio
import signal
import sqlite3
import subprocess
import urllib.request
import html
from pathlib import Path
from datetime import datetime, timedelta
from urllib.parse import urlparse, urlunparse
from concurrent.futures import ThreadPoolExecutor

import requests as req_lib
genius_client = None
import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)
from aiohttp import web

# Local imports
from config import settings, TOKEN, ADMIN_ID, RAILWAY_MODE, VIDEO_COMPRESSION_TARGET_MB, MAX_VIDEO_DURATION_SEC, MAX_STORAGE_MB, WEBHOOK_MODE, PORT, ALLOWED_USERS, MAX_DOWNLOADS_PER_USER_PER_DAY
from logging_config import setup_logging, get_logger, get_correlation_id, set_correlation_id
from yt_dlp_async import (
    extract_info_async,
    extract_artist_tracks_async,
    search_async,
    download_audio_async,
    download_video_async,
    set_executor,
)
from rate_limiter import check_rate_limit
from spotify_resolver import get_spotify_resolver
from lyrics_lrc import fetch_synced_lyrics, embed_synced_lyrics_mp3
from health_check import start_health_server, stop_health_server
from database import get_database, close_database
from download_queue import get_download_queue, close_download_queue, DownloadTask, DownloadStatus
from cookie_manager import get_cookie_manager, convert_cookies, detect_cookie_format
from metrics import init_metrics, record_download, record_error, set_active_downloads, set_queue_stats, record_rate_limit, setup_metrics_app
from scheduler import start_scheduler, stop_scheduler
from chunked_upload import upload_with_progress
from admin_dashboard import (
    cmd_admin, cmd_adduser, cmd_removeuser, cmd_listusers,
    cmd_toggleuser, cmd_userinfo, handle_admin_callback,
    handle_admin_broadcast, is_admin as is_bot_admin
)

# ==================== SETUP ====================
# Initialize structured logging
setup_logging(log_level=settings.log_level, json_output=settings.log_json)
logger = get_logger("mediabot")

# Initialize Genius lyrics API
try:
    import lyricsgenius
    GENIUS_TOKEN = os.environ.get('GENIUS_API_TOKEN', '')
    genius_client = lyricsgenius.Genius(GENIUS_TOKEN) if GENIUS_TOKEN else None
    if genius_client:
        logger.info("Genius API: enabled")
    else:
        logger.info("Genius API: no token")
except ImportError:
    genius_client = None
    logger.info("Genius API: lyricsgenius not installed")

_genius_metadata = {}  # Stores album/year from Genius during lyrics fetch

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "media_downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)
COOKIE_FILE = BASE_DIR / "youtube_downloads" / "cookies.txt"
# Use /tmp for SQLite (writable by non-root user in Docker)
DB_PATH = Path("/tmp/bot.db")

# Async yt-dlp executor (controlled thread pool)
YTDLP_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="yt-dlp")

# Initialize yt-dlp executor
set_executor(YTDLP_EXECUTOR)

# In-memory state
user_sessions = {}

TELEGRAM_MAX_FILE = 49 * 1024 * 1024  # ~50MB Telegram limit

# Default video quality
DEFAULT_VIDEO_QUALITY = "480p" if RAILWAY_MODE else "best"

# Graceful shutdown flag
_shutdown_requested = False

# ==================== DATABASE ====================
# Database is now async PostgreSQL via database.py module
# Old SQLite functions replaced with async database calls

def _init_sqlite():
    """Create SQLite tables if they don't exist."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""CREATE TABLE IF NOT EXISTS download_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT, title TEXT, artist TEXT, album TEXT,
            platform TEXT, content_type TEXT, status TEXT,
            file_path TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS user_reactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            history_id INTEGER, user_id INTEGER, action TEXT
        )""")
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("sqlite_init_failed", error=str(e))

_init_sqlite()

async def init_db():
    """Initialize database connection and schema."""
    db = await get_database()
    if db:
        logger.info("database_initialized")
    else:
        logger.info("running_without_database")

def db_log(url, title, artist, album, platform, content_type, status, file_path=""):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO download_history (url,title,artist,album,platform,content_type,status,file_path) VALUES (?,?,?,?,?,?,?,?)",
            (url, title, artist, album, platform, content_type, status, file_path)
        )
        conn.commit()
        hist_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()
        return hist_id
    except Exception as e:
        logger.warning("db_log_failed", error=str(e))
        return 0

def db_get_history(limit=10):
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT id,title,artist,platform,content_type,status,timestamp FROM download_history ORDER BY id DESC LIMIT ?",
            (limit,)
        ).fetchall()
        conn.close()
        return rows
    except Exception as e:
        logger.warning("db_get_history_failed", error=str(e))
        return []

def db_get_favorites():
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute("""
            SELECT dh.id, dh.title, dh.artist, dh.url, dh.timestamp
            FROM download_history dh
            JOIN user_reactions ur ON dh.id = ur.history_id
            WHERE ur.action = 'like'
            ORDER BY ur.id DESC LIMIT 20
        """).fetchall()
        conn.close()
        return rows
    except Exception as e:
        logger.warning("db_get_favorites_failed", error=str(e))
        return []

def db_get_albums():
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute("""
            SELECT artist, album, COUNT(*) as cnt
            FROM download_history
            WHERE artist != '' AND album != ''
            GROUP BY artist, album
            ORDER BY cnt DESC LIMIT 20
        """).fetchall()
        conn.close()
        return rows
    except Exception as e:
        logger.warning("db_get_albums_failed", error=str(e))
        return []

def db_log_reaction(history_id, user_id, action):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO user_reactions (history_id, user_id, action) VALUES (?,?,?)",
            (history_id, user_id, action)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("db_log_reaction_failed", error=str(e))

def db_get_logs(limit=15):
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT title,platform,status,timestamp FROM download_history ORDER BY id DESC LIMIT ?",
            (limit,)
        ).fetchall()
        conn.close()
        return rows
    except Exception as e:
        logger.warning("db_get_logs_failed", error=str(e))
        return []

# ==================== RATE LIMITING ====================
# Now using rate_limiter module with Redis backend and in-memory fallback
# check_rate_limit imported from rate_limiter

# ==================== URL HELPERS ====================
def resolve_short_url(url):
    url_lower = url.lower()
    if 'on.soundcloud.com' in url_lower or 'spotify.link' in url_lower or 'spoti.fi' in url_lower:
        try:
            resp = req_lib.head(url, allow_redirects=True, timeout=10)
            resolved = resp.url
            logger.info(f"Resolved {url} → {resolved}")
            return resolved
        except Exception as e:
            logger.error(f"Resolve failed: {e}")
    return url

def strip_query_params(url):
    parsed = urlparse(url)
    return urlunparse(parsed._replace(query='', fragment=''))

def detect_url_type(url):
    resolved = resolve_short_url(url)
    resolved_lower = strip_query_params(resolved).lower()

    # YouTube
    if 'youtube.com' in resolved_lower or 'youtu.be' in resolved_lower:
        if re.search(r'youtube\.com/(c/|channel/|@)', resolved_lower):
            return 'artist', resolved
        if 'list=' in resolved_lower or re.search(r'youtube\.com/playlist\?', resolved_lower):
            return 'playlist', resolved
        return 'track', resolved

    # Spotify
    if 'open.spotify.com' in resolved_lower or 'spotify.link' in resolved_lower or 'spoti.fi' in resolved_lower:
        if 'spotify.com/artist/' in resolved_lower:
            return 'artist', resolved
        if 'spotify.com/playlist/' in resolved_lower or 'spotify.com/album/' in resolved_lower:
            return 'playlist', resolved
        if 'spotify.com/track/' in resolved_lower:
            return 'track', resolved
        return 'auto', resolved

    # SoundCloud
    if 'soundcloud.com' in resolved_lower:
        if 'on.soundcloud.com' in resolved_lower:
            return 'auto', resolved
        if '/sets/' in resolved_lower:
            return 'playlist', resolved
        if re.match(r'https?://(?:www\.)?soundcloud\.com/[a-zA-Z0-9_-]+/?$', resolved_lower):
            return 'artist', resolved
        if re.search(r'soundcloud\.com/[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+', resolved_lower):
            return 'track', resolved

    # Instagram
    if 'instagram.com' in resolved_lower:
        if re.search(r'instagram\.com/[a-zA-Z0-9_.]+/?$', resolved_lower):
            return 'artist', resolved
        return 'track', resolved

    # TikTok
    if 'tiktok.com' in resolved_lower:
        if re.search(r'@([a-zA-Z0-9_.]+)/?$', resolved_lower):
            return 'artist', resolved
        return 'track', resolved

    # Twitter/X, Facebook, Twitch - all treated as track/video
    for domain in ['twitter.com', 'x.com', 'facebook.com', 'fb.com', 'fb.watch', 'twitch.tv', 'clips.twitch.tv']:
        if domain in resolved_lower:
            return 'track', resolved

    return 'track', resolved

def get_platform_name(url):
    url_lower = url.lower()
    if 'youtube.com' in url_lower or 'youtu.be' in url_lower:
        return 'YouTube'
    if 'soundcloud.com' in url_lower:
        return 'SoundCloud'
    if 'spotify.com' in url_lower or 'spotify.link' in url_lower:
        return 'Spotify'
    if 'instagram.com' in url_lower:
        return 'Instagram'
    if 'tiktok.com' in url_lower:
        return 'TikTok'
    if 'twitter.com' in url_lower or 'x.com' in url_lower:
        return 'Twitter'
    if 'facebook.com' in url_lower or 'fb.watch' in url_lower:
        return 'Facebook'
    if 'twitch.tv' in url_lower:
        return 'Twitch'
    return 'Unknown'

# ==================== YT-DLP HELPERS (ASYNC) ====================
# These are now imported from yt_dlp_async module
# extract_info_async, extract_artist_tracks_async, search_async, download_audio_async, download_video_async

# ==================== PROGRESS BAR ====================
def make_progress_bar(pct):
    filled = int(pct / 5)
    return "█" * filled + "░" * (20 - filled)

async def progress_hook(d, context, msg_chat_id, msg_message_id):
    if d['status'] == 'downloading':
        total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
        downloaded = d.get('downloaded_bytes', 0)
        if total > 0:
            pct = (downloaded / total) * 100
            bar = make_progress_bar(pct)
            speed = d.get('_speed_str', 'N/A')
            eta = d.get('_eta_str', 'N/A')
            text = (
                f"📥 **دانلود...**\n"
                f"`{bar}` {pct:.1f}%\n"
                f"⚡ سرعت: {speed} | ⏱ باقی‌مانده: {eta}"
            )
            try:
                await context.bot.edit_message_text(
                    text=text, chat_id=msg_chat_id,
                    message_id=msg_message_id, parse_mode='Markdown'
                )
            except:
                pass

# ==================== AUDIO METADATA ====================
def download_cover(url, output_path):
    """Download cover art with proper headers (requests instead of urllib)"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
            'Accept': 'image/webp,image/apng,image/*,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Referer': 'https://www.google.com/',
        }
        resp = req_lib.get(url, headers=headers, timeout=15, stream=True)
        if resp.status_code == 200 and len(resp.content) > 500:
            with open(output_path, 'wb') as f:
                f.write(resp.content)
            logger.info(f"Cover downloaded: {len(resp.content)} bytes from {url[:80]}")
            return True
        else:
            logger.warning(f"Cover download failed: status={resp.status_code}, size={len(resp.content)}")
    except Exception as e:
        logger.error(f"Cover download error: {e}")
    return False

def embed_metadata_ffmpeg(input_path, output_path, info, lyrics=None):
    title = info.get('title', 'Unknown')
    artist = info.get('artist') or info.get('uploader', 'Unknown')
    # Split artist field - remove "feat." etc
    artist_seps = re.split(r'[,،;]', artist)
    if len(artist_seps) > 1:
        artist = artist_seps[0].strip()
    
    # Get album - try multiple sources, exclude junk values
    album = info.get('album') or ''
    # Skip junk album values
    junk_albums = {'', 'unknown', 'youtube', 'telegram', 'music', 'none', 'na', 'n/a'}
    if album.lower().strip() in junk_albums:
        album = ''
    playlist = info.get('playlist_title') or ''
    series = info.get('series') or ''
    # Use playlist as album only if it looks like an album (not a channel name)
    if not album and playlist and playlist.lower() not in junk_albums:
        album = playlist
    if not album and series:
        album = series
    # Try tags for album
    if not album:
        for tag in (info.get('tags') or []):
            tag_lower = tag.lower()
            if 'album' in tag_lower or 'ep' in tag_lower or 'mixtape' in tag_lower:
                album = tag
                break
    # Try Genius metadata
    if not album and _genius_metadata.get('album'):
        album = _genius_metadata['album']
        logger.info(f"Using Genius album: {album}")
    # Fallback: use main artist as album
    if not album:
        album = artist
        logger.info(f"No album found, using main artist as album: {album}")
    
    # Genre detection
    genre = info.get('genre') or ''
    if not genre:
        categories = info.get('categories', []) or []
        tags = info.get('tags', []) or []
        all_tags = [t.lower() for t in (tags + categories)]
        genre_map = {
            'hip-hop': ['hip-hop', 'hip hop', 'rap', 'hiphop', 'persian rap', 'iranian rap', 'farsi rap'],
            'pop': ['pop', 'pop music', 'persian pop'],
            'rock': ['rock', 'alternative', 'indie'],
            'electronic': ['electronic', 'edm', 'dance', 'house', 'techno', 'trance'],
            'r&b': ['r&b', 'rnb', 'rhythm and blues', 'soul'],
            'jazz': ['jazz', 'blues'],
            'classical': ['classical', 'orchestra'],
            'metal': ['metal', 'heavy metal'],
            'reggae': ['reggae', 'ska'],
            'country': ['country', 'folk'],
            'latin': ['latin', 'reggaeton'],
        }
        for genre_name, keywords in genre_map.items():
            for tag in all_tags:
                if any(kw in tag for kw in keywords):
                    genre = genre_name
                    break
            if genre:
                break
    # Default genre based on artist/platform
    if not genre:
        genre = 'hip-hop'  # Default for this bot
    
    # Track number
    track_number = info.get('track_number') or info.get('playlist_index') or 0
    playlist_count = info.get('n_entries') or info.get('playlist_count') or 0
    
    # Year from upload_date
    upload_date = info.get('upload_date', '')
    year = upload_date[:4] if upload_date and len(upload_date) >= 4 else ''
    
    # Clean title
    clean_title = title
    if ' - ' in title:
        parts = title.split(' - ', 1)
        if len(parts) == 2:
            if parts[0].strip().lower() in artist.lower() or artist.lower() in parts[0].strip().lower():
                clean_title = parts[1].strip()
    
    # Download cover art
    thumbnail = info.get('thumbnail')
    cover_path = None
    if thumbnail:
        cover_path = str(input_path) + '.cover.jpg'
        if not download_cover(thumbnail, cover_path):
            alt_urls = []
            if 'i1.sndcdn.com' in thumbnail:
                alt_urls = [
                    thumbnail.replace('-large', '-t300x300'),
                    thumbnail.replace('-large', '-t500x500'),
                    thumbnail.replace('-t100x100', '-t500x500'),
                ]
            elif 'yt3' in thumbnail or 'i.ytimg.com' in thumbnail:
                vid_id = thumbnail.split('/')[-1].split('.')[0]
                alt_urls = [
                    f"https://i.ytimg.com/vi/{vid_id}/maxresdefault.jpg",
                    f"https://i.ytimg.com/vi/{vid_id}/hqdefault.jpg",
                ]
            for alt in alt_urls:
                if download_cover(alt, cover_path):
                    break
            else:
                cover_path = None

    # Build metadata args
    base_meta = ['-metadata', f'title={clean_title}', '-metadata', f'artist={artist}']
    if album:
        base_meta += ['-metadata', f'album={album}']
    if genre:
        base_meta += ['-metadata', f'genre={genre}']
    if year:
        base_meta += ['-metadata', f'date={year}']
    if track_number:
        track_str = str(track_number)
        if playlist_count:
            track_str = f'{track_number}/{playlist_count}'
        base_meta += ['-metadata', f'track={track_str}']
    if lyrics:
        base_meta += ['-metadata', f'lyrics={lyrics[:4000]}']

    has_cover = cover_path and os.path.exists(cover_path)
    input_ext = Path(input_path).suffix.lower()
    output_file = DOWNLOAD_DIR / (Path(output_path).stem + '.mp3')

    # Build ffmpeg command
    if input_ext == '.mp3':
        cmd = ['ffmpeg', '-y', '-i', str(input_path)]
        if has_cover:
            cmd += ['-i', cover_path, '-map', '0:a', '-map', '1:0']
        else:
            cmd += ['-map', '0:a']
        cmd += ['-c:a', 'copy', '-id3v2_version', '3'] + base_meta + [str(output_file)]
    else:
        cmd = ['ffmpeg', '-y', '-i', str(input_path)]
        if has_cover:
            cmd += ['-i', cover_path, '-map', '0:a', '-map', '1:0']
        else:
            cmd += ['-map', '0:a']
        cmd += [
            '-c:a', 'libmp3lame', '-b:a', '320k', '-ar', '44100',
            '-reservoir', '1', '-q:a', '0',
            '-id3v2_version', '3', '-write_id3v1', '1',
        ] + base_meta + [str(output_file)]

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=180)
        if result.returncode != 0:
            logger.error(f"ffmpeg stderr: {result.stderr.decode()[:500]}")
        else:
            logger.info(f"Metadata: title={clean_title}, artist={artist}, album={album}, genre={genre}, track={track_number}, cover={has_cover}")
    except Exception as e:
        logger.error(f"ffmpeg exception: {e}")
        shutil.copy2(str(input_path), str(output_file))
    finally:
        if cover_path and os.path.exists(cover_path):
            os.remove(cover_path)

    # Use mutagen for proper ID3 tags
    if output_file.exists():
        try:
            from mutagen.mp3 import MP3
            from mutagen.id3 import ID3, USLT, TALB, TCON, TPE2, TDRC, TRCK
            audio = MP3(str(output_file))
            if audio.tags is None:
                audio.add_tags()
            if album:
                audio.tags.add(TALB(encoding=3, text=album))
            if genre:
                audio.tags.add(TCON(encoding=3, text=genre))
            if year:
                audio.tags.add(TDRC(encoding=3, text=year))
            if track_number:
                audio.tags.add(TRCK(encoding=3, text=str(track_number) + ('/' + str(playlist_count) if playlist_count else '')))
            audio.tags.add(TPE2(encoding=3, text=artist))  # Album artist
            if lyrics:
                audio.tags.add(USLT(encoding=3, lang='eng', desc='', text=lyrics[:4000]))
            audio.save()
            logger.info(f"Mutagen: album={album}, genre={genre}, track={track_number}/{playlist_count}, lyrics={'yes' if lyrics else 'no'}")
        except Exception as e:
            logger.error(f"Mutagen error: {e}")

    return output_file

def fetch_lyrics_sync(artist, title):
    """Fetch lyrics from multiple sources with fallback.
    Strategy (priority order):
    1. Genius API (best for Persian/Iranian music + album metadata)
    2. lyrics.ovh (free, reliable)
    3. lyrics.fandom (MediaWiki API)
    4. textylate (simple API)
    
    Also extracts album name from Genius. If no album found, uses main_artist as album.
    """
    # Extract main artist (first name before x, X, &, feat., etc.)
    main_artist = artist
    for sep in [' x ', ' X ', ' & ', ' × ', ' feat. ', ' ft. ', 'Feat. ', ' Ft. ', '،', ',']:
        if sep in main_artist:
            main_artist = main_artist.split(sep)[0].strip()
            break
    
    # Clean artist
    artist_clean = re.sub(r'[،。、；：！？（）【】「」《》""''…—]', ' ', artist).strip()
    artist_clean = re.sub(r'\s+', ' ', artist_clean)
    main_artist_clean = re.sub(r'[،。、；：！？（）【】「」《》""''…—]', ' ', main_artist).strip()
    main_artist_clean = re.sub(r'\s+', ' ', main_artist_clean)
    
    # Clean title
    title_clean = re.sub(r'[،。、；：！？（）【】「」《》""''…—]', ' ', title).strip()
    title_clean = re.sub(r'\s+', ' ', title_clean)
    
    # Remove common suffixes
    for suffix in ['Official Video', 'Official Audio', 'Lyrics', 'Music Video',
                   'Official Music Video', 'Audio', 'Video', 'HD', '4K',
                   '(Official Video)', '(Official Audio)', '(Lyrics)',
                   '[Official Video]', '[Official Audio]', '[Lyrics]']:
        title_clean = title_clean.replace(suffix, '').strip()
    
    # Remove producer/feature tags
    title_clean = re.sub(r'\[prod\..*?\]', '', title_clean, flags=re.IGNORECASE).strip()
    title_clean = re.sub(r'\(prod\..*?\)', '', title_clean, flags=re.IGNORECASE).strip()
    title_clean = re.sub(r'\[.*?\]', '', title_clean).strip()
    title_clean = re.sub(r'\(.*?\)', '', title_clean).strip()
    title_clean = re.sub(r'\s*(feat\.|ft\.|featuring)\s*.*', '', title_clean, flags=re.IGNORECASE).strip()
    
    # Extract just song title from "Artist x Artist - SongTitle" format
    if ' - ' in title_clean:
        parts = title_clean.split(' - ')
        candidate = parts[-1].strip()
        if len(candidate) > 2:
            title_clean = candidate
    for sep in [' x ', ' X ', ' & ', ' × ']:
        if sep in title_clean:
            parts = title_clean.split(sep)
            candidate = parts[-1].strip()
            if len(candidate) > 2:
                title_clean = candidate

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'application/json',
    }
    
    # Multiple search strategies for better Persian music coverage
    search_combos = [
        (main_artist_clean, title_clean, f"{main_artist_clean} + {title_clean}"),
        (title_clean, '', f"{title_clean} (title only)"),
    ]
    
    # Add Persian variations if text contains Persian characters
    has_persian = bool(re.search(r'[\u0600-\u06FF]', artist + title))
    if has_persian:
        # Try to get Persian equivalents (basic transliteration detection)
        # For now, just add common variations
        search_combos.extend([
            (artist_clean, title_clean, f"{artist_clean} + {title_clean} (full artist)"),
        ])
    
    global _genius_metadata
    _genius_metadata = {'album': None, 'year': None}
    
    for search_artist, search_title, search_label in search_combos:
        logger.info(f"Searching lyrics ({search_label}): {artist_clean} - {title_clean}")
        
        # Source 1: Genius API (FIRST - best for Persian music + album/year metadata)
        if genius_client:
            try:
                # Use search_song which properly scrapes lyrics from Genius
                song = genius_client.search_song(
                    search_title if search_title else search_artist,
                    search_artist if search_title else ''
                )
                if song and song.lyrics:
                    # Verify the song matches our search (basic check)
                    song_artist = (song.artist or '').lower()
                    song_title = (song.title or '').lower()
                    search_artist_l = search_artist.lower()
                    search_title_l = search_title.lower()
                    
                    # For Persian, be more lenient; for English, require some match
                    if search_artist_l and search_title_l:
                        artist_match = search_artist_l in song_artist or song_artist in search_artist_l
                        title_match = search_title_l in song_title or song_title in search_title_l
                        if not (artist_match or title_match):
                            logger.info(f"Genius returned wrong song: {song.artist} - {song.title}, skipping")
                            song = None
                    
                    if song:
                        lyrics = song.lyrics
                        # Clean Genius lyrics header
                        lyrics = re.sub(r'^\[متن آهنگ.*?\]\s*', '', lyrics).strip()
                        lyrics = re.sub(r'^\d+\s*Embed\s*', '', lyrics).strip()
                        lyrics = re.sub(r'\d+\s*Embed\s*$', '', lyrics).strip()
                        lyrics = re.sub(r'^\d+\s*', '', lyrics).strip()
                        
                        # Extract album info
                        album_info = song.album
                        if isinstance(album_info, dict) and album_info.get('name'):
                            album_name = album_info['name']
                            # Filter out junk album names
                            junk_albums = {'telegram', 'youtube', 'soundcloud', 'instagram', 'spotify', 'apple music', 'music'}
                            if album_name.lower() not in junk_albums:
                                _genius_metadata['album'] = album_name
                                logger.info(f"Genius album: {album_name}")
                        if isinstance(album_info, dict) and album_info.get('release_date_for_display'):
                            _genius_metadata['year'] = album_info['release_date_for_display']
                        
                        if len(lyrics) > 20:
                            logger.info(f"Lyrics found from Genius API: {len(lyrics)} chars")
                            return lyrics[:4000]
            except Exception as e:
                logger.info(f"Genius API failed: {e}")
        
        # Source 2: lyrics.ovh
        try:
            sa = search_artist.replace(' ', '%20')
            st = search_title.replace(' ', '%20') if search_title else sa
            url = f"https://api.lyrics.ovh/v1/{sa}/{st}" if search_title else f"https://api.lyrics.ovh/v1/{sa}"
            resp = req_lib.get(url, timeout=10, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                lyrics = data.get('lyrics', '')
                if lyrics and len(lyrics) > 10:
                    logger.info(f"Lyrics found from lyrics.ovh: {len(lyrics)} chars")
                    return lyrics[:4000]
        except Exception as e:
            logger.info(f"lyrics.ovh failed: {e}")
        
        # Source 3: lyrics.fandom
        try:
            search_q = f"{search_artist} {search_title}" if search_title else search_artist
            search_url = f"https://lyrics.fandom.com/api.php?action=query&list=search&srsearch={search_q.replace(' ', '%20')}&format=json"
            resp = req_lib.get(search_url, timeout=10, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                results = data.get('query', {}).get('search', [])
                if results:
                    page_title = results[0].get('title', '')
                    if page_title:
                        lyrics_url = f"https://lyrics.fandom.com/api.php?action=query&titles={page_title.replace(' ', '%20')}&prop=revisions&rvprop=content&format=json"
                        resp2 = req_lib.get(lyrics_url, timeout=10, headers=headers)
                        if resp2.status_code == 200:
                            data2 = resp2.json()
                            pages = data2.get('query', {}).get('pages', {})
                            for page_id, page_data in pages.items():
                                revisions = page_data.get('revisions', [])
                                if revisions:
                                    fc = revisions[0].get('*', '')
                                    lyrics = fc.replace('[[', '').replace(']]', '')
                                    lyrics = re.sub(r'\[.*?\]', '', lyrics)
                                    lyrics = re.sub(r'\{\{.*?\}\}', '', lyrics, flags=re.DOTALL)
                                    lyrics = lyrics.strip()
                                    if len(lyrics) > 20:
                                        logger.info(f"Lyrics found from fandom: {len(lyrics)} chars")
                                        return lyrics[:4000]
        except Exception as e:
            logger.info(f"lyrics.fandom failed: {e}")
        
        # Source 4: textylate
        try:
            tl_q = f"{search_artist} {search_title}" if search_title else search_artist
            resp = req_lib.get(f"https://api.textylate.com/api/lyrics?q={tl_q.replace(' ', '%20')}", timeout=10, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and len(data) > 0:
                    lyrics_lines = [item.get('lyrics', '') for item in data if item.get('lyrics')]
                    lyrics = chr(10).join(lyrics_lines)
                    if len(lyrics) > 20:
                        logger.info(f"Lyrics found from textylate: {len(lyrics)} chars")
                        return lyrics[:4000]
        except Exception as e:
            logger.info(f"textylate failed: {e}")
    
    logger.warning(f"No lyrics found for: {artist_clean} - {title_clean}")
    return None

# ==================== AUDIO PREVIEW ====================
# ==================== AUDIO PREVIEW ====================
def create_preview(input_path, output_path, duration=30):
    cmd = ['ffmpeg', '-y', '-i', str(input_path), '-ss', '0', '-t', str(duration),
           '-c:a', 'libmp3lame', '-b:a', '128k', str(output_path)]
    try:
        subprocess.run(cmd, capture_output=True, timeout=30)
        return output_path.exists()
    except:
        return False

# ==================== DOWNLOAD: AUDIO ====================
async def download_audio(url, chat_id, context, title_override=None, progress_msg_id=None):
    resolved = resolve_short_url(url)
    try:
        info = await extract_info_async(resolved)
        if not info:
            return None, "اطلاعات آهنگ یافت نشد"

        title = title_override or info.get('title', 'Unknown')
        # Prefer uploader (main artist) over artist (may contain "feat." or multiple artists)
        uploader = info.get('uploader', '')
        artist_field = info.get('artist', '')
        # Split on any comma type (English , Persian ، Chinese ， semicolon ;)
        artist_seps = re.split(r'[,，،;；]', artist_field)
        if len(artist_seps) > 1:
            # Multiple artists - use uploader (main artist) or first one
            artist = uploader or artist_seps[0].strip()
        else:
            artist = artist_field or uploader or 'Unknown'
        album = info.get('album') or info.get('playlist_title', '')
        duration = info.get('duration', 0)
        thumbnail = info.get('thumbnail')
        safe_title = re.sub(r'[<>:"/\\|?*]', '_', title[:50])

        def do_download():
            ydl_opts = {
                'outtmpl': str(DOWNLOAD_DIR / 'temp_audio.%(ext)s'),
                'format': 'bestaudio[ext=opus]/bestaudio[ext=m4a]/bestaudio[ext=mp3]/bestaudio/best',
                'postprocessors': [],
                'quiet': True, 'noplaylist': True,
                'cookiefile': str(COOKIE_FILE) if COOKIE_FILE.exists() else None,
                'progress_hooks': [],
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([resolved])

        await asyncio.get_event_loop().run_in_executor(None, do_download)

        temp_file = None
        for f in DOWNLOAD_DIR.glob('temp_audio.*'):
            if f.is_file():
                temp_file = f
                break
        if not temp_file:
            return None, "فایل دانلود نشد"

        input_ext = temp_file.suffix.lower()
        # Always output as MP3 for full metadata support
        output_file = DOWNLOAD_DIR / f"{safe_title}.mp3"

        # Fetch lyrics for embedding
        lyrics = await asyncio.get_event_loop().run_in_executor(
            None, fetch_lyrics_sync, artist, title
        )
        if lyrics:
            logger.info(f"Lyrics found for {title}: {len(lyrics)} chars")
        
        # Override album with Genius data if available
        if _genius_metadata.get('album'):
            info['album'] = _genius_metadata['album']
            logger.info(f"Album from Genius: {info['album']}")
        if _genius_metadata.get('year'):
            upload_date = _genius_metadata['year']
            # Extract year from "March 6, 2025" format
            import re as _re
            year_match = _re.search(r'\b(\d{4})\b', upload_date)
            if year_match:
                info['upload_date'] = year_match.group(1)

        # embed_metadata_ffmpeg returns the actual output file path
        actual_output = await asyncio.get_event_loop().run_in_executor(
            None, embed_metadata_ffmpeg, temp_file, output_file, info, lyrics
        )
        
        # Use the actual output file from embed_metadata_ffmpeg
        if actual_output and Path(actual_output).exists():
            output_file = Path(actual_output)

        if temp_file.exists():
            temp_file.unlink()

        if not output_file.exists():
            return None, "خطا در پردازش فایل"

        # Check file size - auto-compress if too large for Telegram
        file_size = output_file.stat().st_size
        if file_size > TELEGRAM_MAX_FILE:
            compressed = DOWNLOAD_DIR / f"compressed_{safe_title}.mp3"
            # Calculate appropriate bitrate based on duration
            duration = info.get('duration', 300)
            # Target: fit in 48MB (leave 2MB headroom)
            target_bytes = 48 * 1024 * 1024
            target_bitrate_bps = int((target_bytes * 8) / max(duration, 60))
            if target_bitrate_bps > 320000:
                target_bitrate = '320k'
            elif target_bitrate_bps > 192000:
                target_bitrate = '192k'
            elif target_bitrate_bps > 128000:
                target_bitrate = '128k'
            else:
                target_bitrate = '96k'
            
            compress_cmd = ['ffmpeg', '-y', '-i', str(output_file),
                          '-b:a', target_bitrate, '-ar', '44100', '-id3v2_version', '3',
                          str(compressed)]
            try:
                subprocess.run(compress_cmd, capture_output=True, timeout=120)
                if compressed.exists() and compressed.stat().st_size <= TELEGRAM_MAX_FILE:
                    output_file.unlink()
                    output_file = compressed
                    logger.info(f"Compressed {file_size // (1024*1024)}MB to {output_file.stat().st_size // (1024*1024)}MB ({target_bitrate})")
                else:
                    if compressed.exists():
                        compressed.unlink()
                    output_file.unlink()
                    return None, f"فایل خیلی بزرگه ({file_size // (1024*1024)}MB)"
            except Exception as e:
                logger.error(f"Compression failed: {e}")
                if compressed.exists():
                    compressed.unlink()
                output_file.unlink()
                return None, f"فایل خیلی بزرگه ({file_size // (1024*1024)}MB)"

        return output_file, None
    except Exception as e:
        logger.error(f"Download audio error: {e}")
        return None, str(e)[:200]

async def send_audio_file(chat_id, context, output_file, info, history_id=None):
    title = info.get('title', 'Unknown')
    uploader = info.get('uploader', '')
    artist_field = info.get('artist', '')
    # Split on any comma type (English , Persian ، Chinese ， semicolon ;)
    artist_seps = re.split(r'[,，،;；]', artist_field) if artist_field else []
    if len(artist_seps) > 1:
        artist = uploader or artist_seps[0].strip()
    else:
        artist = artist_field or uploader or 'Unknown'
    album = info.get('album') or info.get('playlist_title', '')
    duration = info.get('duration', 0)
    thumbnail = info.get('thumbnail')

    caption = f"🎵 {title}"
    if artist and artist != 'Unknown':
        caption += f"\n🎤 {artist}"
    if album:
        caption += f"\n💿 {album}"

    with open(output_file, 'rb') as f:
        sent = await context.bot.send_audio(
            chat_id=chat_id, audio=f,
            title=title, performer=artist if artist != 'Unknown' else '',
            duration=duration, caption=caption,
            thumbnail=thumbnail if thumbnail else None,
        )

    # Like/Dislike buttons
    like_buttons = [
        InlineKeyboardButton("👍 لایک", callback_data=f"like_{history_id or 0}"),
        InlineKeyboardButton("👎 دیس‌لایک", callback_data=f"dislike_{history_id or 0}"),
        InlineKeyboardButton("🔊 پیش‌نمایش", callback_data=f"preview_{history_id or 0}"),
    ]
    await context.bot.send_message(
        chat_id=chat_id, text="آهنگ رو چطور پیدا کردی؟",
        reply_markup=InlineKeyboardMarkup([like_buttons])
    )

    # Lyrics are already embedded in the file - just notify
    # (No need to send separately anymore)
    logger.info(f"Audio sent: {title} by {artist} (lyrics embedded in file)")

    # Auto-delete file after successful upload to Telegram
    try:
        if output_file.exists():
            output_file.unlink()
            logger.info(f"Auto-deleted: {output_file.name}")
    except Exception as e:
        logger.warning(f"Failed to auto-delete {output_file}: {e}")

    return sent

# ==================== DOWNLOAD: VIDEO ====================
def compress_video(input_path, output_path, target_size_mb=48):
    """Compress video to fit within Telegram's 50MB limit.
    
    Strategy (in order, preferring no quality loss):
    1. Remux to MP4 with faststart (no re-encode, just container optimization)
    2. HEVC/H.265 re-encode (50% more efficient than H.264, similar quality)
    3. H.264 re-encode with calculated bitrate (last resort)
    """
    try:
        file_size = os.path.getsize(input_path)
        target_bytes = target_size_mb * 1024 * 1024
        
        if file_size <= target_bytes:
            # No compression needed, just ensure MP4 with faststart
            if str(input_path).endswith('.mp4'):
                shutil.copy2(str(input_path), str(output_path))
            else:
                cmd = ['ffmpeg', '-y', '-i', str(input_path), '-c', 'copy',
                       '-movflags', '+faststart', str(output_path)]
                subprocess.run(cmd, capture_output=True, timeout=60)
            return True
        
        # Get video duration and current codec
        probe_cmd = ['ffprobe', '-v', 'error', '-show_entries',
                     'format=duration:stream=codec_name,codec_type',
                     '-of', 'json', str(input_path)]
        result = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=30)
        probe = json.loads(result.stdout) if result.stdout else {}
        duration = float(probe.get('format', {}).get('duration', 60))
        
        # Detect current video codec
        video_codec = 'h264'
        for stream in probe.get('streams', []):
            if stream.get('codec_type') == 'video':
                video_codec = stream.get('codec_name', 'h264')
                break
        
        # Strategy 1: Remux with faststart (no re-encode, saves container overhead)
        remux_cmd = [
            'ffmpeg', '-y', '-i', str(input_path),
            '-c', 'copy', '-movflags', '+faststart',
            str(output_path)
        ]
        result = subprocess.run(remux_cmd, capture_output=True, timeout=120)
        if result.returncode == 0 and os.path.exists(output_path):
            remux_size = os.path.getsize(output_path)
            if remux_size <= target_bytes:
                logger.info("video_remux_ok", original=file_size // (1024*1024),
                           compressed=remux_size // (1024*1024))
                return True
        
        # Strategy 2: HEVC/H.265 (50% more efficient, minimal quality loss)
        target_bitrate = int((target_bytes * 8) / duration * 0.85)
        hevc_cmd = [
            'ffmpeg', '-y', '-i', str(input_path),
            '-c:v', 'libx265', '-crf', '28', '-preset', 'fast',
            '-b:v', str(target_bitrate), '-maxrate', str(int(target_bitrate * 1.5)),
            '-pix_fmt', 'yuv420p',
            '-c:a', 'aac', '-b:a', '128k',
            '-movflags', '+faststart',
            str(output_path)
        ]
        result = subprocess.run(hevc_cmd, capture_output=True, timeout=600)
        if result.returncode == 0 and os.path.exists(output_path):
            hevc_size = os.path.getsize(output_path)
            if hevc_size <= target_bytes:
                logger.info("video_hevc_ok", original=file_size // (1024*1024),
                           compressed=hevc_size // (1024*1024))
                return True
        
        # Strategy 3: H.264 with calculated bitrate (last resort)
        h264_cmd = [
            'ffmpeg', '-y', '-i', str(input_path),
            '-c:v', 'libx264', '-b:v', str(target_bitrate),
            '-preset', 'fast',
            '-c:a', 'aac', '-b:a', '128k',
            '-movflags', '+faststart',
            str(output_path)
        ]
        result = subprocess.run(h264_cmd, capture_output=True, timeout=600)
        if result.returncode == 0:
            new_size = os.path.getsize(output_path)
            logger.info("video_h264_ok", original=file_size // (1024*1024),
                       compressed=new_size // (1024*1024))
            return True
        
        logger.error("video_compress_all_failed")
        return False
        
    except Exception as e:
        logger.error("video_compress_error", error=str(e))
        return False

async def download_video(url, chat_id, context, quality='best'):
    resolved = resolve_short_url(url)
    cid = get_correlation_id()
    logger.info("download_video_started", url=url, quality=quality, chat_id=chat_id, correlation_id=cid)
    start_time = time.time()
    
    # Use Railway-aware default quality if not explicitly specified
    if quality == 'best' and RAILWAY_MODE:
        quality = DEFAULT_VIDEO_QUALITY

    platform = get_platform_name(resolved)
    if platform in ('Instagram', 'TikTok', 'Twitter'):
        # Instagram/TikTok/Twitter have limited formats - use best available
        fmt = 'best'
    else:
        format_map = {
            'best': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best',
            '1080p': 'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]',
            '720p': 'bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]',
            '480p': 'bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480]',
            '360p': 'bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/best[height<=360]',
        }
        fmt = format_map.get(quality, format_map['best'])

    try:
        status_msg = await context.bot.send_message(chat_id=chat_id, text=f"🎬 در حال دانلود ویدئو ({quality})...")

        # Download video using async wrapper
        temp_file_path = await download_video_async(resolved, str(DOWNLOAD_DIR), fmt)
        if not temp_file_path:
            await status_msg.edit_text("❌ فایل ویدئو یافت نشد")
            record_download('YouTube', 'video', 'failed', time.time() - start_time)
            record_error('YouTube', "no_file")
            return

        temp_file = Path(temp_file_path)

        # Check duration limit (Railway mode)
        info = await extract_info_async(resolved)
        duration = info.get('duration', 0)
        if MAX_VIDEO_DURATION_SEC > 0 and duration > MAX_VIDEO_DURATION_SEC:
            temp_file.unlink()
            mins = MAX_VIDEO_DURATION_SEC // 60
            await status_msg.edit_text(f"❌ ویدئو خیلی طولانیه ({duration//60}:{duration%60:02d})\n💡 حداکثر {mins} دقیقه در حالت Railway")
            record_download('YouTube', 'video', 'failed', time.time() - start_time)
            record_error('YouTube', "duration_exceeded")
            return

        file_size = temp_file.stat().st_size

        # If video is too large, try to compress it
        if file_size > TELEGRAM_MAX_FILE:
            await status_msg.edit_text(f"📦 ویدئو خیلی بزرگه ({file_size // (1024*1024)}MB)\n🔄 در حال فشرده‌سازی...")

            compressed_file = DOWNLOAD_DIR / "temp_video_compressed.mp4"

            # Try compression with Railway-aware target
            success = await asyncio.get_event_loop().run_in_executor(
                None, compress_video, temp_file, compressed_file, VIDEO_COMPRESSION_TARGET_MB
            )

            if success and compressed_file.exists() and compressed_file.stat().st_size <= TELEGRAM_MAX_FILE:
                # Use compressed version
                temp_file.unlink()
                temp_file = compressed_file
                await status_msg.edit_text("📤 در حال آپلود (فشرده‌سازی شده)...")
            else:
                # Compression failed or still too large
                if compressed_file.exists():
                    compressed_file.unlink()
                await status_msg.edit_text(f"❌ ویدئو خیلی بزرگه ({file_size // (1024*1024)}MB)\n💡 کیفیت پایین‌تری انتخاب کن")
                temp_file.unlink()
                record_download('YouTube', 'video', 'failed', time.time() - start_time)
                record_error('YouTube', "file_too_large")
                return

        await status_msg.edit_text("📤 در حال آپلود...")

        title = info.get('title', 'Video')
        thumbnail = info.get('thumbnail')
        duration = info.get('duration', 0)
        caption = f"🎬 {title}\n📐 کیفیت: {quality}"

        file_size = temp_file.stat().st_size
        
        # For files > 50MB, use chunked upload
        if file_size > TELEGRAM_MAX_FILE:
            await status_msg.edit_text(f"📤 آپلود فایل بزرگ ({file_size // (1024*1024)}MB)...")
            
            async def progress_cb(sent, total):
                try:
                    pct = (sent / total) * 100
                    await status_msg.edit_text(f"📤 آپلود... {pct:.1f}% ({sent // (1024*1024)}/{total // (1024*1024)} MB)")
                except:
                    pass
            
            result = await upload_with_progress(
                bot_token=TOKEN,
                chat_id=chat_id,
                file_path=temp_file,
                method='sendVideo',
                caption=caption,
                title=title,
                duration=duration,
                thumbnail=thumbnail,
                progress_callback=progress_cb,
            )
            
            if result:
                platform = get_platform_name(url)
                db_log(url, title, info.get('uploader', ''), '', platform, 'video', 'success', str(temp_file))
                await status_msg.edit_text("✅ ویدئو ارسال شد!")
                temp_file.unlink()
                record_download('YouTube', 'video', 'success', time.time() - start_time)
                logger.info("download_video_completed", title=title, correlation_id=cid)
                return
            else:
                await status_msg.edit_text("❌ خطا در آپلود فایل بزرگ")
                temp_file.unlink()
                record_download('YouTube', 'video', 'failed', time.time() - start_time)
                record_error('YouTube', "chunked_upload_failed")
                return

        # Regular upload for files <= 50MB
        with open(temp_file, 'rb') as f:
            await context.bot.send_video(
                chat_id=chat_id, video=f, caption=caption,
                duration=duration, supports_streaming=True,
                thumbnail=thumbnail if thumbnail else None,
            )

        platform = get_platform_name(url)
        db_log(url, title, info.get('uploader', ''), '', platform, 'video', 'success', str(temp_file))
        await status_msg.edit_text("✅ ویدئو ارسال شد!")
        temp_file.unlink()
        record_download('YouTube', 'video', 'success', time.time() - start_time)
        logger.info("download_video_completed", title=title, correlation_id=cid)

    except Exception as e:
        logger.error("download_video_error", error=str(e), correlation_id=cid)
        try:
            await status_msg.edit_text(f"❌ خطا: {str(e)[:200]}")
        except:
            pass
        record_download('YouTube', 'video', 'error', time.time() - start_time)
        record_error('YouTube', type(e).__name__)

# ==================== QUALITY SELECTION (YouTube) ====================
async def show_quality_menu(url, update, context):
    """Show quality selection menu with file sizes."""
    chat_id = update.effective_chat.id
    
    # Try to get format info for size estimates
    size_map = {}
    try:
        info = await extract_info_async(url)
        if info and 'formats' in info:
            for fmt in info['formats']:
                height = fmt.get('height')
                ext = fmt.get('ext', '')
                filesize = fmt.get('filesize') or fmt.get('filesize_approx', 0)
                if height and filesize and ext in ('mp4', 'webm'):
                    key = str(height)
                    if key not in size_map or filesize > size_map[key]:
                        size_map[key] = filesize
    except Exception:
        pass
    
    def fmt_size(size_bytes):
        """Format bytes to human readable."""
        if not size_bytes:
            return ""
        mb = size_bytes / (1024 * 1024)
        if mb >= 1:
            return f" (~{mb:.0f}MB)"
        return f" (~{mb*1024:.0f}KB)"
    
    # Build buttons with sizes
    def btn(label, height_key, callback_prefix):
        size = size_map.get(height_key, 0)
        return InlineKeyboardButton(f"{label}{fmt_size(size)}", callback_data=callback_prefix)
    
    keyboard = [
        [btn("🎯 بهترین کیفیت", "best", "vq_best")],
        [btn("📺 1080p Full HD", "1080", "vq_1080p"),
         btn("📺 720p HD", "720", "vq_720p")],
        [btn("📺 480p", "480", "vq_480p"),
         btn("📺 360p", "360", "vq_360p")],
        [InlineKeyboardButton("🎵 فقط صدا (MP3 320k)", callback_data="vq_audio")],
    ]
    await update.message.reply_text(
        "🎬 **انتخاب کیفیت ویدئو:**\n\nلینک شناسایی شد. کیفیت مورد نظر رو انتخاب کن:",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ==================== ARTIST PROFILE ====================
def build_artist_page_text(title, description, tracks, page, per_page=5):
    total_pages = max(1, (len(tracks) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    end = min(start + per_page, len(tracks))
    page_tracks = tracks[start:end]

    text = f"🎤 **{title}**\n"
    if description:
        desc = description[:100] + "..." if len(description) > 100 else description
        text += f"📝 {desc}\n"
    text += f"\n📋 آهنگ‌ها ({start+1}-{end} از {len(tracks)}):\n\n"

    for i, t in enumerate(page_tracks, start + 1):
        dur = ""
        if t.get('duration'):
            m, s = divmod(t['duration'], 60)
            dur = f" ({m}:{s:02d})"
        text += f"**{i}.** {t['title']}{dur}\n"

    if total_pages > 1:
        text += f"\n📄 صفحه {page+1} از {total_pages}"

    kb = []
    for i, t in enumerate(page_tracks, start):
        btn = f"🎵 {t['title'][:35]}"
        if t.get('duration'):
            m, s = divmod(t['duration'], 60)
            btn += f" ({m}:{s:02d})"
        kb.append([InlineKeyboardButton(btn, callback_data=f"dl_{i}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ قبلی", callback_data=f"page_{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("➡️ بعدی", callback_data=f"page_{page+1}"))
    if nav:
        kb.append(nav)

    kb.append([InlineKeyboardButton(f"📥 دانلود همه ({len(tracks)})", callback_data="dl_all")])
    # Add range download button if >5 tracks
    if len(tracks) > 5:
        kb.append([InlineKeyboardButton("🔢 دانلود بازه عددی", callback_data="dl_range_prompt")])
    return text, InlineKeyboardMarkup(kb)

async def show_artist_profile(url, update, context, page=0):
    chat_id = update.effective_chat.id
    status_msg = await update.message.reply_text("🎤 در حال دریافت اطلاعات آرتیست...")

    try:
        resolved = resolve_short_url(url)
        data = await extract_artist_tracks_async(resolved)

        if not data or not data.get('tracks'):
            await status_msg.edit_text("❌ اطلاعات آرتیست یافت نشد.")
            return

        tracks = data['tracks']
        text, kb = build_artist_page_text(data['title'], data.get('description', ''), tracks, page)

        user_sessions[chat_id] = {
            'tracks': tracks, 'artist_name': data['title'],
            'url': url, 'thumbnail': data.get('thumbnail'),
            'description': data.get('description', ''),
        }

        if data.get('thumbnail'):
            await context.bot.send_photo(chat_id=chat_id, photo=data['thumbnail'],
                                         caption=text, parse_mode='Markdown', reply_markup=kb)
        else:
            await context.bot.send_message(chat_id=chat_id, text=text,
                                           parse_mode='Markdown', reply_markup=kb)
        await status_msg.delete()
    except Exception as e:
        logger.error("show_artist_profile_error", error=str(e))
        await status_msg.edit_text(f"❌ خطا: {str(e)[:200]}")

# ==================== CALLBACK HANDLER ====================
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    data = query.data

    # Admin dashboard callbacks (handle first, before other handlers)
    if data.startswith("admin_"):
        if await handle_admin_callback(update, context):
            return

    # Broadcast state: admin typing broadcast message
    if await handle_admin_broadcast(update, context):
        return

    # Cookie-related callbacks
    if data.startswith("cookie_set|") or data.startswith("cookie_del|"):
        if await handle_cookie_callback(query, chat_id, data, context):
            return

    # Video quality selection
    if data.startswith("vq_"):
        quality = data.replace("vq_", "")
        session = user_sessions.get(chat_id, {})
        url = session.get('pending_video_url', '')
        if not url:
            await query.edit_message_text("❌ لینک یافت نشد. دوباره لینک بفرستید.")
            return
        await query.edit_message_text(f"⏳ در حال دانلود ({quality})...")
        if quality == 'audio':
            output_file, err = await download_audio(url, chat_id, context)
            if output_file:
                info = await extract_info_async(resolve_short_url(url))
                hist_id = db_log(url, info.get('title', ''), info.get('uploader', ''), '', get_platform_name(url), 'audio', 'success')
                await send_audio_file(chat_id, context, output_file, info, hist_id)
                if output_file.exists():
                    output_file.unlink()
                await query.edit_message_text("✅ آهنگ ارسال شد!")
            else:
                await query.edit_message_text(f"❌ {err}")
        else:
            await download_video(url, chat_id, context, quality)
        return

    # Like/Dislike
    if data.startswith("like_") or data.startswith("dislike_"):
        hist_id = int(data.split("_")[1])
        action = "like" if data.startswith("like_") else "dislike"
        if hist_id > 0:
            db_log_reaction(hist_id, chat_id, action)
        emoji = "👍 لایک شد!" if action == "like" else "👎 ثبت شد"
        await query.edit_message_text(emoji)
        return

    # Preview
    if data.startswith("preview_"):
        hist_id = int(data.split("_")[1])
        await query.edit_message_text("🔊 در حال ساخت پیش‌نمایش 30 ثانیه‌ای...")
        row = await db_get_download_file(hist_id)
        if row and row.get("file_path") and Path(row["file_path"]).exists():
            preview_path = DOWNLOAD_DIR / f"preview_{hist_id}.mp3"
            ok = await asyncio.get_event_loop().run_in_executor(
                None, create_preview, row["file_path"], preview_path, 30
            )
            if ok and preview_path.exists():
                with open(preview_path, 'rb') as f:
                    await context.bot.send_audio(chat_id=chat_id, audio=f, title=f"Preview: {row['title']}")
                preview_path.unlink()
                await query.edit_message_text("🔊 پیش‌نمایش ارسال شد!")
            else:
                await query.edit_message_text("❌ خطا در ساخت پیش‌نمایش")
        else:
            await query.edit_message_text("❌ فایل یافت نشد (ممکنه پاک شده باشه)")
        return

    session = user_sessions.get(chat_id)
    if not session:
        await query.edit_message_text("❌ جلسه منقضی شده. دوباره لینک بفرستید.")
        return

    tracks = session.get('tracks', [])

    # Download all tracks (must check BEFORE dl_ single track)
    # Range download prompt
    if data == "dl_range_prompt":
        total = len(tracks)
        await query.edit_message_text(
            f"🔢 بازه دانلود رو مشخص کن:\n"
            f"总计 {total} آهنگ\n\n"
            f"مثال:\n"
            f"• `1-10` → آهنگ ۱ تا ۱۰\n"
            f"• `5-15` → آهنگ ۵ تا ۱۵\n"
            f"• `20-{total}` → آهنگ ۲۰ تا آخر\n\n"
            f"فرمت: `شروع-پایان`",
            parse_mode='Markdown'
        )
        # Set state to wait for range input
        user_sessions[chat_id]['waiting_for'] = 'dl_range_input'
        return

    # Range download input handler
    if data == "dl_range_input" or (user_sessions.get(chat_id, {}).get('waiting_for') == 'dl_range_input' and not data.startswith(('dl_', 'page_', 'admin_', 'cookie_', 'vq_', 'like_', 'dislike_', 'preview_', 'search_'))):
        # This is handled in message handler, skip here
        pass

    if data == "dl_all":
        if not tracks:
            await query.edit_message_text("❌ لیست آهنگ‌ها خالیه.")
            return
        artist_name = session.get('artist_name', 'Unknown')
        total = len(tracks)
        await query.edit_message_text(f"⏳ دانلود {total} آهنگ از {artist_name}...")
        
        # Strategy: download tracks one by one, send each immediately
        # This avoids zip size issues and saves temp disk space
        success = 0
        failed = []
        for i, track in enumerate(tracks):
            url = track.get('url') or track.get('id')
            if not url:
                failed.append(track['title'])
                continue
            # Update progress every 5 tracks to avoid Telegram rate limit
            if i % 5 == 0 or i == total - 1:
                try:
                    await context.bot.send_message(chat_id, f"🎵 [{i+1}/{total}] {track['title']}...")
                except Exception:
                    pass
            # Delay between downloads to avoid rate limiting (SoundCloud 403)
            if i > 0:
                await asyncio.sleep(2)
            try:
                output_file, err = await download_audio(url, chat_id, context, track['title'])
                if output_file:
                    # Send individual file directly (skip zip entirely)
                    info = {'title': track['title'], 'artist': track.get('uploader', ''),
                            'album': '', 'duration': track.get('duration', 0), 'thumbnail': track.get('thumbnail')}
                    platform = get_platform_name(url) if url else 'Unknown'
                    hist_id = db_log(url, track['title'], track.get('uploader', ''), '', platform, 'audio', 'success')
                    await send_audio_file(chat_id, context, output_file, info, hist_id)
                    if output_file.exists():
                        output_file.unlink()
                    success += 1
                else:
                    failed.append(f"{track['title']}: {err[:50]}")
            except Exception as e:
                failed.append(f"{track['title']}: {str(e)[:50]}")
        
        # Send summary
        msg = f"✅ {success}/{total} آهنگ دانلود شد!"
        if failed:
            msg += f"\n\n❌ ناموفق ({len(failed)}):\n" + "\n".join(failed[:10])
        await context.bot.send_message(chat_id, msg)
        return

    # Download single track from artist
    if data.startswith("dl_"):
        try:
            idx = int(data.split("_")[1])
        except (ValueError, IndexError):
            await query.edit_message_text("❌ خطا در پردازش درخواست.")
            return
        if 0 <= idx < len(tracks):
            track = tracks[idx]
            url = track.get('url') or track.get('id')
            if url:
                await query.edit_message_text(f"⏳ در حال دانلود: {track['title']}...")
                # Retry once on failure (SoundCloud 403)
                output_file, err = await download_audio(url, chat_id, context, track['title'])
                if not output_file and '403' in str(err):
                    await asyncio.sleep(3)
                    output_file, err = await download_audio(url, chat_id, context, track['title'])
                if output_file:
                    info = {'title': track['title'], 'artist': track.get('uploader', ''),
                            'album': '', 'duration': track.get('duration', 0), 'thumbnail': track.get('thumbnail')}
                    platform = get_platform_name(url)
                    hist_id = db_log(url, track['title'], track.get('uploader', ''), '', platform, 'audio', 'success')
                    await send_audio_file(chat_id, context, output_file, info, hist_id)
                    if output_file.exists():
                        output_file.unlink()
                    await query.edit_message_text(f"✅ {track['title']} ارسال شد!")
                else:
                    await query.edit_message_text(f"❌ خطا: {err[:100]}")

    # Pagination
    elif data.startswith("page_"):
        page = int(data.split("_")[1])
        title = session.get('artist_name', '')
        desc = session.get('description', '')
        thumb = session.get('thumbnail')
        text, kb = build_artist_page_text(title, desc, tracks, page)

        if thumb and query.message.photo:
            try:
                await query.edit_message_caption(caption=text, reply_markup=kb)
            except:
                await query.message.delete()
                await context.bot.send_photo(chat_id=chat_id, photo=thumb,
                                             caption=text, parse_mode='Markdown', reply_markup=kb)
        else:
            try:
                await query.edit_message_text(text=text, parse_mode='Markdown', reply_markup=kb)
            except:
                await query.message.delete()
                await context.bot.send_message(chat_id=chat_id, text=text,
                                               parse_mode='Markdown', reply_markup=kb)

    # Search result click (dl_all handled above, before dl_ single track)
    elif data.startswith("search_"):
        idx = int(data.split("_")[1])
        results = session.get('search_results', [])
        if 0 <= idx < len(results):
            artist_url = results[idx].get('url')
            if artist_url:
                await query.edit_message_text(f"🎤 در حال باز کردن {results[idx]['title']}...")
                resolved = resolve_short_url(artist_url)
                data2 = await extract_artist_tracks_async(resolved)
                if data2 and data2.get('tracks'):
                    all_tracks = data2['tracks']
                    user_sessions[chat_id] = {
                        'tracks': all_tracks, 'artist_name': data2['title'],
                        'url': artist_url, 'thumbnail': data2.get('thumbnail'),
                        'description': data2.get('description', ''),
                    }
                    text, kb = build_artist_page_text(data2['title'], data2.get('description', ''), all_tracks, 0)
                    await query.message.delete()
                    if data2.get('thumbnail'):
                        await context.bot.send_photo(chat_id=chat_id, photo=data2['thumbnail'],
                                                     caption=text, parse_mode='Markdown', reply_markup=kb)
                    else:
                        await context.bot.send_message(chat_id=chat_id, text=text,
                                                       parse_mode='Markdown', reply_markup=kb)
                else:
                    await query.edit_message_text(f"❌ اطلاعاتی یافت نشد.")

# ==================== COMMANDS ====================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎶 **ربات دانلود مدیا v2.0**\n\n"
        "لینک بفرست از هر پلتفرمی:\n\n"
        "🎵 **آهنگ:** SoundCloud / YouTube / Spotify\n"
        "🎬 **ویدئو:** YouTube / Instagram / TikTok / Twitter / Facebook / Twitch\n"
        "🎤 **آرتیست:** پروفایل آرتیست با لیست آهنگ‌ها\n"
        "📋 **پلی‌لیست:** Spotify / SoundCloud playlists\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📌 **دستورات:**\n"
        "`/search <نام>` — جستجوی آرتیست\n"
        "`/cookie` — آپلود کوکی\n"
        "`/removecookie` — حذف کوکی\n"
        "`/history` — تاریخچه دانلودها\n"
        "`/favorites` — آهنگ‌های مورد علاقه\n"
        "`/albums` — آلبوم‌ها\n"
        "`/stats` — آمار سیستم\n"
        "`/logs` — لاگ‌ها (ادمین)\n"
        "`/admin` — داشبورد مدیریت (ادمین)\n"
        "`/help` — راهنما",
        parse_mode='Markdown'
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 **راهنمای ربات:**\n\n"
        "**دانلود آهنگ:**\n"
        "لینک SoundCloud/YouTube/Spotify بفرست → دانلود خودکار\n\n"
        "**دانلود ویدئو:**\n"
        "لینک YouTube بفرست → انتخاب کیفیت (1080p/720p/...)\n"
        "لینک Instagram/TikTok/Twitter → دانلود خودکار\n\n"
        "**آرتیست:**\n"
        "لینک پروفایل آرتیست → لیست آهنگ‌ها با صفحه‌بندی\n\n"
        "**جستجو:** `/search The Weeknd`\n\n"
        "**پیش‌نمایش:** بعد از هر دانلود دکمه 🔊 موجوده\n\n"
        "**متن آهنگ:** خودکار بعد از دانلود ارسال میشه\n\n"
        "**پلتفرم‌ها:**\n"
        "✅ YouTube, SoundCloud, Spotify (artist/playlist)\n"
        "✅ Instagram, TikTok, Twitter/X\n"
        "✅ Facebook, Twitch\n\n"
        "🍪 **کوکی:** `/cookie` — آپلود کوکی برای دور زدن محدودیت YouTube",
        parse_mode='Markdown'
    )

async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ADMIN_ID and update.effective_user.id != ADMIN_ID:
        return
    query = ' '.join(context.args) if context.args else ''
    if not query:
        await update.message.reply_text("🔍 استفاده: `/search <نام آرتیست>`", parse_mode='Markdown')
        return

    status = await update.message.reply_text(f"🔍 جستجوی '{query}'...")
    results = await search_async(query, 5)

    if not results:
        await status.edit_text(f"❌ نتیجه‌ای یافت نشد.")
        return

    text = f"🔍 نتایج **{query}**:\n\n"
    kb = []
    for i, r in enumerate(results):
        text += f"**{i+1}.** {r['title']}"
        if r.get('uploader'):
            text += f" — 🎤 {r['uploader']}"
        text += "\n"
        kb.append([InlineKeyboardButton(f"🎤 {r['title'][:40]}", callback_data=f"search_{i}")])

    user_sessions[update.effective_chat.id] = {'search_results': results}
    await context.bot.send_message(chat_id=update.effective_chat.id, text=text,
                                   parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))
    await status.delete()

async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ADMIN_ID and update.effective_user.id != ADMIN_ID:
        return
    rows = db_get_history(10)
    if not rows:
        await update.message.reply_text("📭 تاریخچه خالیه.")
        return
    text = "📜 **آخرین دانلودها:**\n\n"
    for r in rows:
        hid, title, artist, platform, ctype, status, ts = r
        emoji = "✅" if status == "success" else "❌"
        text += f"{emoji} **{title}** ({platform})\n"
        if artist:
            text += f"   🎤 {artist}\n"
        text += f"   📅 {ts}\n\n"
    await update.message.reply_text(text, parse_mode='Markdown')

async def cmd_favorites(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ADMIN_ID and update.effective_user.id != ADMIN_ID:
        return
    rows = db_get_favorites()
    if not rows:
        await update.message.reply_text("❤️ هنوز آهنگی لایک نکردی.")
        return
    text = "❤️ **آهنگ‌های مورد علاقه:**\n\n"
    for r in rows:
        hid, title, artist, url, ts = r
        text += f"🎵 **{title}**"
        if artist:
            text += f" — {artist}"
        text += "\n"
    await update.message.reply_text(text, parse_mode='Markdown')

async def cmd_albums(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ADMIN_ID and update.effective_user.id != ADMIN_ID:
        return
    rows = db_get_albums()
    if not rows:
        await update.message.reply_text("💿 آلبومی ثبت نشده.")
        return
    text = "💿 **آلبوم‌ها:**\n\n"
    for artist, album, cnt in rows:
        text += f"🎤 **{artist}** — 💿 {album} ({cnt} آهنگ)\n"
    await update.message.reply_text(text, parse_mode='Markdown')

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ADMIN_ID and update.effective_user.id != ADMIN_ID:
        return
    stat = os.statvfs(str(DOWNLOAD_DIR))
    free_gb = (stat.f_bavail * stat.f_frsize) / (1024**3)
    used_mb = sum(f.stat().st_size for f in DOWNLOAD_DIR.iterdir() if f.is_file()) / (1024**2)
    stats = await db_get_stats()
    await update.message.reply_text(
        f"📊 **آمار سیستم:**\n\n"
        f"💾 فضای دیسک آزاد: {free_gb:.2f} GB\n"
        f"📁 حجم فایل‌ها: {used_mb:.1f} MB\n"
        f"📥 کل دانلودها: {stats['total']}\n"
        f"✅ موفق: {stats['success']}\n"
        f"❤️ لایک‌ها: {stats['likes']}\n"
        f"🔄 فایل‌های فعال: {sum(1 for f in DOWNLOAD_DIR.iterdir() if f.is_file())}",
        parse_mode='Markdown'
    )

async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ADMIN_ID and update.effective_user.id != ADMIN_ID:
        return
    rows = db_get_logs(15)
    if not rows:
        await update.message.reply_text("📭 لاگی نیست.")
        return
    text = "📋 **لاگ اخیر:**\n\n"
    for title, platform, status, ts in rows:
        emoji = "✅" if status == "success" else "❌"
        text += f"{emoji} {title} [{platform}] — {ts}\n"
    await update.message.reply_text(text, parse_mode='Markdown')

# ==================== COOKIE UPLOAD STATE ====================
# Track users waiting to upload cookie file: {chat_id: {'platform': str}}
_cookie_upload_state: dict = {}

# Platform buttons for cookie upload
_COOKIE_PLATFORMS = [
    ("🎬 YouTube", "youtube"),
    ("🎵 SoundCloud", "soundcloud"),
    ("📸 Instagram", "instagram"),
    ("🎵 TikTok", "tiktok"),
    ("🐦 Twitter / X", "twitter"),
    ("📺 Facebook", "facebook"),
]

async def cmd_cookie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /cookie command — show platform selection."""
    if ADMIN_ID and update.effective_user.id != ADMIN_ID:
        return
    keyboard = []
    row = []
    for label, slug in _COOKIE_PLATFORMS:
        row.append(InlineKeyboardButton(label, callback_data=f"cookie_set|{slug}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    await update.message.reply_text(
        "🍪 **آپلود کوکی**\n\n"
        "پلتفرم مورد نظر رو انتخاب کن:\n"
        "بعد فایل کوکی رو بفرست (فرمت‌های Netscape، Cookie-Editor JSON، "
        "EditThisCookie JSON، یا HTTP Cookie header)",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

async def cmd_removecookie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /removecookie — delete stored cookies for a platform."""
    if ADMIN_ID and update.effective_user.id != ADMIN_ID:
        return
    manager = get_cookie_manager()
    # Check args
    if context.args:
        platform = context.args[0].lower()
        ok = await manager.delete_cookies(platform)
        if ok:
            await update.message.reply_text(f"🗑 کوکی {platform} حذف شد.")
        else:
            await update.message.reply_text(f"❌ خطا در حذف کوکی {platform}.")
        return

    keyboard = []
    row = []
    for label, slug in _COOKIE_PLATFORMS:
        row.append(InlineKeyboardButton(label, callback_data=f"cookie_del|{slug}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    await update.message.reply_text(
        "🗑 **حذف کوکی**\n\nپلتفرم رو انتخاب کن:",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

async def handle_cookie_callback(query, chat_id, data, context):
    """Handle cookie-related inline button callbacks."""
    # Platform selected for upload
    if data.startswith("cookie_set|"):
        platform = data.split("|", 1)[1]
        _cookie_upload_state[chat_id] = {"platform": platform}
        await query.edit_message_text(
            f"🍪 **آپلود کوکی — {platform}**\n\n"
            "فایل کوکی رو بفرست:\n"
            "• فایل `.txt` یا `.json`\n"
            "• یا متن کوکی رو مستقیم پیست کن\n\n"
            "فرمت‌های پشتیبانی شده:\n"
            "✅ Netscape (Get cookies.txt LOCALLY)\n"
            "✅ Cookie-Editor JSON\n"
            "✅ EditThisCookie JSON\n"
            "✅ HTTP Cookie header\n\n"
            "❌ `/cancel` برای لغو",
            parse_mode='Markdown',
        )
        return True

    # Platform selected for deletion
    if data.startswith("cookie_del|"):
        platform = data.split("|", 1)[1]
        manager = get_cookie_manager()
        ok = await manager.delete_cookies(platform)
        if ok:
            await query.edit_message_text(f"🗑 کوکی {platform} حذف شد.")
        else:
            await query.edit_message_text(f"❌ خطا در حذف کوکی {platform}.")
        return True

    return False

async def _process_cookie_upload(update: Update, context: ContextTypes.DEFAULT_TYPE, content: str, filename: str = ""):
    """Process uploaded cookie content (from file or text)."""
    chat_id = update.effective_chat.id
    state = _cookie_upload_state.get(chat_id)
    if not state:
        return False

    platform = state["platform"]
    status_msg = await update.message.reply_text("🔄 در حال پردازش کوکی...")

    # Convert to Netscape format
    success, result = convert_cookies(content, platform)

    if not success:
        await status_msg.edit_text(
            f"❌ **خطا در پردازش کوکی:**\n\n{result}\n\n"
            "فرمت‌های پشتیبانی شده:\n"
            "• Netscape (Get cookies.txt LOCALLY)\n"
            "• Cookie-Editor JSON\n"
            "• EditThisCookie JSON\n"
            "• HTTP Cookie header (key=value; key=value)",
            parse_mode='Markdown',
        )
        return True

    # Count cookies
    cookie_count = sum(1 for line in result.split('\n')
                       if line.strip() and not line.strip().startswith('#'))

    # Validate and save
    manager = get_cookie_manager()
    ok, msg = await manager.validate_and_save(platform, result)

    if ok:
        await status_msg.edit_text(
            f"✅ **کوکی ذخیره شد!**\n\n"
            f"📡 پلتفرم: `{platform}`\n"
            f"🍪 تعداد کوکی‌ها: {cookie_count}\n"
            f"📁 فرمت: Netscape\n\n"
            f"از این به بعد دانلودها با کوکی انجام میشه.",
            parse_mode='Markdown',
        )
    else:
        await status_msg.edit_text(
            f"❌ **خطا:** {msg}",
            parse_mode='Markdown',
        )

    # Clear state
    _cookie_upload_state.pop(chat_id, None)
    return True

async def handle_cookie_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle document uploads when user is in cookie upload state."""
    chat_id = update.effective_chat.id
    if chat_id not in _cookie_upload_state:
        return False

    doc = update.message.document
    if not doc:
        return False

    # Validate file type
    filename = doc.file_name or ""
    valid_exts = ('.txt', '.json', '.sqlite', '.sqlite3', '.db', '.cookie', '.cookies')
    if not any(filename.lower().endswith(ext) for ext in valid_exts):
        await update.message.reply_text(
            "❌ فرمت فایل نامعتبره.\n"
            "فایل‌های مجاز: `.txt`, `.json`, `.sqlite`, `.db`",
            parse_mode='Markdown',
        )
        return True

    # Download file
    status_msg = await update.message.reply_text("📥 دریافت فایل...")
    try:
        file = await context.bot.get_file(doc.file_id)
        content = await file.download_as_bytearray()
        raw_content = content.decode('utf-8', errors='replace')
    except Exception as e:
        await status_msg.edit_text(f"❌ خطا در دانلود فایل: {e}")
        return True

    await status_msg.delete()
    return await _process_cookie_upload(update, context, raw_content, filename)

async def handle_cookie_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text messages when user is in cookie upload state."""
    chat_id = update.effective_chat.id
    if chat_id not in _cookie_upload_state:
        return False

    text = update.message.text
    if not text:
        return False

    # /cancel to abort
    if text.strip().lower() == '/cancel':
        _cookie_upload_state.pop(chat_id, None)
        await update.message.reply_text("🚫 آپلود کوکی لغو شد.")
        return True

    # Must look like cookie data (key=value or JSON)
    is_cookie = (
        '=' in text and ';' in text  # HTTP header format
        or text.strip().startswith('[')  # JSON array
        or text.strip().startswith('{')  # JSON object
        or '\t' in text  # Netscape format
    )
    if not is_cookie:
        await update.message.reply_text(
            "🤔 این به نظر کوکی نمیاد.\n"
            "لطفاً فایل کوکی رو بفرست یا متن کوکی رو پیست کن.\n\n"
            "❌ `/cancel` برای لغو",
            parse_mode='Markdown',
        )
        return True

    return await _process_cookie_upload(update, context, text)

# ==================== MAIN MESSAGE HANDLER ====================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    # Handle range download input
    session = user_sessions.get(chat_id, {})
    if session.get('waiting_for') == 'dl_range_input':
        user_sessions[chat_id]['waiting_for'] = None
        tracks = session.get('tracks', [])
        text = update.message.text.strip()
        if not tracks:
            await update.message.reply_text("❌ لیست آهنگ‌ها یافت نشد.")
            return
        import re as _re
        range_match = _re.match(r'(\d+)-(\d+)', text)
        if not range_match:
            await update.message.reply_text("❌ فرمت اشتباهه. مثال: `1-10`", parse_mode='Markdown')
            return
        start = int(range_match.group(1))
        end = int(range_match.group(2))
        total = len(tracks)
        if start < 1 or end > total or start > end:
            await update.message.reply_text(f"❌ بازه اشتباهه. محدوده: 1-{total}")
            return
        selected = tracks[start-1:end]
        artist_name = session.get('artist_name', 'Unknown')
        await update.message.reply_text(f"⏳ دانلود {len(selected)} آهنگ ({start}-{end}) از {artist_name}...")
        success = 0
        failed = []
        for i, track in enumerate(selected):
            url = track.get('url') or track.get('id')
            if not url:
                failed.append(track['title'])
                continue
            if i > 0:
                await asyncio.sleep(2)
            try:
                output_file, err = await download_audio(url, chat_id, context, track['title'])
                if output_file:
                    info = {'title': track['title'], 'artist': track.get('uploader', ''),
                            'album': '', 'duration': track.get('duration', 0), 'thumbnail': track.get('thumbnail')}
                    platform = get_platform_name(url) if url else 'Unknown'
                    hist_id = db_log(url, track['title'], track.get('uploader', ''), '', platform, 'audio', 'success')
                    await send_audio_file(chat_id, context, output_file, info, hist_id)
                    if output_file.exists():
                        output_file.unlink()
                    success += 1
                else:
                    failed.append(f"{track['title']}: {err[:50]}")
            except Exception as e:
                failed.append(f"{track['title']}: {str(e)[:50]}")
        msg = f"✅ {success}/{len(selected)} آهنگ دانلود شد!"
        if failed:
            msg += f"\n\n❌ ناموفق ({len(failed)}):\n" + "\n".join(failed[:10])
        await update.message.reply_text(msg)
        return
    
    # Check if user is in cookie upload state — handle cookie text input
    if chat_id in _cookie_upload_state:
        handled = await handle_cookie_text(update, context)
        if handled:
            return
    
    # Check if user is allowed (env var OR DB — either one grants access)
    allowed = False
    db = await get_database()
    # 1) Check ALLOWED_USERS env var first
    if not ALLOWED_USERS or user_id in ALLOWED_USERS:
        allowed = True
    # 2) If not in env var, check DB
    if not allowed and db:
        allowed = await db.is_user_allowed(user_id)
    if not allowed:
        await update.message.reply_text("❌ شما اجازه استفاده از ربات را ندارید.")
        return

    text = update.message.text
    if not text or not text.startswith('http'):
        return

    # Rate limit: per-minute
    if not await check_rate_limit(user_id):
        remaining = await get_remaining_requests(user_id)
        await update.message.reply_text(
            f"⏳ صبر کن! درخواست‌های زیادی فرستادی.\n"
            f"📊 مجاز: {RATE_LIMIT_PER_MINUTE}/دقیقه | باقی‌مانده: {remaining}"
        )
        record_rate_limit(user_id)
        return

    # Rate limit: per-hour
    from rate_limiter import check_rate_limit_hourly
    if not await check_rate_limit_hourly(user_id):
        await update.message.reply_text(
            f"⏳ محدودیت ساعتی! حداکثر {RATE_LIMIT_PER_HOUR} درخواست در ساعت."
        )
        record_rate_limit(user_id)
        return

    # Daily quota check (DB only)
    if db:
        user_info = await db.get_user_stats(user_id)
        daily_limit = user_info["daily_limit"] if user_info else MAX_DOWNLOADS_PER_USER_PER_DAY
        from rate_limiter import check_daily_quota
        allowed, remaining = await check_daily_quota(user_id, daily_limit)
        if not allowed:
            await update.message.reply_text(
                f"📅 سقف دانلود روزانه تمام شد! ({daily_limit}/روز)"
            )
            record_rate_limit(user_id)
            return

    url_type, resolved = detect_url_type(text)
    chat_id = update.effective_chat.id
    platform = get_platform_name(text)

    logger.info("url_received", url=text, url_type=url_type, platform=platform)

    if url_type == 'artist':
        await show_artist_profile(text, update, context)

    elif url_type == 'playlist':
        await show_artist_profile(text, update, context)

    elif url_type == 'track':
        # YouTube / Instagram / TikTok / Twitter → show quality menu for video
        if platform in ('YouTube', 'Instagram', 'TikTok', 'Twitter'):
            user_sessions[chat_id] = {'pending_video_url': text}
            await show_quality_menu(text, update, context)
        else:
            # Audio download for SoundCloud and others
            status_msg = await update.message.reply_text(f"🎵 در حال دانلود از {platform}...")
            start_time = time.time()
            try:
                for attempt in range(3):
                    output_file, err = await download_audio(text, chat_id, context)
                    if output_file:
                        info = await extract_info_async(resolve_short_url(text))
                        if not info:
                            info = {'title': 'Unknown', 'uploader': '', 'album': '',
                                    'duration': 0, 'thumbnail': None}
                        hist_id = db_log(text, info.get('title', ''), info.get('uploader', ''),
                                             info.get('album', ''), platform, 'audio', 'success')
                        await send_audio_file(chat_id, context, output_file, info, hist_id)
                        if output_file.exists():
                            output_file.unlink()
                        await status_msg.edit_text("✅ ارسال شد!")
                        record_download(platform, 'audio', 'success', time.time() - start_time)
                        # Increment user download count
                        if db:
                            await db.increment_user_downloads(user_id)
                        break
                    else:
                        if attempt < 2:
                            await status_msg.edit_text(f"🔄 تلاش مجدد... ({attempt+2}/3)")
                            await asyncio.sleep(2 ** attempt)
                        else:
                            await status_msg.edit_text(f"❌ خطا: {err[:200]}")
                            db_log(text, '', '', '', platform, 'audio', 'failed')
                            record_download(platform, 'audio', 'failed', time.time() - start_time)
                            record_error(platform, "download_failed")
            except Exception as e:
                logger.error("handle_message_error", error=str(e))
                await status_msg.edit_text(f"❌ خطا: {str(e)[:200]}")
                record_download(platform, 'audio', 'error', time.time() - start_time)
                record_error(platform, type(e).__name__)

    elif url_type == 'auto':
        status_msg = await update.message.reply_text("🔍 در حال شناسایی لینک...")
        start_time = time.time()
        try:
            info = await extract_info_async(resolve_short_url(text))
            if info and info.get('entries'):
                await status_msg.delete()
                await show_artist_profile(text, update, context)
            elif info:
                if platform == 'YouTube':
                    await status_msg.delete()
                    user_sessions[chat_id] = {'pending_video_url': text}
                    await show_quality_menu(text, update, context)
                else:
                    await status_msg.edit_text(f"🎵 در حال دانلود از {platform}...")
                    output_file, err = await download_audio(text, chat_id, context)
                    if output_file:
                        await send_audio_file(chat_id, context, output_file, info)
                        if output_file.exists():
                            output_file.unlink()
                        await status_msg.edit_text("✅ ارسال شد!")
                        record_download(platform, 'audio', 'success', time.time() - start_time)
                    else:
                        await status_msg.edit_text(f"❌ {err[:200]}")
                        record_download(platform, 'audio', 'failed', time.time() - start_time)
                        record_error(platform, "download_failed")
        except Exception as e:
            logger.error("auto_url_error", error=str(e))
            await status_msg.edit_text(f"❌ خطا: {str(e)[:200]}")
            record_download(platform, 'audio', 'error', time.time() - start_time)
            record_error(platform, type(e).__name__)

# ==================== CLEANUP ====================
async def scheduled_cleanup():
    while not _shutdown_requested:
        await asyncio.sleep(3600)
        if _shutdown_requested:
            break
        try:
            cutoff = time.time() - 3600
            for f in DOWNLOAD_DIR.iterdir():
                if f.name not in ("cookies.txt",) and f.is_file():
                    if f.stat().st_mtime < cutoff:
                        try:
                            f.unlink()
                        except:
                            pass
        except Exception as e:
            logger.error("cleanup_error", error=str(e))

# ==================== GRACEFUL SHUTDOWN ====================
async def graceful_shutdown(app):
    """Gracefully shutdown the bot - wait for active downloads, clean up."""
    global _shutdown_requested
    _shutdown_requested = True
    logger.info("shutdown_initiated")

    # Stop scheduler
    await stop_scheduler()

    # Stop download queue worker
    queue = await get_download_queue()
    if queue:
        await queue.stop_worker()
    await close_download_queue()
    
    # Stop health check server
    await stop_health_server()
    
    # Wait for active downloads to complete (max 30 seconds)
    wait_start = time.time()
    while active_downloads and (time.time() - wait_start) < 30:
        logger.info("shutdown_waiting_downloads", active=len(active_downloads))
        await asyncio.sleep(1)
    
    # Clean up temp files
    for f in DOWNLOAD_DIR.iterdir():
        if f.is_file() and f.name.startswith("temp_"):
            try:
                f.unlink()
            except:
                pass
    
    # Shutdown executor
    YTDLP_EXECUTOR.shutdown(wait=True, cancel_futures=True)
    
    # Close database connection
    await close_database()
    
    # Stop the application
    await app.shutdown()
    logger.info("shutdown_complete")


async def process_download_task(task: "DownloadTask") -> None:
    """Process a download task from the queue."""
    logger.info("processing_download_task", task_id=task.id, url=task.url, user_id=task.user_id)
    
    try:
        if task.content_type == 'audio':
            output_file, err = await download_audio(task.url, task.chat_id, None, progress_msg_id=None)
            if output_file:
                info = await extract_info_async(resolve_short_url(task.url))
                if not info:
                    info = {'title': task.title or 'Unknown', 'uploader': task.artist or '', 'album': '',
                            'duration': 0, 'thumbnail': None}
                await send_audio_file(task.chat_id, None, output_file, info, task.id)
                if output_file.exists():
                    output_file.unlink()
            else:
                raise Exception(f"Download failed: {err}")
        elif task.content_type == 'video':
            # For video, we need context which we don't have in worker
            # Store result for user to retrieve
            raise Exception("Video downloads not supported in background worker yet")
        else:
            raise Exception(f"Unknown content type: {task.content_type}")
    except Exception as e:
        logger.error("process_download_task_failed", task_id=task.id, error=str(e))
        raise


def signal_handler(signum, frame):
    """Handle SIGTERM/SIGINT for graceful shutdown."""
    global _shutdown_requested
    _shutdown_requested = True
    logger.info("signal_received", signal=signum)


# ==================== ERROR HANDLER ====================
async def error_handler(update, context):
    """Centralized error handler for unhandled exceptions."""
    logger.error("unhandled_error", error=str(context.error), update=str(update)[:200] if update else "None")
    if update and update.effective_message:
        try:
            await update.effective_message.reply_text("❌ خطای پیش‌بینی نشده‌ای رخ داد. لطفاً دوباره تلاش کنید.")
        except Exception:
            pass
# ==================== MAIN ====================
async def main():
    # Initialize database
    await init_db()
    
    # Initialize download queue
    await get_download_queue()
    
    # Initialize cookie manager
    get_cookie_manager()
    
    # Register signal handlers
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    app = Application.builder().token(TOKEN).build()
    app.add_error_handler(error_handler)
    
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("favorites", cmd_favorites))
    app.add_handler(CommandHandler("albums", cmd_albums))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CommandHandler("cookie", cmd_cookie))
    app.add_handler(CommandHandler("removecookie", cmd_removecookie))
    # Admin commands
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("adduser", cmd_adduser))
    app.add_handler(CommandHandler("removeuser", cmd_removeuser))
    app.add_handler(CommandHandler("listusers", cmd_listusers))
    app.add_handler(CommandHandler("toggleuser", cmd_toggleuser))
    app.add_handler(CommandHandler("userinfo", cmd_userinfo))
    app.add_handler(CallbackQueryHandler(button_callback))
    # Document handler for cookie upload (must be before text handler)
    app.add_handler(MessageHandler(filters.Document.ALL, handle_cookie_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Start background tasks - use APScheduler for proper cron jobs
    loop = asyncio.get_event_loop()
    await start_scheduler()

    # Start health check server in background
    loop.create_task(start_health_server())
    
    # Initialize metrics
    init_metrics("2.0")
    
    # Start metrics server on separate port (PORT + 1)
    metrics_port = settings.port + 1
    metrics_app = setup_metrics_app()
    metrics_runner = web.AppRunner(metrics_app)
    await metrics_runner.setup()
    metrics_site = web.TCPSite(metrics_runner, "0.0.0.0", metrics_port)
    await metrics_site.start()
    logger.info("metrics_server_started", port=metrics_port)
    
    # Start queue stats updater
    loop.create_task(update_queue_stats_periodically())
    
    # Start download queue worker
    if settings.redis_url:
        queue = await get_download_queue()
        await queue.start_worker(process_download_task)
        logger.info("download_queue_worker_started")
    
    logger.info("media_bot_started", version="2.0", railway_mode=RAILWAY_MODE)
    
    # Webhook mode for Railway (cost-effective)
    if settings.webhook_mode and settings.webhook_url:
        logger.info("starting_webhook_mode", webhook_url=settings.webhook_url, port=settings.port)
        await app.initialize()
        await app.start()
        await app.updater.start_webhook(
            listen="0.0.0.0",
            port=settings.port,
            url_path="/webhook",
            webhook_url=settings.webhook_url,
            drop_pending_updates=True,
        )
        # Keep running until shutdown
        try:
            while not _shutdown_requested:
                await asyncio.sleep(1)
        finally:
            await graceful_shutdown(app)
            await metrics_runner.cleanup()
    else:
        logger.info("starting_polling_mode")
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        try:
            while not _shutdown_requested:
                await asyncio.sleep(1)
        finally:
            await graceful_shutdown(app)
            await metrics_runner.cleanup()

async def update_queue_stats_periodically():
    """Periodically update queue stats metrics."""
    while not _shutdown_requested:
        try:
            if settings.redis_url:
                queue = await get_download_queue()
                stats = await queue.get_queue_stats()
                set_queue_stats(stats["pending"], stats["processing"])
        except Exception as e:
            logger.error("queue_stats_update_error", error=str(e))
        await asyncio.sleep(30)

if __name__ == "__main__":
    asyncio.run(main())
