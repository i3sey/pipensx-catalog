#!/usr/bin/env python3
"""Atom-discovered RuTracker scraper for pipensx-catalog.

The Switch Atom feed discovers recent/newly-updated topics and supplies
magnet enclosures. Topic pages enrich those records with covers, descriptions,
Title IDs and release metadata. Existing rich fields survive partial parses;
merges are keyed by topic ID before final infohash deduplication, so a
re-upload replaces the old hash instead of creating a second row.

Transport is env-configured:
  SCRAPER_PROXY     optional HTTP/SOCKS proxy for RuTracker requests
  RUTRACKER_COOKIE  optional authenticated session; never required for Atom

New entries without a readable page are held out of the published catalog
because the current schema requires a cover. Known entries can still receive
Atom title/magnet/size updates if a page fetch is blocked.

Exit codes: 0 = built (or clean fallback), 2 = coverage gate failed
(do NOT publish a shrunk catalog).
"""

from __future__ import annotations

import argparse
import html as html_module
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import build_catalog
from rutracker_transport import TransportError, fetch as _transport_fetch

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
TOPIC_URL = "https://rutracker.org/forum/viewtopic.php?t={topic_id}"
TORRENT_URL = "https://rutracker.org/forum/viewtorrent.php"
SECTION_URL = "https://rutracker.org/forum/viewforum.php?f={section_id}"
ATOM_FEED_URL = "https://feed.rutracker.cc/atom/f/1605.atom"
ATOM_NS = "{http://www.w3.org/2005/Atom}"

MAGNET_RE = re.compile(r"magnet:\?xt=urn:btih:[A-Za-z0-9]+[^\"'\s<>]*")
TOPIC_LINK_RE = re.compile(r"viewtopic\.php\?t=(\d+)")
IMAGE_TAG_RE = re.compile(r"<(?:img|var)\b[^>]*>", re.IGNORECASE)
ATTR_RE = re.compile(r"([\w:-]+)\s*=\s*([\"'])(.*?)\2", re.DOTALL)
# Skip rutracker chrome/smilies: static assets, not release artwork.
SKIP_IMG_RE = re.compile(
    r"rutracker\.org/(static|templates)|/smiles/|/icon|/images/|"
    r"bbcode|/spoiler|button|logo", re.IGNORECASE)
CHALLENGE_MARKERS = ("Just a moment", "challenges.cloudflare.com",
                     "cf-mitigated")


class ScrapeError(OSError):
    def __init__(self, message: str, *, challenge: bool = False):
        super().__init__(message)
        self.challenge = challenge


def fetch(url: str, timeout: float = 30.0,
          data: bytes | None = None) -> str:
    """Fetch a forum page with TLS impersonation and browser fallback."""
    try:
        return _transport_fetch(url, timeout=timeout, data=data)
    except TransportError as error:
        raise ScrapeError(str(error), challenge=error.challenge) from error


def _feed_title(raw_title: str) -> tuple[str, int | None]:
    """Strip feed-only labels and the trailing human-readable size."""
    title = raw_title.strip()
    while True:
        match = re.match(r"^\[([^\]]+)\]\s*", title)
        if not match:
            break
        label = match.group(1).casefold()
        if label in {"обновлено", "обновление", "nintendo switch"}:
            title = title[match.end():].strip()
        else:
            break

    size_bytes = None
    suffix = re.search(r"\s*\[([^\]]+)\]\s*$", title)
    if suffix:
        parsed_size = build_catalog.parse_size_bytes(suffix.group(1))
        if parsed_size:
            size_bytes = parsed_size
            title = title[:suffix.start()].strip()
    return title[:1024], size_bytes


