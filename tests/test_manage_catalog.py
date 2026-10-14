import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from manage_catalog import build_parser, main
from storage import Storage


class TestManageCatalogCLI(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dir_path = Path(self.temp_dir.name)
        self.db_path = self.dir_path / "test.db"
        self.storage = Storage(self.db_path)

        # Seed 1 entry
        self.storage.upsert_topic(500, "https://rutracker.org/forum/viewtopic.php?t=500")
        self.storage.save_catalog_entry(500, "0100000000010000", {
            "topic_id": 500,
            "title": "Test Game",
            "magnet": "magnet:?xt=urn:btih:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "cover": "https://example.com/cover.jpg",
            "size": 1000,
            "size_bytes": 1000,
            "title_id": "0100000000010000",
            "catalog_generated_at": 1600000000,
            "published_date": 1600000000,
        })

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_status_command(self):
        parser = build_parser()
        args = parser.parse_args(["status", "--db", str(self.db_path)])
        buf = io.StringIO()
        with redirect_stdout(buf):
            ret = args.func(args)
        self.assertEqual(ret, 0)
        output = buf.getvalue()
        self.assertIn("Total topics indexed:    1", output)
        self.assertIn("Catalog entries built:   1", output)
        self.assertIn("with TitleID:        1", output)

    def test_export_command(self):
        out_dir = self.dir_path / "out"
        parser = build_parser()
        args = parser.parse_args(["export", "--db", str(self.db_path), "--output", str(out_dir)])
        ret = args.func(args)
        self.assertEqual(ret, 0)
        self.assertTrue((out_dir / "catalog.json").exists())
        self.assertTrue((out_dir / "manifest.json").exists())
        self.assertTrue((out_dir / "report.md").exists())

    @patch("manage_catalog.Scraper")
    def test_scrape_command(self, mock_scraper_cls):
        mock_instance = MagicMock()
        mock_scraper_cls.return_value = mock_instance

        parser = build_parser()
        args = parser.parse_args(["scrape", "--update", "--db", str(self.db_path)])
        ret = args.func(args)
        self.assertEqual(ret, 0)
        mock_instance.run_update.assert_called_once()


if __name__ == "__main__":
    unittest.main()
