"""Video thumbnail embedding using ffmpeg."""

import os
import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Optional
from dataclasses import dataclass

from logging_config import get_logger

logger = get_logger("video_thumbnail")


@dataclass
class VideoThumbnailResult:
    """Result of video thumbnail embedding."""
    input_path: str
    output_path: str
    thumbnail_path: str
    success: bool
    error: str = ""


async def download_thumbnail(thumbnail_url: str, output_path: str) -> bool:
    """Download thumbnail from URL."""
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(thumbnail_url) as resp:
                if resp.status == 200:
                    with open(output_path, 'wb') as f:
                        f.write(await resp.read())
                    return True
        return False
    except Exception as e:
        logger.error("download_thumbnail_failed", url=thumbnail_url, error=str(e))
        return False


async def embed_thumbnail_ffmpeg(
    video_path: str,
    thumbnail_path: str,
    output_path: str,
    quality: int = 2  # 1-31, lower = better quality
) -> VideoThumbnailResult:
    """
    Embed thumbnail into video using ffmpeg.
    
    Args:
        video_path: Path to input video file
        thumbnail_path: Path to thumbnail image
        output_path: Path to output video file
        quality: JPEG quality for embedded thumbnail (1-31, lower=better)
    
    Returns:
        VideoThumbnailResult
    """
    try:
        logger.info("embedding_thumbnail", video=video_path, thumbnail=thumbnail_path, output=output_path)
        
        # Build ffmpeg command
        # -map 0 -map 1: map all streams from video and thumbnail
        # -c copy: copy video/audio streams without re-encoding
        # -c:v:1 mjpeg: encode thumbnail as MJPEG
        # -disposition:v:1 attached_pic: mark thumbnail as attached picture
        cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-i", thumbnail_path,
            "-map", "0",
            "-map", "1",
            "-c", "copy",
            "-c:v:1", "mjpeg",
            "-disposition:v:1", "attached_pic",
            "-q:v:1", str(quality),
            output_path
        ]
        
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        stdout, stderr = await proc.communicate()
        
        if proc.returncode != 0:
            error_msg = stderr.decode() if stderr else "Unknown ffmpeg error"
            logger.error("ffmpeg_thumbnail_failed", error=error_msg)
            return VideoThumbnailResult(
                input_path=video_path,
                output_path=output_path,
                thumbnail_path=thumbnail_path,
                success=False,
                error=error_msg
            )
        
        # Verify output exists and has size
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            return VideoThumbnailResult(
                input_path=video_path,
                output_path=output_path,
                thumbnail_path=thumbnail_path,
                success=False,
                error="Output file not created or empty"
            )
        
        logger.info("thumbnail_embedded", 
                   video=video_path, 
                   output=output_path,
                   output_size=os.path.getsize(output_path))
        
        return VideoThumbnailResult(
            input_path=video_path,
            output_path=output_path,
            thumbnail_path=thumbnail_path,
            success=True
        )
        
    except Exception as e:
        logger.error("embed_thumbnail_failed", video=video_path, error=str(e))
        return VideoThumbnailResult(
            input_path=video_path,
            output_path=output_path,
            thumbnail_path=thumbnail_path,
            success=False,
            error=str(e)
        )


async def embed_thumbnail_to_audio(
    audio_path: str,
    thumbnail_path: str,
    output_path: str,
    quality: int = 2
) -> VideoThumbnailResult:
    """
    Embed thumbnail (cover art) into audio file using ffmpeg.
    This is an alternative to mutagen for embedding cover art.
    """
    try:
        logger.info("embedding_thumbnail_to_audio", audio=audio_path, thumbnail=thumbnail_path)
        
        cmd = [
            "ffmpeg", "-y",
            "-i", audio_path,
            "-i", thumbnail_path,
            "-map", "0",
            "-map", "1",
            "-c", "copy",
            "-c:v:1", "mjpeg",
            "-disposition:v:1", "attached_pic",
            "-q:v:1", str(quality),
            output_path
        ]
        
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        stdout, stderr = await proc.communicate()
        
        if proc.returncode != 0:
            error_msg = stderr.decode() if stderr else "Unknown ffmpeg error"
            logger.error("ffmpeg_audio_thumbnail_failed", error=error_msg)
            return VideoThumbnailResult(
                input_path=audio_path,
                output_path=output_path,
                thumbnail_path=thumbnail_path,
                success=False,
                error=error_msg
            )
        
        return VideoThumbnailResult(
            input_path=audio_path,
            output_path=output_path,
            thumbnail_path=thumbnail_path,
            success=True
        )
        
    except Exception as e:
        logger.error("embed_audio_thumbnail_failed", audio=audio_path, error=str(e))
        return VideoThumbnailResult(
            input_path=audio_path,
            output_path=output_path,
            thumbnail_path=thumbnail_path,
            success=False,
            error=str(e)
        )


