#!/bin/sh
# pipensx-catalog VPS runner entrypoint
# Executes one update cycle:
# 1. Seeds SQLite /state/catalog.db if first run
# 2. Waits for FlareSolverr service
# 3. Runs scrape --update for recent RuTracker topics
# 4. Enriches missing TitleIDs via TitleDB
# 5. Exports catalog and validates against schema.json
# 6. Publishes to GitHub Releases (latest) if changed or forced
set -eu

REPO="${GITHUB_REPO:-i3sey/pipensx-catalog}"
STATE_DIR="${STATE_DIR:-/state}"
OUT_DIR="/tmp/out"
DB_PATH="${STATE_DIR}/catalog.db"
FLARESOLVERR_HOST="${FLARESOLVERR_HOST:-flaresolverr}"
FLARESOLVERR_URL="http://${FLARESOLVERR_HOST}:8191/v1"
PAGES="${PAGES:-5}"
DELAY="${DELAY:-1.5}"
FORCE_PUBLISH="${FORCE_PUBLISH:-0}"

log() {
    echo "$(date '+%H:%M:%S') [runner] $*"
}

# Parse CLI arguments for flags
for arg in "$@"; do
    case "$arg" in
        --force|-f|force)
            FORCE_PUBLISH=1
            ;;
        --probe|probe)
            log "Probe mode requested. Testing FlareSolverr connection..."
            if curl -s -X POST "$FLARESOLVERR_URL" -H "Content-Type: application/json" -d '{"cmd": "sessions.list"}' >/dev/null 2>&1; then
                log "FlareSolverr connection at $FLARESOLVERR_URL: OK"
            else
                log "FlareSolverr connection at $FLARESOLVERR_URL: FAILED"
            fi
            exit 0
            ;;
    esac
done

if [ "$FORCE_PUBLISH" = "1" ] || [ "$FORCE_PUBLISH" = "true" ] || [ "$FORCE_PUBLISH" = "yes" ]; then
    log "=================================================================="
    log ">>> FORCE_PUBLISH ACTIVE: Will publish GitHub release unconditionally"
    log "=================================================================="
    FORCE_PUBLISH=1
fi

