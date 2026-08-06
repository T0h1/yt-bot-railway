import os
import io
import re
import time
import json
import shutil
import asyncio
import signal
import subprocess
import urllib.request
from pathlib import Path
from datetime import datetime, timedelta
from urllib.parse import urlparse, urlunparse
from concurrent.futures import ThreadPoolExecutor

import requests as req_lib
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
from chunked_upload import upload_with_progress

# ==================== SETUP ====================
# Initialize structured logging
setup_logging(log_level=settings.log_level, json_output=settings.log_json)
logger = get_logger("mediabot")

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "media_downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)
COOKIE_FILE = BASE_DIR / "youtube_downloads" / "cookies.txt"

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

async def init_db():
    """Initialize database connection and schema."""
    db = await get_database()
    if db:
        logger.info("database_initialized")
    else:
        logger.info("running_without_database")

def db_log(url, title, artist, album, platform, content_type, status, file_path=""):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO download_history (url,title,artist,album,platform,content_type,status,file_path) VALUES (?,?,?,?,?,?,?,?)",
        (url, title, artist, album, platform, content_type, status, file_path)
    )
    conn.commit()
    hist_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    return hist_id

def db_get_history(limit=10):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id,title,artist,platform,content_type,status,timestamp FROM download_history ORDER BY id DESC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()
    return rows

def db_get_favorites():
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

def db_get_albums():
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

def db_log_reaction(history_id, user_id, action):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO user_reactions (history_id, user_id, action) VALUES (?,?,?)",
        (history_id, user_id, action)
    )
    conn.commit()
    conn.close()

