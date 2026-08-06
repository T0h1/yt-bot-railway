"""Cookie management system for yt-dlp authentication."""

import asyncio
import time
from typing import Optional, Dict, Any
from pathlib import Path

from config import settings
from logging_config import get_logger
# from database import get_database

logger = get_logger("cookie_manager")


class CookieManager:
    """Manage cookies for different platforms with persistent storage."""

    def __init__(self):
        self._cookies_cache: Dict[str, str] = {}
        self._cache_ttl = 300  # 5 minutes

    async def save_cookies(self, platform: str, cookie_data: str, expires_at: Optional[float] = None) -> bool:
        """Save cookies for a platform."""
        try:
            from database import get_database
            db = await get_database()
            if db:
                from datetime import datetime
                expires_dt = datetime.fromtimestamp(expires_at) if expires_at else None
                await db.save_cookies(platform, cookie_data, expires_dt)
            self._cookies_cache[platform] = cookie_data
            logger.info("cookies_saved", platform=platform)
            return True
        except Exception as e:
            logger.error("cookies_save_failed", platform=platform, error=str(e))
            return False

    async def get_cookies(self, platform: str) -> Optional[str]:
        """Get cookies for a platform."""
        # Check cache first
        if platform in self._cookies_cache:
            return self._cookies_cache[platform]

        try:
            from database import get_database
            db = await get_database()
            if db:
                cookie_data = await db.get_cookies(platform)
                if cookie_data:
                    self._cookies_cache[platform] = cookie_data
                return cookie_data
            return None
        except Exception as e:
            logger.error("cookies_get_failed", platform=platform, error=str(e))
            return None

    async def get_cookie_file_path(self, platform: str) -> Optional[str]:
        """Get path to cookie file for yt-dlp (creates temp file if needed)."""
        cookie_data = await self.get_cookies(platform)
        if not cookie_data:
            return None

        # Create temp cookie file
        import tempfile
        temp_dir = Path(settings.cookie_file).parent if settings.cookie_file else Path("/tmp")
        temp_dir.mkdir(parents=True, exist_ok=True)

        cookie_file = temp_dir / f"cookies_{platform}.txt"
        try:
            cookie_file.write_text(cookie_data)
            return str(cookie_file)
        except Exception as e:
            logger.error("cookie_file_create_failed", platform=platform, error=str(e))
            return None

    async def list_cookies(self) -> list:
        """List all stored cookies."""
        try:
            from database import get_database
            db = await get_database()
            if db:
                return await db.list_cookies()
            # Fallback: return cached cookies
            return [{"platform": k, "cookie_data": v[:50] + "...", "created_at": "N/A", "updated_at": "N/A"} for k, v in self._cookies_cache.items()]
        except Exception as e:
            logger.error("cookies_list_failed", error=str(e))
            return []

    async def delete_cookies(self, platform: str) -> bool:
        """Delete cookies for a platform."""
        try:
            from database import get_database
            db = await get_database()
            if db:
                await db.delete_cookies(platform)
            self._cookies_cache.pop(platform, None)
            logger.info("cookies_deleted", platform=platform)
            return True
        except Exception as e:
            logger.error("cookies_delete_failed", platform=platform, error=str(e))
            return False

    def parse_netscape_cookies(self, cookie_text: str) -> bool:
        """Validate Netscape format cookies."""
        lines = cookie_text.strip().split('\n')
        valid_count = 0
        for line in lines:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split('\t')
            if len(parts) >= 7:
                valid_count += 1
        return valid_count > 0

    async def validate_and_save(self, platform: str, cookie_text: str) -> tuple[bool, str]:
        """Validate cookie format and save."""
        if not self.parse_netscape_cookies(cookie_text):
            return False, "Invalid cookie format. Expected Netscape format (tab-separated)."

        # Basic validation - check for required fields
        if platform.lower() == "youtube" or platform.lower() == "youtube.com":
            if "youtube.com" not in cookie_text and ".youtube.com" not in cookie_text:
                return False, "Cookies don't appear to be for YouTube domain."

        success = await self.save_cookies(platform.lower(), cookie_text)
        if success:
            return True, f"Cookies saved for {platform}"
        return False, "Failed to save cookies"


# Global cookie manager
_cookie_manager: Optional[CookieManager] = None


def get_cookie_manager() -> CookieManager:
    global _cookie_manager
    if _cookie_manager is None:
        _cookie_manager = CookieManager()
    return _cookie_manager