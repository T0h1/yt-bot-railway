"""Audio normalization and silence trimming using pydub/ffmpeg."""

import os
import asyncio
import logging
from pathlib import Path
from typing import Optional, Tuple
from dataclasses import dataclass

from pydub import AudioSegment
from pydub.silence import detect_nonsilent
from logging_config import get_logger

logger = get_logger("audio_processing")


@dataclass
class AudioProcessingResult:
    """Result of audio processing."""
    input_path: str
    output_path: str
    original_duration: float
    processed_duration: float
    original_lufs: Optional[float]
    target_lufs: float
    silence_removed_ms: int
    success: bool
    error: str = ""


async def normalize_audio(
    input_path: str,
    output_path: str,
    target_lufs: float = -16.0,
    sample_rate: int = 44100,
    bitrate: str = "320k"
) -> AudioProcessingResult:
    """
    Normalize audio to target LUFS level.
    
    Args:
        input_path: Path to input audio file
        output_path: Path to output audio file
        target_lufs: Target loudness in LUFS (default -16 for podcast/music)
        sample_rate: Output sample rate
        bitrate: Output bitrate
    
    Returns:
        AudioProcessingResult with processing details
    """
    try:
        logger.info("normalizing_audio", input=input_path, output=output_path, target_lufs=target_lufs)
        
        # Load audio
        audio = AudioSegment.from_file(input_path)
        original_duration = len(audio) / 1000.0
        
        # Measure current loudness (approximate using RMS)
        # pydub doesn't have built-in LUFS, so we use RMS as approximation
        rms = audio.rms
        # Convert RMS to approximate LUFS (rough conversion)
        if rms > 0:
            import math
            current_lufs = 20 * math.log10(rms / 32768.0)  # Rough approximation
        else:
            current_lufs = -100.0
        
        # Calculate gain needed
        gain_db = target_lufs - current_lufs
        
        # Apply gain (with limits to prevent clipping)
        gain_db = max(-20, min(20, gain_db))  # Limit to ±20dB
        if abs(gain_db) > 0.5:  # Only apply if significant
            audio = audio.apply_gain(gain_db)
            logger.info("applied_gain", gain_db=gain_db)
        
        # Export with target settings
        audio = audio.set_frame_rate(sample_rate)
        audio.export(
            output_path,
            format="mp3",
            bitrate=bitrate,
            parameters=["-q:a", "0"]  # Highest quality VBR
        )
        
        processed_duration = len(audio) / 1000.0
        
        logger.info("audio_normalized", 
                   input=input_path, output=output_path,
                   original_lufs=round(current_lufs, 1),
                   gain_applied=round(gain_db, 1))
        
        return AudioProcessingResult(
            input_path=input_path,
            output_path=output_path,
            original_duration=original_duration,
            processed_duration=processed_duration,
            original_lufs=round(current_lufs, 1) if rms > 0 else None,
            target_lufs=target_lufs,
            silence_removed_ms=0,
            success=True
        )
        
    except Exception as e:
        logger.error("normalize_audio_failed", input=input_path, error=str(e))
        return AudioProcessingResult(
            input_path=input_path,
            output_path=output_path,
            original_duration=0,
            processed_duration=0,
            original_lufs=None,
            target_lufs=target_lufs,
            silence_removed_ms=0,
            success=False,
            error=str(e)
        )


async def trim_silence(
    input_path: str,
    output_path: str,
    silence_thresh: int = -50,  # dB
    min_silence_len: int = 500,  # ms
    keep_silence: int = 100,  # ms to keep at boundaries
    sample_rate: int = 44100,
    bitrate: str = "320k"
) -> AudioProcessingResult:
    """
    Remove silence from audio file.
    
    Args:
        input_path: Path to input audio file
        output_path: Path to output audio file
        silence_thresh: Threshold below which is considered silence (dB)
        min_silence_len: Minimum silence duration to remove (ms)
        keep_silence: Silence to keep at boundaries (ms)
        sample_rate: Output sample rate
        bitrate: Output bitrate
    
    Returns:
        AudioProcessingResult with processing details
    """
    try:
        logger.info("trimming_silence", input=input_path, output=output_path)
        
        # Load audio
        audio = AudioSegment.from_file(input_path)
        original_duration = len(audio) / 1000.0
        
        # Detect non-silent regions
        non_silent_ranges = detect_nonsilent(
            audio,
            min_silence_len=min_silence_len,
            silence_thresh=silence_thresh,
            seek_step=10
        )
        
        if not non_silent_ranges:
            logger.warning("no_non_silent_regions", input=input_path)
            # Just copy the file
            audio.export(output_path, format="mp3", bitrate=bitrate)
            return AudioProcessingResult(
                input_path=input_path,
                output_path=output_path,
                original_duration=original_duration,
                processed_duration=original_duration,
                original_lufs=None,
                target_lufs=-16,
                silence_removed_ms=0,
                success=True
            )
        
        # Add keep_silence padding
        padded_ranges = []
        for start, end in non_silent_ranges:
            start = max(0, start - keep_silence)
            end = min(len(audio), end + keep_silence)
            padded_ranges.append((start, end))
        
        # Merge overlapping ranges
        merged_ranges = []
        for start, end in padded_ranges:
            if merged_ranges and start <= merged_ranges[-1][1]:
                merged_ranges[-1] = (merged_ranges[-1][0], max(merged_ranges[-1][1], end))
            else:
                merged_ranges.append((start, end))
        
        # Concatenate non-silent segments
        processed_audio = AudioSegment.empty()
        for start, end in merged_ranges:
            processed_audio += audio[start:end]
        
        silence_removed = len(audio) - len(processed_audio)
        
        # Export
        processed_audio = processed_audio.set_frame_rate(sample_rate)
        processed_audio.export(
            output_path,
            format="mp3",
            bitrate=bitrate,
            parameters=["-q:a", "0"]
        )
        
        processed_duration = len(processed_audio) / 1000.0
        
        logger.info("silence_trimmed",
                   input=input_path, output=output_path,
                   original_duration=round(original_duration, 1),
                   processed_duration=round(processed_duration, 1),
                   silence_removed_ms=silence_removed)
        
        return AudioProcessingResult(
            input_path=input_path,
            output_path=output_path,
            original_duration=original_duration,
            processed_duration=processed_duration,
            original_lufs=None,
            target_lufs=-16,
            silence_removed_ms=silence_removed,
            success=True
        )
        
    except Exception as e:
        logger.error("trim_silence_failed", input=input_path, error=str(e))
        return AudioProcessingResult(
            input_path=input_path,
            output_path=output_path,
            original_duration=0,
            processed_duration=0,
            original_lufs=None,
            target_lufs=-16,
            silence_removed_ms=0,
            success=False,
            error=str(e)
        )


