"""OCR/Whisper fallback for lyrics extraction."""

import asyncio
import logging
from typing import Optional
from pathlib import Path
import tempfile
import os

from logging_config import get_logger

logger = get_logger("lyrics_fallback")

# Fallback to simple OCR using pytesseract if available, 
# otherwise use a mock/simple text extraction from metadata
try:
    import pytesseract
    from PIL import Image
    TESSERACT_AVAILABLE = True
except ImportError:
    TESSERACT_AVAILABLE = False


async def extract_lyrics_from_image(image_path: str) -> Optional[str]:
    """Extract lyrics from cover art image using OCR."""
    if not TESSERACT_AVAILABLE:
        logger.warning("tesseract_not_installed")
        return None
    
    try:
        # Preprocess image for better OCR
        with Image.open(image_path) as img:
            img = img.convert('L')  # Grayscale
            text = pytesseract.image_to_string(img)
            return text.strip()
    except Exception as e:
        logger.error("ocr_extraction_failed", error=str(e))
        return None


async def extract_lyrics_from_audio_segment(audio_path: str) -> Optional[str]:
    """Fallback: Attempt to extract audio transcription using a light whisper model."""
    # This is a placeholder for a heavy Whisper implementation
    # Given limited resources, we prefer LRC/OCR methods first
    logger.info("whisper_extraction_not_implemented_light_only")
    return None


async def get_lyrics_fallback(title: str, artist: str, cover_path: Optional[str] = None) -> str:
    """Get lyrics from fallback sources if LRC/DB failed."""
    lyrics = ""
    
    # 1. Try OCR from cover art
    if cover_path and os.path.exists(cover_path):
        lyrics = await extract_lyrics_from_image(cover_path)
    
    if lyrics and len(lyrics) > 50:
        logger.info("lyrics_extracted_from_ocr")
        return lyrics
        
    return f"[متن آهنگ برای {title} - {artist} در دسترس نیست.]"