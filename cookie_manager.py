"""Cookie management system for yt-dlp authentication."""

import asyncio
import json
import re
import time
from typing import Optional, Dict, Any, Tuple
from pathlib import Path

from config import settings
from logging_config import get_logger

logger = get_logger("cookie_manager")

# ==================== DEFAULT COOKIE (for when no cookie is uploaded) ====================
DEFAULT_YOUTUBE_COOKIE = """# Netscape HTTP Cookie File
# Default cookie — works for non-authenticated YouTube downloads
.youtube.com	TRUE	/	TRUE	0	VISITOR_INFO1_LIVE	S3cF9BfU0W4
.youtube.com	TRUE	/	FALSE	0	PREF	f6=40000000&tz=Asia.Tehran&f7=100&f5=30000
.youtube.com	TRUE	/	FALSE	0	CONSENT	PENDING+987
.youtube.com	TRUE	/	FALSE	0	SOCS	CAISEwgDEgk2ODM0MjcwMzEaAmVuIAEaBgiA_LCzBg
.youtube.com	TRUE	/	FALSE	0	YSC	qAr6W4LjHqM
"""


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
            # Always save to file so sync functions can find cookies
            self._save_to_file(platform, cookie_data)
            logger.info("cookies_saved", platform=platform)
            return True
        except Exception as e:
            logger.error("cookies_save_failed", platform=platform, error=str(e))
            return False

    def _save_to_file(self, platform: str, cookie_data: str):
        """Save cookies to file as fallback when no database."""
        try:
            cookie_dir = Path("cookie_data")
            cookie_dir.mkdir(exist_ok=True)
            cookie_file = cookie_dir / f"{platform}_cookies.txt"
            cookie_file.write_text(cookie_data)
        except Exception as e:
            logger.error("cookie_file_save_failed", platform=platform, error=str(e))

    def _load_from_file(self, platform: str) -> Optional[str]:
        """Load cookies from file fallback."""
        try:
            cookie_file = Path("cookie_data") / f"{platform}_cookies.txt"
            if cookie_file.exists():
                return cookie_file.read_text()
        except Exception:
            pass
        return None

    async def get_cookies(self, platform: str) -> Optional[str]:
        """Get cookies for a platform."""
        # Check cache first
        if platform in self._cookies_cache:
            return self._cookies_cache[platform]

        # Check file fallback
        file_cookies = self._load_from_file(platform)
        if file_cookies:
            self._cookies_cache[platform] = file_cookies
            return file_cookies

        if not settings.postgres_dsn:
            # Return default cookie for youtube
            if platform.lower() in ('youtube', 'youtube.com'):
                return DEFAULT_YOUTUBE_COOKIE
            return None
            
        try:
            from database import get_database
            db = await get_database()
            if not db:
                if platform.lower() in ('youtube', 'youtube.com'):
                    return DEFAULT_YOUTUBE_COOKIE
                return None
            cookie_data = await db.get_cookies(platform)
            if cookie_data:
                self._cookies_cache[platform] = cookie_data
                return cookie_data
            # Return default for youtube
            if platform.lower() in ('youtube', 'youtube.com'):
                return DEFAULT_YOUTUBE_COOKIE
            return None
        except Exception as e:
            logger.error("cookies_get_failed", platform=platform, error=str(e))
            if platform.lower() in ('youtube', 'youtube.com'):
                return DEFAULT_YOUTUBE_COOKIE
            return None

    async def get_cookie_file_path(self, platform: str) -> Optional[str]:
        """Get path to cookie file for yt-dlp (creates temp file if needed)."""
        cookie_data = await self.get_cookies(platform)
        if not cookie_data:
            return None

        # Create temp cookie file
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
            # Also delete file
            cookie_file = Path("cookie_data") / f"{platform}_cookies.txt"
            if cookie_file.exists():
                cookie_file.unlink()
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

    async def validate_and_save(self, platform: str, cookie_text: str) -> Tuple[bool, str]:
        """Validate cookie format and save."""
        if not self.parse_netscape_cookies(cookie_text):
            return False, "Invalid cookie format. Expected Netscape format (tab-separated)."

        # Basic validation - check for required fields
        if platform.lower() in ("youtube", "youtube.com"):
            if "youtube.com" not in cookie_text and ".youtube.com" not in cookie_text:
                return False, "Cookies don't appear to be for YouTube domain."

        success = await self.save_cookies(platform.lower(), cookie_text)
        if success:
            return True, f"Cookies saved for {platform}"
        return False, "Failed to save cookies"


# ==================== COOKIE FORMAT DETECTION & CONVERSION ====================

