#!/usr/bin/env python3
"""Unified CLI for pipensx-catalog.

Commands:
  scrape [--full | --update]   Scrape RuTracker topics via FlareSolverr
  export                       Export SQLite catalog to catalog.json, manifest.json, report.md
  status                       Show current SQLite database statistics
  match-titledb                Re-run TitleDB matching for entries without TitleID
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from export_catalog import export_catalog
from extractor import (
    TitleDBMatcher,
    info_hash_from_magnet,
    is_base_title_id,
    to_base_title_id,
)
from flaresolverr_client import FlareSolverrClient
from scraper import DEFAULT_FORUM_ID, Scraper
from storage import Storage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
def load_dotenv(path: str = ".env") -> None:
    """Load simple KEY=VALUE pairs from .env into os.environ if not already set."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip("\"'")
            if k and k not in os.environ:
                os.environ[k] = v


load_dotenv()

logger = logging.getLogger("manage_catalog")



def cmd_scrape(args: argparse.Namespace) -> int:
    storage = Storage(args.db)
    flaresolverr = FlareSolverrClient(
        base_url=args.flaresolverr_url,
        cookie_str=args.cookie,
    )
    titledb = TitleDBMatcher(cache_path=args.titledb_cache)
    scraper = Scraper(
        storage=storage,
        flaresolverr=flaresolverr,
        titledb_matcher=titledb,
        delay=args.delay,
    )

    if args.full:
        logger.info("Executing --full scrape on forum f=%d...", args.forum_id)
        scraper.run_full(forum_id=args.forum_id, limit=args.limit)
    elif args.update:
        logger.info("Executing --update scrape on forum f=%d (pages=%d, limit=%s)...", args.forum_id, args.pages, args.limit)
        scraper.run_update(forum_id=args.forum_id, pages=args.pages, limit=args.limit)
    else:
        logger.error("Must specify either --full or --update for scrape command")
        return 1

    return 0


def cmd_export(args: argparse.Namespace) -> int:
    storage = Storage(args.db)
    try:
        res = export_catalog(
            storage=storage,
            output_dir=args.output,
            overrides_path=args.overrides,
            schema_path=args.schema,
            catalog_commit=args.catalog_commit,
            source_commit=args.source_commit,
        )
        logger.info(
            "Export successful! Entries: %d, Bytes: %d, TitleID Coverage: %.1f%%",
            res["entries"], res["bytes"], res["coverage_pct"]
        )
        return 0
    except Exception as exc:
        logger.error("Export failed: %s", exc)
        return 1


def cmd_status(args: argparse.Namespace) -> int:
    if not Path(args.db).exists():
        logger.error("Database file not found: %s", args.db)
        return 1

    storage = Storage(args.db)
    stats = storage.get_stats()

    total_mb = stats["total_bytes"] / (1024 * 1024)
    total_gb = total_mb / 1024

    print("=" * 50)
    print("pipensx-catalog SQLite Status")
    print("=" * 50)
    print(f"Database path:           {args.db}")
    print(f"Total topics indexed:    {stats['total_topics']}")
    print("Topics by status:")
    for status, count in stats["status_counts"].items():
        print(f"  - {status:<15}: {count}")
    print("-" * 50)
    print(f"Catalog entries built:   {stats['total_catalog_entries']}")
    print(f"  - with TitleID:        {stats['entries_with_title_id']}")
    print(f"  - without TitleID:     {stats['entries_without_title_id']}")
    if stats["total_catalog_entries"] > 0:
        pct = (stats["entries_with_title_id"] / stats["total_catalog_entries"]) * 100
        print(f"  - TitleID coverage:    {pct:.1f}%")
    print(f"Total torrent data size: {total_gb:.2f} GB ({int(stats['total_bytes']):,} bytes)")
    print("=" * 50)
    return 0


