"""Extractor for Nintendo Switch releases from RuTracker topic pages and torrent filelists.

Extracts:
- TitleIDs ([0100...]) from filelists and topic description
- Base vs Update vs DLC content classification
- Version information ([v0], [v65536], v1.2.0)
- Package types (nsp, nsz, xci, etc.)
- Metadata (cover, screenshots <= 6, description, year, genre, dev, pub, languages)
- Magnet link and info_hash
- Fallback matching by title against TitleDB (pipensx-metadata)
"""

from __future__ import annotations

import base64
import html as html_lib
import json
import logging
import os
import re
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

TITLE_ID_RE = re.compile(r"\b(0100[0-9A-Fa-f]{12})\b")
TITLE_ID_BRACKET_RE = re.compile(r"\[(0100[0-9A-Fa-f]{12})\]", re.IGNORECASE)
VERSION_NUMERIC_RE = re.compile(r"\[v(\d+)\]", re.IGNORECASE)
VERSION_SEMVER_RE = re.compile(r"\bv?(\d+\.\d+(?:\.\d+)*)\b", re.IGNORECASE)
MAGNET_RE = re.compile(r"magnet:\?xt=urn:btih:[A-Za-z0-9]+[^\"'\s<>]*")
BTIH_HEX_RE = re.compile(r"^[0-9A-Fa-f]{40}$")
BTIH_B32_RE = re.compile(r"^[A-Z2-7a-z2-7]{32}$")

PACKAGE_EXT_RE = re.compile(r"\.(nsp|nsz|xci|nro|zip|7z)\b", re.IGNORECASE)
PACKAGE_TAG_RE = re.compile(r"\[(nsp|nsz|xci|nro|zip|7z)\]", re.IGNORECASE)

SIZE_RE = re.compile(
    r"(?P<value>\d+(?:[.,]\d+)?)\s*"
    r"(?P<unit>bytes?|b|kb|kib|mb|mib|gb|gib|tb|tib|"
    r"байт(?:а|ов)?|кб|мб|гб|тб)\b",
    re.IGNORECASE,
)
SIZE_MULTIPLIERS = {
    "b": 1, "byte": 1, "bytes": 1,
    "байт": 1, "байта": 1, "байтов": 1,
    "kb": 1024, "kib": 1024, "кб": 1024,
    "mb": 1024**2, "mib": 1024**2, "мб": 1024**2,
    "gb": 1024**3, "gib": 1024**3, "гб": 1024**3,
    "tb": 1024**4, "tib": 1024**4, "тб": 1024**4,
}

LANG_MAP = {
    "английский": "en", "english": "en", "eng": "en",
    "русский": "ru", "russian": "ru", "rus": "ru",
    "японский": "ja", "japanese": "ja", "jpn": "ja",
    "французский": "fr", "french": "fr", "fra": "fr",
    "немецкий": "de", "german": "de", "deu": "de", "ger": "de",
    "испанский": "es", "spanish": "es", "spa": "es",
    "итальянский": "it", "italian": "it", "ita": "it",
    "португальский": "pt", "portuguese": "pt", "por": "pt",
    "китайский": "zh", "chinese": "zh", "chi": "zh", "zho": "zh",
    "корейский": "ko", "korean": "ko", "kor": "ko",
    "польский": "pl", "polish": "pl", "pol": "pl",
    "украинский": "uk", "ukrainian": "uk", "ukr": "uk",
    "нидерландский": "nl", "голландский": "nl", "dutch": "nl",
}

SKIP_IMG_PATTERNS = (
    "rutracker.org/static", "rutracker.org/templates", "/smiles/", "/smilies/",
    "/icon", "icon_", "bbcode", "/spoiler", "button", "logo", "pixel.gif",
    "clear.gif", "rutracker.wiki", "banner",
)

DEFAULT_TITLEDB_URL = "https://github.com/i3sey/pipensx-metadata/releases/latest/download/game_metadata_index.json"

