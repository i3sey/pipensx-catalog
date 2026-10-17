"""RuTracker scraper for pipensx-catalog.

Supports:
- --update: scans recent forum pages (or Atom feed), finds new/updated topics,
  downloads details and enriches SQLite database.
- --full: walks full forum pagination, enqueues all topics, and processes them
  step by step with persistent resume on any failure.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Any

from extractor import (
    TitleDBMatcher,
    extract_from_filelist,
    info_hash_from_magnet,
    is_base_title_id,
    parse_topic_metadata,
    parse_viewtorrent_filelist,
)
from flaresolverr_client import FlareSolverrClient
from storage import Storage

logger = logging.getLogger(__name__)

DEFAULT_FORUM_ID = 1605
FORUM_URL = "https://rutracker.org/forum/viewforum.php?f={forum_id}&start={start}"
TOPIC_URL = "https://rutracker.org/forum/viewtopic.php?t={topic_id}"
TORRENT_URL = "https://rutracker.org/forum/viewtorrent.php"
ATOM_FEED_URL = "https://feed.rutracker.cc/atom/f/{forum_id}.atom"
ATOM_NS = "{http://www.w3.org/2005/Atom}"


def parse_forum_topics(html_body: str, forum_id: int) -> list[dict[str, Any]]:
    """Parse topic rows from RuTracker viewforum.php HTML."""
    topics: list[dict[str, Any]] = []
    seen = set()

    # Strategy 1: Parse table rows with id="tr-XXXX", filtering for torrent releases (dl.php?t=)
    rows = re.findall(r'<tr\b[^>]*id=["\']tr-(\d+)["\'][^>]*>(.*?)</tr>', html_body, re.DOTALL | re.IGNORECASE)
    if rows:
        for tid_str, content in rows:
            try:
                topic_id = int(tid_str)
            except ValueError:
                continue

            # Skip sticky/discussion threads that don't have a torrent download link
            if "dl.php?t=" not in content and "f-dl" not in content:
                continue

            if topic_id in seen:
                continue

            # Extract title
            title_m = re.search(r'<a\b[^>]*class=["\'][^"\']*tt-text[^"\']*["\'][^>]*>(.*?)</a>', content, re.IGNORECASE | re.DOTALL)
            if not title_m:
                title_m = re.search(r'<a\b[^>]*href=["\'][^"\']*viewtopic\.php\?t=\d+[^"\']*["\'][^>]*>(.*?)</a>', content, re.IGNORECASE | re.DOTALL)

            raw_title = title_m.group(1) if title_m else f"Topic {topic_id}"
            title = re.sub(r"<[^>]+>", "", raw_title).strip()
            if not title:
                continue

            seen.add(topic_id)
            topics.append({
                "topic_id": topic_id,
                "url": TOPIC_URL.format(topic_id=topic_id),
                "title": title,
                "forum_id": forum_id,
                "status": "pending",
            })

    if topics:
        return topics

    # Strategy 2: Fallback to link matching
    pattern = re.compile(
        r'<a\b[^>]*href=["\'](?:viewtopic\.php\?t=|.*?/t/)(\d+)["\'][^>]*>(.*?)</a>',
        re.IGNORECASE,
    )

    for match in pattern.finditer(html_body):
        topic_id_str = match.group(1)
        try:
            topic_id = int(topic_id_str)
        except ValueError:
            continue

        if topic_id in seen:
            continue

        raw_title = match.group(2)
        title = re.sub(r"<[^>]+>", "", raw_title).strip()
        if not title:
            continue

        seen.add(topic_id)
        topics.append({
            "topic_id": topic_id,
            "url": TOPIC_URL.format(topic_id=topic_id),
            "title": title,
            "forum_id": forum_id,
            "status": "pending",
        })

    return topics


def parse_forum_pagination(html_body: str) -> list[int]:
    """Find start offsets for pagination in viewforum.php."""
    start_values = set([0])
    for match in re.finditer(r'viewforum\.php\?[^"\']*start=(\d+)', html_body):
        try:
            start_values.add(int(match.group(1)))
        except ValueError:
            pass
    return sorted(list(start_values))


def parse_atom_topics(atom_xml: str, forum_id: int) -> list[dict[str, Any]]:
    """Parse topics from RuTracker Atom feed."""
    topics: list[dict[str, Any]] = []
    try:
        root = ET.fromstring(atom_xml)
    except Exception as exc:
        logger.warning("Failed to parse Atom feed XML: %s", exc)
        return []

    for entry in root.findall(f"{ATOM_NS}entry"):
        id_node = entry.find(f"{ATOM_NS}id")
        title_node = entry.find(f"{ATOM_NS}title")
        updated_node = entry.find(f"{ATOM_NS}updated")

        if id_node is None or not id_node.text:
            continue
        id_text = id_node.text.strip()
        m = re.search(r"(?:/t/|[?&]t=)(\d+)", id_text)
        if not m:
            continue
        topic_id = int(m.group(1))

        title = title_node.text.strip() if (title_node is not None and title_node.text) else f"Topic {topic_id}"
        updated_at = updated_node.text.strip() if (updated_node is not None and updated_node.text) else None

        topics.append({
            "topic_id": topic_id,
            "url": TOPIC_URL.format(topic_id=topic_id),
            "title": title,
            "forum_id": forum_id,
            "last_post_time": updated_at,
            "status": "pending",
        })

    return topics


class Scraper:
    """Scraper orchestrating FlareSolverr, extraction, and SQLite storage."""

    def __init__(
        self,
        storage: Storage,
        flaresolverr: FlareSolverrClient | None = None,
        titledb_matcher: TitleDBMatcher | None = None,
        delay: float = 1.5,
    ):
        self.storage = storage
        self.flaresolverr = flaresolverr or FlareSolverrClient()
        self.titledb = titledb_matcher or TitleDBMatcher()
        self.delay = delay

    def crawl_forum_index(self, forum_id: int = DEFAULT_FORUM_ID, max_pages: int | None = None) -> int:
        """Walk forum pagination and register all topics in SQLite."""
        logger.info("Scanning forum index f=%d...", forum_id)
        start = 0
        total_discovered = 0
        visited_starts = set()

        while True:
            visited_starts.add(start)
            url = FORUM_URL.format(forum_id=forum_id, start=start)
            logger.info("Fetching forum page: %s", url)

            try:
                html = self.flaresolverr.get_page(url)
            except Exception as exc:
                logger.error("Failed to fetch forum page %s: %s", url, exc)
                break

            topics = parse_forum_topics(html, forum_id)
            if not topics:
                logger.info("No topics found on page %s, finishing forum scan", url)
                break

            added = self.storage.upsert_topics_batch(topics)
            total_discovered += added
            logger.info("Discovered %d topics on page (start=%d, total=%d)", len(topics), start, total_discovered)

            pagination = parse_forum_pagination(html)
            next_starts = [s for s in pagination if s not in visited_starts and s > start]
            if not next_starts:
                break

            start = next_starts[0]
            if max_pages and len(visited_starts) >= max_pages:
                logger.info("Reached max_pages limit (%d)", max_pages)
                break

            time.sleep(self.delay)

        return total_discovered

    def process_topic(self, topic_id: int) -> bool:
        """Fetch viewtopic and viewtorrent, parse metadata and save to DB."""
        topic_url = TOPIC_URL.format(topic_id=topic_id)

        logger.info("Processing topic %d...", topic_id)
        try:
            # 1. Fetch viewtopic.php
            topic_html = self.flaresolverr.get_page(topic_url)
            time.sleep(self.delay)

            # 2. Fetch viewtorrent.php via POST (requires session cookies)
            torrent_html = self.flaresolverr.post_request(TORRENT_URL, {"t": str(topic_id)})

            # 3. Parse topic metadata from viewtopic
            topic_meta = parse_topic_metadata(topic_html, topic_id=topic_id)

            # 4. Parse filelist from viewtorrent
            filelist = parse_viewtorrent_filelist(torrent_html)
            file_meta = extract_from_filelist(filelist, raw_title=topic_meta.get("title") or "")

            title_low = (topic_meta.get("title") or "").lower()
            is_port = (
                "[nro" in title_low or ".nro" in title_low or
                "хоумбрю" in title_low or "homebrew" in title_low or
                "(порт" in title_low or " порт" in title_low or "порт)" in title_low or "порты" in title_low
            )

            # Resolve TitleID:
            # Priority: Filelist Base TID -> Topic text TID -> TitleDB Matcher
            title_id = file_meta.get("title_id") or topic_meta.get("title_id")
            if not title_id and not is_port:
                title_id = self.titledb.match(topic_meta.get("title") or "")

            # Ensure valid base TitleID
            if title_id and not is_base_title_id(title_id):
                # Attempt to normalize
                try:
                    val = int(title_id, 16)
                    title_id = f"{(val & ~0xFFF):016X}"
                except ValueError:
                    title_id = None

            # Resolve size
            size_bytes = topic_meta.get("size_bytes")
            if not size_bytes and filelist:
                file_sizes = [f["size"] for f in filelist if f.get("size")]
                if file_sizes:
                    size_bytes = sum(file_sizes)

            info_hash = topic_meta.get("info_hash")

            # Save raw topic details into storage
            self.storage.save_topic_details(
                topic_id=topic_id,
                info_hash=info_hash,
                registered_at=topic_meta.get("registered_at"),
                size_bytes=size_bytes,
                raw_html=topic_html,
                filelist_json=filelist,
            )

            # Build pipensx v2 catalog entry
            now_ts = int(time.time())
            magnet = topic_meta.get("magnet")
            if not magnet and info_hash:
                magnet = f"magnet:?xt=urn:btih:{info_hash}"

            entry = {
                "topic_id": topic_id,
                "title": topic_meta.get("title") or f"Topic {topic_id}",
                "url": topic_url,
                "magnet": magnet,
                "cover": topic_meta.get("cover"),
                "screenshots": topic_meta.get("screenshots") or [],
                "size": size_bytes or 0,
                "size_bytes": size_bytes or 0,
                "title_id": title_id,
                "package_type": file_meta.get("package_type"),
                "version": file_meta.get("version"),
                "version_name": file_meta.get("version_name"),
                "year": topic_meta.get("year"),
                "genre": topic_meta.get("genre"),
                "developer": topic_meta.get("developer"),
                "publisher": topic_meta.get("publisher"),
                "description": topic_meta.get("description"),
                "languages": topic_meta.get("languages") or {"interface": [], "voice": []},
                "catalog_generated_at": now_ts,
                "published_date": topic_meta.get("posted_at") or now_ts,
            }

            self.storage.save_catalog_entry(topic_id, title_id, entry)
            self.storage.set_topic_status(topic_id, "parsed")
            logger.info("Successfully processed topic %d (TID: %s)", topic_id, title_id or "NONE")
            return True

        except Exception as exc:
            logger.error("Failed to process topic %d: %s", topic_id, exc)
            self.storage.set_topic_status(topic_id, "failed")
            return False

    def run_full(self, forum_id: int = DEFAULT_FORUM_ID, limit: int | None = None) -> None:
        """Run full crawl of the forum and process all pending topics."""
        logger.info("Starting --full crawl for forum %d...", forum_id)
        self.crawl_forum_index(forum_id=forum_id)

        pending = self.storage.get_pending_topics(limit=limit, forum_id=forum_id)
        logger.info("Found %d pending topics to process", len(pending))

        for idx, item in enumerate(pending, start=1):
            topic_id = item["topic_id"]
            logger.info("[%d/%d] Processing pending topic %d", idx, len(pending), topic_id)
            self.process_topic(topic_id)
            time.sleep(self.delay)

        logger.info("Full crawl completed. Stats: %s", self.storage.get_stats())

    def run_update(self, forum_id: int = DEFAULT_FORUM_ID, pages: int = 2, limit: int | None = None) -> None:
        """Run quick update: check first N pages / Atom feed for new or modified topics."""
        logger.info("Starting --update for forum %d (pages=%d)...", forum_id, pages)
        self.crawl_forum_index(forum_id=forum_id, max_pages=pages)

        # Process any pending topics
        pending = self.storage.get_pending_topics(limit=limit, forum_id=forum_id)
        logger.info("Found %d pending topics to process in update mode", len(pending))

        for idx, item in enumerate(pending, start=1):
            topic_id = item["topic_id"]
            logger.info("[%d/%d] Processing updated topic %d", idx, len(pending), topic_id)
            self.process_topic(topic_id)
            time.sleep(self.delay)

        logger.info("Update completed. Stats: %s", self.storage.get_stats())
