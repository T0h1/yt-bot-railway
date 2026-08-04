"""Batch/playlist download as ZIP archive."""

import os
import asyncio
import zipfile
import tempfile
import shutil
from pathlib import Path
from typing import List, Optional, Callable
from dataclasses import dataclass
from datetime import datetime

from logging_config import get_logger

logger = get_logger("batch_download")


@dataclass
class BatchDownloadItem:
    """Single item in a batch download."""
    url: str
    title: str = ""
    artist: str = ""
    platform: str = ""
    content_type: str = "audio"
    file_path: str = ""
    success: bool = False
    error: str = ""


@dataclass
class BatchDownloadResult:
    """Result of a batch download."""
    batch_id: str
    items: List[BatchDownloadItem]
    zip_path: str = ""
    total_items: int = 0
    successful: int = 0
    failed: int = 0
    total_size_bytes: int = 0
    created_at: float = 0
    completed_at: float = 0
    success: bool = False
    error: str = ""


class BatchDownloader:
    """Handle batch/playlist downloads and ZIP creation."""
    
    def __init__(self, download_dir: str = "media_downloads", temp_dir: str = None):
        self.download_dir = Path(download_dir)
        self.temp_dir = Path(temp_dir) if temp_dir else Path(tempfile.gettempdir()) / "batch_downloads"
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
    
    async def download_playlist(
        self,
        playlist_url: str,
        progress_callback: Optional[Callable] = None,
        max_items: int = 100,
        audio_only: bool = True,
        quality: str = "best"
    ) -> BatchDownloadResult:
        """
        Download all items from a playlist.
        
        Args:
            playlist_url: URL of playlist/album
            progress_callback: Async callback(current, total, item) for progress updates
            max_items: Maximum items to download
            audio_only: Download audio only
            quality: Quality setting
        
        Returns:
            BatchDownloadResult
        """
        from yt_dlp_async import extract_info_async
        
        batch_id = f"batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        logger.info("starting_batch_download", batch_id=batch_id, url=playlist_url)
        
        # Extract playlist info
        try:
            info = await extract_info_async(playlist_url, download=False)
            if not info:
                return BatchDownloadResult(
                    batch_id=batch_id,
                    items=[],
                    success=False,
                    error="Failed to extract playlist info"
                )
        except Exception as e:
            return BatchDownloadResult(
                batch_id=batch_id,
                items=[],
                success=False,
                error=f"Playlist extraction failed: {str(e)}"
            )
        
        # Get entries
        entries = info.get("entries", [])
        if not entries:
            return BatchDownloadResult(
                batch_id=batch_id,
                items=[],
                success=False,
                error="No items found in playlist"
            )
        
        # Limit items
        entries = entries[:max_items]
        
        # Create batch items
        items = []
        for entry in entries:
            if entry:
                item = BatchDownloadItem(
                    url=entry.get("webpage_url") or entry.get("url", ""),
                    title=entry.get("title", "Unknown"),
                    artist=entry.get("uploader") or entry.get("artist") or entry.get("channel", ""),
                    platform=self._detect_platform(entry.get("webpage_url", "")),
                    content_type="audio" if audio_only else "video"
                )
                items.append(item)
        
        result = BatchDownloadResult(
            batch_id=batch_id,
            items=items,
            total_items=len(items),
            created_at=datetime.now().timestamp()
        )
        
        # Download each item
        for i, item in enumerate(items):
            if progress_callback:
                await progress_callback(i + 1, len(items), item)
            
            try:
                # Download using existing download functions
                if audio_only:
                    from youtube_downloader_bot import download_audio
                    file_path, error = await download_audio(item.url, 0, None)  # chat_id=0, context=None for batch
                else:
                    from youtube_downloader_bot import download_video
                    file_path, error = await download_video(item.url, 0, None)
                
                if file_path and os.path.exists(file_path):
                    item.file_path = file_path
                    item.success = True
                    result.successful += 1
                else:
                    item.error = error or "Download failed"
                    result.failed += 1
                    
            except Exception as e:
                item.error = str(e)
                result.failed += 1
            
            # Small delay to prevent rate limiting
            await asyncio.sleep(0.5)
        
        result.completed_at = datetime.now().timestamp()
        
        # Create ZIP if we have successful downloads
        if result.successful > 0:
            zip_path = await self.create_zip_archive(result)
            result.zip_path = zip_path
            result.total_size_bytes = os.path.getsize(zip_path) if zip_path else 0
            result.success = True
        
        logger.info("batch_download_complete",
                   batch_id=batch_id,
                   total=result.total_items,
                   successful=result.successful,
                   failed=result.failed,
                   zip_size=result.total_size_bytes)
        
        return result
    
    async def download_multiple_urls(
        self,
        urls: List[str],
        progress_callback: Optional[Callable] = None,
        audio_only: bool = True,
        quality: str = "best"
    ) -> BatchDownloadResult:
        """Download multiple URLs as a batch."""
        batch_id = f"batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        logger.info("starting_multi_url_download", batch_id=batch_id, count=len(urls))
        
        items = []
        for url in urls:
            item = BatchDownloadItem(
                url=url,
                platform=self._detect_platform(url)
            )
            items.append(item)
        
        result = BatchDownloadResult(
            batch_id=batch_id,
            items=items,
            total_items=len(items),
            created_at=datetime.now().timestamp()
        )
        
        for i, item in enumerate(items):
            if progress_callback:
                await progress_callback(i + 1, len(items), item)
            
            try:
                if audio_only:
                    from youtube_downloader_bot import download_audio
                    file_path, error = await download_audio(item.url, 0, None)
                else:
                    from youtube_downloader_bot import download_video
                    file_path, error = await download_video(item.url, 0, None)
                
                if file_path and os.path.exists(file_path):
                    item.file_path = file_path
                    item.success = True
                    result.successful += 1
                else:
                    item.error = error or "Download failed"
                    result.failed += 1
                    
            except Exception as e:
                item.error = str(e)
                result.failed += 1
            
            await asyncio.sleep(0.5)
        
        result.completed_at = datetime.now().timestamp()
        
        if result.successful > 0:
            zip_path = await self.create_zip_archive(result)
            result.zip_path = zip_path
            result.total_size_bytes = os.path.getsize(zip_path) if zip_path else 0
            result.success = True
        
        return result
    
    async def create_zip_archive(self, result: BatchDownloadResult) -> str:
        """Create ZIP archive from successful downloads."""
        zip_name = f"{result.batch_id}.zip"
        zip_path = self.download_dir / zip_name
        
        logger.info("creating_zip_archive", batch_id=result.batch_id, zip_path=str(zip_path))
        
        successful_items = [item for item in result.items if item.success and item.file_path]
        
        def create_zip():
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
                for item in successful_items:
                    if os.path.exists(item.file_path):
                        # Use a clean filename in the ZIP
                        safe_title = "".join(c for c in item.title if c.isalnum() or c in " -_()[]").strip()
                        safe_artist = "".join(c for c in item.artist if c.isalnum() or c in " -_()[]").strip()
                        
                        if safe_artist and safe_title:
                            arcname = f"{safe_artist} - {safe_title}.mp3"
                        elif safe_title:
                            arcname = f"{safe_title}.mp3"
                        else:
                            arcname = os.path.basename(item.file_path)
                        
                        # Avoid duplicate names
                        base_name = arcname
                        counter = 1
                        while arcname in zf.namelist():
                            name, ext = os.path.splitext(base_name)
                            arcname = f"{name} ({counter}){ext}"
                            counter += 1
                        
                        zf.write(item.file_path, arcname)
        
        # Run in executor to avoid blocking
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, create_zip)
        
        return str(zip_path)
    
    def _detect_platform(self, url: str) -> str:
        """Detect platform from URL."""
        url_lower = url.lower()
        if "youtube.com" in url_lower or "youtu.be" in url_lower:
            return "youtube"
        elif "soundcloud.com" in url_lower:
            return "soundcloud"
        elif "spotify.com" in url_lower:
            return "spotify"
        elif "instagram.com" in url_lower:
            return "instagram"
        elif "tiktok.com" in url_lower:
            return "tiktok"
        elif "twitter.com" in url_lower or "x.com" in url_lower:
            return "twitter"
        elif "facebook.com" in url_lower:
            return "facebook"
        elif "twitch.tv" in url_lower:
            return "twitch"
        return "unknown"
    
    async def cleanup_batch_files(self, result: BatchDownloadResult, keep_zip: bool = True):
        """Clean up individual downloaded files after ZIP creation."""
        for item in result.items:
            if item.file_path and os.path.exists(item.file_path):
                try:
                    # Only remove if it's in our temp/download directory
                    file_path = Path(item.file_path)
                    if self.download_dir in file_path.parents or self.temp_dir in file_path.parents:
                        os.remove(item.file_path)
                except Exception as e:
                    logger.warning("cleanup_failed", file=item.file_path, error=str(e))
        
        # Also remove ZIP if requested
        if not keep_zip and result.zip_path and os.path.exists(result.zip_path):
            try:
                os.remove(result.zip_path)
            except Exception as e:
                logger.warning("zip_cleanup_failed", zip=result.zip_path, error=str(e))


# Global batch downloader instance
_batch_downloader: Optional[BatchDownloader] = None


def get_batch_downloader() -> BatchDownloader:
    global _batch_downloader
    if _batch_downloader is None:
        _batch_downloader = BatchDownloader()
    return _batch_downloader