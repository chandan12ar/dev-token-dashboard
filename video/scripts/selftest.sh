#!/usr/bin/env bash
# Prove the whole pipeline works on this machine in ~1-2 minutes, before spending 20 on a real build.
#   bash scripts/selftest.sh [--engine kokoro|edge|flite|auto] [--voice NAME]
# Serves the bundled demo app, drafts a storyboard from it, records the bundled storyboard (with blur,
# word-highlighted captions, strict targets), then checks loudness/sync and writes a contact sheet.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; ASSETS="$HERE/../assets/demo-site"
OUT="${SELFTEST_OUT:-$(mktemp -d)}"; PORT="${SELFTEST_PORT:-8787}"
ENGINE_ARGS=("$@"); [ ${#ENGINE_ARGS[@]} -eq 0 ] && ENGINE_ARGS=(--engine auto)
CHROMIUM_ARGS=(); [ -n "${CHROMIUM_PATH:-}" ] && CHROMIUM_ARGS=(--chromium "$CHROMIUM_PATH")

python3 -m http.server "$PORT" -d "$ASSETS" >/dev/null 2>&1 &
SRV=$!; trap 'kill $SRV 2>/dev/null || true' EXIT
for _ in $(seq 30); do curl -sf "http://127.0.0.1:$PORT/" >/dev/null && break; sleep 0.3; done

echo "== 1/4 draft storyboard + brand from the app"
python3 "$HERE/draft_storyboard.py" --url "http://127.0.0.1:$PORT/" --out "$OUT/draft.json" --shots "$OUT/shots" "${CHROMIUM_ARGS[@]}" \
  --nav "#nav button" --name "Acme Reports"
python3 "$HERE/extract_brand.py" --url "http://127.0.0.1:$PORT/" --out "$OUT/brand.json" "${CHROMIUM_ARGS[@]}"
echo "== 2/4 record the bundled storyboard"
python3 "$HERE/build_video.py" --script "$ASSETS/script.json" --url "http://127.0.0.1:$PORT/" --config-js "$ASSETS/cfg.js" \
  --blur-auto --brand "$OUT/brand.json" --size 1280x720 --out "$OUT/selftest.mp4" --work "$OUT/work" "${ENGINE_ARGS[@]}" "${CHROMIUM_ARGS[@]}"
echo "== 3/4 check"
python3 "$HERE/check_video.py" "$OUT/selftest.mp4" --work "$OUT/work" --script "$ASSETS/script.json" --sheet "$OUT/contact.jpg" --frames 12
echo "== 4/4 extras (poster, teaser, vertical, gif)"
python3 "$HERE/make_extras.py" "$OUT/selftest.mp4" --work "$OUT/work" --script "$ASSETS/script.json" --out-dir "$OUT/extras" --teaser-len 20
echo; echo "SELFTEST OK. Files in $OUT (open contact.jpg and look at it)"