def parse_atom_feed(payload: str | bytes) -> list[dict[str, Any]]:
    """Parse RuTracker's Switch Atom feed, including its magnet enclosure."""
    root = ET.fromstring(payload)
    entries: list[dict[str, Any]] = []
    for node in root.findall(f"{ATOM_NS}entry"):
        id_node = node.find(f"{ATOM_NS}id")
        title_node = node.find(f"{ATOM_NS}title")
        if id_node is None or not id_node.text or title_node is None \
                or not title_node.text:
            continue
        id_text = id_node.text.strip()
        topic_match = re.search(r"(?:/t/|[?&]t=)(\d+)(?:$|[/?#])", id_text)
        if topic_match:
            topic_id = topic_match.group(1)
        else:
            topic_match = re.search(r"(\d+)$", id_text)
            if not topic_match:
                continue
            topic_id = topic_match.group(1)

        topic_url = TOPIC_URL.format(topic_id=topic_id)
        magnet = ""
        for link in node.findall(f"{ATOM_NS}link"):
            href = (link.get("href") or "").strip()
            rel = link.get("rel", "alternate").casefold()
            if rel == "enclosure" and href.startswith("magnet:"):
                magnet = html_module.unescape(href)
            elif rel == "alternate" and href.startswith("https://"):
                parsed_url = urllib.parse.urlsplit(href)
                query = urllib.parse.parse_qs(parsed_url.query)
                if (parsed_url.hostname in {"rutracker.org", "www.rutracker.org"}
                        and parsed_url.path.endswith("/viewtopic.php")
                        and query.get("t") == [topic_id]):
                    topic_url = href
        if magnet and not build_catalog.info_hash_from_magnet(magnet):
            magnet = ""

        raw_title = title_node.text.strip()
        title, size_bytes = _feed_title(raw_title)
        if not title:
            continue
        updated_node = node.find(f"{ATOM_NS}updated")
        author_node = node.find(f"{ATOM_NS}author/{ATOM_NS}name")
        category_node = node.find(f"{ATOM_NS}category")
        entries.append({
            "topic_id": topic_id,
            "title": title,
            "raw_title": raw_title,
            "size_bytes": size_bytes,
            "magnet": magnet,
            "url": topic_url,
            "updated": updated_node.text.strip()
            if updated_node is not None and updated_node.text else "",
            "author": author_node.text.strip()
            if author_node is not None and author_node.text else "",
            "category": category_node.get("term", "")
            if category_node is not None else "",
        })
    return entries