def detect_cookie_format(content: str) -> str:
    """Detect the format of cookie data.
    
    Returns: 'netscape', 'cookie_editor_json', 'json_array', 'json_object', 
             'http_header', 'sqlite_info', 'unknown'
    """
    content_stripped = content.strip()
    
    # SQLite binary file (starts with SQLite format header)
    if content_stripped.startswith('SQLite format 3'):
        return 'sqlite_info'
    
    # JSON format
    if content_stripped.startswith('['):
        try:
            data = json.loads(content_stripped)
            if isinstance(data, list) and len(data) > 0:
                first = data[0]
                if isinstance(first, dict):
                    # Cookie-Editor format: has "Host raw", "Name raw", "Content raw"
                    if 'Host raw' in first or 'Name raw' in first:
                        return 'cookie_editor_json'
                    # EditThisCookie format: has "name", "value"
                    if 'name' in first and 'value' in first:
                        return 'json_array'
        except json.JSONDecodeError:
            pass
    
    if content_stripped.startswith('{'):
        try:
            data = json.loads(content_stripped)
            if isinstance(data, dict):
                if 'cookies' in data and isinstance(data['cookies'], list):
                    return 'json_object'
                if 'name' in data and 'value' in data:
                    return 'json_object'
        except json.JSONDecodeError:
            pass
    
    # HTTP Cookie header format: key=value; key=value  (single line or few lines)
    # Must NOT look like Netscape (no tabs separating 7+ fields)
    lines = content_stripped.split('\n')
    first_non_empty = ""
    for line in lines:
        if line.strip():
            first_non_empty = line.strip()
            break
    
    if first_non_empty and '=' in first_non_empty and '\t' not in first_non_empty:
        # Check if it's key=value; key=value pattern
        pairs = first_non_empty.split(';')
        kv_count = sum(1 for p in pairs if '=' in p.strip())
        if kv_count >= 3:
            return 'http_header'
    
    # Netscape format (tab-separated, 7+ fields per line)
    netscape_count = 0
    for line in lines[:20]:
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.split('\t')
        if len(parts) >= 7:
            netscape_count += 1
    if netscape_count > 0:
        return 'netscape'
    
    return 'unknown'


def _parse_expires(raw_expires: str) -> int:
    """Parse expiry from various formats to unix timestamp."""
    if not raw_expires:
        return 0
    # Already a unix timestamp
    try:
        val = int(raw_expires)
        if val > 1000000000:  # Looks like unix timestamp
            return val
    except ValueError:
        pass
    # Date string format: MM-DD-YYYY HH:MM:SS
    from datetime import datetime
    for fmt in ('%m-%d-%Y %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S'):
        try:
            dt = datetime.strptime(raw_expires, fmt)
            return int(dt.timestamp())
        except ValueError:
            continue
    return 0


def _secure_to_bool(val) -> bool:
    """Convert various secure flag representations to bool."""
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.lower() in ('true', '1', 'yes', 'encrypted connections only')
    return False


def convert_cookie_editor_to_netscape(data: list, default_domain: str = ".youtube.com") -> str:
    """Convert Cookie-Editor JSON format to Netscape.
    
    Cookie-Editor fields:
      "Host raw": "https://.youtube.com/"
      "Name raw": "SID"
      "Content raw": "value"
      "Path raw": "/"
      "Expires raw": "1801527884"
      "Send for raw": "true"
      "HTTP only raw": "true"
      "This domain only raw": "false"
    """
    lines = ["# Netscape HTTP Cookie File", "# Generated by yt-bot cookie converter", ""]
    
    for cookie in data:
        name = cookie.get('Name raw', cookie.get('name', ''))
        value = cookie.get('Content raw', cookie.get('value', ''))
        
        if not name:
            continue
        
        # Extract domain from "Host raw"
        host_raw = cookie.get('Host raw', '')
        if host_raw:
            # Remove protocol and trailing slash
            domain = re.sub(r'^https?://', '', host_raw).rstrip('/')
            if not domain.startswith('.'):
                domain = '.' + domain
        else:
            domain = default_domain
        
        path = cookie.get('Path raw', cookie.get('path', '/'))
        secure = _secure_to_bool(cookie.get('Send for raw', cookie.get('secure', False)))
        http_only = cookie.get('HTTP only raw', cookie.get('httpOnly', False))
        
        # Expiry
        exp_raw = cookie.get('Expires raw', cookie.get('expirationDate', cookie.get('expires', 0)))
        expiry = _parse_expires(str(exp_raw)) if exp_raw else 0
        
        # Domain flag
        flag = "TRUE" if domain.startswith('.') else "FALSE"
        secure_str = "TRUE" if secure else "FALSE"
        
        lines.append(f"{domain}\t{flag}\t{path}\t{secure_str}\t{expiry}\t{name}\t{value}")
    
    return '\n'.join(lines)


def convert_json_array_to_netscape(data: list, default_domain: str = ".youtube.com") -> str:
    """Convert EditThisCookie / generic JSON array to Netscape format."""
    lines = ["# Netscape HTTP Cookie File", "# Generated by yt-bot cookie converter", ""]
    
    for cookie in data:
        name = cookie.get('name', '')
        value = cookie.get('value', '')
        
        if not name:
            continue
        
        domain = cookie.get('domain', default_domain)
        path = cookie.get('path', '/')
        secure = _secure_to_bool(cookie.get('secure', False))
        
        exp = cookie.get('expirationDate', cookie.get('expires', 0))
        if isinstance(exp, (int, float)) and exp > 0:
            expiry = int(exp)
        else:
            expiry = _parse_expires(str(exp)) if exp else 0
        
        if domain and not domain.startswith('.'):
            domain = '.' + domain
        
        flag = "TRUE" if domain.startswith('.') else "FALSE"
        secure_str = "TRUE" if secure else "FALSE"
        
        lines.append(f"{domain}\t{flag}\t{path}\t{secure_str}\t{expiry}\t{name}\t{value}")
    
    return '\n'.join(lines)


