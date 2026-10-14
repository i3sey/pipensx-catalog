import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import build_catalog
import scrape

TOPIC_HTML = """<html><head><title>Luxor Evolved [NSZ] • rutracker.org</title></head>
<body>
<h1 class="maintitle"><a id="topic-title" href="viewtopic.php?t=6892780">Luxor Evolved [NSZ][ENG]</a></h1>
<div class="post_body" id="p-1" data-posted="1759300000">
Размер: 56 MB<br />
<a href="magnet:?xt=urn:btih:CA6A0F1A5FD4CFD6FD5A758E9A2CD3D2399B9621&tr=http%3A%2F%2Fbt2.t-ru.org%2Fann%3Fmagnet">magnet</a><br />
<img src="https://cdn.example/cover.png" />
<img src="https://cdn.example/s1.jpg" />
<img src="https://rutracker.org/templates/smiles/smile.gif" />
Описание раздачи: неоновый пазл.
</div>
</body></html>"""

CHALLENGE_HTML = """<html><head><title>Just a moment...</title></head>
<body>challenge platforom challenges.cloudflare.com</body></html>"""

SECTION_HTML = """<html><body>
<a href="viewtopic.php?t=111">Game One</a>
<a href="viewtopic.php?t=222">Game Two</a>
<a href="viewtopic.php?t=111">Game One dup</a>
<a href="viewforum.php?f=5">section</a>
</body></html>"""

ATOM_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Switch</title>
  <entry>
    <id>tag:rto.feed,2026-10-02:/t/1234567</id>
    <title>[Обновлено] [Nintendo Switch] Sample Game [NSZ][ENG] [1.5 GB]</title>
    <updated>2026-10-02T06:24:39+00:00</updated>
    <author><name>uploader</name></author>
    <link href="https://rutracker.org/forum/viewtopic.php?t=1234567" />
    <link rel="enclosure" href="magnet:?xt=urn:btih:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA&amp;tr=http%3A%2F%2Fbt.t-ru.org%2Fann" />
  </entry>
