import json
import unittest
from unittest.mock import MagicMock, patch

from flaresolverr_client import (
    FlareSolverrClient,
    FlareSolverrError,
    FlareSolverrSessionError,
    parse_cookie_string,
)


class TestFlareSolverrClient(unittest.TestCase):
    def test_parse_cookie_string(self):
        cookie_str = "bb_session=12345; bb_data=abcdef; some_var=val"
        cookies = parse_cookie_string(cookie_str, domain=".rutracker.org")
        self.assertEqual(len(cookies), 3)
        self.assertEqual(cookies[0]["name"], "bb_session")
        self.assertEqual(cookies[0]["value"], "12345")
        self.assertEqual(cookies[0]["domain"], ".rutracker.org")
        self.assertEqual(cookies[1]["name"], "bb_data")
        self.assertEqual(cookies[1]["value"], "abcdef")

    def test_parse_cookie_empty(self):
        self.assertEqual(parse_cookie_string(""), [])

    @patch("urllib.request.urlopen")
    def test_create_and_destroy_session(self, mock_urlopen):
        # Mock session create
        resp_mock = MagicMock()
        resp_mock.read.return_value = json.dumps({
            "status": "ok",
            "message": "Session created",
            "session": "test-session-123"
        }).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = resp_mock

        client = FlareSolverrClient(base_url="http://localhost:8191")
        session_id = client.create_session()
        self.assertEqual(session_id, "test-session-123")
        self.assertEqual(client.session_id, "test-session-123")

        # Mock session destroy
        resp_mock.read.return_value = json.dumps({
            "status": "ok",
            "message": "Session destroyed"
        }).encode("utf-8")
        client.destroy_session()
        self.assertIsNone(client.session_id)

    @patch("urllib.request.urlopen")
    def test_get_page_success(self, mock_urlopen):
        resp_mock = MagicMock()
        resp_mock.read.return_value = json.dumps({
            "status": "ok",
            "solution": {
                "response": "<html><head><title>RuTracker</title></head><body>OK</body></html>",
                "status": 200
            }
        }).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = resp_mock

        client = FlareSolverrClient(default_session_id="existing-session")
        html = client.get_page("https://rutracker.org/forum/viewtopic.php?t=100")
        self.assertIn("RuTracker", html)

    @patch("urllib.request.urlopen")
    def test_retry_on_failure(self, mock_urlopen):
        fail_resp = MagicMock()
        fail_resp.read.return_value = json.dumps({
            "status": "error",
            "message": "Challenge not solved"
        }).encode("utf-8")

        success_resp = MagicMock()
        success_resp.read.return_value = json.dumps({
            "status": "ok",
            "solution": {"response": "<html>Solved</html>"}
        }).encode("utf-8")

        create_session_resp = MagicMock()
        create_session_resp.read.return_value = json.dumps({
            "status": "ok",
            "session": "recovered-session"
        }).encode("utf-8")

        destroy_session_resp = MagicMock()
        destroy_session_resp.read.return_value = json.dumps({
            "status": "ok",
            "message": "Destroyed"
        }).encode("utf-8")

        mock_urlopen.return_value.__enter__.side_effect = [
            fail_resp,
            destroy_session_resp,
            create_session_resp,
            success_resp
        ]

        client = FlareSolverrClient(default_session_id="broken-session")
        html = client.get_page("https://rutracker.org/forum/viewtopic.php?t=100", retries=2, backoff=0.01)
        self.assertEqual(html, "<html>Solved</html>")


if __name__ == "__main__":
    unittest.main()