def fetch_atom_feed(timeout: float = 30.0) -> list[dict[str, Any]]:
    """Fetch the public feed without forwarding RuTracker session cookies."""
    handlers: list[Any] = []
    proxy = os.environ.get("SCRAPER_PROXY", "").strip()
    if proxy:
        handlers.append(urllib.request.ProxyHandler(
            {"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(ATOM_FEED_URL, headers={
        "User-Agent": DEFAULT_UA,
        "Accept": "application/atom+xml, application/xml;q=0.9, */*;q=0.8",
    })
    with opener.open(request, timeout=timeout) as response:
        payload = response.read(2 * 1024 * 1024 + 1)
    if len(payload) > 2 * 1024 * 1024:
        raise ScrapeError("Atom feed exceeded 2 MiB limit")
    entries = parse_atom_feed(payload)
    if not entries:
        raise ScrapeError("Atom feed contained no valid entries")
    return entries


def _clean_text(value: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    text = re.sub(
        r"</(?:p|div|li|tr|table|h[1-6])\s*>", "\n", text,
        flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_module.unescape(text)
    lines = [re.sub(r"[ \t\xa0]+", " ", line).strip()
             for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def extract_title_id(text: str) -> str | None:
    """Extract and normalize a Switch application ID to its base game ID."""
    matches = [m.upper() for m in re.findall(
        r"0100[0-9A-Fa-f]{12}", text or "", flags=re.IGNORECASE)]
    for match in matches:
        if match.endswith("000"):
            return match
    return matches[0][:-3] + "000" if matches else None


def _extract_images(scope: str) -> list[str]:
    images: list[str] = []
    for match in IMAGE_TAG_RE.finditer(scope):
        tag = match.group(0)
        attrs = {name.casefold(): html_module.unescape(value.strip())
                 for name, _, value in ATTR_RE.findall(tag)}
        classes = attrs.get("class", "").casefold().split()
        if tag[:4].casefold() == "<var" and "postimg" not in classes:
            continue
        for key in ("src", "data-src", "init-src", "title"):
            src = attrs.get(key, "").strip()
            if not src.startswith(("https://", "http://")):
                continue
            if SKIP_IMG_RE.search(src) or src in images:
                continue
            images.append(src)
            break
    return images


def parse_topic_page(html: str, topic_id: str, url: str,
                     feed_entry: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Extract release metadata from a RuTracker topic page.

    The post page is the enrichment source; Atom is used for discovery and
    supplies a fallback magnet for already-known topics when this fetch is
    blocked. New rows still need a page-derived cover to pass catalog schema.
    """
    bodies = re.findall(
        r'<div class="post_body[^"]*"[^>]*>(.*?)</div>\s*(?:<div|$)',
        html, flags=re.IGNORECASE | re.DOTALL)
    scope = max(bodies, key=len) if bodies else html

    magnet = None
    for candidate in MAGNET_RE.findall(scope):
        candidate = html_module.unescape(candidate)
        if build_catalog.info_hash_from_magnet(candidate):
            magnet = candidate
            break
    if not magnet and feed_entry:
        candidate = feed_entry.get("magnet")
        if isinstance(candidate, str) and build_catalog.info_hash_from_magnet(candidate):
            magnet = candidate
    if not magnet:
        return None

    title = None
    for pattern in (r'<a[^>]+id="topic-title"[^>]*>(.*?)</a>',
                    r'<h1[^>]*class="maintitle"[^>]*>(.*?)</h1>',
                    r"<title>(.*?)</title>"):
        match = re.search(pattern, html, flags=re.IGNORECASE | re.DOTALL)
        if match:
            title = _clean_text(match.group(1))
            title = re.sub(r"\s*[•|–-]\s*rutracker.*$", "", title,
                           flags=re.IGNORECASE).strip()
            if title:
                break
    if not title and feed_entry:
        candidate_title = feed_entry.get("title")
        if isinstance(candidate_title, str) and candidate_title.strip():
            title = candidate_title.strip()
    if not title:
        return None

    images = _extract_images(scope)
    text = _clean_text(scope)
    size: Any = None
    exact_size = re.search(
        r'<[^>]+id=["\']tor-size-humn["\'][^>]*>(.*?)</[^>]+>',
        html, flags=re.IGNORECASE | re.DOTALL)
    if exact_size:
        size = build_catalog.parse_size_bytes(_clean_text(exact_size.group(1)))
    if not size:
        for line in text.splitlines():
            candidate = build_catalog.parse_size_bytes(line)
            if candidate:
                size = candidate
                break
    if not size:
        size = build_catalog.parse_size_bytes(text[:2000])
    if not size and feed_entry:
        size = build_catalog.parse_size_bytes(feed_entry.get("size_bytes"))

    posted_at = 0
    for pattern in (r"data-posted=['\"](\d{10})['\"]",
                    r"Добавлено:\s*(\d{10})",
                    r"posted['\"]?\s*[:=]\s*['\"]?(\d{10})"):
        match = re.search(pattern, html, flags=re.IGNORECASE)
        if match:
            posted_at = int(match.group(1))
            break

    record: dict[str, Any] = {
        "title": title,
        "magnet": magnet,
        "topic_id": str(topic_id),
        "url": url,
        "published_date": posted_at,
    }
    if size:
        record["size"] = size
    if images:
        record["cover"] = images[0]
        if len(images) > 1:
            record["screenshots"] = images[1:10]

    labels = {
        "year": r"(?:Год выпуска|Дата выхода|Год выхода)\s*:\s*([^\n]+)",
        "genre": r"Жанр\s*:\s*([^\n]+)",
        "developer": r"Разработчик\s*:\s*([^\n]+)",
        "publisher": r"Издатель\s*:\s*([^\n]+)",
        "image_format": r"Формат образа\s*:\s*([^\n]+)",
        "interface_lang": r"Язык интерфейса\s*:\s*([^\n]+)",
        "voice_lang": r"(?:Язык озвучки|Озвучка)\s*:\s*([^\n]+)",
        "performance": r"Работоспособность проверена\s*:\s*([^\n]+)",
        "multiplayer": r"Мультиплеер\s*:\s*([^\n]+)",
    }
    for key, pattern in labels.items():
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            record[key] = match.group(1).strip()[:512]

    title_id = extract_title_id(text) or extract_title_id(html)
    if title_id:
        record["title_id"] = title_id
    description_match = re.search(
        r"(?:Описание(?: раздачи)?|Description)\s*:\s*(.+?)"
        r"(?=\n\s*(?:Доп\.?\s*информация|Скриншоты|Трейлер|"
        r"Список файлов|FAQ|Системные требования)\s*:|$)",
        text, flags=re.IGNORECASE | re.DOTALL)
    description = (description_match.group(1).strip()
                   if description_match else text.strip())
    if description:
        record["description"] = description[:4096]
    return record


def fetch_title_id(topic_id: str, timeout: float) -> str | None:
    """Fallback to the authenticated torrent file listing for an application ID."""
    if not os.environ.get("RUTRACKER_COOKIE", "").strip():
        return None
    try:
        body = urllib.parse.urlencode({"t": topic_id}).encode("ascii")
        html = fetch(TORRENT_URL, timeout=timeout, data=body)
    except ScrapeError:
        return None
    return extract_title_id(html)


def parse_section_page(html: str) -> list[str]:
    """Topic ids from a section listing, order-preserving, deduplicated."""
    seen: list[str] = []
    for topic_id in TOPIC_LINK_RE.findall(html):
        if topic_id not in seen:
            seen.append(topic_id)
    return seen


def _entry_has_value(key: str, value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if key == "languages" and isinstance(value, dict):
        return bool(value.get("interface") or value.get("voice")
                    or value.get("note"))
    if key == "players" and isinstance(value, dict):
        return (value.get("online") is True or value.get("min", 1) != 1
                or value.get("max", 1) != 1)
    if isinstance(value, (list, dict, tuple, set)):
        return bool(value)
    if key == "published_date":
        return isinstance(value, int) and value > 0
    if key == "topic_id":
        return isinstance(value, int) and value > 0
    return True


def _merge_entry(old: dict[str, Any], new: dict[str, Any],
                 generated_at: int) -> tuple[dict[str, Any], bool]:
    """Refresh populated fields while retaining rich metadata on thin parses."""
    merged = dict(old)
    changed = False
    for key, value in new.items():
        if key == "catalog_generated_at" or not _entry_has_value(key, value):
            continue
        if key == "languages" and isinstance(value, dict):
            old_languages = merged.get("languages", {})
            if not isinstance(old_languages, dict):
                old_languages = {}
            new_languages = dict(old_languages)
            languages_changed = False
            for language_key in ("interface", "voice", "note"):
                language_value = value.get(language_key)
                if _entry_has_value(language_key, language_value) \
                        and new_languages.get(language_key) != language_value:
                    new_languages[language_key] = language_value
                    languages_changed = True
            if languages_changed:
                merged[key] = new_languages
                changed = True
            continue
        if merged.get(key) != value:
            merged[key] = value
            changed = True
    if changed:
        merged["catalog_generated_at"] = generated_at
    return merged, changed


def merge_with_previous(
    previous: list[dict[str, Any]],
    scraped: list[dict[str, Any]],
    generated_at: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Merge by topic ID first, then deduplicate the resulting infohashes.

    RuTracker can replace a torrent while retaining its topic. Keying only by
    infohash would leave the old and new hashes as duplicate catalog rows.
    Empty fields from a thin page parse never erase richer stored metadata.
    """
    by_topic: dict[str, dict[str, Any]] = {}
    by_hash: dict[str, dict[str, Any]] = {}
    status_by_topic: dict[str, str] = {}
    previous_hashes: set[str] = set()

    for entry in previous:
        if not isinstance(entry, dict):
            continue
        topic = str(entry.get("topic_id", "")).strip()
        info_hash = build_catalog.info_hash_from_magnet(entry.get("magnet"))
        if topic:
            by_topic.setdefault(topic, entry)
            status_by_topic.setdefault(topic, "kept")
        if info_hash:
            previous_hashes.add(info_hash)
            by_hash.setdefault(info_hash, entry)

    stats = {"previous": len(previous), "scraped": len(scraped),
             "updated": 0, "added": 0, "keptStale": 0}
    for entry in scraped:
        if not isinstance(entry, dict):
            continue
        topic = str(entry.get("topic_id", "")).strip()
        info_hash = build_catalog.info_hash_from_magnet(entry.get("magnet"))
        if not info_hash:
            continue
        old = by_topic.get(topic) if topic else None
        if old is None:
            old = by_hash.get(info_hash)
        if old is None:
            merged_entry, status = entry, "added"
        else:
            merged_entry, changed = _merge_entry(old, entry, generated_at)
            status = "updated" if changed else "kept"
        if topic:
            by_topic[topic] = merged_entry
            status_by_topic[topic] = status
        by_hash[info_hash] = merged_entry

    # Keep topic-keyed rows first; then include legacy rows without a topic ID.
    candidates: list[tuple[dict[str, Any], str]] = []
    seen_topics: set[str] = set()
    for topic, entry in by_topic.items():
        candidates.append((entry, status_by_topic.get(topic, "kept")))
        seen_topics.add(topic)
    for info_hash, entry in by_hash.items():
        topic = str(entry.get("topic_id", "")).strip()
        if topic and topic in seen_topics:
            continue
        candidates.append((entry, "kept"))

    # Deduplicate after topic updates, so a changed magnet replaces the old one.
    candidates.sort(key=lambda item: (
        {"updated": 2, "added": 1, "kept": 0}.get(item[1], 0),
        int(item[0].get("published_date", 0) or 0)), reverse=True)
    unique: dict[str, tuple[dict[str, Any], str]] = {}
    for entry, status in candidates:
        info_hash = build_catalog.info_hash_from_magnet(entry.get("magnet"))
        if info_hash and info_hash not in unique:
            unique[info_hash] = (entry, status)
    merged = [item[0] for item in unique.values()]
    merged.sort(key=lambda e: (-int(e.get("published_date", 0) or 0),
                               str(e.get("title", ""))))
    stats["merged"] = len(merged)
    stats["updated"] = sum(status == "updated" for _, status in unique.values())
    stats["added"] = sum(
        status == "added"
        and build_catalog.info_hash_from_magnet(entry.get("magnet"))
        not in previous_hashes
        for entry, status in unique.values())
    stats["keptStale"] = max(
        0, stats["merged"] - stats["updated"] - stats["added"])
    return merged, stats

def check_coverage_gate(merged: int, previous: int,
                        min_keep_ratio: float) -> str | None:
    """None when publishable, else a human-readable veto reason."""
    if previous <= 0 or merged <= 0:
        return "empty merge or empty previous snapshot"
    if merged < previous * min_keep_ratio:
        return (f"coverage gate: merged {merged} < "
                f"{min_keep_ratio:.0%} of previous {previous}")
    return None


def scrape_topics(topic_ids: list[str], delay: float,
                  timeout: float,
                  feed_by_topic: dict[str, dict[str, Any]] | None = None
                  ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    records: list[dict[str, Any]] = []
    stats: dict[str, Any] = {"fetched": 0, "parsed": 0, "challenge": False,
                             "titleIdFallbacks": 0, "errors": []}
    unique_ids = list(dict.fromkeys(str(topic).strip() for topic in topic_ids
                                    if str(topic).strip().isdigit()))
    for index, topic_id in enumerate(unique_ids):
        if index and delay > 0:
            time.sleep(delay)
        url = TOPIC_URL.format(topic_id=topic_id)
        try:
            html = fetch(url, timeout)
        except ScrapeError as error:
            stats["errors"].append({"topicId": topic_id,
                                   "error": str(error)})
            if error.challenge:
                stats["challenge"] = True
                break
            continue
        stats["fetched"] += 1
        record = parse_topic_page(
            html, topic_id, url, (feed_by_topic or {}).get(topic_id))
        if record:
            if not record.get("title_id"):
                title_id = fetch_title_id(topic_id, timeout)
                if title_id:
                    record["title_id"] = title_id
                    stats["titleIdFallbacks"] += 1
            records.append(record)
            stats["parsed"] += 1
        else:
            stats["errors"].append({"topicId": topic_id,
                                   "error": "no magnet/title in page"})
    return records, stats


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Update the catalog from RuTracker Atom + topic pages; "
                    "never drop stale rows on partial failure.")
    parser.add_argument("--previous", required=True,
                        help="Previous catalog.json (v2, normalized).")
    parser.add_argument("--previous-manifest", default="",
                        help="Previous manifest.json (for provenance).")
    parser.add_argument("--output", required=True,
                        help="Output directory for catalog.json/manifest.json/report.md.")
    parser.add_argument("--overrides", default="overrides.json")
    parser.add_argument("--catalog-commit", default="local")
    parser.add_argument("--catalog-url", default=(
        "https://github.com/i3sey/pipensx-catalog/"
        "releases/latest/download/catalog.json"))
    parser.add_argument("--topics", default="",
                        help="Comma-separated topic IDs to scrape.")
    parser.add_argument("--atom", action="store_true",
                        help="Discover/update recent topics from the Switch Atom feed.")
    parser.add_argument("--section", default="",
                        help="Section ID to list topics from.")
    parser.add_argument("--section-limit", type=int, default=50)
    parser.add_argument("--delay", type=float, default=2.0)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--min-keep-ratio", type=float, default=0.98)
    parser.add_argument("--generated-at", type=int, default=0)
    args = parser.parse_args()

    import datetime as dt
    previous = json.loads(Path(args.previous).read_text(encoding="utf-8"))
    if not isinstance(previous, list):
        print("scrape: previous snapshot must be an array", file=sys.stderr)
        return 2

    feed_entries: list[dict[str, Any]] = []
    if args.atom:
        try:
            feed_entries = fetch_atom_feed(args.timeout)
            print(f"scrape: Atom discovered {len(feed_entries)} entries from "
                  f"{ATOM_FEED_URL}")
        except Exception as error:  # network/XML errors should not discard rotation
            print(f"scrape: Atom unavailable ({type(error).__name__})",
                  file=sys.stderr)

    topic_ids = [str(item["topic_id"]) for item in feed_entries]
    topic_ids.extend(t.strip() for t in args.topics.split(",")
                     if t.strip().isdigit())
    if args.section:
        try:
            section_html = fetch(SECTION_URL.format(section_id=args.section),
                                 args.timeout)
        except ScrapeError as error:
            print(f"scrape: section fetch failed: {error}", file=sys.stderr)
            return 0 if error.challenge else 1
        topic_ids += parse_section_page(section_html)[:args.section_limit]
    topic_ids = list(dict.fromkeys(topic_ids))

    feed_by_topic = {str(entry["topic_id"]): entry for entry in feed_entries}
    records, fetch_stats = scrape_topics(
        topic_ids, args.delay, args.timeout, feed_by_topic=feed_by_topic)
    page_scraped_ids = {str(record.get("topic_id")) for record in records
                        if record.get("topic_id")}
    print(f"scrape: fetched={fetch_stats['fetched']} "
          f"parsed={fetch_stats['parsed']} "
          f"titleIdFallbacks={fetch_stats['titleIdFallbacks']} "
          f"errors={len(fetch_stats['errors'])} "
          f"challenge={fetch_stats['challenge']}")

    # If a known topic page is blocked, Atom can still refresh title/magnet;
    # its rounded size is only a fallback when the stored size is missing.
    previous_by_topic = {
        str(entry.get("topic_id")): entry for entry in previous
        if isinstance(entry, dict) and entry.get("topic_id")
    }
    feed_fallback_ids: set[str] = set()
    if args.atom:
        for item in feed_entries:
            topic_id = str(item["topic_id"])
            old = previous_by_topic.get(topic_id)
            if (topic_id in page_scraped_ids or old is None
                    or not item.get("magnet")):
                continue
            fallback = dict(old)
            fallback["title"] = item["title"]
            fallback["magnet"] = item["magnet"]
            fallback["url"] = item["url"]
            if (item.get("size_bytes")
                    and not build_catalog.parse_size_bytes(
                        old.get("size_bytes", old.get("size")))):
                # Feed sizes are rounded title labels; don't replace a known
                # exact size with that approximation.
                fallback["size"] = item["size_bytes"]
                fallback["size_bytes"] = item["size_bytes"]
            records.append(fallback)
            feed_fallback_ids.add(topic_id)
        if feed_fallback_ids:
            print(f"scrape: used Atom magnet fallback for "
                  f"{len(feed_fallback_ids)} known topics")

    if not records and fetch_stats["challenge"]:
        print("scrape: blocked by anti-bot; keeping previous snapshot")
        out = Path(args.output)
        out.mkdir(parents=True, exist_ok=True)
        (out / "catalog.json").write_bytes(
            Path(args.previous).read_bytes())
        if args.previous_manifest and Path(args.previous_manifest).exists():
            (out / "manifest.json").write_bytes(
                Path(args.previous_manifest).read_bytes())
        return 0

    generated_at = args.generated_at or int(dt.datetime.now(
        dt.timezone.utc).timestamp())
    # Fill only schema-required fields before normalization. Optional old
    # metadata is merged afterwards, preserving the v2 languages/players structs.
    for index, record in enumerate(records):
        old = previous_by_topic.get(str(record.get("topic_id", "")))
        if old is None:
            continue
        enriched = dict(record)
        for key in ("title", "magnet", "cover", "url"):
            if not _entry_has_value(key, enriched.get(key)):
                enriched[key] = old.get(key)
        if not _entry_has_value("size", enriched.get("size_bytes",
                                                    enriched.get("size"))):
            enriched["size"] = old.get("size_bytes", old.get("size"))
        if not _entry_has_value("published_date", enriched.get("published_date")):
            enriched["published_date"] = old.get("published_date", 0)
        records[index] = enriched

    overrides = build_catalog.load_overrides(
        Path(args.overrides) if Path(args.overrides).exists() else None)
    scraped_entries, build_stats = build_catalog.build_catalog(
        records, overrides, generated_at)
    normalized_feed_ids = {str(entry.get("topic_id"))
                           for entry in scraped_entries if entry.get("topic_id")}
    pending_feed_ids = {
        str(item["topic_id"]) for item in feed_entries
        if str(item["topic_id"]) not in normalized_feed_ids
    }
    if pending_feed_ids:
        print(f"scrape: {len(pending_feed_ids)} Atom topics need page enrichment "
              "(not published as incomplete new rows)")
    print(f"scrape: normalized {len(scraped_entries)}/{len(records)} "
          f"(dropped magnet/size/cover: "
          f"{build_stats.get('droppedNoMagnet', 0)}/"
          f"{build_stats.get('droppedNoSize', 0)}/"
          f"{build_stats.get('droppedNoCover', 0)})")
    merged, stats = merge_with_previous(previous, scraped_entries,
                                        generated_at)
    veto = check_coverage_gate(stats["merged"], stats["previous"],
                               args.min_keep_ratio)
    print(f"scrape: previous={stats['previous']} scraped={stats['scraped']} "
          f"updated={stats['updated']} added={stats['added']} "
          f"keptStale={stats['keptStale']} merged={stats['merged']}")
    if veto:
        print(f"scrape: VETO: {veto}", file=sys.stderr)
        return 2

    source_commit = "rutracker-atom+topic-pages" if args.atom else "topic-pages"
    report_stats = dict(stats)
    report_stats["seedEntries"] = len(records)
    report_stats["atomEntries"] = len(feed_entries)
    report_stats["atomFallbacks"] = len(feed_fallback_ids)
    report_stats["pendingAtomEntries"] = len(pending_feed_ids)
    report_stats["titleIdCoverage"] = (
        sum(1 for e in merged if e.get("title_id")) / len(merged)
        if merged else 0.0)
    report_stats["missingTitleId"] = (
        len(merged) - sum(1 for e in merged if e.get("title_id")))
    manifest = build_catalog.write_outputs(
        Path(args.output), merged, report_stats,
        source_commit=source_commit, catalog_commit=args.catalog_commit,
        catalog_url=args.catalog_url)
    catalog = manifest["catalog"]
    print(f"scrape: wrote entries={catalog['entries']} "
          f"bytes={catalog['bytes']} sha256={catalog['sha256'][:12]}...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