SWITCH_2_RE = re.compile(
    r"\s*(?:[-–—:]\s*)?(?:Nintendo\s+)?Switch\s+2(?:\s+Edition)?\b",
    re.IGNORECASE,
)


def clean_switch_2_edition(text: str) -> str:
    """Strip 'Nintendo Switch 2 Edition' or similar suffix from game titles."""
    if not text:
        return ""
    cleaned = SWITCH_2_RE.sub("", text)
    cleaned = re.sub(r"\[Nintendo\s+Switch\s+1-2\]", "[Nintendo Switch]", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned



def parse_size_bytes(value: Any) -> int | None:
    """Parse integer bytes or human-readable size string into bytes integer."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        num = int(value)
        return num if num > 0 else None
    if not isinstance(value, str):
        return None
    match = SIZE_RE.search(value.strip())
    if not match:
        return None
    mult = SIZE_MULTIPLIERS.get(match.group("unit").casefold())
    if mult is None:
        return None
    try:
        return int(float(match.group("value").replace(",", ".")) * mult) or None
    except ValueError:
        return None


def info_hash_from_magnet(magnet: str | None) -> str | None:
    """Extract 40-character uppercase hexadecimal info_hash from magnet URI."""
    if not magnet or not isinstance(magnet, str):
        return None
    try:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(magnet).query)
    except Exception:
        return None
    for value in query.get("xt", []):
        if not value.lower().startswith("urn:btih:"):
            continue
        encoded = value.rsplit(":", 1)[-1].strip()
        if BTIH_HEX_RE.fullmatch(encoded):
            return encoded.upper()
        if BTIH_B32_RE.fullmatch(encoded):
            try:
                return base64.b32decode(encoded.upper()).hex().upper()
            except Exception:
                return None
    return None


def is_base_title_id(tid: str | None) -> bool:
    """Check if TitleID is a valid Nintendo Switch Base Game ID (ends in 000)."""
    if not tid or not isinstance(tid, str) or len(tid) != 16:
        return False
    try:
        val = int(tid, 16)
        return tid.upper().startswith("0100") and (val & 0xFFF) == 0
    except ValueError:
        return False


def to_base_title_id(tid: str | None) -> str | None:
    """Derive the Base Game TitleID from an update, DLC, or base TitleID."""
    if not tid or not isinstance(tid, str):
        return None
    cleaned = tid.strip().upper()
    if not (len(cleaned) == 16 and cleaned.startswith("0100")):
        return None
    try:
        val = int(cleaned, 16)
        base_val = val & ~0xFFF
        return f"{base_val:016X}"
    except ValueError:
        return None


def classify_title_id(tid: str | None) -> str:
    """Classify TitleID as 'base', 'update', or 'dlc'."""
    if not tid or len(tid) != 16:
        return "unknown"
    try:
        val = int(tid, 16)
        lower_12 = val & 0xFFF
        if lower_12 == 0:
            return "base"
        if lower_12 == 0x800:
            return "update"
        return "dlc"
    except ValueError:
        return "unknown"


def clean_html_text(raw_html: str) -> str:
    """Strip tags and unescape HTML text."""
    if not raw_html:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", raw_html, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    # Collapse multiple whitespaces per line
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(lines).strip()


def parse_viewtorrent_filelist(html_body: str) -> list[dict[str, Any]]:
    """Parse file list from RuTracker viewtorrent.php HTML.

    RuTracker renders torrent file trees in:
    - <ul class="ftree"><li><div><b>filename.ext</b><s></s><i>bytes</i></div></li></ul>
    - Tables with classes attach_downloads or attach-filelist
    """
    if not html_body:
        return []

    files: list[dict[str, Any]] = []

    # Strategy 1: RuTracker ftree structure: <b>filename</b>...<i>size_in_bytes</i>
    # Matches files in the torrent tree (files have <i>bytes</i>)
    ftree_matches = re.finditer(
        r"<b>(?P<name>[^<]+)</b>(?:<s>.*?</s>)?(?:<i>(?P<bytes>\d+)</i>)",
        html_body,
        flags=re.IGNORECASE,
    )
    for m in ftree_matches:
        name = clean_html_text(m.group("name"))
        byte_str = m.group("bytes")
        size = int(byte_str) if byte_str and byte_str.isdigit() else None
        files.append({"name": name, "size": size})

    if not files:
        # Fallback if <i> is not present or structured differently
        ftree_matches = re.finditer(
            r"<b>(?P<name>[^<]+\.(?:nsp|nsz|xci|nro|zip|7z))</b>(?:<s>.*?</s>)?(?:<i>(?P<bytes>\d+)</i>)?",
            html_body,
            flags=re.IGNORECASE,
        )
        for m in ftree_matches:
            name = clean_html_text(m.group("name"))
            byte_str = m.group("bytes")
            size = int(byte_str) if byte_str and byte_str.isdigit() else None
            if not size:
                # Fallback to parsing human size from name if present, e.g. (1.5 GB)
                size_m = re.search(r"\(([0-9.,]+\s*[KMGTP]?B)\)", name, re.IGNORECASE)
                if size_m:
                    size = parse_size_bytes(size_m.group(1))
            files.append({"name": name, "size": size})

    if files:
        return files

    # Strategy 2: Iterate over table rows and parse <td> cells
    tr_matches = re.finditer(r"<tr\b[^>]*>(.*?)</tr>", html_body, flags=re.IGNORECASE | re.DOTALL)
    for tr in tr_matches:
        row_content = tr.group(1)
        tds = re.findall(r"<td\b[^>]*>(.*?)</td>", row_content, flags=re.IGNORECASE | re.DOTALL)
        if not tds:
            continue
        cleaned_tds = [clean_html_text(td) for td in tds]
        # Check if any cell has a switch file extension
        matched_name = None
        matched_size = None
        for i, text in enumerate(cleaned_tds):
            if PACKAGE_EXT_RE.search(text):
                matched_name = text
                # Check subsequent cells for size
                for next_cell in cleaned_tds[i + 1:]:
                    parsed_s = parse_size_bytes(next_cell)
                    if parsed_s:
                        matched_size = parsed_s
                        break
                break

        if matched_name:
            files.append({
                "name": matched_name,
                "size": matched_size,
            })

    # Strategy 3: General fallback regex
    if not files:
        line_matches = re.finditer(
            r"([^\r\n<>\"']+\.(?:nsp|nsz|xci|nro|zip|7z))(?:\s+&nbsp;\s*|\s+-\s*|\s+)?(?:\(?([0-9.,]+\s*(?:[KMGTP]?B|байт|кб|мб|гб|тб))\)?)?",
            html_body,
            re.IGNORECASE,
        )
        seen_names = set()
        for match in line_matches:
            name = clean_html_text(match.group(1)).strip().strip("\"'").strip()
            if name in seen_names or not name:
                continue
            seen_names.add(name)
            size = parse_size_bytes(match.group(2)) if match.group(2) else None
            files.append({"name": name, "size": size})

    return files

    # Strategy 2: If no structured rows matched, scan for all filenames with switch extensions
    if not files:
        line_matches = re.finditer(
            r"([^\r\n<>\"']+\.(?:nsp|nsz|xci|nro|zip|7z))(?:\s+&nbsp;\s*|\s+-\s*|\s+)?(?:\(?([0-9.,]+\s*(?:[KMGTP]?B|байт|кб|мб|гб|тб))\)?)?",
            html_body,
            re.IGNORECASE,
        )
        seen_names = set()
        for match in line_matches:
            name = clean_html_text(match.group(1)).strip().strip("\"'").strip()
            if name in seen_names or not name:
                continue
            seen_names.add(name)
            size = parse_size_bytes(match.group(2)) if match.group(2) else None
            files.append({"name": name, "size": size})

    return files


def extract_from_filelist(filelist: list[dict[str, Any]], raw_title: str = "") -> dict[str, Any]:
    """Extract TitleIDs, versions, content types, and package types from files list."""
    base_tids: set[str] = set()
    update_tids: set[str] = set()
    dlc_tids: set[str] = set()
    all_tids: list[str] = []

    package_types: set[str] = set()
    versions_numeric: list[int] = []
    versions_semver: list[str] = []

    for item in filelist:
        filename = item.get("name", "")

        # Detect package type
        ext_match = PACKAGE_EXT_RE.search(filename)
        if ext_match:
            package_types.add(ext_match.group(1).lower())

        # TitleID detection: bracketed first, then unbracketed
        tids = TITLE_ID_BRACKET_RE.findall(filename)
        if not tids:
            tids = TITLE_ID_RE.findall(filename)

        for tid in tids:
            tid_upper = tid.upper()
            all_tids.append(tid_upper)
            classification = classify_title_id(tid_upper)
            if classification == "base":
                base_tids.add(tid_upper)
            elif classification == "update":
                update_tids.add(tid_upper)
            elif classification == "dlc":
                dlc_tids.add(tid_upper)

        # Version detection
        v_num = VERSION_NUMERIC_RE.search(filename)
        if v_num:
            try:
                versions_numeric.append(int(v_num.group(1)))
            except ValueError:
                pass

        v_sem = VERSION_SEMVER_RE.search(filename)
        if v_sem:
            versions_semver.append(v_sem.group(1))

    # Determine primary base title ID
    chosen_base_tid: str | None = None
    if base_tids:
        # If there's an explicit base TID, use the first/most common
        chosen_base_tid = sorted(list(base_tids))[0]
    elif update_tids:
        # Map update back to base
        chosen_base_tid = to_base_title_id(sorted(list(update_tids))[0])
    elif dlc_tids:
        # Map DLC back to base
        chosen_base_tid = to_base_title_id(sorted(list(dlc_tids))[0])

    # Package type priority: nsp > nsz > xci > nro > zip > 7z
    priority = ["nsp", "nsz", "xci", "nro", "zip", "7z"]
    primary_pkg = None
    for pkg in priority:
        if pkg in package_types:
            primary_pkg = pkg
            break

    # If title indicates an NRO port or homebrew, prioritize nro and drop forwarder TID
    title_low = raw_title.lower() if raw_title else ""
    is_port = (
        "[nro" in title_low or ".nro" in title_low or
        "хоумбрю" in title_low or "homebrew" in title_low or
        "(порт" in title_low or " порт" in title_low or "порт)" in title_low or "порты" in title_low
    )
    if is_port and "nro" in package_types:
        primary_pkg = "nro"
        chosen_base_tid = None

    # Highest version
    version_str: str | None = None
    version_name: str | None = None
    if versions_numeric:
        max_num = max(versions_numeric)
        version_str = f"v{max_num}"
    if versions_semver:
        version_name = f"v{versions_semver[-1]}"

    return {
        "title_id": chosen_base_tid,
        "base_title_ids": sorted(list(base_tids)),
        "update_title_ids": sorted(list(update_tids)),
        "dlc_title_ids": sorted(list(dlc_tids)),
        "all_title_ids": all_tids,
        "package_type": primary_pkg,
        "version": version_str,
        "version_name": version_name,
    }


def is_valid_image_url(url: str) -> bool:
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        return False
    low = url.lower()
    return not any(pat in low for pat in SKIP_IMG_PATTERNS)


def extract_topic_images(html_body: str) -> tuple[str | None, list[str]]:
    """Extract cover image and up to 6 screenshots from viewtopic.php HTML."""
    cover: str | None = None
    screenshots: list[str] = []

    # Find all <img> tags and <var class="postImg" title="URL"> (RuTracker postImg format)
    urls: list[str] = []

    # RuTracker commonly uses <var class="postImg" title="http://...">
    var_matches = re.finditer(r'<var\b[^>]*class=["\'][^"\']*postImg[^"\']*["\'][^>]*title=["\']([^"\']+)["\']', html_body, re.IGNORECASE)
    for m in var_matches:
        urls.append(m.group(1).strip())

    # Standard <img> src
    img_matches = re.finditer(r'<img\b[^>]*src=["\']([^"\']+)["\']', html_body, re.IGNORECASE)
    for m in img_matches:
        urls.append(m.group(1).strip())

    # Clean and filter URLs
    seen = set()
    valid_images: list[str] = []
    for u in urls:
        if u.startswith("//"):
            u = f"https:{u}"
        elif u.startswith("http://"):
            u = f"https://{u[7:]}"

        if u not in seen and is_valid_image_url(u):
            seen.add(u)
            valid_images.append(u)

    if valid_images:
        cover = valid_images[0]
        screenshots = valid_images[1:7]

    return cover, screenshots


def parse_languages(text: str) -> dict[str, Any]:
    """Parse interface and voice languages from topic text into BCP-47 codes."""
    interface_langs: set[str] = set()
    voice_langs: set[str] = set()

    for line in text.splitlines():
        low = line.casefold()
        is_interface = any(k in low for k in ("язык интерфейса", "интерфейс", "субтитры", "текст"))
        is_voice = any(k in low for k in ("язык озвучки", "озвучка", "аудио", "голос"))

        for name, code in LANG_MAP.items():
            if name in low:
                if is_voice:
                    voice_langs.add(code)
                elif is_interface:
                    interface_langs.add(code)
                else:
                    interface_langs.add(code)

    return {
        "interface": sorted(list(interface_langs)),
        "voice": sorted(list(voice_langs)),
    }


def parse_topic_metadata(html_body: str, topic_id: int) -> dict[str, Any]:
    """Parse rich metadata fields from viewtopic.php HTML."""
    # Extract magnet link
    magnet_match = MAGNET_RE.search(html_body)
    magnet = html_lib.unescape(magnet_match.group(0)) if magnet_match else None
    info_hash = info_hash_from_magnet(magnet)

    # Extract topic title
    title_match = re.search(r"<h1 class=\"maintitle\">.*?<a[^>]*>(.*?)</a>", html_body, re.DOTALL | re.IGNORECASE)
    if not title_match:
        title_match = re.search(r"<title>(.*?)(?:(?:&bull;|::|\-|\|).*)?</title>", html_body, re.IGNORECASE)
    raw_title = clean_switch_2_edition(clean_html_text(title_match.group(1))) if title_match else f"Topic {topic_id}"

    # Extract post body
    post_body_match = re.search(r'<div class="post_body"[^>]*>(.*?)</div>\s*<div class="post_after">', html_body, re.DOTALL | re.IGNORECASE)
    if not post_body_match:
        post_body_match = re.search(r'<div class="post_body"[^>]*>(.*?)</div>', html_body, re.DOTALL | re.IGNORECASE)
    post_html = post_body_match.group(1) if post_body_match else html_body
    clean_text = clean_html_text(post_html)

    # Extract images
    cover, screenshots = extract_topic_images(post_html)

    # Normalize HTML entities and whitespace around header fields
    norm_html = html_lib.unescape(re.sub(r"&nbsp;|\xa0", " ", html_body))

    # Extract registered timestamp if present
    reg_match = re.search(r'Зарегистрирован:\s*(?:<[^>]+>\s*)*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})', norm_html)
    registered_at = reg_match.group(1) if reg_match else None

    # Extract size from html
    size_match = re.search(
        r"Размер:\s*(?:<[^>]+>\s*)*([0-9.,]+)\s*(?:<[^>]+>\s*)*([KMGTP]?B|байт|кб|мб|гб|тб)",
        norm_html,
        re.IGNORECASE,
    )
    size_bytes = parse_size_bytes(f"{size_match.group(1)} {size_match.group(2)}") if size_match else None

    # Extract post time from viewtopic post link: <a href="viewtopic.php?p=...">DD-Mon-YY HH:MM</a>
    post_time = None
    post_match = re.search(
        r'viewtopic\.php\?p=\d+#\d+">(\d{2})-([А-Яа-я]+)-(\d{2})\s+(\d{2}):(\d{2})</a>',
        html_body,
    )
    if post_match:
        day = int(post_match.group(1))
        mon_str = post_match.group(2)[:3]
        yr = int(post_match.group(3)) + 2000
        hr = int(post_match.group(4))
        mn = int(post_match.group(5))
        ru_months = {
            'Янв': 1, 'Фев': 2, 'Мар': 3, 'Апр': 4, 'Май': 5, 'Июн': 6,
            'Июл': 7, 'Авг': 8, 'Сен': 9, 'Окт': 10, 'Ноя': 11, 'Дек': 12
        }
        mon = ru_months.get(mon_str)
        if mon:
            try:
                dt_obj = dt.datetime(yr, mon, day, hr, mn, tzinfo=dt.timezone.utc)
                post_time = int(dt_obj.timestamp())
            except Exception:
                pass

    # TitleID fallback from title or post text
    tids_in_title = TITLE_ID_BRACKET_RE.findall(raw_title) or TITLE_ID_RE.findall(raw_title)
    tids_in_body = TITLE_ID_BRACKET_RE.findall(clean_text) or TITLE_ID_RE.findall(clean_text)
    fallback_tid = None
    for tid in tids_in_title + tids_in_body:
        base_tid = to_base_title_id(tid)
        if base_tid:
            fallback_tid = base_tid
            break

    # Parse metadata attributes from text lines
    year = None
    genre = None
    developer = None
    publisher = None

    for line in clean_text.splitlines():
        line_clean = line.strip()
        if not line_clean:
            continue
        if re.match(r"^(?:год\s*(?:выпуска|релиза)?|year)[:\s]+", line_clean, re.IGNORECASE):
            year = re.sub(r"^(?:год\s*(?:выпуска|релиза)?|year)[:\s]+", "", line_clean, flags=re.IGNORECASE).strip()[:32]
        elif re.match(r"^(?:жанр|genre)[:\s]+", line_clean, re.IGNORECASE):
            genre = re.sub(r"^(?:жанр|genre)[:\s]+", "", line_clean, flags=re.IGNORECASE).strip()[:256]
        elif re.match(r"^(?:разработчик|developer)[:\s]+", line_clean, re.IGNORECASE):
            developer = re.sub(r"^(?:разработчик|developer)[:\s]+", "", line_clean, flags=re.IGNORECASE).strip()[:256]
        elif re.match(r"^(?:издатель(?:ство)?|publisher)[:\s]+", line_clean, re.IGNORECASE):
            publisher = re.sub(r"^(?:издатель(?:ство)?|publisher)[:\s]+", "", line_clean, flags=re.IGNORECASE).strip()[:256]

    languages = parse_languages(clean_text)

    # Clean description: up to 4096 chars
    description = clean_text[:4096] if clean_text else None

    return {
        "topic_id": topic_id,
        "title": raw_title[:1024],
        "magnet": magnet,
        "info_hash": info_hash,
        "cover": cover,
        "screenshots": screenshots,
        "registered_at": registered_at,
        "posted_at": post_time,
        "size_bytes": size_bytes,
        "title_id": fallback_tid,
        "year": year,
        "genre": genre,
        "developer": developer,
        "publisher": publisher,
        "languages": languages,
        "description": description,
    }


def normalize_game_name(title: str) -> str:
    """Normalize game title for fuzzy TitleDB matching.

    Removes region tags, file formats, version tags, and punctuation.
    """
    if not title:
        return ""
    # Remove bracketed and parenthesized tags
    text = re.sub(r"\[[^\]]*\]", " ", title)
    text = re.sub(r"\([^\)]*\)", " ", text)
    # Remove common release words
    text = re.sub(
        r"\b(?:nintendo\s+switch|switch|nsp|nsz|xci|update|upd|dlc|rus|eng|multi\d*)\b",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    # Unicode normalize
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    # Punctuation to space
    text = re.sub(r"[^\w\s]", " ", text)
    # Collapse whitespace
    return " ".join(text.lower().split())


class TitleDBMatcher:
    """Matches release names to TitleIDs via pipensx-metadata TitleDB."""

    def __init__(self, cache_path: str | Path = ".cache/titledb.json", url: str | None = None):
        self.cache_path = Path(cache_path)
        self.url = url or os.environ.get("TITLEDB_URL") or DEFAULT_TITLEDB_URL
        self._name_to_id: dict[str, str] = {}
        self._loaded = False

    def ensure_loaded(self) -> None:
        if self._loaded:
            return
        if not self.cache_path.exists():
            self._download_titledb()
        else:
            try:
                self._load_file(self.cache_path)
            except Exception as exc:
                logger.warning("Failed to load cached TitleDB (%s), re-downloading: %s", self.cache_path, exc)
                self._download_titledb()
        self._loaded = True

    def _load_file(self, path: Path) -> None:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        mapping: dict[str, str] = {}
        # Supports dictionary { "0100000000010000": {"name": "..."}, ... }
        if isinstance(data, dict):
            for tid, item in data.items():
                base_tid = to_base_title_id(tid)
                if not base_tid:
                    continue
                name = item.get("name") if isinstance(item, dict) else str(item)
                if name:
                    clean_name = clean_switch_2_edition(name)
                    norm = normalize_game_name(clean_name)
                    if norm and norm not in mapping:
                        mapping[norm] = base_tid

        # Supports list [ {"id": "0100000000010000", "name": "..."}, ... ]
        elif isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                tid = item.get("id") or item.get("title_id") or item.get("titleId")
                base_tid = to_base_title_id(tid)
                name = item.get("name")
                if base_tid and name:
                    clean_name = clean_switch_2_edition(name)
                    norm = normalize_game_name(clean_name)
                    if norm and norm not in mapping:
                        mapping[norm] = base_tid

        self._name_to_id = mapping
        logger.info("Loaded %d normalized titles into TitleDB matcher", len(self._name_to_id))

    def _download_titledb(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        logger.info("Downloading TitleDB from %s to %s", self.url, self.cache_path)
        req = urllib.request.Request(self.url, headers={"User-Agent": "pipensx-catalog/2.0"})
        try:
            with urllib.request.urlopen(req, timeout=30.0) as resp:
                content = resp.read()
            self.cache_path.write_bytes(content)
            self._load_file(self.cache_path)
        except Exception as exc:
            logger.error("Failed to download TitleDB from %s: %s", self.url, exc)
            # Create empty file so we don't crash without internet
            self._name_to_id = {}

    def match(self, title: str) -> str | None:
        """Find base TitleID by release title."""
        self.ensure_loaded()
        if not title:
            return None

        norm = normalize_game_name(title)
        if not norm:
            return None

        # Exact match
        if norm in self._name_to_id:
            return self._name_to_id[norm]

        # Substring / word prefix match
        words = norm.split()
        if len(words) >= 2:
            prefix_2 = " ".join(words[:2])
            for db_name, tid in self._name_to_id.items():
                if db_name == prefix_2 or db_name.startswith(f"{norm} ") or norm.startswith(f"{db_name} "):
                    return tid

        return None
