"""M3U8/HLS live stream support using yt-dlp and ffmpeg."""

import os
import asyncio
import logging
import tempfile
import re
from pathlib import Path
from typing import Optional, List, Dict, Any
from dataclasses import dataclass
from datetime import datetime

from yt_dlp_async import extract_info_async, download_with_ydl_async
from logging_config import get_logger

logger = get_logger("m3u8_support")


@dataclass
class HLSStreamInfo:
    """Information about an HLS stream."""
    url: str
    title: str = ""
    is_live: bool = False
    formats: List[Dict[str, Any]] = None
    duration: Optional[int] = None
    thumbnail: str = ""
    uploader: str = ""

    def __post_init__(self):
        if self.formats is None:
            self.formats = []


@dataclass
class HLSDownloadResult:
    """Result of HLS stream download."""
    stream_url: str
    output_path: str
    success: bool
    duration: float = 0
    file_size: int = 0
    error: str = ""
    is_live_recording: bool = False


async def detect_m3u8_url(url: str) -> bool:
    """Check if URL is an M3U8/HLS stream."""
    url_lower = url.lower()
    return any([
        '.m3u8' in url_lower,
        'hls' in url_lower and ('stream' in url_lower or 'live' in url_lower),
        'manifest.m3u8' in url_lower,
        'playlist.m3u8' in url_lower,
        '/live/' in url_lower and ('.m3u8' in url_lower or 'hls' in url_lower),
    ])


async def extract_hls_info(url: str) -> Optional[HLSStreamInfo]:
    """Extract information from an HLS/M3U8 stream URL."""
    try:
        logger.info("extracting_hls_info", url=url)
        
        # Use yt-dlp to extract info
        info = await extract_info_async(url, download=False)
        
        if not info:
            logger.warning("no_hls_info_extracted", url=url)
            return None
        
        formats = info.get("formats", [])
        hls_formats = [f for f in formats if f.get("protocol", "").startswith("http") and 
                      ("m3u8" in f.get("url", "").lower() or "hls" in f.get("protocol", "").lower())]
        
        return HLSStreamInfo(
            url=url,
            title=info.get("title", "Live Stream"),
            is_live=info.get("is_live", False),
            formats=hls_formats,
            duration=info.get("duration"),
            thumbnail=info.get("thumbnail", ""),
            uploader=info.get("uploader", "") or info.get("channel", "")
        )
        
    except Exception as e:
        logger.error("hls_info_extraction_failed", url=url, error=str(e))
        return None


async def download_hls_stream(
    url: str,
    output_path: str,
    max_duration: Optional[int] = None,
    quality: str = "best",
    live_restart: bool = True
) -> HLSDownloadResult:
    """
    Download an HLS/M3U8 stream.
    
    Args:
        url: M3U8 stream URL
        output_path: Output file path
        max_duration: Maximum recording duration in seconds (None = unlimited for live)
        quality: Quality selection ('best', 'worst', or specific height like '720p')
        live_restart: For live streams, restart from beginning if available
    
    Returns:
        HLSDownloadResult
    """
    start_time = datetime.now()
    
    try:
        logger.info("downloading_hls_stream", url=url, output=output_path, max_duration=max_duration)
        
        # Build yt-dlp options for HLS
        ydl_opts = {
            'format': quality,
            'outtmpl': output_path,
            'noplaylist': True,
            'no_warnings': False,
            'ignoreerrors': False,
            'retries': 3,
            'fragment_retries': 3,
            'skip_unavailable_fragments': True,
            'keep_fragments': False,
            'hls_prefer_native': True,
            'hls_use_mpegts': False,  # Use native HLS downloader
            'concurrent_fragment_downloads': 4,
        }
        
        # Add live stream specific options
        if max_duration:
            ydl_opts['download_ranges'] = [{'start_time': 0, 'end_time': max_duration}]
        
        # For live streams, try to restart from beginning
        if live_restart:
            ydl_opts['live_from_start'] = True
            ydl_opts['wait_for_video'] = (5, 30)  # Wait 5-30 seconds for stream
        
        # Download using yt-dlp
        success = await download_with_ydl_async(url, ydl_opts)
        
        if not success:
            return HLSDownloadResult(
                stream_url=url,
                output_path=output_path,
                success=False,
                error="yt-dlp download failed"
            )
        
        # Check output file
        if not os.path.exists(output_path):
            return HLSDownloadResult(
                stream_url=url,
                output_path=output_path,
                success=False,
                error="Output file not created"
            )
        
        file_size = os.path.getsize(output_path)
        duration = (datetime.now() - start_time).total_seconds()
        
        # Get actual media duration using ffprobe
        media_duration = await _get_media_duration(output_path)
        
        logger.info("hls_download_complete",
                   url=url, output=output_path,
                   file_size=file_size, duration=duration,
                   media_duration=media_duration)
        
        return HLSDownloadResult(
            stream_url=url,
            output_path=output_path,
            success=True,
            duration=media_duration or duration,
            file_size=file_size,
            is_live_recording=max_duration is not None
        )
        
    except Exception as e:
        logger.error("hls_download_failed", url=url, error=str(e))
        return HLSDownloadResult(
            stream_url=url,
            output_path=output_path,
            success=False,
            error=str(e)
        )


async def _get_media_duration(file_path: str) -> Optional[float]:
    """Get media duration using ffprobe."""
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            file_path
        ]
        
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        stdout, stderr = await proc.communicate()
        
        if proc.returncode == 0 and stdout:
            return float(stdout.decode().strip())
        
    except Exception as e:
        logger.warning("ffprobe_duration_failed", file=file_path, error=str(e))
    
    return None


