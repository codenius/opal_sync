import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
import httpx

from opal_sync.utils.logging import get_logger

logger = get_logger()
OPAL_BASE_URL = "https://bildungsportal.sachsen.de"
MY_COURSES_URL = f"{OPAL_BASE_URL}/opal/auth/resource/courses"


class SessionManager:
    """Manages persistent HTTP sessions and cookie storage."""

    def __init__(self, cookie_file: Path, session_ttl_hours: int = 8):
        self.cookie_file = Path(cookie_file).expanduser().resolve()
        self.session_ttl_hours = session_ttl_hours

    def save_session(self, client: httpx.AsyncClient) -> None:
        """Save cookies from httpx client to disk with metadata."""
        self.cookie_file.parent.mkdir(parents=True, exist_ok=True)
        cookie_list: List[Dict[str, Any]] = []

        for cookie in client.cookies.jar:
            cookie_list.append({
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path,
                "secure": cookie.secure,
            })

        data = {
            "saved_at": time.time(),
            "ttl_hours": self.session_ttl_hours,
            "cookies": cookie_list,
        }

        with open(self.cookie_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        logger.debug(f"Saved {len(cookie_list)} cookies to {self.cookie_file}")

    def load_session(self, client: httpx.AsyncClient) -> bool:
        """Load cookies from disk if within TTL."""
        if not self.cookie_file.is_file():
            return False

        try:
            with open(self.cookie_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            saved_at = data.get("saved_at", 0)
            max_age_seconds = self.session_ttl_hours * 3600

            if time.time() - saved_at > max_age_seconds:
                logger.info("Saved session has expired based on TTL")
                return False

            cookie_list = data.get("cookies", [])
            for c in cookie_list:
                client.cookies.set(
                    name=c["name"],
                    value=c["value"],
                    domain=c.get("domain", "bildungsportal.sachsen.de"),
                    path=c.get("path", "/"),
                )

            logger.debug(f"Loaded {len(cookie_list)} cookies from {self.cookie_file}")
            return True
        except Exception as e:
            logger.warning(f"Failed to load session cookies: {e}")
            return False

    def clear_session(self) -> None:
        """Delete saved cookie file."""
        if self.cookie_file.is_file():
            try:
                self.cookie_file.unlink()
                logger.debug("Deleted cached session cookie file")
            except OSError as e:
                logger.warning(f"Could not delete cookie file: {e}")

    async def is_session_valid(self, client: httpx.AsyncClient) -> bool:
        """Test if the current session can access protected OPAL pages."""
        try:
            resp = await client.get(MY_COURSES_URL, follow_redirects=True)
            if resp.status_code != 200:
                return False

            text_lower = resp.text.lower()
            final_url = str(resp.url).lower()

            if "shiblogin" in final_url or "login" in final_url:
                return False
            if "bitte melden sie sich an" in text_lower:
                return False
            if "shiblogin" in text_lower and "willkommen auf der lernplattform opal" in text_lower:
                return False
            if "anmeldung" in text_lower and "benutzername" in text_lower:
                return False

            # Confirm it's truly an authenticated OPAL dashboard
            if "meine kurse" in text_lower or "abmelden" in text_lower or "logout" in text_lower:
                return True

            return False
        except Exception as e:
            logger.debug(f"Session validation check failed: {e}")
            return False
