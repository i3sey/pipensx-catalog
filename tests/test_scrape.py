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
