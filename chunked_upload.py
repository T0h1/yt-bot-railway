"""Video chunked upload for large files using Telegram Bot API."""

import os
import asyncio
from pathlib import Path
from typing import Optional, Callable
from aiohttp import ClientSession, FormData, MultipartWriter
from logging_config import get_logger

logger = get_logger("chunked_upload")

# Telegram Bot API limits
TELEGRAM_MAX_FILE = 50 * 1024 * 1024  # 50MB for bots
CHUNK_SIZE = 8 * 1024 * 1024  # 8MB chunks for multipart upload


async def upload_large_file(
    bot_token: str,
    chat_id: int,
    file_path: Path,
    caption: str = "",
    title: str = "",
    duration: int = 0,
    thumbnail: Optional[str] = None,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> Optional[dict]:
    """
    Upload a file larger than 50MB using multipart/form-data chunked upload.
    
    Telegram Bot API supports files up to 2GB via multipart upload.
    """
    file_size = file_path.stat().st_size
    
    if file_size <= TELEGRAM_MAX_FILE:
        logger.warning("upload_large_file_called_for_small_file", file_size=file_size)
        return None  # Should use regular send_video/send_audio
    
    logger.info("starting_chunked_upload", file_size=file_size, chat_id=chat_id)
    
    url = f"https://api.telegram.org/bot{bot_token}/sendVideo"
    
    async with ClientSession() as session:
        # Create multipart form data
        form = FormData()
        form.add_field('chat_id', str(chat_id))
        form.add_field('caption', caption)
        if title:
            form.add_field('title', title)
        if duration:
            form.add_field('duration', str(duration))
        form.add_field('supports_streaming', 'true')
        
        # Add file as stream
        form.add_field(
            'video',
            file_path.open('rb'),
            filename=file_path.name,
            content_type='video/mp4'
        )
        
        if thumbnail and os.path.exists(thumbnail):
            form.add_field(
                'thumbnail',
                open(thumbnail, 'rb'),
                filename='thumb.jpg',
                content_type='image/jpeg'
            )
        
        try:
            async with session.post(url, data=form) as response:
                if response.status == 200:
                    result = await response.json()
                    logger.info("chunked_upload_success", file_size=file_size)
                    if progress_callback:
                        progress_callback(file_size, file_size)
                    return result
                else:
                    error_text = await response.text()
                    logger.error("chunked_upload_failed", status=response.status, error=error_text)
                    return None
        except Exception as e:
            logger.error("chunked_upload_error", error=str(e))
            return None


async def upload_large_audio(
    bot_token: str,
    chat_id: int,
    file_path: Path,
    caption: str = "",
    title: str = "",
    performer: str = "",
    duration: int = 0,
    thumbnail: Optional[str] = None,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> Optional[dict]:
    """
    Upload a large audio file (>50MB) using multipart/form-data chunked upload.
    """
    file_size = file_path.stat().st_size
    
    if file_size <= TELEGRAM_MAX_FILE:
        return None
    
    logger.info("starting_chunked_audio_upload", file_size=file_size, chat_id=chat_id)
    
    url = f"https://api.telegram.org/bot{bot_token}/sendAudio"
    
    async with ClientSession() as session:
        form = FormData()
        form.add_field('chat_id', str(chat_id))
        form.add_field('caption', caption)
        if title:
            form.add_field('title', title)
        if performer:
            form.add_field('performer', performer)
        if duration:
            form.add_field('duration', str(duration))
        
        form.add_field(
            'audio',
            file_path.open('rb'),
            filename=file_path.name,
            content_type='audio/mpeg'
        )
        
        if thumbnail and os.path.exists(thumbnail):
            form.add_field(
                'thumbnail',
                open(thumbnail, 'rb'),
                filename='thumb.jpg',
                content_type='image/jpeg'
            )
        
        try:
            async with session.post(url, data=form) as response:
                if response.status == 200:
                    result = await response.json()
                    logger.info("chunked_audio_upload_success", file_size=file_size)
                    if progress_callback:
                        progress_callback(file_size, file_size)
                    return result
                else:
                    error_text = await response.text()
                    logger.error("chunked_audio_upload_failed", status=response.status, error=error_text)
                    return None
        except Exception as e:
            logger.error("chunked_audio_upload_error", error=str(e))
            return None


async def upload_with_progress(
    bot_token: str,
    chat_id: int,
    file_path: Path,
    method: str,  # 'sendVideo' or 'sendAudio'
    caption: str = "",
    title: str = "",
    performer: str = "",
    duration: int = 0,
    thumbnail: Optional[str] = None,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> Optional[dict]:
    """
    Unified upload with progress tracking for large files.
    """
    file_size = file_path.stat().st_size
    
    if file_size <= TELEGRAM_MAX_FILE:
        return None
    
    url = f"https://api.telegram.org/bot{bot_token}/{method}"
    
    # For progress tracking, we need to stream the file in chunks
    # Using MultipartWriter with a custom progress reader
    from aiohttp import MultipartWriter
    import aiohttp
    
    # Create a progress-tracking file reader
    class ProgressFileReader:
        def __init__(self, file_path: Path, callback: Optional[Callable] = None):
            self.file = file_path.open('rb')
            self.size = file_size
            self.read_bytes = 0
            self.callback = callback
        
        def read(self, n: int = -1) -> bytes:
            data = self.file.read(n)
            self.read_bytes += len(data)
            if self.callback and self.read_bytes % (1024 * 1024) == 0:  # Every 1MB
                self.callback(self.read_bytes, self.size)
            return data
        
        def close(self):
            self.file.close()
        
        def __enter__(self):
            return self
        
        def __exit__(self, *args):
            self.close()
    
    reader = ProgressFileReader(file_path, progress_callback)
    
    async with ClientSession() as session:
        form = FormData()
        form.add_field('chat_id', str(chat_id))
        form.add_field('caption', caption)
        if title:
            form.add_field('title', title)
        if performer and method == 'sendAudio':
            form.add_field('performer', performer)
        if duration:
            form.add_field('duration', str(duration))
        if method == 'sendVideo':
            form.add_field('supports_streaming', 'true')
        
        form.add_field(
            'video' if method == 'sendVideo' else 'audio',
            reader,
            filename=file_path.name,
            content_type='video/mp4' if method == 'sendVideo' else 'audio/mpeg'
        )
        
        if thumbnail and os.path.exists(thumbnail):
            form.add_field(
                'thumbnail',
                open(thumbnail, 'rb'),
                filename='thumb.jpg',
                content_type='image/jpeg'
            )
        
        try:
            async with session.post(url, data=form) as response:
                reader.close()
                if response.status == 200:
                    result = await response.json()
                    logger.info("large_file_upload_success", method=method, file_size=file_size)
                    if progress_callback:
                        progress_callback(file_size, file_size)
                    return result
                else:
                    error_text = await response.text()
                    logger.error("large_file_upload_failed", method=method, status=response.status, error=error_text)
                    return None
        except Exception as e:
            reader.close()
            logger.error("large_file_upload_error", method=method, error=str(e))
            return None