mkdir -p "$STATE_DIR" "$OUT_DIR"
rm -rf "${OUT_DIR:?}"/* 2>/dev/null || true

# 1. Sync baseline catalog into database (on first run or updating existing DB)
if [ -f "/app/catalog.json" ]; then
    log "Syncing baseline entries from /app/catalog.json into $DB_PATH..."
    python3 /app/manage_catalog.py seed --file /app/catalog.json --db "$DB_PATH"
elif [ ! -f "$DB_PATH" ]; then
    log "Database not found in $DB_PATH. Downloading latest catalog.json release from GitHub (${REPO})..."
    if curl -fsSL --max-time 180 "https://github.com/${REPO}/releases/latest/download/catalog.json" -o /tmp/bootstrap-catalog.json 2>/dev/null; then
        python3 /app/manage_catalog.py seed --file /tmp/bootstrap-catalog.json --db "$DB_PATH"
        rm -f /tmp/bootstrap-catalog.json
    else
        log "Notice: Could not download remote bootstrap, starting with fresh empty database."
    fi
    log "Running initial TitleDB enrichment..."
    python3 /app/manage_catalog.py match-titledb --db "$DB_PATH"
else
    log "Existing database found in $DB_PATH."
fi

# Print current DB status
python3 /app/manage_catalog.py status --db "$DB_PATH"

# 2. Wait for FlareSolverr to be ready
log "Waiting for FlareSolverr at $FLARESOLVERR_URL..."
MAX_WAIT=60
WAITED=0
while [ "$WAITED" -lt "$MAX_WAIT" ]; do
    if curl -s -X POST "$FLARESOLVERR_URL" -H "Content-Type: application/json" -d '{"cmd": "sessions.list"}' >/dev/null 2>&1; then
        log "FlareSolverr is ready!"
        break
    fi
    sleep 2
    WAITED=$((WAITED + 2))
done

if [ "$WAITED" -ge "$MAX_WAIT" ]; then
    log "ERROR: FlareSolverr service not available at $FLARESOLVERR_URL after ${MAX_WAIT}s"
    exit 1
fi

# 3. Check remote release manifest sha
REMOTE_SHA=""
if curl -fsSL --max-time 30 "https://github.com/${REPO}/releases/latest/download/manifest.json" -o /tmp/remote-manifest.json 2>/dev/null; then
    REMOTE_SHA="$(python3 -c "import json; print(json.load(open('/tmp/remote-manifest.json')).get('catalog', {}).get('sha256', ''))" 2>/dev/null || true)"
    log "Current GitHub latest release SHA: ${REMOTE_SHA:-none}"
else
    log "No existing GitHub latest release found or unreachable."
fi

# 4. Run incremental scrape
log "Running incremental update (pages=${PAGES}, delay=${DELAY}s)..."
python3 /app/manage_catalog.py scrape --update --pages "$PAGES" --delay "$DELAY" --db "$DB_PATH" --flaresolverr-url "$FLARESOLVERR_URL"

# 5. Run TitleDB matcher for any newly added topics without TitleID
log "Running TitleDB matcher..."
python3 /app/manage_catalog.py match-titledb --db "$DB_PATH"

# 6. Export catalog artifacts
log "Exporting catalog..."
python3 /app/manage_catalog.py export --db "$DB_PATH" --output "$OUT_DIR" --schema /app/schema.json --overrides /app/overrides.json

if [ ! -f "$OUT_DIR/manifest.json" ] || [ ! -f "$OUT_DIR/catalog.json" ]; then
    log "ERROR: Exported files not found in $OUT_DIR"
    exit 1
fi

NEW_SHA="$(python3 -c "import json; print(json.load(open('$OUT_DIR/manifest.json'))['catalog']['sha256'])")"
ENTRIES="$(python3 -c "import json; print(json.load(open('$OUT_DIR/manifest.json'))['catalog']['entries'])")"
BYTES="$(python3 -c "import json; print(json.load(open('$OUT_DIR/manifest.json'))['catalog']['bytes'])")"
log "Built catalog SHA: $NEW_SHA ($ENTRIES entries, $BYTES bytes)"

# 7. Check if changed (unless force publish is requested)
if [ -n "$REMOTE_SHA" ] && [ "$NEW_SHA" = "$REMOTE_SHA" ] && [ "$FORCE_PUBLISH" != "1" ]; then
    log "Catalog unchanged (SHA: $NEW_SHA). Nothing to publish. (Pass FORCE_PUBLISH=1 or --force to override)"
    exit 0
fi

if [ -z "${GITHUB_TOKEN:-}" ]; then
    log "WARNING: GITHUB_TOKEN not set. Skipping GitHub release publication."
    log "Catalog files generated in $OUT_DIR."
    exit 0
fi

# 8. Publish to GitHub Releases (only releases, no git commits to main!)
TAG="catalog-$(date -u +%Y%m%d-%H%M%S)"
log "Publishing new GitHub release: $TAG (make_latest: true)..."

export GITHUB_TOKEN REPO TAG NEW_SHA ENTRIES BYTES OUT_DIR
python3 - <<'EOF'
import json
import os
import sys
import urllib.error
import urllib.request

token = os.environ["GITHUB_TOKEN"]
repo = os.environ["REPO"]
tag = os.environ["TAG"]
new_sha = os.environ["NEW_SHA"]
entries = os.environ["ENTRIES"]
bytes_count = os.environ["BYTES"]
out_dir = os.environ["OUT_DIR"]

headers = {
    "Authorization": f"Bearer {token}",
    "Accept": "application/vnd.github+json",
    "User-Agent": "pipensx-catalog-runner/2.0",
}

# Create GitHub Release
create_url = f"https://api.github.com/repos/{repo}/releases"
release_body = {
    "tag_name": tag,
    "name": f"Catalog {tag}",
    "body": (
        f"Automated pipensx-catalog release\n\n"
        f"- **Entries**: {entries}\n"
        f"- **Size**: {int(bytes_count):,} bytes\n"
        f"- **SHA-256**: `{new_sha}`\n"
        f"- **Timestamp**: `{tag}`\n"
    ),
    "draft": False,
    "prerelease": False,
    "make_latest": "true",
}

req = urllib.request.Request(
    create_url,
    data=json.dumps(release_body).encode("utf-8"),
    headers={**headers, "Content-Type": "application/json"},
    method="POST",
)

try:
    with urllib.request.urlopen(req, timeout=60) as resp:
        release_data = json.loads(resp.read().decode("utf-8"))
except urllib.error.HTTPError as exc:
    err_msg = exc.read().decode("utf-8", errors="replace")
    print(f"runner: Failed to create release HTTP {exc.code}: {err_msg}", file=sys.stderr)
    sys.exit(1)
except Exception as exc:
    print(f"runner: Failed to create release: {exc}", file=sys.stderr)
    sys.exit(1)

html_url = release_data.get("html_url", f"https://github.com/{repo}/releases/tag/{tag}")
upload_url_template = release_data.get("upload_url", "")
upload_base = upload_url_template.split("{")[0]

print(f"runner: Created GitHub Release object: {html_url}")

# Upload catalog.json, manifest.json, report.md
assets = ["catalog.json", "manifest.json", "report.md"]
for asset_name in assets:
    file_path = os.path.join(out_dir, asset_name)
    if not os.path.exists(file_path):
        continue
    with open(file_path, "rb") as f:
        file_bytes = f.read()

    upload_url = f"{upload_base}?name={asset_name}"
    upload_req = urllib.request.Request(
        upload_url,
        data=file_bytes,
        headers={**headers, "Content-Type": "application/octet-stream"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(upload_req, timeout=120) as uresp:
            print(f"runner: [OK] Uploaded asset '{asset_name}' ({len(file_bytes):,} bytes)")
    except Exception as exc:
        print(f"runner: [FAIL] Failed to upload asset {asset_name}: {exc}", file=sys.stderr)
        sys.exit(1)

print("=" * 60)
print(f"runner: SUCCESS! Published release: {html_url}")
print(f"runner: Latest release URL: https://github.com/{repo}/releases/latest")
print("=" * 60)
EOF

log "Job finished successfully."
