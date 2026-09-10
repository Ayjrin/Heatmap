#!/usr/bin/env bash
# Screenshot the running app with headless Chrome, one shot per URL state.
# Used to eyeball the renderer without a manual browser session.
#
#   ./scripts/shoot_ui.sh [outdir] [port]
set -uo pipefail

OUT="${1:-/tmp/ui}"
PORT="${2:-8760}"
WEB="$(cd "$(dirname "$0")/.." && pwd)/web"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

[[ -x "$CHROME" ]] || { echo "Chrome not found at $CHROME" >&2; exit 1; }
[[ -f "$WEB/data/v1/manifest.json" ]] || { echo "no dataset — run build_dataset.py" >&2; exit 1; }

mkdir -p "$OUT"
python3 -m http.server "$PORT" --bind 127.0.0.1 --directory "$WEB" >/dev/null 2>&1 &
SRV=$!
trap 'kill $SRV 2>/dev/null' EXIT
until curl -s -o /dev/null "http://127.0.0.1:$PORT/"; do :; done

shoot() {  # name, querystring
  local name="$1" q="$2"
  "$CHROME" --headless=new --disable-gpu --no-sandbox --hide-scrollbars \
    --force-device-scale-factor=1 --window-size=1560,980 \
    --virtual-time-budget=9000 \
    --screenshot="$OUT/$name.png" \
    "http://127.0.0.1:$PORT/?$q" >/dev/null 2>&1
  if [[ -f "$OUT/$name.png" ]]; then
    printf "  %-22s %s bytes\n" "$name.png" "$(stat -f%z "$OUT/$name.png")"
  else
    printf "  %-22s FAILED\n" "$name.png"
  fi
}

echo "shooting UI states -> $OUT"
shoot 01-deaths       "layer=deaths"
shoot 02-kills        "layer=kills"
shoot 03-danger       "layer=danger&s.role=1"
shoot 04-opportunity  "layer=opportunity&s.role=1"
shoot 05-filtered     "layer=kills&s.role=4&c.cause=0&t=480,1200"
shoot 06-negation     "layer=deaths&s.role=1&s.champ=!266"
echo done
