"""RuTracker transport: curl_cffi impersonation with a browser fallback.

The public Atom feed is fetched separately by scrape.py. This module handles
protected forum pages and never logs cookie values or proxy credentials.
"""
from __future__ import annotations

import atexit
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
CHALLENGE_MARKERS = (
    "just a moment", "один момент", "cf-browser-verification",
    "cf-mitigated", "challenge-platform", "attention required",
)


class TransportError(OSError):
    """A fetch failure, with an explicit anti-bot challenge flag."""

    def __init__(self, message: str, *, challenge: bool = False):
        super().__init__(message)
        self.challenge = challenge


def parse_cookie_header(value: str | None = None) -> dict[str, str]:
    """Parse a Cookie header without logging or normalizing its values."""
    raw = os.environ.get("RUTRACKER_COOKIE", "") if value is None else value
    cookies: dict[str, str] = {}
    for item in raw.split(";"):
        name, separator, cookie_value = item.strip().partition("=")
        if separator and name:
            cookies[name] = cookie_value.strip()
    return cookies


def is_cloudflare_challenge(html: str) -> bool:
    text = (html or "").casefold()
    return any(marker in text for marker in CHALLENGE_MARKERS)


def _user_agent() -> str:
    return os.environ.get("RUTRACKER_UA", "").strip() or DEFAULT_UA


def _impersonation() -> str:
    configured = os.environ.get("RUTRACKER_IMPERSONATE", "").strip()
    if configured:
        return configured
    ua = _user_agent().casefold()
    if "firefox/" in ua:
        return "firefox"
    if "safari/" in ua and "chrome/" not in ua:
        return "safari"
    return "chrome120"


def _expected_page_keywords(url: str, data: bytes | None) -> tuple[str, ...]:
    if data is not None:
        return ()
    path = urlsplit(url).path.casefold()
    if path.endswith("viewtopic.php"):
        return ("post_body", "attach_link")
    if path.endswith("viewforum.php"):
        return ("forumtable", "hl-tr", "viewtopic.php?t=")
    return ()


def _valid_page(html: str, expected_keywords: tuple[str, ...]) -> bool:
    low = (html or "").casefold()
    if is_cloudflare_challenge(low):
        return False
    if expected_keywords:
        return any(word.casefold() in low for word in expected_keywords)
    return bool(low.strip())


