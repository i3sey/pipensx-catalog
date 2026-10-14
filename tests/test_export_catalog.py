import json
import tempfile
import unittest
from pathlib import Path

from export_catalog import export_catalog
from storage import Storage


class TestExportCatalog(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dir_path = Path(self.temp_dir.name)
        self.db_path = self.dir_path / "test.db"
        self.storage = Storage(self.db_path)

        # Create overrides
        self.overrides_path = self.dir_path / "overrides.json"
        self.overrides_path.write_text(json.dumps({
            "hide": [1002],
            "title_id": {"1003": "0100999000010000"},
            "rename": {"1001": "Custom Super Mario"}
        }), encoding="utf-8")

        # Create sample entries in storage
        # 1001: valid entry
        self.storage.upsert_topic(1001, "https://rutracker.org/forum/viewtopic.php?t=1001")
        self.storage.save_catalog_entry(1001, "0100000000010000", {
            "topic_id": 1001,
            "title": "Super Mario Odyssey",
            "magnet": "magnet:?xt=urn:btih:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "cover": "https://images.example.com/cover1.jpg",
            "size": 5000000,
            "size_bytes": 5000000,
            "title_id": "0100000000010000",
            "package_type": "nsp",
            "catalog_generated_at": 1600000000,
            "published_date": 1600000000,
        })

        # 1002: to be hidden
        self.storage.upsert_topic(1002, "https://rutracker.org/forum/viewtopic.php?t=1002")
        self.storage.save_catalog_entry(1002, "0100000000020000", {
            "topic_id": 1002,
            "title": "Hidden Game",
            "magnet": "magnet:?xt=urn:btih:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
            "cover": "https://images.example.com/cover2.jpg",
            "size": 2000000,
            "size_bytes": 2000000,
            "title_id": "0100000000020000",
            "catalog_generated_at": 1600000000,
            "published_date": 1600000000,
        })

        # 1003: title_id overridden
        self.storage.upsert_topic(1003, "https://rutracker.org/forum/viewtopic.php?t=1003")
        self.storage.save_catalog_entry(1003, None, {
            "topic_id": 1003,
            "title": "Zelda",
            "magnet": "magnet:?xt=urn:btih:CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC",
            "cover": "https://images.example.com/cover3.jpg",
            "size": 3000000,
            "size_bytes": 3000000,
            "title_id": None,
            "catalog_generated_at": 1600000000,
            "published_date": 1600000000,
        })

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_export_catalog_success(self):
        out_dir = self.dir_path / "out"
        res = export_catalog(
            storage=self.storage,
            output_dir=out_dir,
            overrides_path=self.overrides_path,
            schema_path="schema.json",
        )

        self.assertEqual(res["entries"], 2)  # 1001 and 1003 (1002 hidden)

        # Check catalog.json
        cat_file = out_dir / "catalog.json"
        self.assertTrue(cat_file.exists())
        catalog = json.loads(cat_file.read_text(encoding="utf-8"))
        self.assertEqual(len(catalog), 2)

        # Verify rename applied to 1001
        entry_1001 = next(e for e in catalog if e["topic_id"] == 1001)
        self.assertEqual(entry_1001["title"], "Custom Super Mario")

        # Verify title_id override applied to 1003
        entry_1003 = next(e for e in catalog if e["topic_id"] == 1003)
        self.assertEqual(entry_1003["title_id"], "0100999000010000")

        # Check manifest.json
        man_file = out_dir / "manifest.json"
        self.assertTrue(man_file.exists())
        manifest = json.loads(man_file.read_text(encoding="utf-8"))
        self.assertEqual(manifest["catalog"]["entries"], 2)
        self.assertEqual(manifest["catalog"]["sha256"], res["sha256"])

        # Check report.md
        rep_file = out_dir / "report.md"
        self.assertTrue(rep_file.exists())
        report_text = rep_file.read_text(encoding="utf-8")
        self.assertIn("hidden by overrides: 1", report_text)
        self.assertIn("renames applied: 1", report_text)
        self.assertIn("title_id overrides applied: 1", report_text)


if __name__ == "__main__":
    unittest.main()
