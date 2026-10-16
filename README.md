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
  manage_catalog.py    # Unified CLI: status, scrape, export, match-titledb, seed
  flaresolverr_client.py # FlareSolverr API client with session recovery & exponential backoff
  storage.py           # SQLite storage with resume support (topics, topic_details, catalog_entries)
  extractor.py         # TitleID regex [0100...], Base/Update/DLC mapping, TitleDB matcher
  scraper.py           # RuTracker scraper orchestrator (--full / --update)
  export_catalog.py    # Artifact generator (catalog.json, manifest.json, report.md)
  requirements.txt     # Lightweight dependencies (curl-cffi, jsonschema)
  overrides.json       # Manual topic_id / rename / hide fixes
  schema.json          # JSON Schema v2 (CI gate)
  catalog.json         # Latest built artifact (7,094 entries, 27.6 TB)
  manifest.json        # Release manifest (sha256/bytes/entries)
  report.md            # Coverage, drops, diff counters
  tests/               # Unit tests (storage, extractor, flaresolverr, scraper, export, CLI)
  Dockerfile           # Lightweight runner image for VPS
  runner/              # entrypoint.sh, docker-compose.yml, env.example, README.md
  .github/workflows/
    docker-ci.yml      # CI/CD: runs unit tests and pushes image to GHCR
```

## CLI Usage (manage_catalog.py)

```bash
# View current database statistics
python3 manage_catalog.py status --db catalog.db

# Seed database from existing catalog.json
python3 manage_catalog.py seed --file catalog.json --db catalog.db

# Match missing TitleIDs against pipensx-metadata TitleDB
python3 manage_catalog.py match-titledb --db catalog.db

# Incremental scrape of recent topics (e.g. daily cron)
python3 manage_catalog.py scrape --update --pages 2 --db catalog.db

# Full forum crawl with automatic progress resume
python3 manage_catalog.py scrape --full --delay 1.5 --db catalog.db

# Export catalog.json, manifest.json, report.md
python3 manage_catalog.py export --db catalog.db --output output
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

The current `catalog.json` is the bootstrap snapshot. Future updates no
longer fetch Langegen's JSON: GitHub Actions discovers recent topics from
RuTracker's own Switch Atom feed and enriches them from the topic pages.

## Scraper (Atom + topic-page enrichment)

```bash
python3 scrape.py \
  --previous catalog.json --previous-manifest manifest.json \
  --output output --atom --topics 6892780,6877101
```

- Atom: `https://feed.rutracker.cc/atom/f/1605.atom` supplies recent topic
  IDs, titles, update timestamps, approximate title sizes, and magnet
  enclosures. The feed has a rolling 50-entry window; it is for discovery,
  not historical backfill.
- Enrichment: each discovered topic is fetched for its cover, screenshots,
  description, size, language/developer fields, and Title ID. If a known
  topic page is blocked, its Atom title/magnet can still refresh the existing
  row while retaining its previous rich metadata (rounded feed size only
  fills a missing size). A new row without a page-derived cover is held out
  rather than published incomplete.
- Merge: topic ID is the primary key, then entries are deduplicated by
  infohash. A re-upload with a new hash replaces the old row. Empty fields
  from a partial fetch never erase existing metadata. The 98% coverage gate
  prevents accidental catalog shrinkage.
- Topic-page transport uses `curl_cffi` browser impersonation when installed
  (stdlib compatibility request otherwise), then a persistent
  `undetected_chromedriver` session on a challenge/invalid page.
  The browser path waits for forum content, tries a visible Turnstile control,
  and reuses that browser for the rest of the run. `SCRAPER_PROXY` is optional.
- `RUTRACKER_COOKIE` is an optional authentication Cookie header. Atom requests
  never receive it; the browser fallback does not transplant an old
  `cf_clearance` from another client, and instead lets its own browser session
  obtain clearance. Store cookies only in an ignored local env file or GitHub
  secrets, never in commits or chat.
- `requirements.txt` installs the lightweight TLS-impersonation client.
  Browser fallback additionally needs `requirements-browser.txt` and a local
  Chrome/Chromium binary. The current small Docker image intentionally omits
  Chrome; it cannot run the browser fallback until built with browser support
  and given substantially more memory.
- Local one-topic transport probe (does not publish):
  `python3 -m venv .venv && .venv/bin/pip install -r requirements-browser.txt`
  followed by `.venv/bin/python scripts/probe_rutracker.py`. It uses the first
  current Atom item and the configured transport; `RUTRACKER_COOKIE` may be
  supplied through the local environment. A successful Atom request alone
  does not prove topic-page enrichment will work.
- `mode: probe` reports basic connectivity without publishing. The default
  lightweight Docker image has `curl_cffi` but no Chrome/UC installation, so
  it cannot perform the browser fallback described above.
- GitHub Actions `build-catalog.yml` is manual-only (`workflow_dispatch`), used
  for connectivity probes and explicit IDs. Daily scheduled catalog updates
  are fully delegated to the VPS runner via Docker & FlareSolverr.
- The feed host's `robots.txt` currently disallows crawlers. Automated polling
  is therefore kept to a low daily frequency; check RuTracker's rules before
  enabling a persistent schedule.

## VPS Runner (Docker & Cron)

Runs on any host with Docker and publishes new releases to GitHub Releases:

```bash
cd runner
cp env.example .env
chmod 600 .env
# Fill GITHUB_TOKEN and RUTRACKER_COOKIE in .env
docker compose up --build
```

Host cron (e.g. daily at 03:30):

```cron
30 3 * * * cd /opt/pipensx-catalog/runner && docker compose pull catalog-runner && docker compose up --abort-on-container-exit >> /var/log/catalog-runner.log 2>&1
```

- FlareSolverr handles Cloudflare challenges and passes authenticated session cookies.
- Runner updates SQLite state in persistent volume `catalog-data:/state`.
- Creates a new GitHub Release (`releases/latest`) only when content changed.
- Does not pollute `main` git history with heavy commits.

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
