import os
import tempfile
import unittest

from storage import Storage


class TestStorage(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_catalog.db")
        self.storage = Storage(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_upsert_topic_and_get(self):
        self.storage.upsert_topic(
            topic_id=12345,
            url="https://rutracker.org/forum/viewtopic.php?t=12345",
            title="Super Mario Odyssey",
            forum_id=1605,
            status="pending",
        )
        topic = self.storage.get_topic(12345)
        self.assertIsNotNone(topic)
        self.assertEqual(topic["topic_id"], 12345)
        self.assertEqual(topic["title"], "Super Mario Odyssey")
        self.assertEqual(topic["status"], "pending")

        # Update status
        self.storage.set_topic_status(12345, "downloaded")
        topic = self.storage.get_topic(12345)
        self.assertEqual(topic["status"], "downloaded")

    def test_upsert_topics_batch_and_pending(self):
        batch = [
            {"topic_id": 1, "url": "https://rutracker.org/1", "title": "Game 1", "forum_id": 1605},
            {"topic_id": 2, "url": "https://rutracker.org/2", "title": "Game 2", "forum_id": 1605},
            {"topic_id": 3, "url": "https://rutracker.org/3", "title": "Game 3", "forum_id": 2000},
        ]
        count = self.storage.upsert_topics_batch(batch)
        self.assertEqual(count, 3)

        pending_1605 = self.storage.get_pending_topics(forum_id=1605)
        self.assertEqual(len(pending_1605), 2)
        self.assertEqual([t["topic_id"] for t in pending_1605], [1, 2])

    def test_save_and_get_topic_details(self):
        self.storage.upsert_topic(topic_id=10, url="https://rutracker.org/10")
        files = [
            {"name": "Game [0100000000010000][v0].nsp", "size": 1024000},
            {"name": "Update [0100000000010800][v65536].nsp", "size": 512000},
        ]
        self.storage.save_topic_details(
            topic_id=10,
            info_hash="ABCDEF1234567890ABCDEF1234567890ABCDEF12",
            registered_at="2023-01-01 12:00:00",
            size_bytes=1536000,
            raw_html="<html><body>Topic</body></html>",
            filelist_json=files,
        )

        details = self.storage.get_topic_details(10)
        self.assertIsNotNone(details)
        self.assertEqual(details["info_hash"], "ABCDEF1234567890ABCDEF1234567890ABCDEF12")
        self.assertEqual(details["size_bytes"], 1536000)
        self.assertEqual(len(details["filelist"]), 2)
        self.assertEqual(details["filelist"][0]["name"], "Game [0100000000010000][v0].nsp")

    def test_save_and_get_catalog_entries_and_stats(self):
        self.storage.upsert_topic(topic_id=10, url="https://rutracker.org/10")
        entry = {
            "title": "Zelda BOTW",
            "magnet": "magnet:?xt=urn:btih:ABCDEF1234567890ABCDEF1234567890ABCDEF12",
            "cover": "https://example.com/cover.jpg",
            "size": 1536000,
            "size_bytes": 1536000,
            "title_id": "0100000000010000",
            "catalog_generated_at": 1600000000,
            "published_date": 1600000000,
        }
        self.storage.save_catalog_entry(10, "0100000000010000", entry)

        fetched = self.storage.get_catalog_entry(10)
        self.assertEqual(fetched["title"], "Zelda BOTW")
        self.assertEqual(fetched["title_id"], "0100000000010000")

        all_entries = self.storage.get_all_catalog_entries()
        self.assertEqual(len(all_entries), 1)

        stats = self.storage.get_stats()
        self.assertEqual(stats["total_topics"], 1)
        self.assertEqual(stats["total_catalog_entries"], 1)
        self.assertEqual(stats["entries_with_title_id"], 1)
        self.assertEqual(stats["entries_without_title_id"], 0)


if __name__ == "__main__":
    unittest.main()