def db_get_logs(limit=15):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT title,platform,status,timestamp FROM download_history ORDER BY id DESC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()
    return rows

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
    album = info.get('album') or info.get('playlist_title', '') or info.get('series', '')
    genre = info.get('genre') or info.get('categories', [''])[0] if info.get('categories') else ''
    thumbnail = info.get('thumbnail')
    input_ext = Path(input_path).suffix.lower()
    
    # Smart genre detection from tags/categories
    if not genre:
        tags = info.get('tags', [])
        categories = info.get('categories', [])
        all_tags = [t.lower() for t in (tags + categories)]
        genre_map = {
            'hip hop': ['hip-hop', 'hip hop', 'rap', 'hiphop'],
            'pop': ['pop', 'pop music'],
            'rock': ['rock', 'alternative', 'indie'],
            'electronic': ['electronic', 'edm', 'dance', 'house', 'techno', 'trance'],
            'r&b': ['r&b', 'rnb', 'rhythm and blues', 'soul'],
            'jazz': ['jazz', 'blues'],
            'classical': ['classical', 'orchestra', 'symphony'],
            'metal': ['metal', 'heavy metal', 'death metal'],
            'reggae': ['reggae', 'ska', 'dancehall'],
            'country': ['country', 'folk'],
            'latin': ['latin', 'reggaeton', 'bachata'],
            'k-pop': ['k-pop', 'kpop', 'korean'],
        }
        for genre_name, keywords in genre_map.items():
            for tag in all_tags:
                if any(kw in tag for kw in keywords):
                    genre = genre_name
                    break
            if genre:
                break
    
    # Clean title for album detection
    # If title contains " - " it might be "Artist - Title" format
    clean_title = title
    if ' - ' in title and not album:
        parts = title.split(' - ', 1)
        if len(parts) == 2:
            # Check if first part looks like an artist
            potential_artist = parts[0].strip()
            potential_title = parts[1].strip()
            # Only split if artist matches
            if potential_artist.lower() in artist.lower() or artist.lower() in potential_artist.lower():
                clean_title = potential_title

    # ALWAYS output as MP3 for full metadata support
    output_file = DOWNLOAD_DIR / (Path(output_path).stem + '.mp3')

    # Download cover art with proper headers
    cover_path = None
    if thumbnail:
        cover_path = str(input_path) + '.cover.jpg'
        if not download_cover(thumbnail, cover_path):
            # Try alternative thumbnail URLs
            alt_urls = []
            if 'i1.sndcdn.com' in thumbnail:
                alt_urls = [
                    thumbnail.replace('-large', '-t300x300'),
                    thumbnail.replace('-large', '-t500x500'),
                    thumbnail.replace('-t100x100', '-t500x500'),
                    thumbnail.replace('-t100x100', '-t300x300'),
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

    # Build base metadata args
    base_meta = ['-metadata', f'title={clean_title}', '-metadata', f'artist={artist}', '-metadata', f'album={album}']
    if genre:
        base_meta += ['-metadata', f'genre={genre}']
    
    # Add lyrics metadata if available
    if lyrics:
        base_meta += ['-metadata', f'lyrics={lyrics[:4000]}']

    has_cover = cover_path and os.path.exists(cover_path)

    # === SMART CONVERSION LOGIC ===
    # Source is already MP3 → just remux metadata (no quality loss)
    if input_ext == '.mp3':
        cmd = ['ffmpeg', '-y', '-i', str(input_path)]
        if has_cover:
            cmd += ['-i', cover_path, '-map', '0:a', '-map', '1:0']
        else:
            cmd += ['-map', '0:a']
        cmd += ['-c:a', 'copy', '-id3v2_version', '3'] + base_meta + [str(output_file)]
    else:
        # Source is m4a/opus/flac/aac/webm → convert to MP3 320kbps
        cmd = ['ffmpeg', '-y', '-i', str(input_path)]
        if has_cover:
            cmd += ['-i', cover_path, '-map', '0:a', '-map', '1:0']
        else:
            cmd += ['-map', '0:a']
        
        # Best quality MP3 encoding settings
        cmd += [
            '-c:a', 'libmp3lame',
            '-b:a', '320k',           # 320kbps CBR - maximum MP3 quality
            '-ar', '44100',           # Standard CD sample rate
            '-reservoir', '1',        # Enable reservoir for better quality
            '-q:a', '0',             # VBR quality 0 (highest) - redundant but ensures best
            '-id3v2_version', '3',    # ID3v2.3 for best player compatibility
            '-write_id3v1', '1',      # Also write ID3v1 tag for old players
        ] + base_meta + [str(output_file)]

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=180)
        if result.returncode != 0:
            logger.error(f"ffmpeg stderr: {result.stderr.decode()[:500]}")
        else:
            logger.info(f"Metadata embedded: cover={has_cover}, lyrics={'yes' if lyrics else 'no'}, format=mp3-320k")
    except Exception as e:
        logger.error(f"ffmpeg exception: {e}")
        shutil.copy2(str(input_path), str(output_file))
    finally:
        if cover_path and os.path.exists(cover_path):
            os.remove(cover_path)
        lyrics_file = str(input_path) + '.lyrics.txt'
        if os.path.exists(lyrics_file):
            os.remove(lyrics_file)

    # Use mutagen to properly embed lyrics (USLT frame) + album/genre (TALB/TCON)
    if output_file.exists():
        try:
            from mutagen.mp3 import MP3
            from mutagen.id3 import ID3, USLT, TALB, TCON, TPE2, TDRC
            import mutagen
            
            audio = MP3(str(output_file))
            if audio.tags is None:
                audio.add_tags()
            
            # Add album (TALB frame)
            if album:
                audio.tags.add(TALB(encoding=3, text=album))
            
            # Add genre (TCON frame)
            if genre:
                audio.tags.add(TCON(encoding=3, text=genre))
            
            # Add year if available
            upload_date = info.get('upload_date', '')
            if upload_date and len(upload_date) == 8:
                audio.tags.add(TDRC(encoding=3, text=upload_date[:4]))
            
            # Add lyrics (USLT frame) - this is what players actually read
            if lyrics:
                audio.tags.add(USLT(
                    encoding=3,  # UTF-8
                    lang='eng',
                    desc='',
                    text=lyrics[:4000]
                ))
            
            audio.save()
            logger.info(f"Mutagen tags: album={album}, genre={genre}, lyrics={'yes' if lyrics else 'no'}")
        except Exception as e:
            logger.error(f"Mutagen embed error: {e}")

    return output_file

