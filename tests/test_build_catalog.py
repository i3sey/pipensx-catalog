import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import build_catalog


def raw(title="Game [NSP]", topic="123", **kw):
    entry = {
        "title": title,
        "magnet": "magnet:?xt=urn:btih:" + kw.pop("btih", "A" * 40),
        "cover": "https://cdn.example/cover.png",
        "size": kw.pop("size", "1.5 GB"),
        "topic_id": topic,
        "url": "https://rutracker.org/forum/viewtopic.php?t=123",
    }
    entry.update(kw)
    return entry


class BuildCatalogTests(unittest.TestCase):
    def test_info_hash_hex_and_base32(self):
        self.assertEqual(
            build_catalog.info_hash_from_magnet(
                "magnet:?xt=urn:btih:" + "a" * 40), "A" * 40)
        import base64
        digest = bytes(range(20))
        b32 = base64.b32encode(digest).decode()
        self.assertEqual(
            build_catalog.info_hash_from_magnet(
                f"magnet:?xt=urn:btih:{b32}"), digest.hex().upper())
        self.assertIsNone(build_catalog.info_hash_from_magnet("not a magnet"))

    def test_size_parsing(self):
        self.assertEqual(build_catalog.parse_size_bytes("56 MB"), 56 * 1024**2)
        self.assertEqual(build_catalog.parse_size_bytes(58720256), 58720256)
        self.assertIsNone(build_catalog.parse_size_bytes("unknown"))

    def test_title_id_validation(self):
        self.assertEqual(
            build_catalog.normalize_title_id("0100a2301bde8000"),
            "0100A2301BDE8000")
        # patch id (low 12 bits set) is not a base id
        self.assertIsNone(build_catalog.normalize_title_id("0100A2301BDE8800"))
        self.assertIsNone(build_catalog.normalize_title_id("garbage"))

    def test_normalize_emits_numeric_size_and_v2_fields(self):
        entry = build_catalog.normalize_entry(
            raw(title="Luxor Evolved [NSZ][ENG]",
                image_format=".NSZ (compressed)",
                title_id="0100a2301bde8000",
                screenshots=["https://a.example/1.jpg"] * 9,
                description=": leading colon",
                interface_lang="Английский [ENG]",
                multiplayer="нет"),
            generated_at=1700000000)
        assert entry is not None
        self.assertEqual(entry["size"], entry["size_bytes"])
        self.assertIsInstance(entry["size"], int)
        self.assertEqual(entry["package_type"], "nsz")
        self.assertEqual(entry["title_id"], "0100A2301BDE8000")
        self.assertEqual(entry["description"], "leading colon")
        self.assertLessEqual(len(entry["screenshots"]), 6)
        self.assertEqual(entry["languages"]["interface"], ["en"])
        self.assertEqual(entry["published_date"], 0)
        self.assertEqual(entry["catalog_generated_at"], 1700000000)

    def test_roundtrip_keeps_v2_structs(self):
        once = build_catalog.normalize_entry(
            raw(title="Game [NSZ][ENG]", interface_lang="Английский",
                multiplayer="до 4 игроков", performance="Да",
                title_id="0100A2301BDE8000"),
            generated_at=1700000000)
        assert once is not None
        twice = build_catalog.normalize_entry(dict(once),
                                              generated_at=1700000000)
        assert twice is not None
        self.assertEqual(twice, once)

    def test_http_cover_kept_for_seed_compat(self):
        entry = build_catalog.normalize_entry(
            raw(cover="http://images.vfl.ru/ii/164338/sample.jpg"),
            generated_at=1)
        assert entry is not None
        self.assertTrue(entry["cover"].startswith("http://"))

    def test_http_screenshots_kept_for_seed_compat(self):
        entry = build_catalog.normalize_entry(
            raw(screenshots=["http://images.vfl.ru/ii/164338/s.jpg"]),
            generated_at=1)
        assert entry is not None
        self.assertEqual(len(entry["screenshots"]), 1)

    def test_screenshots_thumb_dedup_and_cap(self):
        entry = build_catalog.normalize_entry(
            raw(screenshots=[
                "https://i128.fastpic.org/thumb/2026/0809/d6/abc.jpeg",
                "https://i128.fastpic.org/2026/0809/d6/abc.jpeg",
                "ftp://nope.example/1.jpg",
                "https://a.example/2.jpg",
            ]),
            generated_at=1)
        assert entry is not None
        self.assertNotIn("ftp://nope.example/1.jpg", entry["screenshots"])
        # thumb + original collapse to one
        self.assertEqual(len(entry["screenshots"]), 2)

    def test_dedup_by_info_hash(self):
        seed = [raw(btih="A" * 40), raw(btih="A" * 40, title="Dup"),
                raw(btih="B" * 40)]
        entries, stats = build_catalog.build_catalog(
            seed, {"hide": [], "title_id": {}, "rename": {}}, 1)
        self.assertEqual(len(entries), 2)
        self.assertEqual(stats["deduped"], 1)

    def test_overrides_hide_and_title_id(self):
        seed = [raw(topic="111", btih="A" * 40, title_id=None),
                raw(topic="222", btih="B" * 40)]
        overrides = {"hide": ["111"], "title_id": {"222": "0100A2301BDE8000"},
                     "rename": {}}
        entries, _ = build_catalog.build_catalog(seed, overrides, 1)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["title_id"], "0100A2301BDE8000")

    def test_manifest_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            entries, stats = build_catalog.build_catalog([raw()], {
                "hide": [], "title_id": {}, "rename": {}}, 1700000000)
            manifest = build_catalog.write_outputs(
                out, entries, stats, source_commit="abc",
                catalog_commit="def",
                catalog_url="https://github.com/i3sey/pipensx-catalog/releases/latest/download/catalog.json")
            payload = (out / "catalog.json").read_bytes()
            self.assertEqual(manifest["catalog"]["bytes"], len(payload))
            self.assertEqual(manifest["schemaVersion"], 2)
            import hashlib
            self.assertEqual(manifest["catalog"]["sha256"],
                             hashlib.sha256(payload).hexdigest())
            self.assertTrue((out / "report.md").exists())

    def test_validate_rejects_bad_catalog(self):
        with self.assertRaises(ValueError):
            build_catalog.validate_entries([], b"[]")


if __name__ == "__main__":
    unittest.main()
