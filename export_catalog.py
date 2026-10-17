"""Artifact builder and exporter for pipensx-catalog (v2).

Reads entries from SQLite storage, applies overrides.json, validates against
schema.json, and outputs:
- catalog.json
- manifest.json
- report.md
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html as html_lib
import json
import logging
from pathlib import Path
from typing import Any

from extractor import (
    clean_switch_2_edition,
    info_hash_from_magnet,
    is_base_title_id,
    to_base_title_id,
)
from storage import Storage

logger = logging.getLogger(__name__)

MAX_CATALOG_BYTES = 48 * 1024 * 1024
MAX_CATALOG_ENTRIES = 20000


def estimate_published_date(topic_id: int) -> int:
    """Monotonic timestamp estimate for topics that have no registered timestamp."""
    # Anchors:
    # 5622623 -> 2018-10-01 (1538352000)
    # 6922509 -> 2026-10-14 (1791978000)
    if topic_id <= 5622623:
        return 1538352000
    if topic_id >= 6922509:
        return 1791978000
    ratio = (topic_id - 5622623) / (6922509 - 5622623)
    return int(1538352000 + ratio * (1791978000 - 1538352000))


def load_overrides(overrides_path: Path) -> dict[str, Any]:
    """Load overrides.json if it exists."""
    if not overrides_path.exists():
        return {"hide": [], "title_id": {}, "rename": {}}
    try:
        with open(overrides_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return {
                "hide": set(str(x) for x in data.get("hide", [])),
                "title_id": {str(k): str(v).upper() for k, v in data.get("title_id", {}).items()},
                "rename": {str(k): str(v) for k, v in data.get("rename", {}).items()},
            }
    except Exception as exc:
        logger.warning("Could not read overrides file %s: %s", overrides_path, exc)
        return {"hide": set(), "title_id": {}, "rename": {}}


def validate_catalog(entries: list[dict[str, Any]], schema_path: Path | None = None) -> None:
    """Validate entries array against schema.json if available or basic assertions."""
    if not isinstance(entries, list):
        raise ValueError("Catalog must be a JSON array")
    if not (1 <= len(entries) <= MAX_CATALOG_ENTRIES):
        raise ValueError(f"Catalog entries count ({len(entries)}) out of bounds (1..{MAX_CATALOG_ENTRIES})")

    if schema_path and schema_path.exists():
        try:
            import jsonschema
            with open(schema_path, "r", encoding="utf-8") as f:
                schema = json.load(f)
            jsonschema.validate(instance=entries, schema=schema)
            logger.info("Validated %d entries against %s successfully", len(entries), schema_path)
            return
        except ImportError:
            logger.warning("jsonschema library not available, using built-in assertions")

    # Built-in strict validation
    for i, entry in enumerate(entries):
        for req in ("title", "magnet", "cover", "size", "size_bytes", "catalog_generated_at", "published_date"):
            if req not in entry or entry[req] is None:
                raise ValueError(f"Entry #{i} (topic_id={entry.get('topic_id')}) missing required field: {req}")
        if not str(entry["magnet"]).startswith("magnet:?xt=urn:btih:"):
            raise ValueError(f"Entry #{i} invalid magnet URI: {entry['magnet']}")
        if not (str(entry["cover"]).startswith("http://") or str(entry["cover"]).startswith("https://")):
            raise ValueError(f"Entry #{i} invalid cover URL: {entry['cover']}")
        if int(entry["size_bytes"]) <= 0:
            raise ValueError(f"Entry #{i} invalid size_bytes: {entry['size_bytes']}")
        if "title_id" in entry and entry["title_id"]:
            tid = str(entry["title_id"]).upper()
            if not is_base_title_id(tid):
                raise ValueError(f"Entry #{i} has non-base title_id: {tid}")


def export_catalog(
    storage: Storage,
    output_dir: str | Path = "output",
    overrides_path: str | Path = "overrides.json",
    schema_path: str | Path = "schema.json",
    release_url: str = "https://github.com/i3sey/pipensx-catalog/releases/latest/download/catalog.json",
    catalog_commit: str = "local",
    source_commit: str = "local",
) -> dict[str, Any]:
    """Export SQLite entries into catalog.json, manifest.json, and report.md."""
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    overrides = load_overrides(Path(overrides_path))
    raw_entries = storage.get_all_catalog_entries()

    clean_entries: list[dict[str, Any]] = []
    seen_hashes: set[str] = set()

    # Counters for report
    stat_total_raw = len(raw_entries)
    stat_dropped_no_magnet = 0
    stat_dropped_no_size = 0
    stat_dropped_no_cover = 0
    stat_hidden_by_overrides = 0
    stat_deduped_by_hash = 0
    stat_title_id_overrides = 0
    stat_renames = 0

    now_ts = int(dt.datetime.now(dt.timezone.utc).timestamp())

    for raw in raw_entries:
        topic_id = str(raw.get("topic_id", ""))

        # 1. Overrides: Hide
        if topic_id in overrides["hide"]:
            stat_hidden_by_overrides += 1
            continue

        # 2. Overrides: Rename
        title = raw.get("title", "").strip()
        if topic_id in overrides["rename"]:
            title = overrides["rename"][topic_id]
            stat_renames += 1
        title = clean_switch_2_edition(title)

        # 3. Check magnet & info_hash
        magnet = html_lib.unescape(raw.get("magnet") or "")
        info_hash = info_hash_from_magnet(magnet)
        if not magnet or not info_hash:
            stat_dropped_no_magnet += 1
            continue

        # Deduplication by info_hash
        if info_hash in seen_hashes:
            stat_deduped_by_hash += 1
            continue
        seen_hashes.add(info_hash)

        # 4. Check size
        size_bytes = raw.get("size_bytes") or raw.get("size")
        if not size_bytes or int(size_bytes) <= 0:
            stat_dropped_no_size += 1
            continue
        size_bytes = int(size_bytes)

        # 5. Check cover
        cover = raw.get("cover")
        if not cover or not (cover.startswith("http://") or cover.startswith("https://")):
            stat_dropped_no_cover += 1
            continue

        # 6. Overrides: TitleID
        title_id = raw.get("title_id")
        if topic_id in overrides["title_id"]:
            title_id = overrides["title_id"][topic_id]
            stat_title_id_overrides += 1

        if title_id:
            title_id = to_base_title_id(title_id)

        # Truncate screenshots to 6
        screenshots = raw.get("screenshots") or []
        if isinstance(screenshots, list):
            screenshots = [s for s in screenshots if isinstance(s, str) and (s.startswith("http://") or s.startswith("https://"))][:6]
        else:
            screenshots = []

        # Languages struct
        langs = raw.get("languages") or {}
        if not isinstance(langs, dict):
            langs = {"interface": [], "voice": []}
        interface_langs = [l for l in langs.get("interface", []) if isinstance(l, str)]
        voice_langs = [l for l in langs.get("voice", []) if isinstance(l, str)]

        # Published date resolution
        pub_date = raw.get("published_date")
        if not pub_date or int(pub_date) <= 0:
            pub_date = estimate_published_date(int(raw["topic_id"])) if raw.get("topic_id") else now_ts
        else:
            pub_date = int(pub_date)

        entry = {
            "title": title[:1024],
            "magnet": magnet,
            "cover": cover,
            "size": size_bytes,
            "size_bytes": size_bytes,
            "catalog_generated_at": raw.get("catalog_generated_at") or now_ts,
            "published_date": pub_date,
        }

        if raw.get("topic_id"):
            entry["topic_id"] = int(raw["topic_id"])
        if raw.get("url"):
            entry["url"] = str(raw["url"])[:2048]
        if title_id:
            entry["title_id"] = title_id
        if raw.get("package_type"):
            pkg = str(raw["package_type"]).lower()
            if pkg in ("nsp", "nsz", "xci", "nro", "zip", "7z"):
                entry["package_type"] = pkg
        if raw.get("version"):
            entry["version"] = str(raw["version"])[:32]
        if raw.get("version_name"):
            entry["version_name"] = str(raw["version_name"])[:32]
        if raw.get("year"):
            entry["year"] = str(raw["year"])[:32]
        if raw.get("genre"):
            entry["genre"] = str(raw["genre"])[:256]
        if raw.get("developer"):
            entry["developer"] = str(raw["developer"])[:256]
        if raw.get("publisher"):
            entry["publisher"] = str(raw["publisher"])[:256]
        if raw.get("description"):
            entry["description"] = str(raw["description"])[:4096]

        entry["screenshots"] = screenshots
        entry["languages"] = {"interface": interface_langs, "voice": voice_langs}

        clean_entries.append(entry)

    # Sort entries deterministically by newest published_date desc, topic_id desc, title asc
    clean_entries.sort(key=lambda x: (-int(x.get("published_date", 0) or 0), -int(x.get("topic_id", 0) or 0), x.get("title", "").casefold()))

    # Validate against schema
    schema_p = Path(schema_path)
    if clean_entries:
        validate_catalog(clean_entries, schema_p if schema_p.exists() else None)

    # Serialize catalog.json
    catalog_bytes = json.dumps(clean_entries, ensure_ascii=False, indent=2).encode("utf-8")
    if len(catalog_bytes) > MAX_CATALOG_BYTES:
        raise ValueError(f"Catalog payload {len(catalog_bytes)} bytes exceeds limit {MAX_CATALOG_BYTES}")

    catalog_path = out_dir / "catalog.json"
    catalog_path.write_bytes(catalog_bytes)
    sha256 = hashlib.sha256(catalog_bytes).hexdigest()

    # Create manifest.json
    now_iso = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest = {
        "schemaVersion": 2,
        "generatedAt": now_iso,
        "catalogCommit": catalog_commit,
        "sourceCommit": source_commit,
        "catalog": {
            "url": release_url,
            "sha256": sha256,
            "bytes": len(catalog_bytes),
            "entries": len(clean_entries),
        },
    }
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # Generate report.md
    with_tid_count = sum(1 for e in clean_entries if e.get("title_id"))
    missing_tid_count = len(clean_entries) - with_tid_count
    coverage_pct = (with_tid_count / len(clean_entries) * 100.0) if clean_entries else 0.0

    report = f"""# pipensx-catalog build report

- generatedAt: `{now_iso}`
- sourceCommit: `{source_commit}`
- entries: **{len(clean_entries)}** ({stat_total_raw} raw in SQLite)
- bytes: {len(catalog_bytes)}
- sha256: `{sha256}`
- title_id coverage: {coverage_pct:.1f}% (missing {missing_tid_count})

## Pipeline

- dropped (no/invalid magnet): {stat_dropped_no_magnet}
- dropped (no size): {stat_dropped_no_size}
- dropped (no cover): {stat_dropped_no_cover}
- hidden by overrides: {stat_hidden_by_overrides}
- deduped by infoHash: {stat_deduped_by_hash}
- title_id overrides applied: {stat_title_id_overrides}
- renames applied: {stat_renames}
"""
    report_path = out_dir / "report.md"
    report_path.write_text(report, encoding="utf-8")

    logger.info("Export completed: %s (%d entries, %d bytes)", catalog_path, len(clean_entries), len(catalog_bytes))

    return {
        "entries": len(clean_entries),
        "bytes": len(catalog_bytes),
        "sha256": sha256,
        "coverage_pct": coverage_pct,
        "manifest": manifest,
    }
