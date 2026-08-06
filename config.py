"""Configuration management with Pydantic Settings."""

import os
from typing import Optional
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Telegram Bot (REQUIRED - only these two need manual setup)
    bot_token: str = Field(
        default="", description="Telegram Bot Token", validation_alias="BOT_TOKEN"
    )
    admin_id: int = Field(default=0, description="Admin Telegram User ID", validation_alias="ADMIN_ID")

    # Railway / Deployment - all optional with defaults
    railway_mode: bool = Field(default=True, description="Enable Railway-specific limits")
    video_compression_target_mb: int = Field(
        default=20, description="Target video size in MB for compression"
    )
    max_video_duration_sec: int = Field(
        default=0, description="Max video duration in seconds (0 = no limit)"
    )
    max_storage_mb: int = Field(default=400, description="Max storage in MB before cleanup")

    # Optional: External services - only used if provided
    redis_url: str = Field(default="", description="Redis URL for rate limiting/queue")
    postgres_dsn: str = Field(default="", description="PostgreSQL DSN for persistent storage")
    sentry_dsn: str = Field(default="", description="Sentry DSN for error tracking")
    spotify_client_id: str = Field(default="", description="Spotify API Client ID")
    spotify_client_secret: str = Field(default="", description="Spotify API Client Secret")
    cookie_file: str = Field(default="", description="Path to cookies file for yt-dlp")
    admin_api_key: str = Field(default="", description="API key for admin dashboard", validation_alias="ADMIN_API_KEY")

    # Optional: Railway public domain for webhook
    railway_public_domain: str = Field(
        default="", description="Railway public domain (auto-set by Railway)"
    )

    # Webhook settings
    webhook_mode: bool = Field(default=True, description="Enable webhook mode (vs polling)")
    port: int = Field(default=8080, description="Port for webhook server", validation_alias="PORT")

    # Logging
    log_level: str = Field(default="INFO", description="Log level")
    log_json: bool = Field(default=True, description="Output logs as JSON")

    # Multi-user support
    allowed_users: str = Field(default="", description="Comma-separated list of allowed user IDs (empty = all)")
    max_downloads_per_user_per_day: int = Field(default=50, description="Default daily download limit per user")

    model_config = SettingsConfigDict(
        env_file=".env_ytdl",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    @property
    def token(self) -> str:
        """Get bot token."""
        return self.bot_token

    @property
    def admin_id_resolved(self) -> int:
        """Get admin ID."""
        return self.admin_id

    @property
    def webhook_url(self) -> Optional[str]:
        """Construct webhook URL if domain available."""
        if self.railway_public_domain:
            return f"https://{self.railway_public_domain}/webhook"
        return None


# Global settings instance
settings = Settings()


# Computed constants for backward compatibility
TOKEN = settings.token
ADMIN_ID = settings.admin_id_resolved
RAILWAY_MODE = settings.railway_mode
VIDEO_COMPRESSION_TARGET_MB = settings.video_compression_target_mb
MAX_VIDEO_DURATION_SEC = settings.max_video_duration_sec
MAX_STORAGE_MB = settings.max_storage_mb
WEBHOOK_MODE = settings.webhook_mode
PORT = settings.port

# Multi-user
ALLOWED_USERS = [int(x.strip()) for x in settings.allowed_users.split(",") if x.strip()] if settings.allowed_users else []
MAX_DOWNLOADS_PER_USER_PER_DAY = settings.max_downloads_per_user_per_day