async def record_live_stream(
    url: str,
    output_dir: str,
    segment_duration: int = 3600,  # 1 hour segments
    max_segments: int = 24,  # Max 24 hours
    quality: str = "best",
    on_segment_complete: Optional[callable] = None
) -> List[HLSDownloadResult]:
    """
    Record a live stream in segments.
    
    Args:
        url: M3U8 stream URL
        output_dir: Directory to save segments
        segment_duration: Duration of each segment in seconds
        max_segments: Maximum number of segments to record
        quality: Quality selection
        on_segment_complete: Callback(segment_path) when segment is done
    
    Returns:
        List of HLSDownloadResult for each segment
    """
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    
    results = []
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    
    for i in range(max_segments):
        segment_name = f"live_{timestamp}_part{i+1:03d}.mp4"
        segment_path = str(output_path / segment_name)
        
        logger.info("recording_live_segment", segment=i+1, path=segment_path)
        
        result = await download_hls_stream(
            url=url,
            output_path=segment_path,
            max_duration=segment_duration,
            quality=quality,
            live_restart=(i == 0)  # Only restart from beginning for first segment
        )
        
        results.append(result)
        
        if result.success and on_segment_complete:
            await on_segment_complete(segment_path)
        
        # If segment failed, wait before retry
        if not result.success:
            logger.warning("segment_failed_retrying", segment=i+1, error=result.error)
            await asyncio.sleep(10)
            continue
        
        # Small gap between segments
        await asyncio.sleep(2)
    
    return results


async def convert_hls_to_mp3(
    input_path: str,
    output_path: str,
    bitrate: str = "320k"
) -> bool:
    """Convert HLS stream recording to MP3."""
    try:
        cmd = [
            "ffmpeg", "-y",
            "-i", input_path,
            "-vn",  # No video
            "-acodec", "libmp3lame",
            "-b:a", bitrate,
            "-q:a", "0",
            output_path
        ]
        
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        stdout, stderr = await proc.communicate()
        
        return proc.returncode == 0 and os.path.exists(output_path)
        
    except Exception as e:
        logger.error("hls_to_mp3_conversion_failed", input=input_path, error=str(e))
        return False


async def merge_hls_segments(
    segment_paths: List[str],
    output_path: str
) -> bool:
    """Merge multiple HLS segments into single file."""
    if not segment_paths:
        return False
    
    if len(segment_paths) == 1:
        # Just copy
        import shutil
        shutil.copy2(segment_paths[0], output_path)
        return True
    
    try:
        # Create concat file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            for path in segment_paths:
                f.write(f"file '{os.path.abspath(path)}'\n")
            concat_file = f.name
        
        try:
            cmd = [
                "ffmpeg", "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", concat_file,
                "-c", "copy",
                output_path
            ]
            
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await proc.communicate()
            
            return proc.returncode == 0 and os.path.exists(output_path)
            
        finally:
            if os.path.exists(concat_file):
                os.remove(concat_file)
                
    except Exception as e:
        logger.error("merge_hls_segments_failed", error=str(e))
        return False


def is_m3u8_playlist(content: str) -> bool:
    """Check if content is an M3U8 playlist."""
    return content.strip().startswith('#EXTM3U')


async def parse_m3u8_playlist(playlist_url: str) -> Dict[str, Any]:
    """Parse M3U8 playlist and extract stream info."""
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(playlist_url) as resp:
                if resp.status != 200:
                    return {"error": f"HTTP {resp.status}"}
                
                content = await resp.text()
        
        if not is_m3u8_playlist(content):
            return {"error": "Not a valid M3U8 playlist"}
        
        lines = content.strip().split('\n')
        info = {
            "is_master": False,
            "is_live": False,
            "segments": [],
            "streams": [],
            "target_duration": 0,
            "sequence": 0
        }
        
        current_segment = {}
        for line in lines:
            line = line.strip()
            
            if line.startswith('#EXT-X-STREAM-INF:'):
                info["is_master"] = True
                # Parse stream attributes
                attrs = line[18:]  # Remove '#EXT-X-STREAM-INF:'
                stream_info = {}
                for attr in attrs.split(','):
                    if '=' in attr:
                        k, v = attr.split('=', 1)
                        stream_info[k.strip()] = v.strip().strip('"')
                current_segment = stream_info
                
            elif line.startswith('#EXTINF:'):
                # Segment duration
                duration_str = line[8:]
                if ',' in duration_str:
                    duration_str = duration_str.split(',')[0]
                try:
                    current_segment['duration'] = float(duration_str)
                except:
                    pass
                    
            elif line.startswith('#EXT-X-TARGETDURATION:'):
                info["target_duration"] = int(line.split(':')[1])
                
            elif line.startswith('#EXT-X-MEDIA-SEQUENCE:'):
                info["sequence"] = int(line.split(':')[1])
                
            elif line.startswith('#EXT-X-ENDLIST'):
                info["is_live"] = False
                
            elif line and not line.startswith('#'):
                # This is a segment URL or stream URL
                if info["is_master"]:
                    current_segment['url'] = line
                    info["streams"].append(current_segment)
                    current_segment = {}
                else:
                    current_segment['url'] = line
                    info["segments"].append(current_segment)
                    current_segment = {}
        
        info["is_live"] = not info.get("is_live", True)  # If no ENDLIST, it's live
        info["segment_count"] = len(info["segments"])
        info["stream_count"] = len(info["streams"])
        
        return info
        
    except Exception as e:
        logger.error("parse_m3u8_failed", url=playlist_url, error=str(e))
        return {"error": str(e)}