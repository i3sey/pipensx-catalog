#!/usr/bin/env python3
"""RuTracker scraper for pipensx-catalog (P1).

Fetches topic pages, extracts raw records and merges them with the previous
catalog snapshot so a partially blocked run NEVER drops entries: scraped
records win per infoHash, everything else is kept stale and counted.

Transport (plan §3.1) is fully env-configured, no code changes needed:
  SCRAPER_PROXY   e.g. socks5h://127.0.0.1:1080 (Cloudflare WARP
                  `warp-cli mode proxy`, or any HTTP/SOCKS proxy)
  RUTRACKER_COOKIE  e.g. 'bb_session=...; bb_data=...' (authenticated
                  session; cookies are not IP-portable, see README)
  empty/absent = direct anonymous access.

Reality check (2026-10-02): anonymous topic fetches answer HTTP 403 with a
Cloudflare "Just a moment..." managed challenge — no header trick bypasses
it. The scraper detects the challenge explicitly (ChallengeBlocked) and the
CI falls back to the previous snapshot instead of publishing a shrunk
catalog. First validated parser run happens via workflow_dispatch `probe`.

Exit codes: 0 = seed written (or clean fallback), 2 = coverage gate failed
(drop > --min-keep-ratio vs previous — do NOT publish this seed).
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
from pathlib import Path
from typing import Any

import build_catalog

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
TOPIC_URL = "https://rutracker.org/forum/viewtopic.php?t={topic_id}"
SECTION_URL = "https://rutracker.org/forum/viewforum.php?f={section_id}"

MAGNET_RE = re.compile(r"magnet:\?xt=urn:btih:[A-Za-z0-9]+[^\"'\s<>]*")
TOPIC_LINK_RE = re.compile(r"viewtopic\.php\?t=(\d+)")
IMG_RE = re.compile(r'<img[^>]+src="([^"]+)"', re.IGNORECASE)
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


def build_opener() -> urllib.request.OpenerDirector:
    handlers: list[Any] = []
    proxy = os.environ.get("SCRAPER_PROXY", "").strip()
    if proxy:
        handlers.append(urllib.request.ProxyHandler(
            {"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    cookie = os.environ.get("RUTRACKER_COOKIE", "").strip()
    opener.addheaders = [
        ("User-Agent", DEFAULT_UA),
        ("Accept", "text/html,application/xhtml+xml"),
        ("Accept-Language", "ru-RU,ru;q=0.9,en;q=0.8"),
    ]
    if cookie:
        opener.addheaders.append(("Cookie", cookie))
    return opener


def fetch(url: str, timeout: float = 30.0) -> str:
    request = urllib.request.Request(url)
    opener = build_opener()
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, "replace")
    except urllib.error.HTTPError as error:
        snippet = error.read()[:8000].decode("utf-8", "replace")
        if error.code == 403 and any(m in snippet for m in CHALLENGE_MARKERS):
            raise ScrapeError(f"Cloudflare challenge for {url}",
                              challenge=True) from error
        raise ScrapeError(f"HTTP {error.code} for {url}") from error
    except OSError as error:
        raise ScrapeError(f"fetch failed for {url}: {error}") from error


def _clean_text(value: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_module.unescape(text)
    return re.sub(r"[ \t\xa0]+", " ", text).strip()


def parse_topic_page(html: str, topic_id: str, url: str) -> dict[str, Any] | None:
    """Extract a raw seed-shaped record from a topic page.

    Tolerant by design: magnet + title are required, everything else is
    best-effort (a thin record still merges — previous values fill the
    gaps, and normalize_entry drops only magnet-less/sizeless/cover-less
    rows). Markup assumptions are pinned by tests/test_scrape.py fixtures
    and re-validated on every live probe run.
    """
    bodies = re.findall(
        r'<div class="post_body[^"]*"[^>]*>(.*?)</div>\s*(?:<div|$)',
        html, flags=re.IGNORECASE | re.DOTALL)
    scope = max(bodies, key=len) if bodies else html

    magnets = MAGNET_RE.findall(scope)
    info_hash = None
    magnet = None
    for candidate in magnets:
        info_hash = build_catalog.info_hash_from_magnet(candidate)
        if info_hash:
            magnet = candidate
            break
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
    if not title:
        return None

    images: list[str] = []
    for src in IMG_RE.findall(scope):
        src = src.strip()
        if (not src.startswith(("https://", "http://")) or
                SKIP_IMG_RE.search(src) or src in images):
            continue
        images.append(src)
    cover = images[0] if images else ""
    screenshots = images[1:]

    text = _clean_text(scope)
    size: Any = None
    for line in text.splitlines():
        candidate = build_catalog.parse_size_bytes(line)
        if candidate:
            size = candidate
            break
    if size is None:
        size = build_catalog.parse_size_bytes(text[:2000])

    posted_at = 0
    for pattern in (r"data-posted=['\"](\d{10})['\"]",
                    r"Добавлено:\s*(\d{10})",
                    r"posted['\"]?\s*[:=]\s*['\"]?(\d{10})"):
        match = re.search(pattern, html)
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
    if cover:
        record["cover"] = cover
    if screenshots:
        record["screenshots"] = screenshots
    description = text.strip()
    if description:
        record["description"] = description[:4096]
    return record


def parse_section_page(html: str) -> list[str]:
    """Topic ids from a section listing, order-preserving, deduplicated."""
    seen: list[str] = []
    for topic_id in TOPIC_LINK_RE.findall(html):
        if topic_id not in seen:
            seen.append(topic_id)
    return seen


def merge_with_previous(
    previous: list[dict[str, Any]],
    scraped: list[dict[str, Any]],
    generated_at: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Merge normalized scraped entries over the previous snapshot.

    Both sides are v2-normalized (scraped raws go through
    build_catalog.build_catalog first, so overrides already apply).
    Keyed by infoHash: scraped wins, previous-only rows are kept stale
    so a blocked run changes nothing but the report.
    """
    _ = generated_at  # kept for API symmetry; normalization is upstream.
    by_hash: dict[str, dict[str, Any]] = {}
    for entry in previous:
        info_hash = build_catalog.info_hash_from_magnet(entry.get("magnet"))
        if info_hash:
            by_hash.setdefault(info_hash, entry)
    stats = {"previous": len(previous), "scraped": len(scraped),
             "updated": 0, "added": 0, "keptStale": 0}
    for entry in scraped:
        if not isinstance(entry, dict):
            continue
        info_hash = build_catalog.info_hash_from_magnet(entry.get("magnet"))
        if not info_hash:
            continue
        if info_hash in by_hash:
            stats["updated"] += 1
        else:
            stats["added"] += 1
        by_hash[info_hash] = entry
    merged = sorted(by_hash.values(),
                    key=lambda e: (-int(e.get("published_date", 0)),
                                   str(e.get("title", ""))))
    stats["merged"] = len(merged)
    stats["keptStale"] = stats["merged"] - stats["updated"] - stats["added"]
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
                  timeout: float) -> tuple[list[dict[str, Any]],
                                           dict[str, Any]]:
    records: list[dict[str, Any]] = []
    stats: dict[str, Any] = {"fetched": 0, "parsed": 0, "challenge": False,
                             "errors": []}
    for index, topic_id in enumerate(topic_ids):
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
        record = parse_topic_page(html, topic_id, url)
        if record:
            records.append(record)
            stats["parsed"] += 1
        else:
            stats["errors"].append({"topicId": topic_id,
                                   "error": "no magnet/title in page"})
    return records, stats


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scrape rutracker topics and merge with the previous "
                    "snapshot (never drops on partial failure).")
    parser.add_argument("--previous", required=True,
                        help="Previous catalog.json (v2, normalized).")
    parser.add_argument("--previous-manifest", default="",
                        help="Previous manifest.json (for sourceCommit).")
    parser.add_argument("--output", required=True,
                        help="Output directory for catalog.json/manifest.json/report.md.")
    parser.add_argument("--overrides", default="overrides.json")
    parser.add_argument("--catalog-commit", default="local")
    parser.add_argument("--catalog-url", default=(
        "https://github.com/i3sey/pipensx-catalog/"
        "releases/latest/download/catalog.json"))
    parser.add_argument("--topics", default="",
                        help="Comma-separated topic ids to scrape.")
    parser.add_argument("--section", default="",
                        help="Section id to list topics from.")
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
    topic_ids = [t.strip() for t in args.topics.split(",") if t.strip()]
    if args.section:
        try:
            section_html = fetch(SECTION_URL.format(section_id=args.section),
                                 args.timeout)
        except ScrapeError as error:
            print(f"scrape: section fetch failed: {error}", file=sys.stderr)
            return 0 if error.challenge else 1
        topic_ids += parse_section_page(section_html)[:args.section_limit]
    records, fetch_stats = scrape_topics(topic_ids, args.delay, args.timeout)
    print(f"scrape: fetched={fetch_stats['fetched']} "
          f"parsed={fetch_stats['parsed']} "
          f"errors={len(fetch_stats['errors'])} "
          f"challenge={fetch_stats['challenge']}")
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
    overrides = build_catalog.load_overrides(
        Path(args.overrides) if Path(args.overrides).exists() else None)
    scraped_entries, build_stats = build_catalog.build_catalog(
        records, overrides, generated_at)
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
    source_commit = "scrape"
    if args.previous_manifest and Path(args.previous_manifest).exists():
        try:
            prev_manifest = json.loads(
                Path(args.previous_manifest).read_text(encoding="utf-8"))
            prev_source = prev_manifest.get("sourceCommit", "")
            if prev_source:
                source_commit = f"{prev_source}+scrape"
        except (ValueError, OSError):
            pass
    report_stats = dict(stats)
    report_stats["seedEntries"] = len(records)
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
