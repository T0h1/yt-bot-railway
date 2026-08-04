import os
import io
import re
import time
import json
import shutil
import sqlite3
import asyncio
import logging
import subprocess
import urllib.request
from pathlib import Path
from datetime import datetime, timedelta
from urllib.parse import urlparse, urlunparse

import requests as req_lib
import yt_dlp
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)

# ==================== SETUP ====================
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("MediaBot")

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "media_downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)
COOKIE_FILE = BASE_DIR / "youtube_downloads" / "cookies.txt"
DB_PATH = BASE_DIR / "bot_data.db"

load_dotenv(dotenv_path=BASE_DIR / ".env_ytdl")
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN_YTDL") or os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID_YTDL") or os.getenv("ADMIN_ID") or 0)

# In-memory state
user_sessions = {}
download_queue = asyncio.Queue()
active_downloads = {}
rate_limit_store = {}  # user_id -> [timestamps]

TELEGRAM_MAX_FILE = 49 * 1024 * 1024  # ~50MB Telegram limit

# ==================== DATABASE ====================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS download_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        url TEXT, title TEXT, artist TEXT, album TEXT,
        platform TEXT, content_type TEXT, status TEXT,
        file_path TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_reactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        history_id INTEGER, user_id INTEGER, action TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    conn.commit()
    conn.close()

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
def check_rate_limit(user_id):
    now = time.time()
    if user_id not in rate_limit_store:
        rate_limit_store[user_id] = []
    rate_limit_store[user_id] = [t for t in rate_limit_store[user_id] if now - t < 60]
    if len(rate_limit_store[user_id]) >= 5:
        return False
    rate_limit_store[user_id].append(now)
    return True

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

# ==================== YT-DLP HELPERS ====================
def extract_info_sync(url, extra_opts=None):
    opts = {
        'quiet': True, 'no_warnings': True, 'skip_download': True,
        'noplaylist': True,
        'cookiefile': str(COOKIE_FILE) if COOKIE_FILE.exists() else None,
    }
    if extra_opts:
        opts.update(extra_opts)
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)

