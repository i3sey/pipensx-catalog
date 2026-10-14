import os
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import rutracker_transport as transport


class TransportTests(unittest.TestCase):
    def test_parse_cookie_header_keeps_equals_in_values(self):
        parsed = transport.parse_cookie_header(
            " bb_guid=abc ; bb_t=a%3A1%3D2; empty= ; broken ")
        self.assertEqual(parsed, {
            "bb_guid": "abc",
            "bb_t": "a%3A1%3D2",
            "empty": "",
        })

    def test_challenge_detection_is_case_insensitive(self):
        self.assertTrue(transport.is_cloudflare_challenge(
            "<title>JUST A MOMENT...</title>"))
        self.assertTrue(transport.is_cloudflare_challenge(
            "cf-mitigated: challenge"))
        self.assertFalse(transport.is_cloudflare_challenge(
            "<div class='post_body'>topic</div>"))

    def test_impersonation_tracks_firefox_ua(self):
        with patch.dict(os.environ, {
            "RUTRACKER_UA": "Mozilla/5.0 Firefox/155.0",
            "RUTRACKER_IMPERSONATE": "",
        }, clear=False):
            self.assertEqual(transport._impersonation(), "firefox")

    def test_browser_major_version_is_read_from_chromium(self):
        result = SimpleNamespace(stdout="Chromium 153.0.8010.36", stderr="")
        with patch.object(transport.subprocess, "run", return_value=result):
            self.assertEqual(
                transport._browser_major_version("/usr/bin/chromium"), 153)

    def test_topic_page_requires_topic_content(self):
        self.assertEqual(
            transport._expected_page_keywords(
                "https://rutracker.org/forum/viewtopic.php?t=1", None),
            ("post_body", "attach_link"),
        )
        self.assertFalse(transport._valid_page(
            "<title>Just a moment...</title>", ("post_body",)))
        self.assertTrue(transport._valid_page(
            "<div class='post_body'>ok</div>", ("post_body",)))

    def test_http_200_challenge_is_not_accepted_as_a_topic(self):
        fake_requests = SimpleNamespace(
            get=lambda *args, **kwargs: SimpleNamespace(
                status_code=200, text="<title>Just a moment...</title>"))
        fake_package = types.ModuleType("curl_cffi")
        fake_package.requests = fake_requests
        with patch.dict(sys.modules, {
            "curl_cffi": fake_package,
            "curl_cffi.requests": fake_requests,
        }), patch.dict(os.environ, {
            "RUTRACKER_COOKIE": "",
            "SCRAPER_PROXY": "",
            "RUTRACKER_IMPERSONATE": "chrome120",
        }, clear=False):
            with self.assertRaises(transport.TransportError) as caught:
                transport._curl_fetch(
                    "https://rutracker.org/forum/viewtopic.php?t=1", 5, None,
                    ("post_body",))
        self.assertTrue(caught.exception.challenge)

    def test_missing_curl_cffi_uses_stdlib_then_detects_challenge(self):
        class Response:
            status = 200
            headers = SimpleNamespace(get_content_charset=lambda: None)

            def read(self):
                return b"<title>Just a moment...</title>"

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        opener = SimpleNamespace(open=lambda *args, **kwargs: Response())
        with patch.dict(sys.modules, {"curl_cffi": None}), \
             patch.object(transport.urllib.request, "build_opener",
                          return_value=opener), \
             patch("builtins.print"), \
             patch.dict(os.environ, {
                 "RUTRACKER_COOKIE": "",
                 "SCRAPER_PROXY": "",
             }, clear=False):
            with self.assertRaises(transport.TransportError) as caught:
                transport._curl_fetch(
                    "https://rutracker.org/forum/viewtopic.php?t=1", 5, None,
                    ("post_body",))
        self.assertTrue(caught.exception.challenge)

    def test_curl_challenge_falls_back_to_browser(self):
        with patch.object(transport, "_BROWSER", None), \
             patch.dict(os.environ, {"RUTRACKER_BROWSER": "auto"}, clear=False), \
             patch.object(transport, "_curl_fetch",
                          side_effect=transport.TransportError(
                              "Cloudflare challenge", challenge=True)) as curl, \
             patch.object(transport, "_browser_fetch",
                          return_value="<div class='post_body'>ok</div>") as browser, \
             patch("builtins.print"):
            result = transport.fetch(
                "https://rutracker.org/forum/viewtopic.php?t=1", timeout=5)
        self.assertIn("post_body", result)
        curl.assert_called_once()
        browser.assert_called_once()

    def test_browser_off_preserves_challenge_status(self):
        with patch.object(transport, "_BROWSER", None), \
             patch.dict(os.environ, {"RUTRACKER_BROWSER": "off"}, clear=False), \
             patch.object(transport, "_curl_fetch",
                          side_effect=transport.TransportError(
                              "Cloudflare challenge", challenge=True)), \
             patch.object(transport, "_browser_fetch") as browser:
            with self.assertRaises(transport.TransportError) as caught:
                transport.fetch(
                    "https://rutracker.org/forum/viewtopic.php?t=1")
        self.assertTrue(caught.exception.challenge)
        browser.assert_not_called()

    def test_browser_failure_keeps_challenge_flag(self):
        with patch.object(transport, "_BROWSER", None), \
             patch.dict(os.environ, {"RUTRACKER_BROWSER": "auto"}, clear=False), \
             patch.object(transport, "_curl_fetch",
                          side_effect=transport.TransportError(
                              "HTTP 403", challenge=True)), \
             patch.object(transport, "_browser_fetch",
                          side_effect=transport.TransportError(
                              "Turnstile timeout")), \
             patch("builtins.print"):
            with self.assertRaises(transport.TransportError) as caught:
                transport.fetch(
                    "https://rutracker.org/forum/viewtopic.php?t=1")
        self.assertTrue(caught.exception.challenge)
        self.assertNotIn("cookie", str(caught.exception).casefold())


if __name__ == "__main__":
    unittest.main()