def convert_json_object_to_netscape(data: dict, default_domain: str = ".youtube.com") -> str:
    """Convert JSON object format to Netscape format."""
    if 'cookies' in data and isinstance(data['cookies'], list):
        return convert_json_array_to_netscape(data['cookies'], default_domain)
    if 'name' in data and 'value' in data:
        return convert_json_array_to_netscape([data], default_domain)
    return ""


def convert_http_header_to_netscape(cookie_header: str, domain: str = ".youtube.com") -> str:
    """Convert HTTP Cookie header string to Netscape format.
    
    Example: "SID=xxx; HSID=yyy; SSID=zzz"
    """
    lines = ["# Netscape HTTP Cookie File", "# Generated by yt-bot cookie converter", ""]
    
    # Handle multi-line (just join all non-empty lines)
    cookie_header = ' '.join(l.strip() for l in cookie_header.splitlines() if l.strip())
    
    pairs = cookie_header.split(';')
    for pair in pairs:
        pair = pair.strip()
        if '=' not in pair:
            continue
        name, value = pair.split('=', 1)
        name = name.strip()
        value = value.strip()
        if name:
            lines.append(f"{domain}\tTRUE\t/\tFALSE\t0\t{name}\t{value}")
    
    return '\n'.join(lines)


def convert_cookies(raw_content: str, platform: str = "youtube") -> Tuple[bool, str]:
    """Auto-detect and convert cookie data to Netscape format.
    
    Args:
        raw_content: Raw cookie file content (any format)
        platform: Platform name for domain detection
        
    Returns:
        (success, netscape_content_or_error)
    """
    fmt = detect_cookie_format(raw_content)
    logger.info("cookie_format_detected", format=fmt, platform=platform)
    
    # Default domain based on platform
    domain_map = {
        'youtube': '.youtube.com',
        'youtube.com': '.youtube.com',
        'soundcloud': '.soundcloud.com',
        'soundcloud.com': '.soundcloud.com',
        'instagram': '.instagram.com',
        'instagram.com': '.instagram.com',
        'tiktok': '.tiktok.com',
        'tiktok.com': '.tiktok.com',
        'twitter': '.twitter.com',
        'twitter.com': '.twitter.com',
        'x.com': '.x.com',
        'facebook': '.facebook.com',
        'facebook.com': '.facebook.com',
    }
    default_domain = domain_map.get(platform.lower(), '.youtube.com')
    
    if fmt == 'netscape':
        return True, raw_content.strip()
    
    elif fmt == 'cookie_editor_json':
        try:
            data = json.loads(raw_content.strip())
            result = convert_cookie_editor_to_netscape(data, default_domain)
            if result:
                return True, result
            return False, "Cookie-Editor JSON is empty"
        except Exception as e:
            return False, f"Failed to parse Cookie-Editor JSON: {e}"
    
    elif fmt == 'json_array':
        try:
            data = json.loads(raw_content.strip())
            return True, convert_json_array_to_netscape(data, default_domain)
        except Exception as e:
            return False, f"Failed to parse JSON: {e}"
    
    elif fmt == 'json_object':
        try:
            data = json.loads(raw_content.strip())
            result = convert_json_object_to_netscape(data, default_domain)
            if result:
                return True, result
            return False, "JSON object doesn't contain cookie data"
        except Exception as e:
            return False, f"Failed to parse JSON: {e}"
    
    elif fmt == 'http_header':
        return True, convert_http_header_to_netscape(raw_content.strip(), default_domain)
    
    elif fmt == 'sqlite_info':
        return False, (
            "⚠️ فایل SQLite (مرورگر) دریافت شد.\n"
            "لطفاً از افزونه \"Get cookies.txt LOCALLY\" استفاده کن:\n"
            "1. افزونه رو نصب کن\n"
            "2. به سایت مورد نظر برو\n"
            "3. روی آیکون افزونه کلیک کن → Export\n"
            "4. فایل .txt خروجی رو بفرست"
        )
    
    else:
        return False, (
            "❌ فرمت فایل ناشناخته.\n\n"
            "فرمت‌های پشتیبانی شده:\n"
            "• Netscape (خروجی Get cookies.txt LOCALLY)\n"
            "• Cookie-Editor JSON (خروجی افزونه Cookie-Editor)\n"
            "• EditThisCookie JSON\n"
            "• HTTP Cookie header (key=value; key=value)\n\n"
            "💡 پیشنهاد: از افزونه \"Get cookies.txt LOCALLY\" یا \"Cookie-Editor\" استفاده کن"
        )


# Global cookie manager
_cookie_manager: Optional[CookieManager] = None

def get_cookie_manager() -> CookieManager:
    global _cookie_manager
    if _cookie_manager is None:
        _cookie_manager = CookieManager()
    return _cookie_manager