# ==================== LYRICS ====================
def fetch_lyrics_sync(artist, title):
    """Fetch lyrics from multiple sources with fallback"""
    # Clean artist/title for better search results
    artist_clean = re.sub(r'[，。、；：！？（）【】「」《》""''…—]', ' ', artist).strip()
    artist_clean = re.sub(r'\s+', ' ', artist_clean)
    title_clean = re.sub(r'[，。、；：！？（）【】「」《》""''…—]', ' ', title).strip()
    title_clean = re.sub(r'\s+', ' ', title_clean)
    
    # Remove common suffixes that break search
    for suffix in ['Official Video', 'Official Audio', 'Lyrics', 'Music Video', 
                   'Official Music Video', 'Audio', 'Video', 'HD', '4K',
                   '(Official Video)', '(Official Audio)', '(Lyrics)', 
                   '[Official Video]', '[Official Audio]', '[Lyrics]']:
        title_clean = title_clean.replace(suffix, '').strip()
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'application/json',
    }
    
    logger.info(f"Searching lyrics for: {artist_clean} - {title_clean}")
    
    # Source 1: lyrics.ovh (most reliable)
    try:
        search_artist = artist_clean.replace(' ', '%20')
        search_title = title_clean.replace(' ', '%20')
        resp = req_lib.get(f"https://api.lyrics.ovh/v1/{search_artist}/{search_title}", timeout=10, headers=headers)
        if resp.status_code == 200:
            data = resp.json()
            lyrics = data.get('lyrics', '')
            if lyrics and len(lyrics) > 10:
                logger.info(f"Lyrics found from lyrics.ovh: {len(lyrics)} chars")
                return lyrics[:4000]
    except Exception as e:
        logger.debug(f"lyrics.ovh failed: {e}")
    
    # Source 2: lyrics.fandom (via MediaWiki API)
    try:
        search_url = f"https://lyrics.fandom.com/api.php?action=query&list=search&srsearch={artist_clean}%20{title_clean}&format=json"
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
                                content = revisions[0].get('*', '')
                                lyrics = content.replace('[[', '').replace(']]', '')
                                lyrics = re.sub(r'\[.*?\]', '', lyrics)
                                lyrics = re.sub(r'\{\{.*?\}\}', '', lyrics, flags=re.DOTALL)
                                lyrics = lyrics.strip()
                                if len(lyrics) > 20:
                                    logger.info(f"Lyrics found from fandom: {len(lyrics)} chars")
                                    return lyrics[:4000]
    except Exception as e:
        logger.debug(f"lyrics.fandom failed: {e}")

    # Source 3: Genius API (scrape search page)
    try:
        search_url = f"https://genius.com/api/search?q={artist_clean}%20{title_clean}"
        resp = req_lib.get(search_url, timeout=10, headers=headers)
        if resp.status_code == 200:
            data = resp.json()
            hits = data.get('response', {}).get('hits', [])
            if hits:
                song_url = hits[0].get('result', {}).get('url', '')
                if song_url:
                    resp2 = req_lib.get(song_url, timeout=10, headers=headers)
                    if resp2.status_code == 200:
                        match = re.search(r'<div[^>]*data-lyrics-container="true"[^>]*>(.*?)</div>', resp2.text, re.DOTALL)
                        if match:
                            lyrics_html = match.group(1)
                            lyrics = re.sub(r'<br\s*/?>', '\n', lyrics_html)
                            lyrics = re.sub(r'<[^>]+>', '', lyrics)
                            lyrics = html.unescape(lyrics).strip()
                            if len(lyrics) > 20:
                                logger.info(f"Lyrics found from Genius: {len(lyrics)} chars")
                                return lyrics[:4000]
    except Exception as e:
        logger.debug(f"Genius failed: {e}")

    # Source 4: Try simple lyrics API
    try:
        resp = req_lib.get(f"https://api.textylate.com/api/lyrics?q={artist_clean}%20{title_clean}", timeout=10, headers=headers)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list) and len(data) > 0:
                lyrics_lines = [item.get('lyrics', '') for item in data if item.get('lyrics')]
                lyrics = '\n'.join(lyrics_lines)
                if len(lyrics) > 20:
                    logger.info(f"Lyrics found from textylate: {len(lyrics)} chars")
                    return lyrics[:4000]
    except Exception as e:
        logger.debug(f"textylate failed: {e}")

    logger.warning(f"No lyrics found for: {artist_clean} - {title_clean}")
    return None

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
        info = await asyncio.get_event_loop().run_in_executor(None, extract_info_sync, resolved)
        if not info:
            return None, "اطلاعات آهنگ یافت نشد"

        title = title_override or info.get('title', 'Unknown')
        artist = info.get('artist') or info.get('uploader', 'Unknown')
        album = info.get('album') or info.get('playlist_title', '')
        duration = info.get('duration', 0)
        thumbnail = info.get('thumbnail')
        safe_title = re.sub(r'[<>:"/\\|?*]', '_', title[:50])

        def do_download():
            ydl_opts = {
                'outtmpl': str(DOWNLOAD_DIR / 'temp_audio.%(ext)s'),
                'format': 'bestaudio/best',
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

        # Check file size
        file_size = output_file.stat().st_size
        if file_size > TELEGRAM_MAX_FILE:
            output_file.unlink()
            return None, f"فایل خیلی بزرگه ({file_size // (1024*1024)}MB)"

        return output_file, None
    except Exception as e:
        logger.error(f"Download audio error: {e}")
        return None, str(e)[:200]

async def send_audio_file(chat_id, context, output_file, info, history_id=None):
    title = info.get('title', 'Unknown')
    artist = info.get('artist') or info.get('uploader', 'Unknown')
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
    """Compress video to fit within Telegram's 50MB limit"""
    try:
        # Get current size
        file_size = os.path.getsize(input_path)
        target_bytes = target_size_mb * 1024 * 1024
        
        if file_size <= target_bytes:
            # No compression needed
            shutil.copy2(str(input_path), str(output_path))
            return True
        
        # Get video duration for bitrate calculation
        probe_cmd = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                    '-of', 'default=noprint_wrappers=1:nokey=1', str(input_path)]
        result = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=30)
        duration = float(result.stdout.strip()) if result.stdout.strip() else 60
        
        # Calculate target bitrate (leave some room for audio)
        target_bitrate = int((target_bytes * 8) / duration * 0.85)  # 85% for video, 15% for audio
        
        # Compress with ffmpeg
        cmd = [
            'ffmpeg', '-y', '-i', str(input_path),
            '-c:v', 'libx264',
            '-b:v', str(target_bitrate),
            '-preset', 'fast',  # Balance between speed and quality
            '-c:a', 'aac',
            '-b:a', '128k',
            '-movflags', '+faststart',
            str(output_path)
        ]
        
        result = subprocess.run(cmd, capture_output=True, timeout=300)
        if result.returncode == 0:
            new_size = os.path.getsize(output_path)
            logger.info(f"Video compressed: {file_size // (1024*1024)}MB -> {new_size // (1024*1024)}MB")
            return True
        else:
            logger.error(f"Compression failed: {result.stderr.decode()[:200]}")
            return False
    except Exception as e:
        logger.error(f"Compression error: {e}")
        return False