def cmd_match_titledb(args: argparse.Namespace) -> int:
    if not Path(args.db).exists():
        logger.error("Database file not found: %s", args.db)
        return 1

    storage = Storage(args.db)
    titledb = TitleDBMatcher(cache_path=args.titledb_cache)
    titledb.ensure_loaded()

    all_entries = storage.get_all_catalog_entries()
    updated_count = 0

    for entry in all_entries:
        topic_id = entry.get("topic_id")
        current_tid = entry.get("title_id")
        if not topic_id:
            continue

        if not current_tid or not is_base_title_id(current_tid):
            matched_tid = titledb.match(entry.get("title") or "")
            if matched_tid and is_base_title_id(matched_tid):
                entry["title_id"] = matched_tid
                storage.save_catalog_entry(topic_id, matched_tid, entry)
                updated_count += 1
                logger.info("Matched topic %d '%s' -> %s", topic_id, entry.get("title"), matched_tid)

    logger.info("TitleDB matching completed: updated %d entries", updated_count)
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    seed_path = Path(args.file)
    if not seed_path.exists():
        logger.error("Seed file not found: %s", seed_path)
        return 1

    storage = Storage(args.db)
    logger.info("Importing seed entries from %s...", seed_path)
    with open(seed_path, "r", encoding="utf-8") as f:
        entries = json.load(f)

    if not isinstance(entries, list):
        logger.error("Seed file must contain a JSON array of entries")
        return 1

    imported = 0
    for entry in entries:
        topic_id = entry.get("topic_id")
        if not topic_id:
            continue
        url = entry.get("url") or f"https://rutracker.org/forum/viewtopic.php?t={topic_id}"
        title = entry.get("title")
        title_id = entry.get("title_id")
        size_bytes = entry.get("size_bytes") or entry.get("size")
        magnet = entry.get("magnet")
        info_hash = info_hash_from_magnet(magnet)

        storage.upsert_topic(
            topic_id=int(topic_id),
            url=url,
            title=title,
            forum_id=args.forum_id,
            status="parsed",
        )
        storage.save_topic_details(
            topic_id=int(topic_id),
            info_hash=info_hash,
            size_bytes=int(size_bytes) if size_bytes else None,
        )
        storage.save_catalog_entry(
            topic_id=int(topic_id),
            title_id=title_id,
            entry_dict=entry,
        )
        imported += 1

    logger.info("Seed import completed: imported %d entries into %s", imported, args.db)
    return 0



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="manage_catalog.py",
        description="RuTracker Nintendo Switch catalog manager and autonomous scraper.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # scrape command
    p_scrape = subparsers.add_parser("scrape", help="Scrape topics from RuTracker via FlareSolverr")
    mode_group = p_scrape.add_mutually_exclusive_group(required=True)
    mode_group.add_argument("--full", action="store_true", help="Crawl all forum pagination and scrape all topics")
    mode_group.add_argument("--update", action="store_true", help="Incremental scrape of recent pages")
    p_scrape.add_argument("--forum-id", type=int, default=DEFAULT_FORUM_ID, help=f"RuTracker forum ID (default: {DEFAULT_FORUM_ID})")
    p_scrape.add_argument("--pages", type=int, default=2, help="Pages to scan in --update mode (default: 2)")
    p_scrape.add_argument("--db", default="catalog.db", help="Path to SQLite database (default: catalog.db)")
    p_scrape.add_argument("--delay", type=float, default=1.5, help="Delay between requests in seconds (default: 1.5)")
    p_scrape.add_argument("--limit", type=int, default=None, help="Limit number of pending topics to process")
    p_scrape.add_argument("--flaresolverr-url", default=None, help="FlareSolverr API URL (or env FLARESOLVERR_URL)")
    p_scrape.add_argument("--cookie", default=None, help="RuTracker cookies string (or env RUTRACKER_COOKIE)")
    p_scrape.add_argument("--titledb-cache", default=".cache/titledb.json", help="Path to TitleDB cache file")
    p_scrape.set_defaults(func=cmd_scrape)

    # export command
    p_export = subparsers.add_parser("export", help="Export catalog to catalog.json, manifest.json, report.md")
    p_export.add_argument("--db", default="catalog.db", help="Path to SQLite database (default: catalog.db)")
    p_export.add_argument("--output", default="output", help="Output directory (default: output)")
    p_export.add_argument("--overrides", default="overrides.json", help="Path to overrides.json (default: overrides.json)")
    p_export.add_argument("--schema", default="schema.json", help="Path to schema.json (default: schema.json)")
    p_export.add_argument("--catalog-commit", default="local", help="Git commit label for manifest")
    p_export.add_argument("--source-commit", default="local", help="Source commit label for manifest")
    p_export.set_defaults(func=cmd_export)

    # status command
    p_status = subparsers.add_parser("status", help="Show SQLite database statistics")
    p_status.add_argument("--db", default="catalog.db", help="Path to SQLite database (default: catalog.db)")
    p_status.set_defaults(func=cmd_status)

    # match-titledb command
    p_match = subparsers.add_parser("match-titledb", help="Match release titles against TitleDB for missing TitleIDs")
    p_match.add_argument("--db", default="catalog.db", help="Path to SQLite database (default: catalog.db)")
    p_match.add_argument("--titledb-cache", default=".cache/titledb.json", help="Path to TitleDB cache file")
    p_match.set_defaults(func=cmd_match_titledb)

    # seed command
    p_seed = subparsers.add_parser("seed", help="Import existing catalog.json into SQLite database")
    p_seed.add_argument("--file", default="catalog.json", help="Path to source catalog.json (default: catalog.json)")
    p_seed.add_argument("--db", default="catalog.db", help="Path to SQLite database (default: catalog.db)")
    p_seed.add_argument("--forum-id", type=int, default=DEFAULT_FORUM_ID, help=f"Default forum ID (default: {DEFAULT_FORUM_ID})")
    p_seed.set_defaults(func=cmd_seed)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
