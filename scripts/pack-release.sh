#!/bin/bash
# Crea dist/quelo-palinsesto-radio-<VERSION>.{tar.gz,zip,rar}
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VER="$(tr -d '[:space:]' < "$ROOT/VERSION")"
NAME="quelo-palinsesto-radio-${VER}"
DIST="$ROOT/dist"
STAGE="$DIST/.stage"
OUT_DIR="$STAGE/$NAME"

rm -rf "$STAGE"
mkdir -p "$OUT_DIR" "$DIST"

rsync -a \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude 'dist/' \
  --exclude '.stage/' \
  --exclude '{c\[end_ts\]}' \
  --exclude 'docs/github_assets/gui/emb-*' \
  --exclude 'docs/github_assets/gui/page-*' \
  --exclude 'docs/github_assets/gui/gui-main-crop.png' \
  "$ROOT/" "$OUT_DIR/"

# file spurio eventuale (nome con parentesi)
rm -f "$OUT_DIR/{c[end_ts]}"

chmod +x "$OUT_DIR/bin/quelo-palinsesto-radio" 2>/dev/null || true
chmod +x "$OUT_DIR/bin/"* 2>/dev/null || true

(
  cd "$STAGE"
  tar -czf "$DIST/${NAME}.tar.gz" "$NAME"
  zip -qr "$DIST/${NAME}.zip" "$NAME"
  rar a -r -idq "$DIST/${NAME}.rar" "$NAME" >/dev/null
)

rm -rf "$STAGE"
ls -lh "$DIST/${NAME}".*
echo "OK: $DIST/${NAME}.{tar.gz,zip,rar}"