def _stdlib_fetch(url: str, timeout: float, data: bytes | None,
                  expected_keywords: tuple[str, ...]) -> str:
    """Compatibility transport when curl_cffi is not installed."""
    headers = {
        "User-Agent": _user_agent(),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    }
    cookies = parse_cookie_header()
    if cookies:
        headers["Cookie"] = "; ".join(
            f"{name}={value}" for name, value in cookies.items())
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        headers["X-Requested-With"] = "XMLHttpRequest"
    request = urllib.request.Request(url, data=data, headers=headers)
    handlers = []
    proxy = os.environ.get("SCRAPER_PROXY", "").strip()
    if proxy:
        handlers.append(urllib.request.ProxyHandler(
            {"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read().decode(
                response.headers.get_content_charset() or "utf-8", "replace")
            status = int(response.status)
    except urllib.error.HTTPError as exc:
        body = exc.read(8192).decode("utf-8", "replace")
        raise TransportError(
            f"forum returned HTTP {exc.code}",
            challenge=(exc.code in (403, 429)
                       or is_cloudflare_challenge(body)),
        ) from exc
    except Exception as exc:
        raise TransportError(
            f"stdlib request failed ({type(exc).__name__})") from exc
    if status != 200:
        raise TransportError(
            f"forum returned HTTP {status}",
            challenge=(status in (403, 429)
                       or is_cloudflare_challenge(body)),
        )
    if is_cloudflare_challenge(body):
        raise TransportError("Cloudflare challenge in HTTP 200 response",
                             challenge=True)
    if expected_keywords and not any(
            word.casefold() in body.casefold() for word in expected_keywords):
        raise TransportError("HTTP 200 did not contain expected forum content")
    return body


def _curl_fetch(url: str, timeout: float, data: bytes | None,
                expected_keywords: tuple[str, ...]) -> str:
    try:
        from curl_cffi import requests as cf_requests
    except ImportError:
        print("scrape: curl_cffi unavailable; using stdlib compatibility transport")
        return _stdlib_fetch(url, timeout, data, expected_keywords)

    headers = {
        "User-Agent": _user_agent(),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    }
    kwargs: dict[str, Any] = {
        "headers": headers,
        "cookies": parse_cookie_header(),
        "impersonate": _impersonation(),
        "timeout": timeout,
    }
    proxy = os.environ.get("SCRAPER_PROXY", "").strip()
    if proxy:
        kwargs["proxies"] = {"http": proxy, "https": proxy}
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        headers["X-Requested-With"] = "XMLHttpRequest"

    try:
        if data is None:
            response = cf_requests.get(url, **kwargs)
        else:
            response = cf_requests.post(url, data=data, **kwargs)
    except Exception as exc:  # Do not echo exception text: it can contain proxy credentials.
        raise TransportError(
            f"curl_cffi request failed ({type(exc).__name__})") from exc

    body = response.text or ""
    status = int(response.status_code)
    challenged = is_cloudflare_challenge(body) or status in (403, 429)
    if status != 200:
        message = f"forum returned HTTP {status}"
        raise TransportError(message, challenge=challenged)
    if is_cloudflare_challenge(body):
        raise TransportError("Cloudflare challenge in HTTP 200 response",
                             challenge=True)
    if expected_keywords and not any(
            word.casefold() in body.casefold() for word in expected_keywords):
        raise TransportError("HTTP 200 did not contain expected forum content")
    return body


_BROWSER: Any | None = None
_BROWSER_PROFILE: str | None = None
_BROWSER_READY = False
_BROWSER_LOCK = threading.RLock()
_BROWSER_ATEXIT_REGISTERED = False


def _browser_enabled() -> bool:
    return os.environ.get("RUTRACKER_BROWSER", "auto").strip().casefold() \
        not in {"0", "false", "no", "off", "disabled"}


def _browser_wait_seconds() -> int:
    raw = os.environ.get("RUTRACKER_BROWSER_WAIT", "120")
    try:
        return min(180, max(15, int(raw)))
    except ValueError:
        return 120


def _find_browser() -> str | None:
    configured = os.environ.get("CHROME_BINARY", "").strip()
    if configured:
        return configured if os.path.isfile(configured) else None
    for name in ("google-chrome", "google-chrome-stable", "chromium",
                 "chromium-browser", "chrome"):
        path = shutil.which(name)
        if path:
            return path
    return None


def _browser_major_version(binary: str) -> int | None:
    try:
        result = subprocess.run(
            [binary, "--version"], check=False, capture_output=True,
            text=True, timeout=5)
        match = re.search(r"\b(\d+)\.\d+", result.stdout + result.stderr)
        return int(match.group(1)) if match else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _launch_browser() -> Any:
    global _BROWSER, _BROWSER_PROFILE, _BROWSER_ATEXIT_REGISTERED
    try:
        # undetected_chromedriver still imports distutils on newer Python.
        import setuptools._distutils as distutils_compat
        import setuptools._distutils.version as distutils_version_compat
        import sys
        sys.modules.setdefault("distutils", distutils_compat)
        sys.modules.setdefault("distutils.version", distutils_version_compat)
        import undetected_chromedriver as uc
    except Exception as exc:
        raise TransportError(
            f"browser fallback dependencies unavailable ({type(exc).__name__})") from exc

    binary = _find_browser()
    if not binary:
        raise TransportError("browser fallback needs Chrome/Chromium (set CHROME_BINARY)")

    headless_setting = os.environ.get("RUTRACKER_HEADLESS", "").strip().casefold()
    if headless_setting:
        modes = [headless_setting in {"1", "true", "yes", "on"}]
    elif os.environ.get("DISPLAY"):
        # Prefer a real browser when a desktop/Xvfb display is present, but
        # fall back to headless if the inherited DISPLAY is stale/unusable.
        modes = [False, True]
    else:
        modes = [True]

    proxy = os.environ.get("SCRAPER_PROXY", "").strip()
    browser_ua = os.environ.get("RUTRACKER_BROWSER_UA", "").strip()
    version_main = _browser_major_version(binary)
    last_error: Exception | None = None
    for index, headless in enumerate(modes):
        options = uc.ChromeOptions()
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-extensions")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")
        options.add_argument("--window-size=1365,1000")
        if browser_ua:
            options.add_argument(f"--user-agent={browser_ua}")
        if os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0:
            options.add_argument("--no-sandbox")
        if proxy:
            options.add_argument(f"--proxy-server={proxy}")

        _BROWSER_PROFILE = tempfile.mkdtemp(prefix="rutracker-chrome-")
        try:
            _BROWSER = uc.Chrome(
                options=options,
                browser_executable_path=binary,
                user_data_dir=_BROWSER_PROFILE,
                version_main=version_main,
                headless=headless,
                use_subprocess=True,
                log_level=3,
            )
            _BROWSER.set_page_load_timeout(30)
            break
        except Exception as exc:
            last_error = exc
            _BROWSER = None
            shutil.rmtree(_BROWSER_PROFILE, ignore_errors=True)
            _BROWSER_PROFILE = None
            if index + 1 < len(modes):
                print("scrape: headed Chrome unavailable; retrying headless")

    if _BROWSER is None:
        error_name = type(last_error).__name__ if last_error else "unknown"
        raise TransportError(
            f"could not start Chrome/Chromium ({error_name})") from last_error

    if not _BROWSER_ATEXIT_REGISTERED:
        atexit.register(close_browser)
        _BROWSER_ATEXIT_REGISTERED = True
    return _BROWSER


def close_browser() -> None:
    """Close the transient browser and remove its temporary profile."""
    global _BROWSER, _BROWSER_PROFILE, _BROWSER_READY
    with _BROWSER_LOCK:
        browser, profile = _BROWSER, _BROWSER_PROFILE
        _BROWSER = None
        _BROWSER_PROFILE = None
        _BROWSER_READY = False
        if browser is not None:
            try:
                browser.quit()
            except Exception:
                pass
        if profile:
            shutil.rmtree(profile, ignore_errors=True)


def _safe_get(driver: Any, url: str) -> None:
    try:
        driver.get(url)
    except Exception:
        try:
            driver.execute_script("window.stop();")
        except Exception:
            pass


def _inject_auth_cookies(driver: Any) -> None:
    cookies = parse_cookie_header()
    injected: list[str] = []
    for name, value in cookies.items():
        # Clearance is bound to the browser/session that obtained it; let this
        # browser complete a fresh challenge instead of reusing another client's token.
        if name.casefold() == "cf_clearance":
            continue
        try:
            driver.add_cookie({
                "name": name,
                "value": value,
                "domain": ".rutracker.org",
                "path": "/forum/",
            })
            injected.append(name)
        except Exception:
            continue
    if injected:
        print(f"scrape: browser injected auth cookies: {', '.join(injected)}")


def _click_turnstile(driver: Any) -> bool:
    try:
        from selenium.webdriver.common.by import By
        for frame in driver.find_elements(By.TAG_NAME, "iframe"):
            src = frame.get_attribute("src") or ""
            if "challenges.cloudflare.com" not in src:
                continue
            driver.switch_to.frame(frame)
            try:
                for selector in ("input[type='checkbox']", "#challenge-stage"):
                    for element in driver.find_elements(By.CSS_SELECTOR, selector):
                        if element.is_displayed() and element.is_enabled():
                            element.click()
                            print("scrape: clicked visible Cloudflare challenge control")
                            return True
            finally:
                driver.switch_to.default_content()
    except Exception:
        try:
            driver.switch_to.default_content()
        except Exception:
            pass
    return False


def _wait_for_page(driver: Any, expected_keywords: tuple[str, ...],
                   timeout: int) -> str:
    deadline = time.monotonic() + timeout
    last_reload = time.monotonic()
    last_click = 0.0
    last_html = ""
    last_challenge = False
    while time.monotonic() < deadline:
        try:
            last_html = driver.page_source or ""
            title = driver.title or ""
            ready = driver.execute_script("return document.readyState") == "complete"
        except Exception:
            title, ready = "", False

        combined = (title + "\n" + last_html).casefold()
        has_expected = (any(word.casefold() in combined
                            for word in expected_keywords)
                        if expected_keywords else len(last_html) > 256)
        last_challenge = is_cloudflare_challenge(combined)
        if ready and has_expected and not last_challenge:
            return last_html

        now = time.monotonic()
        if last_challenge and now - last_click >= 5:
            _click_turnstile(driver)
            last_click = now
        if last_challenge and now - last_reload >= 45:
            try:
                driver.refresh()
            except Exception:
                pass
            last_reload = now
        time.sleep(1)

    reason = "Cloudflare challenge was not cleared" if last_challenge \
        else "forum page did not reach expected content"
    raise TransportError(reason, challenge=last_challenge)


def _browser_fetch(url: str, timeout: float, data: bytes | None,
                   expected_keywords: tuple[str, ...]) -> str:
    global _BROWSER, _BROWSER_READY
    with _BROWSER_LOCK:
        if not _browser_enabled():
            raise TransportError("browser fallback is disabled")
        driver = _BROWSER or _launch_browser()
        wait_seconds = _browser_wait_seconds()
        origin = "https://rutracker.org/forum/index.php"
        try:
            if not _BROWSER_READY:
                _safe_get(driver, origin)
                _inject_auth_cookies(driver)
                _BROWSER_READY = True

            if data is None:
                _safe_get(driver, url)
                return _wait_for_page(driver, expected_keywords, wait_seconds)

            # Establish a browser session on the forum before making the same
            # authenticated POST that the standard client would have sent.
            _safe_get(driver, origin)
            _inject_auth_cookies(driver)
            _wait_for_page(driver, (), min(wait_seconds, 60))
            body = data.decode("ascii", "strict")
            result = driver.execute_async_script(
                """
                const done = arguments[arguments.length - 1];
                fetch(arguments[0], {
                  method: 'POST', credentials: 'include',
                  headers: {
                    'Content-Type': 'application/x-www-form-urlencoded',
                    'X-Requested-With': 'XMLHttpRequest'
                  }, body: arguments[1]
                }).then(async response => done({
                  status: response.status, text: await response.text()
                })).catch(() => done({status: 0, text: ''}));
                """,
                url, body,
            )
            status = int(result.get("status", 0)) if isinstance(result, dict) else 0
            response_text = result.get("text", "") if isinstance(result, dict) else ""
            if status == 200 and not is_cloudflare_challenge(response_text):
                return response_text
            raise TransportError(
                f"browser POST returned HTTP {status or 'no response'}",
                challenge=(status in (403, 429)
                           or is_cloudflare_challenge(response_text)),
            )
        except TransportError:
            raise
        except Exception as exc:
            # Driver exception text may include URLs, proxy details, or page data.
            raise TransportError(
                f"browser request failed ({type(exc).__name__})") from exc


def fetch(url: str, timeout: float = 30.0, data: bytes | None = None,
          expected_keywords: tuple[str, ...] | None = None) -> str:
    """Fetch a forum page using TLS impersonation, then a persistent browser.

    Once the browser fallback is active, later forum requests stay in that same
    browser so cf_clearance, user-agent, TLS and cookies remain aligned.
    """
    expected = (expected_keywords if expected_keywords is not None
                else _expected_page_keywords(url, data))
    if _BROWSER is not None:
        return _browser_fetch(url, timeout, data, expected)

    curl_error: TransportError | None = None
    try:
        return _curl_fetch(url, timeout, data, expected)
    except TransportError as exc:
        curl_error = exc

    if not _browser_enabled():
        assert curl_error is not None
        raise curl_error

    print("scrape: HTTP transport did not yield a forum page; trying Chrome")
    try:
        return _browser_fetch(url, timeout, data, expected)
    except TransportError as browser_error:
        if curl_error is None:
            raise
        challenge = curl_error.challenge or browser_error.challenge
        raise TransportError(
            f"{curl_error}; browser fallback: {browser_error}",
            challenge=challenge,
        ) from browser_error
