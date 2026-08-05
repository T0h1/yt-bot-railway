"""Configuration management with Pydantic Settings."""

import os
from typing import Optional
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Telegram Bot (REQUIRED - only these two need manual setup)
    telegram_bot_token_ytdl: str = Field(
        default="", description="Telegram Bot Token (primary)", validation_alias="BOT_TOKEN"
    )
    admin_id_ytdl: int = Field(default=0, description="Admin Telegram User ID (primary)", validation_alias="ADMIN_ID")

    # Fallback env vars (backward compatibility)
    bot_token: str = Field(default="", description="Alternative token variable")
    admin_id: int = Field(default=0, description="Alternative admin ID variable")

    # Railway / Deployment
    railway_mode: bool = Field(default=True, description="Enable Railway-specific limits")
    video_compression_target_mb: int = Field(
        default=20, description="Target video size in MB for compression"
    )
    max_video_duration_sec: int = Field(
        default=300, description="Max video duration in seconds (Railway mode)"
    )
    max_storage_mb: int = Field(default=400, description="Max storage in MB before cleanup")

    # Optional: External services - Mapped from Railway standard env vars
    redis_url: str = Field(default="", description="Redis URL", validation_alias="REDIS_URL")
    postgres_dsn: str = Field(default="", description="PostgreSQL DSN", validation_alias="DATABASE_URL")
    sentry_dsn: str = Field(default="", description="Sentry DSN for error tracking", validation_alias="SENTRY_DSN")
    spotify_client_id: str = Field(default="", description="Spotify API Client ID", validation_alias="SPOTIFY_CLIENT_ID")
    spotify_client_secret: str = Field(default="", description="Spotify API Client Secret", validation_alias="SPOTIFY_CLIENT_SECRET")
    cookie_file: str = Field(default="", description="Path to cookies file for yt-dlp")

    # Optional: Railway public domain for webhook (AUTO-SET by Railway)
    railway_public_domain: str = Field(
        default="", description="Railway public domain", validation_alias="RAILWAY_PUBLIC_DOMAIN"
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
        # Allow population by field name AND alias
        populate_by_name=True,
    )

    @model_validator(mode="after")
    def _auto_detect_railway_vars(self) -> "Settings":
        """Auto-detect Railway-provided environment variables after validation."""
        # PostgreSQL: Railway provides DATABASE_URL (mapped via validation_alias above)
        # But also check for POSTGRES_DSN as fallback
        if not self.postgres_dsn:
            self.postgres_dsn = os.environ.get("DATABASE_URL", "") or os.environ.get("POSTGRES_DSN", "")
        
        # Redis: Railway provides REDIS_URL (mapped via validation_alias above)
        if not self.redis_url:
            self.redis_url = os.environ.get("REDIS_URL", "")
        
        # Railway public domain: Railway provides RAILWAY_PUBLIC_DOMAIN or RAILWAY_STATIC_URL
        if not self.railway_public_domain:
            self.railway_public_domain = (
                os.environ.get("RAILWAY_PUBLIC_DOMAIN", "") 
                or os.environ.get("RAILWAY_STATIC_URL", "").replace("https://", "").replace("http://", "")
            )
        
        # Port: Railway provides PORT (mapped via validation_alias above)
        if not self.port or self.port == 8080:
            self.port = int(os.environ.get("PORT", "8080"))
        
        # Telegram token fallback
        if not self.telegram_bot_token_ytdl:
            self.telegram_bot_token_ytdl = os.environ.get("BOT_TOKEN", "") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        
        # Admin ID fallback
        if not self.admin_id_ytdl:
            admin_id_str = os.environ.get("ADMIN_ID", "") or os.environ.get("ADMIN_USER_ID", "")
            if admin_id_str:
                try:
                    self.admin_id_ytdl = int(admin_id_str)
                except ValueError:
                    pass
        
        return self

    @property
    def token(self) -> str:
        """Get bot token with fallback."""
        return self.telegram_bot_token_ytdl or self.bot_token

    @property
    def admin_id_resolved(self) -> int:
        """Get admin ID with fallback."""
        return self.admin_id_ytdl or self.admin_id

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