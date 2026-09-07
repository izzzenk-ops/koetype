#!/bin/bash
# 受講生に配る用のこえタイプを作る。
# けんじさん個人の証明書ではなく ad-hoc 署名にする（他のMacでも有効な署名になる）。
# curl/git 経由で受け取れば隔離の印が付かないので、Gatekeeperの警告は出ない。
set -euo pipefail
cd "$(dirname "$0")"
APP="dist/こえタイプ.app"
OUT="配布/こえタイプ.zip"

rm -rf dist build_pyi
.venv/bin/pyinstaller --noconfirm --clean --workpath build_pyi --distpath dist koetype.spec

find "$APP" -type f \( -name "*.so" -o -name "*.dylib" \) -print0 \
  | xargs -0 -n1 -P4 codesign --force --sign - 2>/dev/null || true
codesign --force --sign - "$APP/Contents/MacOS/koetype"
codesign --force --sign - "$APP"
codesign --verify --strict "$APP"

mkdir -p 配布
rm -f "$OUT"
ditto -c -k --sequesterRsrc --keepParent "$APP" "$OUT"
echo "できました: $OUT  ($(du -h "$OUT" | cut -f1))"
codesign -d -r- "$APP" 2>&1 | tail -1
