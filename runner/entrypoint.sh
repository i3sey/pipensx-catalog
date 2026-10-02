#!/bin/sh
# pipensx-catalog runner entrypoint. One run = one tick (the host cron
# starts a fresh container per tick; nothing daemonic lives inside).
#
#   MODE=run    scrape + merge + publish when changed (default)
#   MODE=probe  transport matrix only, publishes nothing
#
# Traffic thrift: the remote manifest (~500 B) is checked first; the
# 23 MB snapshot downloads only when its sha256 actually changed, and
# /state persists between ticks. A tick costs ~topics*page + overhead.
set -u

REPO="${GITHUB_REPO:-i3sey/pipensx-catalog}"
STATE="${STATE_DIR:-/state}"
OUT=/tmp/run-out
WINDOW="${TOPICS_WINDOW:-60}"
DELAY="${SCRAPE_DELAY:-3}"
TIMEOUT="${SCRAPE_TIMEOUT:-30}"
if [ $# -gt 0 ]; then
    MODE="$1"
fi

log() { echo "runner: $*"; }

if [ "${MODE:-run}" = "probe" ]; then
    exec python3 <<'EOF'
import os, urllib.request
url = "https://rutracker.org/forum/viewtopic.php?t=6892780"
for name, proxy, cookie in [
        ("direct", "", ""),
        ("proxy", os.environ.get("SCRAPER_PROXY", ""), ""),
        ("cookie", "", os.environ.get("RUTRACKER_COOKIE", "")),
        ("proxy+cookie", os.environ.get("SCRAPER_PROXY", ""),
         os.environ.get("RUTRACKER_COOKIE", ""))]:
    if "proxy" in name and not proxy:
        print(f"{name}: skipped (no SCRAPER_PROXY)");
        continue
    if "cookie" in name and not cookie:
        print(f"{name}: skipped (no RUTRACKER_COOKIE)");
        continue
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler(
            {"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    headers = {"User-Agent": "Mozilla/5.0"}
    if cookie:
        headers["Cookie"] = cookie
    try:
        with opener.open(urllib.request.Request(url, headers=headers),
                          timeout=25) as r:
            body = r.read()
            print(f"{name}: HTTP {r.status} bytes={len(body)} "
                  f"challenge={b'Just a moment' in body}")
    except Exception as e:  # noqa: BLE001 - probe reports, never fails
        print(f"{name}: ERROR {e}")
EOF
fi

mkdir -p "$STATE" "$OUT"
rm -rf "${OUT:?}/"* || true

if ! curl -fsSL --max-time 60 \
    "https://github.com/${REPO}/releases/latest/download/manifest.json" \
    -o "$OUT/remote-manifest.json"; then
    log "remote manifest unreachable, nothing to do"
    exit 0
fi
REMOTE_SHA="$(python3 -c "
import json;print(json.load(open('$OUT/remote-manifest.json'))['catalog']['sha256'])")"
LOCAL_SHA=""
if [ -s "$STATE/manifest.json" ] && [ -s "$STATE/catalog.json" ]; then
    LOCAL_SHA="$(python3 -c "
import json;print(json.load(open('$STATE/manifest.json'))['catalog']['sha256'])")"
fi
if [ -n "$LOCAL_SHA" ] && [ "$LOCAL_SHA" = "$REMOTE_SHA" ] \
    && [ -z "${TOPICS_FORCE:-}" ]; then
    log "state already at $REMOTE_SHA, refreshing topics only"
    PREV="$STATE/catalog.json"
    PREV_MANIFEST="$STATE/manifest.json"
else
    log "fetching snapshot for $REMOTE_SHA"
    if ! curl -fsSL --max-time 300 \
        "https://github.com/${REPO}/releases/latest/download/catalog.json" \
        -o "$STATE/catalog.json"; then
        log "snapshot download failed, keeping previous state (if any)"
        if [ -s "$STATE/catalog.json" ] && [ -s "$STATE/manifest.json" ]; then
            PREV="$STATE/catalog.json"
            PREV_MANIFEST="$STATE/manifest.json"
        else
            log "no usable state, nothing to do"
            exit 1
        fi
    else
        cp "$OUT/remote-manifest.json" "$STATE/manifest.json"
        PREV="$STATE/catalog.json"
        PREV_MANIFEST="$STATE/manifest.json"
    fi
fi

if [ -n "${TOPICS:-}" ]; then
    TOPIC_LIST="$TOPICS"
else
    TOPIC_LIST="$(python3 -c "
import json
prev = json.load(open('$PREV'))
tids = sorted({str(e.get('topic_id')) for e in prev if e.get('topic_id')})
import time
start = ((int(time.time()) // 86400) * $WINDOW) % max(1, len(tids))
print(','.join((tids + tids)[start:start + $WINDOW]))")"
fi

# Coverage veto exits 2: a shrunk merge fails loudly, publishes nothing.
python3 /app/scrape.py \
    --previous "$PREV" \
    --previous-manifest "$PREV_MANIFEST" \
    --output "$OUT" \
    --overrides /app/overrides.json \
    --topics "$TOPIC_LIST" \
    --delay "$DELAY" \
    --timeout "$TIMEOUT" \
    --catalog-commit "vps-runner"
SCRAPE_RC=$?
if [ "$SCRAPE_RC" -eq 2 ]; then
    log "VETO from coverage gate, not publishing"
    exit 2
elif [ "$SCRAPE_RC" -ne 0 ]; then
    log "scrape failed rc=$SCRAPE_RC"
    exit "$SCRAPE_RC"
fi

NEW_SHA="$(python3 -c "
import json;print(json.load(open('$OUT/manifest.json'))['catalog']['sha256'])")"
if [ "$NEW_SHA" = "$REMOTE_SHA" ] && [ -z "${FORCE_PUBLISH:-}" ]; then
    log "unchanged ($NEW_SHA), state refreshed, no publish"
    cp "$OUT/catalog.json" "$STATE/catalog.json"
    cp "$OUT/manifest.json" "$STATE/manifest.json"
    exit 0
fi
if [ -z "${GITHUB_TOKEN:-}" ]; then
    log "changed ($NEW_SHA) but no GITHUB_TOKEN, leaving files in $OUT"
    exit 0
fi

TAG="catalog-${NEW_SHA:0:12}-vps-$(date +%s)"
log "publishing $TAG"
export TAG NEW_SHA REPO OUT
python3 - <<'EOF'
import json, os, urllib.request
token = os.environ["GITHUB_TOKEN"]
repo = os.environ["REPO"]
tag = os.environ["TAG"]
out = os.environ["OUT"]

def api(url, data=None, raw=None, ctype="application/json"):
    req = urllib.request.Request(
        url, data=data,
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "Content-Type": ctype,
                 "User-Agent": "pipensx-catalog-runner/1"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)

release = api(f"https://api.github.com/repos/{repo}/releases", json.dumps({
    "tag_name": tag, "name": f"Catalog {tag}",
    "body": f"VPS runner merge sha {os.environ['NEW_SHA']}",
    "make_latest": True}).encode())
upbase = release["upload_url"].split("{")[0]
for name in ("catalog.json", "manifest.json", "report.md"):
    with open(f"{out}/{name}", "rb") as f:
        api(f"{upbase}?name={name}", f.read(),
            ctype="application/octet-stream")
    print(f"runner: uploaded {name}")
print(f"runner: released {tag}")
EOF

log "pushing snapshot to main"
rm -rf /tmp/repo && git clone --depth 1 \
    "https://x-access-token:${GITHUB_TOKEN}@github.com/${REPO}.git" /tmp/repo
cp "$OUT/catalog.json" "$OUT/manifest.json" "$OUT/report.md" /tmp/repo/
cd /tmp/repo
git config user.name "pipensx-catalog-runner"
git config user.email "runner@local"
git add catalog.json manifest.json report.md
if git diff --quiet --cached; then
    log "snapshot already current"
else
    git commit -m "data: refresh catalog snapshot ($TAG)" && git push origin HEAD:main
fi
cp "$OUT/catalog.json" "$STATE/catalog.json"
cp "$OUT/manifest.json" "$STATE/manifest.json"
log "done"
