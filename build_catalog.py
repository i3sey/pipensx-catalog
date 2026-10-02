#!/usr/bin/env python3
"""Build pipensx-catalog v2 from a seed snapshot.

P0: seed is the last `switch_games.json` (Langegen fork of the data, not the
code). P1 will replace the seed fetch with a rutracker scraper behind
`SCRAPER_PROXY` (see README); this script's normalize/validate/publish
contract stays the same.

Pipeline (plan SS3):
  raw {title, magnet, topic_id, url, ...} -> normalize -> overrides ->
  dedup by infoHash -> catalog.json + manifest.json + report.md

Limits mirror the client parser (`catalog_service.cpp`):
  kMaxCatalogBytes = 48 MiB, kMaxCatalogEntries = 20000.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import re
import urllib.parse
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2
MAX_CATALOG_BYTES = 48 * 1024 * 1024
MAX_CATALOG_ENTRIES = 20000
MAX_SCREENSHOTS = 6

TITLE_ID_RE = re.compile(r"^[0-9A-F]{16}$")
BTIH_HEX_RE = re.compile(r"^[0-9A-Fa-f]{40}$")
BTIH_B32_RE = re.compile(r"^[A-Z2-7a-z2-7]{32}$")

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

PACKAGE_TYPES = ("nsp", "nsz", "xci", "nro", "zip", "7z")
PACKAGE_RE = re.compile(r"\[(nsp|nsz|xci|nro|zip|7z)\]", re.IGNORECASE)

# Free-form Russian -> BCP-47 for the `languages` struct (§2.2).
LANG_MAP = {
    "английский": "en", "english": "en", "eng": "en",
    "русский": "ru", "russian": "ru", "rus": "ru",
    "японский": "ja", "japanese": "ja",
    "французский": "fr", "french": "fr",
    "немецкий": "de", "german": "de",
    "испанский": "es", "spanish": "es",
    "итальянский": "it", "italian": "it",
    "португальский": "pt", "portuguese": "pt",
    "китайский": "zh", "chinese": "zh",
    "корейский": "ko", "korean": "ko",
    "польский": "pl", "polish": "pl",
    "украинский": "uk", "ukrainian": "uk",
    "нидерландский": "nl", "голландский": "nl", "dutch": "nl",
}

HEALTH_OK = {"ok", "no_peers", "metadata_timeout",
             "tracker_not_registered", "replaced", "dead"}


def info_hash_from_magnet(magnet: Any) -> str | None:
    if not isinstance(magnet, str):
        return None
    try:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(magnet).query)
    except ValueError:
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
            except ValueError:
                return None
    return None


def parse_size_bytes(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = int(value)
        return number if number > 0 else None
    if not isinstance(value, str):
        return None
    match = SIZE_RE.search(value.strip())
    if not match:
        return None
    mult = SIZE_MULTIPLIERS.get(match.group("unit").casefold())
    if mult is None:
        return None
    return int(float(match.group("value").replace(",", ".")) * mult) or None


def is_base_title_id(value: Any) -> bool:
    return (isinstance(value, str) and bool(TITLE_ID_RE.fullmatch(value.upper()))
            and (int(value.upper(), 16) & 0xFFF) == 0)


def normalize_title_id(value: Any) -> str | None:
    """UPPER base title id or None. Non-base / garbage -> None (unmatched, not fatal)."""
    if not isinstance(value, str):
        return None
    text = value.strip().upper()
    if not TITLE_ID_RE.fullmatch(text):
        return None
    if (int(text, 16) & 0xFFF) != 0:
        return None
    return text


def parse_package_type(title: Any, image_format: Any) -> str | None:
    for source in (title, image_format):
        if not isinstance(source, str):
            continue
        match = PACKAGE_RE.search(source)
        if match:
            return match.group(1).lower()
    if isinstance(image_format, str):
        lowered = image_format.strip().lower()
        if lowered in PACKAGE_TYPES:
            return lowered
    return None


def clean_description(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if text.startswith(": "):
        text = text[2:].lstrip()
    return text[:4096]


def canonical_screenshot_key(url: str) -> str:
    """Map thumb -> original so thumb/original duplicates collapse.

    fastpic thumbs live under `/thumb/YYYY/...`; imageban `thumbs/` vs
    direct `out/` variants share the trailing hash-ish basename.
    """
    key = url.strip()
    key = re.sub(r"/thumb(?=/)", "", key)
    key = key.replace("/thumbs/", "/out/")
    # `i128.fastpic.org/644/...` vs `i128.fastpic.org/thumb/...` already handled;
    # fall back to basename for cross-host same-file pairs.
    return key


def clean_screenshots(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        url = item.strip()
        # http allowed for P0 seed compat (same reason as covers).
        if (not url.startswith(("https://", "http://"))
                or len(url) > 2048 or url in seen):
            continue
        key = canonical_screenshot_key(url)
        if key in seen:
            continue
        seen.add(url)
        seen.add(key)
        cleaned.append(url)
        if len(cleaned) >= MAX_SCREENSHOTS:
            break
    return cleaned


def parse_languages(value: Any) -> tuple[list[str], str]:
    if not isinstance(value, str) or not value.strip():
        return [], ""
    lowered = value.casefold()
    if any(word in lowered for word in ("не озвуч", "отсутств", "нет ", "n/a", "—")) \
            and "английский" not in lowered and "русский" not in lowered:
        return [], value.strip()[:256]
    found: list[str] = []
    for name, code in LANG_MAP.items():
        if name in lowered and code not in found:
            found.append(code)
    return found, "" if found else value.strip()[:256]


def parse_players(value: Any) -> dict[str, Any]:
    """Free-form `multiplayer` -> {min, max, online}. Conservative defaults 1/1/false."""
    result: dict[str, Any] = {"min": 1, "max": 1, "online": False}
    if not isinstance(value, str):
        return result
    text = value.casefold()
    if any(word in text for word in ("online", "онлайн", "по сети", "интернет")):
        result["online"] = True
    numbers = [int(n) for n in re.findall(r"\d+", text)]
    if "нет" in text or "no" in text or "single" in text or "один" in text:
        return result
    if numbers:
        biggest = max(numbers)
        if biggest >= 1:
            result["max"] = min(biggest, 64)
    if any(word in text for word in ("кооп", "coop", "совмест")) and result["max"] < 2:
        result["max"] = 2
    return result


def normalize_entry(raw: dict[str, Any], generated_at: int) -> dict[str, Any] | None:
    title = raw.get("title")
    magnet = raw.get("magnet", raw.get("magnetURI"))
    if (not isinstance(title, str) or not 1 <= len(title) <= 1024
            or not isinstance(magnet, str) or len(magnet) > 2048):
        return None
    info_hash = info_hash_from_magnet(magnet)
    if not info_hash:
        return None
    size_bytes = parse_size_bytes(raw.get("size_bytes", raw.get("size")))
    if not size_bytes or size_bytes <= 0:
        return None

    title_id = normalize_title_id(raw.get("title_id"))

    def read_str(key: str, limit: int) -> str:
        value = raw.get(key)
        if not isinstance(value, str):
            return ""
        return value.strip()[:limit]

    cover = ""
    for key in ("cover", "poster"):
        value = raw.get(key)
        # P0 seed compat: 2853/7045 seed covers are plain http:// (vfl.ru).
        # The client accepts any string here, so keep them as-is instead of
        # dropping 40% of the catalog; new scraper entries should be https.
        # `cover_is_https` is reported for the P1 https-upgrade pass.
        if isinstance(value, str) and value.startswith(("https://", "http://")) \
                and len(value) <= 2048:
            cover = value.strip()
            break
    if not cover:
        return None

    topic_id: int | None = None
    topic_raw = raw.get("topic_id")
    if isinstance(topic_raw, bool):
        pass
    elif isinstance(topic_raw, int) and topic_raw > 0:
        topic_id = topic_raw
    elif isinstance(topic_raw, str) and topic_raw.strip().isdigit():
        topic_id = int(topic_raw.strip())

    published = raw.get("published_date", raw.get("publishedAt", 0))
    if isinstance(published, str) and published.strip().lstrip("-").isdigit():
        published = int(published.strip())
    if not isinstance(published, int) or published < 0:
        published = 0

    interface, voice_note = parse_languages(raw.get("interface_lang"))
    voice, _ = parse_languages(raw.get("voice_lang"))
    players = parse_players(raw.get("multiplayer"))

    health = raw.get("health", "ok")
    if health not in HEALTH_OK:
        health = "ok"
    failure = raw.get("failure_reason", "")
    if not isinstance(failure, str):
        failure = ""

    entry: dict[str, Any] = {
        "title": title.strip(),
        "magnet": magnet.strip(),
        "cover": cover,
        # Transitional duplicate (§2.1): numeric `size` for the current
        # client + canonical `size_bytes`. No string sizes emitted.
        "size": size_bytes,
        "size_bytes": size_bytes,
        "title_id": title_id or "",
        "topic_id": topic_id if topic_id is not None else 0,
        "url": raw.get("url") if isinstance(raw.get("url"), str)
        and str(raw.get("url")).startswith("https://") else "",
        "year": read_str("year", 32),
        "genre": read_str("genre", 256),
        "developer": read_str("developer", 256),
        "publisher": read_str("publisher", 256),
        "description": clean_description(raw.get("description")),
        "screenshots": clean_screenshots(raw.get("screenshots")),
        "catalog_generated_at": generated_at,
        "published_date": published,
        "health": health,
    }
    package_type = parse_package_type(raw.get("title"), raw.get("image_format"))
    if package_type:
        entry["package_type"] = package_type
    version = read_str("version", 32) or read_str("version_name", 32)
    if version:
        entry["version"] = version
    entry["languages"] = {
        "interface": interface,
        "voice": voice,
        "note": voice_note,
    }
    entry["players"] = players
    performance = read_str("performance", 256)
    if performance:
        entry["performance_note"] = performance
    if topic_id is None:
        del entry["topic_id"]
    if not entry["url"]:
        del entry["url"]
    if not entry["title_id"]:
        del entry["title_id"]
    if failure:
        entry["failure_reason"] = failure[:512]
    return entry


def load_overrides(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {"hide": [], "title_id": {}, "rename": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("overrides.json must be an object")
    hide = data.get("hide", [])
    title_map = data.get("title_id", {})
    rename = data.get("rename", {})
    if not isinstance(hide, list) or not isinstance(title_map, dict) \
            or not isinstance(rename, dict):
        raise ValueError("overrides.json: hide(list)/title_id(obj)/rename(obj)")
    return {"hide": [str(v) for v in hide],
            "title_id": {str(k): str(v) for k, v in title_map.items()},
            "rename": {str(k): str(v) for k, v in rename.items()}}


def build_catalog(seed: list[dict[str, Any]],
                  overrides: dict[str, Any],
                  generated_at: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    hidden = set(overrides.get("hide", []))
    title_map = overrides.get("title_id", {})
    rename = overrides.get("rename", {})
    entries: list[dict[str, Any]] = []
    seen_hashes: set[str] = set()
    stats = {
        "seedEntries": len(seed),
        "droppedNoMagnet": 0,
        "droppedNoSize": 0,
        "droppedNoCover": 0,
        "droppedHidden": 0,
        "deduped": 0,
        "overriddenTitleId": 0,
        "renamed": 0,
        "missingTitleId": 0,
    }
    for raw in seed:
        if not isinstance(raw, dict):
            continue
        topic = str(raw.get("topic_id", ""))
        if topic in hidden:
            stats["droppedHidden"] += 1
            continue
        record = dict(raw)
        if topic in title_map:
            record["title_id"] = title_map[topic]
            stats["overriddenTitleId"] += 1
        if topic in rename:
            record["title"] = rename[topic]
            stats["renamed"] += 1
        entry = normalize_entry(record, generated_at)
        if entry is None:
            # Classify coarsely for the report.
            if not info_hash_from_magnet(
                    record.get("magnet", record.get("magnetURI"))):
                stats["droppedNoMagnet"] += 1
            elif not parse_size_bytes(
                    record.get("size_bytes", record.get("size"))):
                stats["droppedNoSize"] += 1
            else:
                stats["droppedNoCover"] += 1
            continue
        info_hash = info_hash_from_magnet(entry["magnet"])
        assert info_hash is not None
        if info_hash in seen_hashes:
            stats["deduped"] += 1
            continue
        seen_hashes.add(info_hash)
        if not entry.get("title_id"):
            stats["missingTitleId"] += 1
        cover = entry.get("cover", "")
        if isinstance(cover, str) and cover.startswith("http://"):
            stats["httpCovers"] = stats.get("httpCovers", 0) + 1
        entries.append(entry)
    # Client re-sorts by publishedAt desc; emit sorted for readability.
    entries.sort(key=lambda e: (-int(e.get("published_date", 0)),
                               str(e.get("title", ""))))
    stats["emitted"] = len(entries)
    stats["titleIdCoverage"] = (
        (len(entries) - stats["missingTitleId"]) / len(entries)
        if entries else 0.0)
    return entries, stats


def encode_catalog(entries: list[dict[str, Any]]) -> bytes:
    return (json.dumps(entries, ensure_ascii=False, separators=(",", ":"))
            + "\n").encode("utf-8")


def validate_entries(entries: list[dict[str, Any]], payload: bytes) -> None:
    if not 0 < len(entries) <= MAX_CATALOG_ENTRIES:
        raise ValueError(
            f"catalog must contain 1..{MAX_CATALOG_ENTRIES} entries")
    if len(payload) > MAX_CATALOG_BYTES:
        raise ValueError(
            f"catalog payload {len(payload)} exceeds {MAX_CATALOG_BYTES}")
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        info_hash = info_hash_from_magnet(entry.get("magnet"))
        if info_hash is None:
            raise ValueError(f"entry {index} has an invalid magnet")
        if info_hash in seen:
            raise ValueError(f"entry {index} duplicates infoHash {info_hash}")
        seen.add(info_hash)
        title = entry.get("title")
        if not isinstance(title, str) or not 1 <= len(title) <= 1024:
            raise ValueError(f"entry {index} has an invalid title")
        size = entry.get("size_bytes")
        if not isinstance(size, int) or size <= 0:
            raise ValueError(f"entry {index} has an invalid size_bytes")
        title_id = entry.get("title_id", "")
        if title_id and not is_base_title_id(title_id):
            raise ValueError(f"entry {index} has an invalid title_id")
        for key in ("cover",):
            url = entry.get(key)
            # http allowed for P0 seed compat (vfl.ru covers); P1 scraper
            # emits https only.
            if (not isinstance(url, str)
                    or not url.startswith(("https://", "http://"))
                    or len(url) > 2048):
                raise ValueError(f"entry {index} has an invalid {key}")
        shots = entry.get("screenshots", [])
        if not isinstance(shots, list) or len(shots) > MAX_SCREENSHOTS:
            raise ValueError(f"entry {index} has invalid screenshots")
        if entry.get("package_type") not in (None, *PACKAGE_TYPES):
            raise ValueError(f"entry {index} has an invalid package_type")


def write_outputs(output: Path, entries: list[dict[str, Any]],
                  stats: dict[str, Any], *, source_commit: str,
                  catalog_commit: str,
                  catalog_url: str) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    payload = encode_catalog(entries)
    validate_entries(entries, payload)
    (output / "catalog.json").write_bytes(payload)
    sha = hashlib.sha256(payload).hexdigest()
    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": dt.datetime.now(dt.timezone.utc).replace(
            microsecond=0).isoformat().replace("+00:00", "Z"),
        "catalogCommit": catalog_commit,
        "sourceCommit": source_commit,
        "catalog": {
            "url": catalog_url,
            "sha256": sha,
            "bytes": len(payload),
            "entries": len(entries),
        },
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    (output / "report.md").write_text(
        render_report(manifest, stats), encoding="utf-8")
    return manifest


def render_report(manifest: dict[str, Any], stats: dict[str, Any]) -> str:
    catalog = manifest["catalog"]
    lines = [
        "# pipensx-catalog build report",
        "",
        f"- generatedAt: `{manifest['generatedAt']}`",
        f"- sourceCommit: `{manifest['sourceCommit']}`",
        f"- entries: **{catalog['entries']}** "
        f"({stats.get('seedEntries', '?')} seed)",
        f"- bytes: {catalog['bytes']}",
        f"- sha256: `{catalog['sha256']}`",
        f"- title_id coverage: {stats.get('titleIdCoverage', 0.0):.1%} "
        f"(missing {stats.get('missingTitleId', 0)})",
        "",
        "## Pipeline",
        "",
        f"- dropped (no/invalid magnet): {stats.get('droppedNoMagnet', 0)}",
        f"- dropped (no size): {stats.get('droppedNoSize', 0)}",
        f"- dropped (no cover): {stats.get('droppedNoCover', 0)}",
        f"- hidden by overrides: {stats.get('droppedHidden', 0)}",
        f"- deduped by infoHash: {stats.get('deduped', 0)}",
        f"- http:// covers kept (P0 seed compat): "
        f"{stats.get('httpCovers', 0)}",
        f"- title_id overrides applied: "
        f"{stats.get('overriddenTitleId', 0)}",
        f"- renames applied: {stats.get('renamed', 0)}",
        "",
        "_Unmatched title_id rows are expected (homebrew/ports without an "
        "eShop record); they stay in the catalog, only the coverage counts "
        "them._",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build pipensx-catalog v2 from a seed JSON snapshot.")
    parser.add_argument("--seed", required=True,
                        help="Input seed JSON (array of raw entries).")
    parser.add_argument("--output", required=True,
                        help="Output directory for catalog.json/manifest.json/report.md.")
    parser.add_argument("--overrides", default="overrides.json",
                        help="Manual overrides file (hide/title_id/rename).")
    parser.add_argument("--source-commit", default="local",
                        help="Seed/scrape revision recorded in the manifest.")
    parser.add_argument("--catalog-commit", default="local",
                        help="Catalog repo revision recorded in the manifest.")
    parser.add_argument("--catalog-url", default=(
        "https://github.com/i3sey/pipensx-catalog/"
        "releases/latest/download/catalog.json"),
        help="Release URL recorded in the manifest.")
    parser.add_argument("--generated-at", type=int, default=0,
                        help="Unix time for catalog_generated_at (0 = now).")
    args = parser.parse_args()

    seed_path = Path(args.seed)
    seed = json.loads(seed_path.read_text(encoding="utf-8"))
    if not isinstance(seed, list):
        raise SystemExit("seed JSON must be an array")
    overrides_path = Path(args.overrides)
    overrides = load_overrides(
        overrides_path if overrides_path.exists() else None)
    # P1 hook: scraper transport. Unset = direct; CI sets
    # SCRAPER_PROXY=socks5h://127.0.0.1:1080 for WARP (plan §3.1).
    if os.environ.get("SCRAPER_PROXY"):
        print(f"[catalog] SCRAPER_PROXY={os.environ['SCRAPER_PROXY']} "
              "(seed mode: unused)")
    generated_at = args.generated_at or int(dt.datetime.now(
        dt.timezone.utc).timestamp())
    entries, stats = build_catalog(seed, overrides, generated_at)
    manifest = write_outputs(
        Path(args.output), entries, stats,
        source_commit=args.source_commit, catalog_commit=args.catalog_commit,
        catalog_url=args.catalog_url)
    catalog = manifest["catalog"]
    print(f"[catalog] entries={catalog['entries']} bytes={catalog['bytes']} "
          f"sha256={catalog['sha256'][:12]}...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