def extract_artist_tracks_sync(url):
    opts = {
        'quiet': True, 'no_warnings': True, 'skip_download': True,
        'extract_flat': True,
        'cookiefile': str(COOKIE_FILE) if COOKIE_FILE.exists() else None,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
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

def search_sync(query, max_results=5):
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
        logger.error(f"Search error: {e}")
        return []

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
async def download_video(url, chat_id, context, quality='best'):
    resolved = resolve_short_url(url)
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

        def do_download():
            opts = {
                'outtmpl': str(DOWNLOAD_DIR / 'temp_video.%(ext)s'),
                'format': fmt,
                'merge_output_format': 'mp4',
                'quiet': True,
                'cookiefile': str(COOKIE_FILE) if COOKIE_FILE.exists() else None,
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(resolved, download=True)

        info = await asyncio.get_event_loop().run_in_executor(None, do_download)

        temp_file = None
        for f in DOWNLOAD_DIR.glob('temp_video.*'):
            if f.is_file():
                temp_file = f
                break

        if not temp_file:
            await status_msg.edit_text("❌ فایل ویدئو یافت نشد")
            return

        file_size = temp_file.stat().st_size
        if file_size > TELEGRAM_MAX_FILE:
            await status_msg.edit_text(f"❌ ویدئو خیلی بزرگه ({file_size // (1024*1024)}MB)")
            temp_file.unlink()
            return

        await status_msg.edit_text("📤 در حال آپلود...")

        title = info.get('title', 'Video')
        thumbnail = info.get('thumbnail')
        duration = info.get('duration', 0)
        caption = f"🎬 {title}\n📐 کیفیت: {quality}"

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

    except Exception as e:
        logger.error(f"Video download error: {e}")
        try:
            await status_msg.edit_text(f"❌ خطا: {str(e)[:200]}")
        except:
            pass

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
        data = await asyncio.get_event_loop().run_in_executor(None, extract_artist_tracks_sync, resolved)

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
        await status_msg.edit_text(f"❌ خطا: {str(e)[:200]}")

# ==================== CALLBACK HANDLER ====================
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    data = query.data

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
                info = await asyncio.get_event_loop().run_in_executor(None, extract_info_sync, resolve_short_url(url))
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
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute("SELECT file_path, title FROM download_history WHERE id=?", (hist_id,)).fetchone()
        conn.close()
        if row and row[0] and Path(row[0]).exists():
            preview_path = DOWNLOAD_DIR / f"preview_{hist_id}.mp3"
            ok = await asyncio.get_event_loop().run_in_executor(
                None, create_preview, row[0], preview_path, 30
            )
            if ok and preview_path.exists():
                with open(preview_path, 'rb') as f:
                    await context.bot.send_audio(chat_id=chat_id, audio=f, title=f"Preview: {row[1]}")
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
                data2 = await asyncio.get_event_loop().run_in_executor(None, extract_artist_tracks_sync, resolved)
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
        "✅ Facebook, Twitch",
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
    results = await asyncio.get_event_loop().run_in_executor(None, search_sync, query, 5)

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
    conn = sqlite3.connect(DB_PATH)
    total_dl = conn.execute("SELECT COUNT(*) FROM download_history").fetchone()[0]
    success_dl = conn.execute("SELECT COUNT(*) FROM download_history WHERE status='success'").fetchone()[0]
    likes = conn.execute("SELECT COUNT(*) FROM user_reactions WHERE action='like'").fetchone()[0]
    conn.close()
    await update.message.reply_text(
        f"📊 **آمار سیستم:**\n\n"
        f"💾 فضای دیسک آزاد: {free_gb:.2f} GB\n"
        f"📁 حجم فایل‌ها: {used_mb:.1f} MB\n"
        f"📥 کل دانلودها: {total_dl}\n"
        f"✅ موفق: {success_dl}\n"
        f"❤️ لایک‌ها: {likes}\n"
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

# ==================== MAIN MESSAGE HANDLER ====================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ADMIN_ID and update.effective_user.id != ADMIN_ID:
        return

    text = update.message.text
    if not text or not text.startswith('http'):
        return

    # Rate limit
    if not check_rate_limit(update.effective_user.id):
        await update.message.reply_text("⏳ صبر کن! زیاد دانلود کردی (5 دانلود در دقیقه).")
        return

    url_type, resolved = detect_url_type(text)
    chat_id = update.effective_chat.id
    platform = get_platform_name(text)

    logger.info(f"URL: {text} → {url_type} ({platform})")

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
            try:
                for attempt in range(3):
                    output_file, err = await download_audio(text, chat_id, context)
                    if output_file:
                        info = await asyncio.get_event_loop().run_in_executor(
                            None, extract_info_sync, resolve_short_url(text)
                        )
                        if not info:
                            info = {'title': 'Unknown', 'uploader': '', 'album': '',
                                    'duration': 0, 'thumbnail': None}
                        hist_id = db_log(text, info.get('title', ''), info.get('uploader', ''),
                                         info.get('album', ''), platform, 'audio', 'success')
                        await send_audio_file(chat_id, context, output_file, info, hist_id)
                        if output_file.exists():
                            output_file.unlink()
                        await status_msg.edit_text("✅ ارسال شد!")
                        break
                    else:
                        if attempt < 2:
                            await status_msg.edit_text(f"🔄 تلاش مجدد... ({attempt+2}/3)")
                            await asyncio.sleep(2 ** attempt)
                        else:
                            await status_msg.edit_text(f"❌ خطا: {err[:200]}")
                            db_log(text, '', '', '', platform, 'audio', 'failed')
            except Exception as e:
                await status_msg.edit_text(f"❌ خطا: {str(e)[:200]}")

    elif url_type == 'auto':
        status_msg = await update.message.reply_text("🔍 در حال شناسایی لینک...")
        try:
            info = await asyncio.get_event_loop().run_in_executor(
                None, extract_info_sync, resolve_short_url(text)
            )
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
                    else:
                        await status_msg.edit_text(f"❌ {err[:200]}")
        except Exception as e:
            await status_msg.edit_text(f"❌ خطا: {str(e)[:200]}")

# ==================== CLEANUP ====================
async def scheduled_cleanup():
    while True:
        await asyncio.sleep(3600)
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
            logger.error(f"Cleanup error: {e}")

# ==================== MAIN ====================
def main():
    init_db()

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("favorites", cmd_favorites))
    app.add_handler(CommandHandler("albums", cmd_albums))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    loop = asyncio.get_event_loop()
    loop.create_task(scheduled_cleanup())

    logger.info("🎵 Media Bot v2.0 started!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