async def process_audio_full(
    input_path: str,
    output_path: str,
    normalize: bool = True,
    trim_silence_enabled: bool = True,
    target_lufs: float = -16.0,
    silence_thresh: int = -50,
    min_silence_len: int = 500,
    keep_silence: int = 100,
    sample_rate: int = 44100,
    bitrate: str = "320k"
) -> AudioProcessingResult:
    """
    Full audio processing pipeline: normalize + trim silence.
    
    Args:
        input_path: Path to input audio file
        output_path: Path to output audio file
        normalize: Whether to apply loudness normalization
        trim_silence_enabled: Whether to remove silence
        target_lufs: Target loudness for normalization
        silence_thresh: Silence threshold in dB
        min_silence_len: Minimum silence length to remove (ms)
        keep_silence: Silence to keep at boundaries (ms)
        sample_rate: Output sample rate
        bitrate: Output bitrate
    
    Returns:
        AudioProcessingResult with combined processing details
    """
    temp_path = None
    current_input = input_path
    
    try:
        combined_result = AudioProcessingResult(
            input_path=input_path,
            output_path=output_path,
            original_duration=0,
            processed_duration=0,
            original_lufs=None,
            target_lufs=target_lufs,
            silence_removed_ms=0,
            success=True
        )
        
        # Step 1: Normalize if requested
        if normalize:
            temp_path = f"{output_path}.temp_norm.mp3"
            norm_result = await normalize_audio(
                current_input, temp_path, target_lufs, sample_rate, bitrate
            )
            if not norm_result.success:
                return norm_result
            combined_result.original_lufs = norm_result.original_lufs
            combined_result.original_duration = norm_result.original_duration
            current_input = temp_path
        
        # Step 2: Trim silence if requested
        if trim_silence_enabled:
            temp_path2 = f"{output_path}.temp_trim.mp3"
            trim_result = await trim_silence(
                current_input, temp_path2,
                silence_thresh, min_silence_len, keep_silence,
                sample_rate, bitrate
            )
            if not trim_result.success:
                return trim_result
            combined_result.silence_removed_ms = trim_result.silence_removed_ms
            combined_result.processed_duration = trim_result.processed_duration
            current_input = temp_path2
        else:
            combined_result.processed_duration = combined_result.original_duration
        
        # Move final result to output
        import shutil
        shutil.move(current_input, output_path)
        
        # Clean up temp files
        for temp in [f"{output_path}.temp_norm.mp3", f"{output_path}.temp_trim.mp3"]:
            if os.path.exists(temp):
                os.remove(temp)
        
        logger.info("full_audio_processing_complete",
                   input=input_path, output=output_path,
                   normalized=normalize, trimmed=trim_silence_enabled)
        
        return combined_result
        
    except Exception as e:
        logger.error("full_audio_processing_failed", input=input_path, error=str(e))
        # Clean up temp files
        for temp in [f"{output_path}.temp_norm.mp3", f"{output_path}.temp_trim.mp3"]:
            if os.path.exists(temp):
                os.remove(temp)
        return AudioProcessingResult(
            input_path=input_path,
            output_path=output_path,
            original_duration=0,
            processed_duration=0,
            original_lufs=None,
            target_lufs=target_lufs,
            silence_removed_ms=0,
            success=False,
            error=str(e)
        )


def get_audio_info(file_path: str) -> dict:
    """Get basic audio file info."""
    try:
        audio = AudioSegment.from_file(file_path)
        return {
            "duration": len(audio) / 1000.0,
            "channels": audio.channels,
            "sample_rate": audio.frame_rate,
            "sample_width": audio.sample_width,
            "frame_count": len(audio.get_array_of_samples()),
            "max_amplitude": audio.max,
            "rms": audio.rms
        }
    except Exception as e:
        logger.error("get_audio_info_failed", file=file_path, error=str(e))
        return {"error": str(e)}