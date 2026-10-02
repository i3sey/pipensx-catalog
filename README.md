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
  scrape.py          # rutracker fetch + merge (never drops) + coverage gate
  overrides.json     # manual topic_id / rename / hide fixes
  schema.json        # JSON Schema v2 (CI gate)
  catalog.json       # latest built artifact (raw channel)
  manifest.json      # sha256/bytes/entries for the release channel
  report.md          # coverage, drops, diff counters
  tests/test_build_catalog.py
  tests/test_scrape.py
  Dockerfile         # runner image (scrape+publish, see below)
  runner/            # entrypoint.sh, docker-compose.yml, env.example
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

## Scraper (P1)

```bash
python3 scrape.py \
  --previous catalog.json --previous-manifest manifest.json \
  --output output --topics 6892780,6877101
```

- fetch: topic pages (rate-limited, browser UA); section listings via
  `--section ID --section-limit N`; transport fully env-configured:
  `SCRAPER_PROXY` (HTTP/SOCKS proxy) and `RUTRACKER_COOKIE`
  (`bb_session=...`, authenticated session — cookies are not IP-portable).
- anti-bot reality: anonymous fetches answer HTTP 403 with a Cloudflare
  managed challenge. The scraper detects it (`ChallengeBlocked`) and exits 0
  keeping the previous snapshot byte-identical — a blocked run publishes
  nothing. Run the workflow with `mode: probe` for the empirical transport
  matrix (direct / proxy / cookie / proxy+cookie) before relying on CI
  scrapes. Notes from the field (2026-10-02): a `bb_session` cookie alone
  does NOT pass the managed challenge, and environments with rotating
  egress IPs defeat cookies entirely (Cloudflare binds clearance to IP).
  What works: a stable egress (home machine, static-IP VPS, or a
  self-hosted runner) combined with `RUTRACKER_COOKIE`. Until then, run
  `scrape.py` from a home machine and commit the seed — merge keeps it
  incremental.
- merge: scraped raws go through `build_catalog` (overrides apply), then
  merge normalized-over-normalized by infoHash — scraped wins, the rest is
  kept stale and counted in the report. Coverage gate: a merge below 98% of
  the previous snapshot exits 2 (veto — CI fails loudly instead of
  publishing a shrunk catalog).
- CI rotation: each scheduled run refreshes a rotating window of 120 known
  topics (full pass in ~2 months at the 6h cadence); publish only when the
  manifest sha256 changed.

## VPS runner (Docker)

For a stable-egress host (home server, static-IP VPS): one container per
cron tick, nothing daemonic inside. Datacenter IPs are usually challenged
too — the runner then just keeps the previous snapshot; `probe` tells the
truth about your egress.

```bash
cp runner/env.example runner/.env   # fill RUTRACKER_COOKIE / GITHUB_TOKEN
docker compose -f runner/docker-compose.yml --env-file runner/.env up --build
MODE=probe docker compose -f runner/docker-compose.yml --env-file runner/.env up --build  # transport matrix
```

Host cron (every 6h), or `restart: unless-stopped` with your own scheduler:

```cron
0 */6 * * * docker compose -f /opt/pipensx-catalog/runner/docker-compose.yml --env-file /opt/pipensx-catalog/runner/.env up --build --quiet-pull >/var/log/catalog-runner.log 2>&1
```

Thrift (defaults): manifest-first (~500 B; the 23 MB snapshot downloads
only when its sha changed, else the `/state` volume is reused), 60 topics
per tick with 3 s delay (~10–15 MB traffic, ~1–2 min CPU on 1 core,
<512 MB RAM — see compose limits), uploads only when the catalog actually
changed. Needs `GITHUB_TOKEN` (Contents read/write) only for publishing;
scraping alone works without it.

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
