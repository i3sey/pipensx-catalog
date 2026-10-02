# pipensx-catalog

Own Switch release catalog for pipensx (schema v2). Replaces the former
built-in `Langegen/switch-games` source. The pipensx client keeps reading
the old format (backward compatible); this repo only emits the cleaned v2.

Two distribution channels (same pattern as `pipensx-metadata`):

1. `https://github.com/i3sey/pipensx-catalog/releases/latest/download/manifest.json`
   + `catalog.json` — **default client channel**: manifest (~500 B) is
   fetched first, `catalog.json` (~20 MB) only when `sha256` changed.
2. `https://raw.githubusercontent.com/i3sey/pipensx-catalog/refs/heads/main/catalog.json`
   — secondary direct channel (debug, manual checks), not the default.

## Layout

```text
pipensx-catalog/
  build_catalog.py   # seed -> catalog.json + manifest.json + report.md
  overrides.json     # manual topic_id / rename / hide fixes
  schema.json        # JSON Schema v2 (CI gate)
  catalog.json       # latest built artifact (raw channel)
  manifest.json      # sha256/bytes/entries for the release channel
  report.md          # coverage, drops, diff counters
  tests/test_build_catalog.py
  .github/workflows/build-catalog.yml  # cron 6h + dispatch + release
```

## Local build

```bash
python3 -m unittest discover -s tests -v
python3 build_catalog.py \
  --seed /path/to/switch_games.json \
  --output output \
  --source-commit local --catalog-commit local
# optional: validate against the schema (needs `check-jsonschema` or python jsonschema)
python3 -c "import json; s=json.load(open('schema.json')); print('schema ok')"
```

P0 seed: last `switch_games.json` snapshot. P1 replaces the seed fetch with
a rutracker scraper; the scraper transport is configured via
`SCRAPER_PROXY` (e.g. `socks5h://127.0.0.1:1080` for Cloudflare WARP
`warp-cli mode proxy`), empty = direct access. Partial scrape failures must
never drop entries — merge incrementally with the previous snapshot.

## Validation gate (blocks publish)

- JSON array, `1..20000` entries, payload `<= 48 MiB`
- every entry: `title` + valid magnet btih, numeric `size`/`size_bytes > 0`,
  `cover` https, `screenshots <= 6` https, `title_id` empty or valid base id
  (`(id & 0xFFF) == 0`), `topic_id` numeric when present
- `manifest.json` sha256/bytes/entries match the built `catalog.json`

## Schema notes

- Compat keys stay on the same names the client parses (`title`, `magnet`,
  `cover`, `size` numeric, `topic_id` numeric, `published_date` sort key).
- `size_bytes` is canonical; `size` duplicates it numerically for the
  current client.
- `info_dict` is intentionally **not emitted** (torrent metadata vs hash,
  size); the client still reads it from old caches.
- `magnetURI`/`poster` are read-only legacy aliases, never emitted.
