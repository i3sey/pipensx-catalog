#!/usr/bin/env python3
"""Read-only, one-topic probe of the configured RuTracker transport."""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scrape  # noqa: E402
from rutracker_transport import close_browser  # noqa: E402


def main() -> int:
    try:
        entries = scrape.fetch_atom_feed(timeout=25)
    except Exception as exc:
        print(f"probe: Atom failed ({type(exc).__name__})")
        return 1
    if not entries:
        print("probe: Atom returned no topics")
        return 1

    item = entries[0]
    topic_id = str(item.get("topic_id", ""))
    print(f"probe: Atom OK ({len(entries)} entries); testing topic {topic_id}")
    try:
        html = scrape.fetch(item["url"], timeout=30)
    except scrape.ScrapeError as exc:
        print(f"probe: topic failed; challenge={exc.challenge}; {exc}")
        return 2 if exc.challenge else 1

    low = html.casefold()
    title = re.search(r"<title[^>]*>(.*?)</title>", html,
                      flags=re.IGNORECASE | re.DOTALL)
    clean_title = re.sub(r"\s+", " ", title.group(1)).strip()[:100] \
        if title else ""
    print(f"probe: topic OK; bytes={len(html.encode('utf-8', 'replace'))}; "
          f"post_body={'post_body' in low}; "
          f"magnet={'magnet:?xt=urn:btih:' in low}")
    if clean_title:
        print(f"probe: title={clean_title}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        close_browser()
