"""Podcast/RSS feed support with yt-dlp."""

import asyncio
import re
from typing import Optional, List, Dict, Any
from dataclasses import dataclass
from urllib.parse import urlparse

from yt_dlp_async import extract_info_async
from logging_config import get_logger

logger = get_logger("podcast_support")


@dataclass
class PodcastEpisode:
    """Represents a podcast episode."""
    title: str
    url: str
    description: str = ""
    duration: int = 0
    published: str = ""
    thumbnail: str = ""
    audio_url: str = ""


@dataclass
class PodcastInfo:
    """Represents a podcast feed."""
    title: str
    feed_url: str
    description: str = ""
    author: str = ""
    thumbnail: str = ""
    episodes: List[PodcastEpisode] = None
    total_episodes: int = 0

    def __post_init__(self):
        if self.episodes is None:
            self.episodes = []


# Known podcast platforms and their URL patterns
PODCAST_PLATFORMS = {
    "anchor.fm": r"anchor\.fm/([^/]+)/?(episodes/)?",
    "podcasts.apple.com": r"podcasts\.apple\.com/.*/id(\d+)",
    "spotify.com": r"spotify\.com/.*/show/([a-zA-Z0-9]+)",
    "google.com/podcasts": r"podcasts\.google\.com/.*/feed/([a-zA-Z0-9%]+)",
    "overcast.fm": r"overcast\.fm/\+([a-zA-Z0-9]+)",
    "pocketcasts.com": r"pocketcasts\.com/.*/podcast/([a-zA-Z0-9-]+)",
    "soundcloud.com": r"soundcloud\.com/([^/]+)/sets/([^/]+)",
    "rss": r"\.xml$|\.rss$|feed\.xml|rss\.xml",
}

RSS_PATTERNS = [
    r"\.xml$",
    r"\.rss$",
    r"feed\.xml",
    r"rss\.xml",
    r"/feed/?$",
    r"/rss/?$",
]


def is_podcast_url(url: str) -> bool:
    """Check if URL is a podcast feed or podcast platform URL."""
    url_lower = url.lower()
    
    # Check for RSS feed patterns
    for pattern in RSS_PATTERNS:
        if re.search(pattern, url_lower):
            return True
    
    # Check for podcast platform patterns
    for platform, pattern in PODCAST_PLATFORMS.items():
        if re.search(pattern, url_lower):
            return True
    
    return False


def detect_podcast_platform(url: str) -> Optional[str]:
    """Detect which podcast platform the URL belongs to."""
    url_lower = url.lower()
    
    for platform, pattern in PODCAST_PLATFORMS.items():
        if re.search(pattern, url_lower):
            return platform
    
    return None


async def extract_podcast_info(url: str) -> Optional[PodcastInfo]:
    """Extract podcast information from a feed URL or platform URL."""
    try:
        logger.info("extracting_podcast_info", url=url)
        
        # Use yt-dlp to extract info (it supports many podcast platforms)
        info = await extract_info_async(url, download=False)
        
        if not info:
            logger.warning("no_info_extracted", url=url)
            return None
        
        # Handle different yt-dlp response types
        if info.get("_type") == "playlist" or "entries" in info:
            # This is a playlist/feed
            return await _parse_playlist_as_podcast(info, url)
        elif info.get("_type") == "url":
            # Redirect - follow it
            return await extract_podcast_info(info.get("url", ""))
        else:
            # Single episode
            return await _parse_single_episode(info, url)
            
    except Exception as e:
        logger.error("podcast_extraction_failed", url=url, error=str(e))
        return None


async def _parse_playlist_as_podcast(info: Dict[str, Any], feed_url: str) -> PodcastInfo:
    """Parse a playlist/feed as a podcast."""
    entries = info.get("entries", [])
    
    episodes = []
    for entry in entries:
        if entry:
            ep = PodcastEpisode(
                title=entry.get("title", "Unknown Episode"),
                url=entry.get("webpage_url") or entry.get("url", ""),
                description=entry.get("description", "")[:500],
                duration=entry.get("duration", 0) or 0,
                published=entry.get("upload_date", "") or entry.get("release_date", ""),
                thumbnail=entry.get("thumbnail", ""),
                audio_url=entry.get("url", "")
            )
            episodes.append(ep)
    
    return PodcastInfo(
        title=info.get("title", "Unknown Podcast"),
        feed_url=feed_url,
        description=info.get("description", "")[:1000],
        author=info.get("uploader", "") or info.get("channel", ""),
        thumbnail=info.get("thumbnail", ""),
        episodes=episodes,
        total_episodes=len(episodes)
    )


async def _parse_single_episode(info: Dict[str, Any], url: str) -> PodcastInfo:
    """Parse a single episode as a podcast with one episode."""
    episode = PodcastEpisode(
        title=info.get("title", "Unknown Episode"),
        url=info.get("webpage_url", url),
        description=info.get("description", "")[:500],
        duration=info.get("duration", 0) or 0,
        published=info.get("upload_date", "") or info.get("release_date", ""),
        thumbnail=info.get("thumbnail", ""),
        audio_url=info.get("url", "")
    )
    
    return PodcastInfo(
        title=info.get("title", "Single Episode"),
        feed_url=url,
        description=info.get("description", "")[:1000],
        author=info.get("uploader", "") or info.get("channel", ""),
        thumbnail=info.get("thumbnail", ""),
        episodes=[episode],
        total_episodes=1
    )


async def get_podcast_episodes(feed_url: str, limit: int = 50) -> List[PodcastEpisode]:
    """Get podcast episodes from a feed URL."""
    podcast = await extract_podcast_info(feed_url)
    if podcast:
        return podcast.episodes[:limit]
    return []


async def download_podcast_episode(episode: PodcastEpisode, chat_id: int, context) -> tuple:
    """Download a podcast episode as audio."""
    from youtube_downloader_bot import download_audio
    
    # Use the existing download_audio function with the episode URL
    return await download_audio(episode.url, chat_id, context)


def format_podcast_info(podcast: PodcastInfo) -> str:
    """Format podcast info for display."""
    lines = [
        f"🎙 **{podcast.title}**",
        f"📝 {podcast.description[:200]}{'...' if len(podcast.description) > 200 else ''}",
        f"👤 {podcast.author}" if podcast.author else "",
        f"📻 {len(podcast.episodes)} اپیزود",
        "",
        "**اپیزودهای اخیر:**"
    ]
    
    for i, ep in enumerate(podcast.episodes[:10], 1):
        duration_str = f" ⏱ {ep.duration//60}:{ep.duration%60:02d}" if ep.duration else ""
        pub_str = f" 📅 {ep.published}" if ep.published else ""
        lines.append(f"{i}. **{ep.title}**{duration_str}{pub_str}")
    
    if len(podcast.episodes) > 10:
        lines.append(f"... و {len(podcast.episodes) - 10} اپیزود دیگر")
    
    return "\n".join(filter(None, lines))


def format_episode_info(episode: PodcastEpisode) -> str:
    """Format episode info for display."""
    lines = [
        f"🎙 **{episode.title}**",
        f"⏱ مدت: {episode.duration//60}:{episode.duration%60:02d}" if episode.duration else "",
        f"📅 انتشار: {episode.published}" if episode.published else "",
        f"🔗 لینک: {episode.url}",
    ]
    return "\n".join(filter(None, lines))