async def download_video(url, chat_id, context, quality='best'):
    resolved = resolve_short_url(url)
    cid = get_correlation_id()
    logger.info("download_video_started", url=url, quality=quality, chat_id=chat_id, correlation_id=cid)
    start_time = time.time()
    
    # Use Railway-aware default quality if not explicitly specified
    if quality == 'best' and RAILWAY_MODE:
        quality = DEFAULT_VIDEO_QUALITY

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
                await db_log(url, title, info.get('uploader', ''), '', platform, 'video', 'success', str(temp_file))
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
        await db_log(url, title, info.get('uploader', ''), '', platform, 'video', 'success', str(temp_file))
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
    keyboard = [
        [InlineKeyboardButton("🎯 بهترین کیفیت", callback_data=f"vq_best|{url[:80]}")],
        [InlineKeyboardButton("📺 1080p Full HD", callback_data=f"vq_1080p|{url[:80]}"),
         InlineKeyboardButton("📺 720p HD", callback_data=f"vq_720p|{url[:80]}")],
        [InlineKeyboardButton("📺 480p", callback_data=f"vq_480p|{url[:80]}"),
         InlineKeyboardButton("📺 360p", callback_data=f"vq_360p|{url[:80]}")],
        [InlineKeyboardButton("🎵 فقط صدا (MP3 320k)", callback_data=f"vq_audio|{url[:80]}")],
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

    # Cookie-related callbacks
    if data.startswith("cookie_set|") or data.startswith("cookie_del|"):
        if await handle_cookie_callback(query, chat_id, data, context):
            return

    # Video quality selection
    if data.startswith("vq_"):
        parts = data.split("|", 1)
        quality = parts[0].replace("vq_", "")
        url = parts[1] if len(parts) > 1 else ""
        if not url:
            session = user_sessions.get(chat_id, {})
            url = session.get('pending_video_url', '')
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

    # Download single track from artist
    if data.startswith("dl_"):
        idx = int(data.split("_")[1])
        if 0 <= idx < len(tracks):
            track = tracks[idx]
            url = track.get('url') or track.get('id')
            if url:
                await query.edit_message_text(f"⏳ در حال دانلود: {track['title']}...")
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

    # Download all tracks
    elif data == "dl_all":
        await query.edit_message_text(f"⏳ دانلود {len(tracks)} آهنگ...")
        success = 0
        for i, track in enumerate(tracks):
            url = track.get('url') or track.get('id')
            if url:
                await context.bot.send_message(chat_id, f"🎵 [{i+1}/{len(tracks)}] {track['title']}")
                output_file, err = await download_audio(url, chat_id, context, track['title'])
                if output_file:
                    info = {'title': track['title'], 'artist': track.get('uploader', ''),
                            'album': '', 'duration': track.get('duration', 0), 'thumbnail': track.get('thumbnail')}
                    await send_audio_file(chat_id, context, output_file, info)
                    if output_file.exists():
                        output_file.unlink()
                    success += 1
        await context.bot.send_message(chat_id, f"✅ {success}/{len(tracks)} آهنگ دانلود شد!")

    # Search result click
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
    if ADMIN_ID and update.effective_user.id != ADMIN_ID:
        return
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
        "`/help` — راهنما",
        parse_mode='Markdown'
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ADMIN_ID and update.effective_user.id != ADMIN_ID:
        return
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
    
    # Check if user is in cookie upload state — handle cookie text input
    if chat_id in _cookie_upload_state:
        handled = await handle_cookie_text(update, context)
        if handled:
            return
    
    # Check if user is allowed (if ALLOWED_USERS is configured)
    if ALLOWED_USERS and user_id not in ALLOWED_USERS:
        await update.message.reply_text("❌ شما اجازه استفاده از ربات را ندارید.")
        return
    
    # Check user quota
    if settings.postgres_dsn:
        from database import get_database
        db = await get_database()
        if db:
            user = await db.get_or_create_user(user_id, 
                update.effective_user.username or "",
                update.effective_user.first_name or "",
                update.effective_user.last_name or "")
            
            if user.get("is_banned"):
                await update.message.reply_text("❌ شما از استفاده از ربات محروم شده‌اید.")
                return
                
            allowed, remaining = await db.check_user_quota(user_id)
            if not allowed:
                await update.message.reply_text(f"❌ سهمیه روزانه شما تمام شده است ({MAX_DOWNLOADS_PER_USER_PER_DAY} دانلود در روز).")
                return

    text = update.message.text
    if not text or not text.startswith('http'):
        return

    # Rate limit using new rate limiter
    if not await check_rate_limit(user_id):
        await update.message.reply_text("⏳ صبر کن! زیاد دانلود کردی (5 دانلود در دقیقه).")
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
        # YouTube video → show quality menu
        if platform == 'YouTube':
            user_sessions[chat_id] = {'pending_video_url': text}
            await show_quality_menu(text, update, context)
        else:
            # Audio download for all other platforms
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
                        hist_id = await db_log(text, info.get('title', ''), info.get('uploader', ''),
                                             info.get('album', ''), platform, 'audio', 'success')
                        await send_audio_file(chat_id, context, output_file, info, hist_id)
                        if output_file.exists():
                            output_file.unlink()
                        await status_msg.edit_text("✅ ارسال شد!")
                        record_download(platform, 'audio', 'success', time.time() - start_time)
                        # Increment user download count
                        if settings.postgres_dsn:
                            from database import get_database
                            db = await get_database()
                            if db:
                                await db.increment_user_downloads(user_id)
                        break
                    else:
                        if attempt < 2:
                            await status_msg.edit_text(f"🔄 تلاش مجدد... ({attempt+2}/3)")
                            await asyncio.sleep(2 ** attempt)
                        else:
                            await status_msg.edit_text(f"❌ خطا: {err[:200]}")
                            await db_log(text, '', '', '', platform, 'audio', 'failed')
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
    app.add_handler(CallbackQueryHandler(button_callback))
    # Document handler for cookie upload (must be before text handler)
    app.add_handler(MessageHandler(filters.Document.ALL, handle_cookie_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Start background tasks
    loop = asyncio.get_event_loop()
    loop.create_task(scheduled_cleanup())
    
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
