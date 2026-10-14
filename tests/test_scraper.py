import json
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from scraper import (
    Scraper,
    parse_atom_topics,
    parse_forum_pagination,
    parse_forum_topics,
)
from storage import Storage


class TestScraper(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage = Storage(f"{self.temp_dir.name}/test.db")
        self.mock_flaresolverr = MagicMock()
        self.mock_titledb = MagicMock()
        self.mock_titledb.match.return_value = "0100000000010000"
        self.scraper = Scraper(
            storage=self.storage,
            flaresolverr=self.mock_flaresolverr,
            titledb_matcher=self.mock_titledb,
            delay=0.0,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_parse_forum_topics(self):
        html = """
        <div class="forum">
            <a class="tt-text" href="viewtopic.php?t=6000001">Game A [NSP]</a>
            <a class="tt-text" href="viewtopic.php?t=6000002">Game B [XCI]</a>
        </div>
        """
        topics = parse_forum_topics(html, forum_id=1605)
        self.assertEqual(len(topics), 2)
        self.assertEqual(topics[0]["topic_id"], 6000001)
        self.assertEqual(topics[0]["title"], "Game A [NSP]")
        self.assertEqual(topics[1]["topic_id"], 6000002)

    def test_parse_forum_pagination(self):
        html = """
        <a href="viewforum.php?f=1605&amp;start=50">2</a>
        <a href="viewforum.php?f=1605&amp;start=100">3</a>
        """
        starts = parse_forum_pagination(html)
        self.assertEqual(starts, [0, 50, 100])

    def test_process_topic_end_to_end(self):
        topic_id = 7000001
        self.storage.upsert_topic(topic_id, "https://rutracker.org/forum/viewtopic.php?t=7000001")

        topic_html = """
        <title>Mario Odyssey [0100000000010000] :: RuTracker.org</title>
        <div class="post_body">
            <var class="postImg" title="https://img.example.com/cover.jpg"></var>
            <a href="magnet:?xt=urn:btih:A1B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4E5F6A1B2">Magnet</a>
            Год выпуска: 2017
            Жанр: Platformer
        </div>
        """
        torrent_html = """
        <table class="forumline attach-filelist">
            <tr>
                <td>Mario Odyssey [0100000000010000][v0].nsp</td>
                <td>5 GB</td>
            </tr>
        </table>
        """
        self.mock_flaresolverr.get_page.return_value = topic_html
        self.mock_flaresolverr.post_request.return_value = torrent_html

        success = self.scraper.process_topic(topic_id)
        self.assertTrue(success)

        # Verify entry in DB
        entry = self.storage.get_catalog_entry(topic_id)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["title_id"], "0100000000010000")
        self.assertEqual(entry["package_type"], "nsp")
        self.assertEqual(entry["cover"], "https://img.example.com/cover.jpg")
        self.assertEqual(entry["size"], int(5 * 1024**3))

        topic = self.storage.get_topic(topic_id)
        self.assertEqual(topic["status"], "parsed")


if __name__ == "__main__":
    unittest.main()