</feed>"""


def entry(topic="1", btih="A" * 40, **kw):
    record = {
        "title": f"Game {topic}",
        "magnet": "magnet:?xt=urn:btih:" + btih,
        "cover": "https://cdn.example/cover.png",
        "size": 100,
        "topic_id": topic,
    }
    record.update(kw)
    return build_catalog.normalize_entry(record, 1700000000)


class ScrapeTests(unittest.TestCase):
    def test_parse_topic_page(self):
        record = scrape.parse_topic_page(
            TOPIC_HTML, "6892780",
            "https://rutracker.org/forum/viewtopic.php?t=6892780")
        assert record is not None
        self.assertEqual(record["title"], "Luxor Evolved [NSZ][ENG]")
        self.assertIn("btih:CA6A0F1A5FD4CFD6FD5A758E9A2CD3D2399B9621",
                      record["magnet"])
        self.assertEqual(record["topic_id"], "6892780")
        self.assertEqual(record["cover"], "https://cdn.example/cover.png")
        self.assertEqual(record["screenshots"], ["https://cdn.example/s1.jpg"])
        self.assertEqual(record["size"], 56 * 1024**2)
        self.assertEqual(record["published_date"], 1759300000)

    def test_parse_atom_feed_uses_enclosure_magnet_and_size(self):
        entries = scrape.parse_atom_feed(ATOM_XML)
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["topic_id"], "1234567")
        self.assertEqual(entry["title"], "Sample Game [NSZ][ENG]")
        self.assertEqual(entry["size_bytes"], int(1.5 * 1024**3))
        self.assertEqual(entry["updated"], "2026-10-02T06:24:39+00:00")
        self.assertEqual(entry["author"], "uploader")
        self.assertEqual(
            build_catalog.info_hash_from_magnet(entry["magnet"]), "A" * 40)
        self.assertEqual(entry["url"],
                         "https://rutracker.org/forum/viewtopic.php?t=1234567")

    def test_topic_page_uses_atom_fields_as_fallback(self):
        feed_entry = scrape.parse_atom_feed(ATOM_XML)[0]
        html = """<html><head><title>Page title</title></head><body>
        <div class="post_body"><img src="https://cdn.example/cover.png" />
        Размер: 56 MB</div></body></html>"""
        record = scrape.parse_topic_page(
            html, feed_entry["topic_id"], feed_entry["url"], feed_entry)
        assert record is not None
        self.assertEqual(record["magnet"], feed_entry["magnet"])
        self.assertEqual(record["size"], 56 * 1024**2)
        self.assertEqual(record["cover"], "https://cdn.example/cover.png")

    def test_parse_langegen_style_topic_metadata(self):
        html = """<html><head><title>Sample Game • rutracker.org</title></head>
        <body><h1 class="maintitle"><a id="topic-title">Sample Game [NSZ]</a></h1>
        <div class="post_body" data-posted="1759300000">
        <a href="magnet:?xt=urn:btih:CA6A0F1A5FD4CFD6FD5A758E9A2CD3D2399B9621&amp;tr=http%3A%2F%2Ftracker">magnet</a><br />
        <var class="postImg" title="https://cdn.example/cover.png"></var>
        <img data-src="https://cdn.example/screen.png" />
        Размер: 56 MB<br />Год выпуска: 2025<br />Жанр: Action<br />
        Разработчик: Studio<br />Издатель: Publisher<br />
        Формат образа: .NSZ<br />Язык интерфейса: Английский<br />
        Язык озвучки: Русский<br />Мультиплеер: до 4 игроков<br />
        Title ID: 0100A2301BDE8800<br />
        Описание: Short description.<br />Доп. информация: More.</div>
        </body></html>"""
        record = scrape.parse_topic_page(
            html, "1234567",
            "https://rutracker.org/forum/viewtopic.php?t=1234567")
        assert record is not None
        self.assertEqual(record["cover"], "https://cdn.example/cover.png")
        self.assertEqual(record["screenshots"],
                         ["https://cdn.example/screen.png"])
        self.assertEqual(record["year"], "2025")
        self.assertEqual(record["developer"], "Studio")
        self.assertEqual(record["publisher"], "Publisher")
        self.assertEqual(record["title_id"], "0100A2301BDE8000")
        self.assertEqual(record["description"], "Short description.")
        self.assertEqual(record["size"], 56 * 1024**2)

    def test_title_id_normalizes_patch_to_base(self):
        self.assertEqual(scrape.extract_title_id("0100A2301BDE8800"),
                         "0100A2301BDE8000")

    def test_topic_id_merge_replaces_old_hash_and_preserves_metadata(self):
        old = entry("123", "A" * 40, year="2020", developer="Old studio",
                    description="Keep this description")
        assert old is not None
        replacement, _ = build_catalog.build_catalog(
            [{"title": "Game updated", "magnet": "magnet:?xt=urn:btih:" + "C" * 40,
              "cover": "https://cdn.example/new.png", "size": 200,
              "topic_id": "123"}],
            {"hide": [], "title_id": {}, "rename": {}}, 1700000001)
        merged, stats = scrape.merge_with_previous([old], replacement, 1700000001)
        self.assertEqual(len(merged), 1)
        self.assertEqual(build_catalog.info_hash_from_magnet(merged[0]["magnet"]),
                         "C" * 40)
        self.assertEqual(merged[0]["year"], "2020")
        self.assertEqual(merged[0]["developer"], "Old studio")
        self.assertEqual(merged[0]["description"], "Keep this description")
        self.assertEqual(stats["updated"], 1)
        self.assertEqual(stats["added"], 0)

    def test_language_refresh_preserves_unparsed_voice_languages(self):
        old = entry("321", "A" * 40)
        assert old is not None
        old["languages"] = {"interface": ["en"], "voice": ["ja"], "note": ""}
        fresh, _ = build_catalog.build_catalog(
            [{"title": "Game 321", "magnet": "magnet:?xt=urn:btih:" + "A" * 40,
              "cover": "https://cdn.example/cover.png", "size": 100,
              "topic_id": "321", "interface_lang": "Русский"}],
            {"hide": [], "title_id": {}, "rename": {}}, 1700000001)
        merged, _ = scrape.merge_with_previous([old], fresh, 1700000001)
        self.assertEqual(merged[0]["languages"]["interface"], ["ru"])
        self.assertEqual(merged[0]["languages"]["voice"], ["ja"])

    def test_parse_topic_page_needs_magnet_and_title(self):
        self.assertIsNone(scrape.parse_topic_page("<html></html>", "1", "u"))
        self.assertIsNone(scrape.parse_topic_page(
            "<html><title>T</title>no magnet here</html>", "1", "u"))

    def test_challenge_markers(self):
        self.assertTrue(any(m in CHALLENGE_HTML
                            for m in scrape.CHALLENGE_MARKERS))

    def test_parse_section_page(self):
        self.assertEqual(scrape.parse_section_page(SECTION_HTML),
                         ["111", "222"])

    def test_merge_scraped_wins_stale_kept(self):
        old_a = entry("1", "A" * 40)
        old_b = entry("2", "B" * 40)
        assert old_a is not None and old_b is not None
        scraped, _ = build_catalog.build_catalog(
            [{"title": "Game 1 v2", "magnet": "magnet:?xt=urn:btih:" + "A" * 40,
              "cover": "https://cdn.example/cover.png", "size": 200,
              "topic_id": "1"}],
            {"hide": [], "title_id": {}, "rename": {}}, 1700000001)
        self.assertEqual(len(scraped), 1)
        merged, stats = scrape.merge_with_previous([old_a, old_b], scraped,
                                                   1700000001)
        self.assertEqual(len(merged), 2)
        self.assertEqual(stats["updated"], 1)
        self.assertEqual(stats["added"], 0)
        self.assertEqual(stats["keptStale"], 1)
        by_hash = {build_catalog.info_hash_from_magnet(e["magnet"]): e
                   for e in merged}
        self.assertEqual(by_hash["A" * 40]["title"], "Game 1 v2")
        self.assertEqual(by_hash["B" * 40]["title"], "Game 2")

    def test_merge_empty_scrape_keeps_everything(self):
        old = entry("1", "A" * 40)
        assert old is not None
        merged, stats = scrape.merge_with_previous([old], [], 1)
        self.assertEqual(len(merged), 1)
        self.assertEqual(stats["keptStale"], 1)

    def test_coverage_gate(self):
        self.assertIsNone(scrape.check_coverage_gate(7045, 7045, 0.98))
        self.assertIsNone(scrape.check_coverage_gate(6905, 7045, 0.98))
        self.assertIsNotNone(scrape.check_coverage_gate(6000, 7045, 0.98))
        self.assertIsNotNone(scrape.check_coverage_gate(0, 7045, 0.98))
        self.assertIsNotNone(scrape.check_coverage_gate(7045, 0, 0.98))


if __name__ == "__main__":
    unittest.main()
