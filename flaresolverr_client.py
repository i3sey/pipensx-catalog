"""FlareSolverr API client for RuTracker catalog scraper.

Handles sessions, Cloudflare challenges, cookies injection, and retries
with exponential backoff.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_FLARESOLVERR_URL = "http://localhost:8191/v1"
DEFAULT_TIMEOUT_MS = 60000


def _load_dotenv(path: str = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip("\"'")
            if k and k not in os.environ:
                os.environ[k] = v


_load_dotenv()


class FlareSolverrError(Exception):
    """Base exception for FlareSolverr client failures."""
    pass


class FlareSolverrSessionError(FlareSolverrError):
    """Session management failures."""
    pass


def parse_cookie_string(cookie_str: str, domain: str = ".rutracker.org") -> list[dict[str, str]]:
    """Convert cookie string (e.g. 'bb_session=xxx; bb_data=yyy') into FlareSolverr cookie list."""
    if not cookie_str:
        return []
    cookies: list[dict[str, str]] = []
    for part in cookie_str.split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            name, value = part.split("=", 1)
            name = name.strip()
            value = value.strip()
            if name:
                cookies.append({
                    "name": name,
                    "value": value,
                    "domain": domain,
                    "path": "/",
                })
    return cookies


class FlareSolverrClient:
    """Client for FlareSolverr v1 API."""

    def __init__(
        self,
        base_url: str | None = None,
        cookie_str: str | None = None,
        default_session_id: str | None = None,
        max_timeout: int = DEFAULT_TIMEOUT_MS,
    ):
        self.base_url = (
            base_url
            or os.environ.get("FLARESOLVERR_URL")
            or DEFAULT_FLARESOLVERR_URL
        ).rstrip("/")
        if not self.base_url.endswith("/v1"):
            self.base_url = f"{self.base_url}/v1"

        cookie_value = cookie_str if cookie_str is not None else os.environ.get("RUTRACKER_COOKIE", "")
        self.cookies = parse_cookie_string(cookie_value)
        self.max_timeout = max_timeout
        self.session_id: str | None = default_session_id
        self._owned_session = False

    def _call(self, payload: dict[str, Any], timeout_seconds: float = 90.0) -> dict[str, Any]:
        """Send a JSON command to FlareSolverr and return parsed JSON."""
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
                data = resp.read().decode("utf-8", errors="replace")
                return json.loads(data)
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(err_body)
                msg = parsed.get("message") or err_body
            except Exception:
                msg = err_body
            raise FlareSolverrError(f"HTTP {exc.code} from FlareSolverr: {msg}") from exc
        except urllib.error.URLError as exc:
            raise FlareSolverrError(f"Connection failed to FlareSolverr ({self.base_url}): {exc}") from exc
        except Exception as exc:
            raise FlareSolverrError(f"Unexpected error communicating with FlareSolverr: {exc}") from exc

    def create_session(self, session_id: str | None = None) -> str:
        """Create a new FlareSolverr browser session."""
        payload: dict[str, Any] = {"cmd": "sessions.create"}
        if session_id:
            payload["session"] = session_id
        res = self._call(payload)
        status = res.get("status")
        if status != "ok":
            raise FlareSolverrSessionError(f"Failed to create session: {res.get('message')}")
        self.session_id = res.get("session") or session_id
        self._owned_session = True
        logger.info("FlareSolverr session created: %s", self.session_id)
        return self.session_id

    def destroy_session(self, session_id: str | None = None) -> None:
        """Destroy an existing session."""
        target_session = session_id or self.session_id
        if not target_session:
            return
        payload = {"cmd": "sessions.destroy", "session": target_session}
        try:
            self._call(payload)
            logger.info("FlareSolverr session destroyed: %s", target_session)
        except Exception as exc:
            logger.warning("Failed to destroy FlareSolverr session %s: %s", target_session, exc)
        finally:
            if target_session == self.session_id:
                self.session_id = None
                self._owned_session = False

    def ensure_session(self) -> str:
        """Ensure an active session exists, creating one if necessary."""
        if not self.session_id:
            return self.create_session()
        return self.session_id

    def get_page(
        self,
        url: str,
        session_id: str | None = None,
        retries: int = 3,
        backoff: float = 2.0,
    ) -> str:
        """Fetch a page via FlareSolverr using request.get with retries and session recovery."""
        return self._execute_request(
            cmd="request.get",
            url=url,
            session_id=session_id,
            retries=retries,
            backoff=backoff,
        )

    def post_request(
        self,
        url: str,
        post_data: str | dict[str, Any],
        session_id: str | None = None,
        retries: int = 3,
        backoff: float = 2.0,
    ) -> str:
        """Perform a POST request via FlareSolverr with retries and session recovery."""
        if isinstance(post_data, dict):
            post_str = urllib.parse.urlencode(post_data)
        else:
            post_str = str(post_data)
        return self._execute_request(
            cmd="request.post",
            url=url,
            post_data=post_str,
            session_id=session_id,
            retries=retries,
            backoff=backoff,
        )

    def _execute_request(
        self,
        cmd: str,
        url: str,
        post_data: str | None = None,
        session_id: str | None = None,
        retries: int = 3,
        backoff: float = 2.0,
    ) -> str:
        current_session = session_id or self.session_id or self.ensure_session()
        delay = backoff

        for attempt in range(1, retries + 1):
            payload: dict[str, Any] = {
                "cmd": cmd,
                "url": url,
                "session": current_session,
                "maxTimeout": self.max_timeout,
            }
            if self.cookies:
                payload["cookies"] = self.cookies
            if post_data is not None:
                payload["postData"] = post_data

            try:
                res = self._call(payload, timeout_seconds=(self.max_timeout / 1000.0) + 30.0)
                status = res.get("status")
                if status == "ok":
                    solution = res.get("solution") or {}
                    html = solution.get("response", "")
                    if html:
                        return html
                    raise FlareSolverrError("Empty response body from FlareSolverr solution")

                message = res.get("message", "Unknown FlareSolverr error")
                raise FlareSolverrError(f"FlareSolverr returned status '{status}': {message}")

            except (FlareSolverrError, Exception) as exc:
                logger.warning(
                    "Request %s (attempt %d/%d) to %s failed: %s",
                    cmd, attempt, retries, url, exc
                )
                if attempt == retries:
                    raise

                # Recreate session on session or challenge errors
                if "session" in str(exc).lower() or "challenge" in str(exc).lower():
                    logger.info("Attempting to recreate FlareSolverr session...")
                    try:
                        self.destroy_session(current_session)
                    except Exception:
                        pass
                    current_session = self.create_session()

                time.sleep(delay)
                delay *= 2.0

        raise FlareSolverrError(f"Failed to fetch {url} after {retries} retries")

    def __enter__(self) -> FlareSolverrClient:
        self.ensure_session()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._owned_session:
            self.destroy_session()
