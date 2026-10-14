import json
import tempfile
import unittest
from pathlib import Path

from extractor import (
    TitleDBMatcher,
    classify_title_id,
    clean_switch_2_edition,
    extract_from_filelist,
    extract_topic_images,
    is_base_title_id,
    normalize_game_name,
    parse_languages,
    parse_size_bytes,
    parse_topic_metadata,
    parse_viewtorrent_filelist,
    to_base_title_id,
)


class TestExtractor(unittest.TestCase):
    def test_parse_size_bytes(self):
        self.assertEqual(parse_size_bytes("1.5 GB"), int(1.5 * 1024**3))
        self.assertEqual(parse_size_bytes("500 МБ"), 500 * 1024**2)
        self.assertEqual(parse_size_bytes(1024), 1024)
        self.assertIsNone(parse_size_bytes("unknown"))
        self.assertIsNone(parse_size_bytes(None))

    def test_title_id_helpers(self):
        base_tid = "0100000000010000"
        update_tid = "0100000000010800"
        dlc_tid = "0100000000010001"

        self.assertTrue(is_base_title_id(base_tid))
        self.assertFalse(is_base_title_id(update_tid))
        self.assertFalse(is_base_title_id(dlc_tid))

        self.assertEqual(classify_title_id(base_tid), "base")
        self.assertEqual(classify_title_id(update_tid), "update")
        self.assertEqual(classify_title_id(dlc_tid), "dlc")

        self.assertEqual(to_base_title_id(update_tid), base_tid)
        self.assertEqual(to_base_title_id(dlc_tid), base_tid)
        self.assertEqual(to_base_title_id(base_tid), base_tid)

    def test_parse_viewtorrent_filelist_and_extraction(self):
        html = """
        <table class="forumline attach-filelist">
            <tr>
                <td>Super Mario Odyssey [0100000000010000][v0].nsp</td>
                <td>5.6 GB</td>
            </tr>
            <tr>
                <td>Super Mario Odyssey [0100000000010800][v196608].nsz</td>
                <td>500 MB</td>
            </tr>
        </table>
        """
        filelist = parse_viewtorrent_filelist(html)
        self.assertEqual(len(filelist), 2)
        self.assertEqual(filelist[0]["name"], "Super Mario Odyssey [0100000000010000][v0].nsp")
        self.assertEqual(filelist[0]["size"], int(5.6 * 1024**3))

        extracted = extract_from_filelist(filelist)
        self.assertEqual(extracted["title_id"], "0100000000010000")
        self.assertEqual(extracted["package_type"], "nsp")
        self.assertEqual(extracted["version"], "v196608")
        self.assertIn("0100000000010000", extracted["base_title_ids"])
        self.assertIn("0100000000010800", extracted["update_title_ids"])

    def test_extract_from_filelist_update_only(self):
        # Case where torrent only has an update file
        filelist = [
            {"name": "Zelda BOTW Update [01007EF00011E800][v851968].nsp", "size": 1024}
        ]
        extracted = extract_from_filelist(filelist)
        # Should correctly deduce base title id ending in 000
        self.assertEqual(extracted["title_id"], "01007EF00011E000")
        self.assertEqual(extracted["version"], "v851968")

    def test_extract_topic_images(self):
        html = """
        <div class="post_body">
            <var class="postImg" title="https://images.example.com/cover.jpg"></var>
            <img src="https://rutracker.org/forum/images/icon_star.gif">
            <var class="postImg" title="https://images.example.com/screen1.jpg"></var>
            <var class="postImg" title="https://images.example.com/screen2.jpg"></var>
        </div>
        """
        cover, screens = extract_topic_images(html)
        self.assertEqual(cover, "https://images.example.com/cover.jpg")
        self.assertEqual(screens, ["https://images.example.com/screen1.jpg", "https://images.example.com/screen2.jpg"])

    def test_parse_languages(self):
        text = """
        Язык интерфейса: Русский, Английский
        Язык озвучки: Английский, Японский
        """
        langs = parse_languages(text)
        self.assertIn("ru", langs["interface"])
        self.assertIn("en", langs["interface"])
        self.assertIn("ja", langs["voice"])
        self.assertIn("en", langs["voice"])

    def test_parse_topic_metadata(self):
        html = """
        <title>The Legend of Zelda: Breath of the Wild [01007EF00011E000] [NSP] :: RuTracker.org</title>
        <div class="post_body">
            <var class="postImg" title="https://example.com/cover.jpg"></var>
            <a href="magnet:?xt=urn:btih:0123456789ABCDEF0123456789ABCDEF01234567&dn=Zelda">Download</a>
            <br>
            Год выпуска: 2017<br>
            Жанр: Action, Adventure<br>
            Разработчик: Nintendo<br>
            Издательство: Nintendo<br>
            Язык интерфейса: Русский<br>
            Язык озвучки: Русский, Английский<br>
            Описание: Отличная игра.
        </div>
        """
        meta = parse_topic_metadata(html, topic_id=5000000)
        self.assertEqual(meta["topic_id"], 5000000)
        self.assertEqual(meta["info_hash"], "0123456789ABCDEF0123456789ABCDEF01234567")
        self.assertEqual(meta["cover"], "https://example.com/cover.jpg")
        self.assertEqual(meta["title_id"], "01007EF00011E000")
        self.assertEqual(meta["year"], "2017")
        self.assertEqual(meta["genre"], "Action, Adventure")
        self.assertEqual(meta["developer"], "Nintendo")
        self.assertEqual(meta["publisher"], "Nintendo")

    def test_clean_switch_2_edition(self):
        self.assertEqual(
            clean_switch_2_edition("Hollow Knight – Nintendo Switch 2 Edition"),
            "Hollow Knight"
        )
        self.assertEqual(
            clean_switch_2_edition("Stardew Valley: Nintendo Switch 2 Edition"),
            "Stardew Valley"
        )
        self.assertEqual(
            clean_switch_2_edition("[Nintendo Switch 1-2] Video Player"),
            "[Nintendo Switch] Video Player"
        )
        # Should not damage titles with other 2s
        self.assertEqual(
            clean_switch_2_edition("Double Switch 25th Anniversary Edition"),
            "Double Switch 25th Anniversary Edition"
        )
        self.assertEqual(
            clean_switch_2_edition("Overcooked! 2"),
            "Overcooked! 2"
        )

    def test_titledb_matcher(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tf:
            json.dump({
                "0100000000010000": {"name": "Super Mario Odyssey"},
                "01007EF00011E000": {"name": "The Legend of Zelda: Breath of the Wild"}
            }, tf)
            temp_path = tf.name

        try:
            matcher = TitleDBMatcher(cache_path=temp_path)
            self.assertEqual(
                matcher.match("Super Mario Odyssey [Switch] [NSP] [RUS]"),
                "0100000000010000"
            )
            self.assertEqual(
                matcher.match("The Legend of Zelda: Breath of the Wild (Update 1.6.0)"),
                "01007EF00011E000"
            )
            self.assertIsNone(matcher.match("Non Existent Game 999"))
        finally:
            Path(temp_path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