async def extract_video_thumbnail(
    video_path: str,
    output_path: str,
    timestamp: str = "00:00:01"
) -> bool:
    """
    Extract a frame from video as thumbnail.
    
    Args:
        video_path: Path to input video
        output_path: Path to output thumbnail image
        timestamp: Time position to extract frame (HH:MM:SS)
    
    Returns:
        True if successful
    """
    try:
        logger.info("extracting_video_thumbnail", video=video_path, timestamp=timestamp)
        
        cmd = [
            "ffmpeg", "-y",
            "-ss", timestamp,
            "-i", video_path,
            "-vframes", "1",
            "-q:v", "2",
            output_path
        ]
        
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        stdout, stderr = await proc.communicate()
        
        if proc.returncode != 0:
            logger.error("extract_thumbnail_failed", error=stderr.decode())
            return False
        
        return os.path.exists(output_path) and os.path.getsize(output_path) > 0
        
    except Exception as e:
        logger.error("extract_thumbnail_failed", error=str(e))
        return False


async def process_video_with_thumbnail(
    video_path: str,
    thumbnail_url: str,
    output_path: str,
    extract_if_missing: bool = True
) -> VideoThumbnailResult:
    """
    Complete pipeline: download thumbnail and embed into video.
    
    Args:
        video_path: Path to input video
        thumbnail_url: URL of thumbnail image
        output_path: Path to output video with embedded thumbnail
        extract_if_missing: If thumbnail download fails, extract frame from video
    
    Returns:
        VideoThumbnailResult
    """
    temp_thumbnail = None
    
    try:
        # Create temp file for thumbnail
        with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as tmp:
            temp_thumbnail = tmp.name
        
        # Download thumbnail
        downloaded = await download_thumbnail(thumbnail_url, temp_thumbnail)
        
        if not downloaded and extract_if_missing:
            # Try to extract from video
            logger.info("thumbnail_download_failed_extracting_from_video", url=thumbnail_url)
            downloaded = await extract_video_thumbnail(video_path, temp_thumbnail)
        
        if not downloaded:
            if temp_thumbnail and os.path.exists(temp_thumbnail):
                os.remove(temp_thumbnail)
            return VideoThumbnailResult(
                input_path=video_path,
                output_path=output_path,
                thumbnail_path=thumbnail_url,
                success=False,
                error="Failed to download or extract thumbnail"
            )
        
        # Embed thumbnail
        result = await embed_thumbnail_ffmpeg(video_path, temp_thumbnail, output_path)
        
        # Cleanup
        if temp_thumbnail and os.path.exists(temp_thumbnail):
            os.remove(temp_thumbnail)
        
        return result
        
    except Exception as e:
        if temp_thumbnail and os.path.exists(temp_thumbnail):
            os.remove(temp_thumbnail)
        logger.error("process_video_thumbnail_failed", video=video_path, error=str(e))
        return VideoThumbnailResult(
            input_path=video_path,
            output_path=output_path,
            thumbnail_path=thumbnail_url,
            success=False,
            error=str(e)
        )


async def add_metadata_to_video(
    video_path: str,
    output_path: str,
    title: str = "",
    artist: str = "",
    album: str = "",
    year: str = "",
    genre: str = "",
    description: str = ""
) -> VideoThumbnailResult:
    """Add metadata to video file using ffmpeg."""
    try:
        logger.info("adding_video_metadata", video=video_path)
        
        cmd = ["ffmpeg", "-y", "-i", video_path]
        
        # Add metadata
        metadata = {}
        if title:
            metadata["title"] = title
        if artist:
            metadata["artist"] = artist
        if album:
            metadata["album"] = album
        if year:
            metadata["date"] = year
        if genre:
            metadata["genre"] = genre
        if description:
            metadata["description"] = description
        
        for key, value in metadata.items():
            cmd.extend(["-metadata", f"{key}={value}"])
        
        cmd.extend(["-c", "copy", output_path])
        
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        stdout, stderr = await proc.communicate()
        
        if proc.returncode != 0:
            error_msg = stderr.decode() if stderr else "Unknown ffmpeg error"
            return VideoThumbnailResult(
                input_path=video_path,
                output_path=output_path,
                thumbnail_path="",
                success=False,
                error=error_msg
            )
        
        return VideoThumbnailResult(
            input_path=video_path,
            output_path=output_path,
            thumbnail_path="",
            success=True
        )
        
    except Exception as e:
        logger.error("add_video_metadata_failed", video=video_path, error=str(e))
        return VideoThumbnailResult(
            input_path=video_path,
            output_path=output_path,
            thumbnail_path="",
            success=False,
            error=str(e)
        )