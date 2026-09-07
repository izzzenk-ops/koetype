#!/bin/bash
# こえタイプを作り直して /Applications に入れる。
# 自分の証明書で署名するので、作り直してもmacOSの許可（マイク・入力監視・
# アクセシビリティ）は外れない。証明書は ~/.config/koetype-signing/ にある。
set -euo pipefail
cd "$(dirname "$0")"
IDENTITY="Koe Type Self Signed"
APP="dist/こえタイプ.app"

pkill -f "MacOS/koetype" 2>/dev/null || true
rm -rf dist build_pyi
.venv/bin/pyinstaller --noconfirm --clean --workpath build_pyi --distpath dist koetype.spec

# 内側のMach-Oを先に、最後に本体。--deep は大きなバンドルで取りこぼすので使わない
find "$APP" -type f \( -name "*.so" -o -name "*.dylib" \) -print0 \
  | xargs -0 -n1 -P4 codesign --force --sign "$IDENTITY" 2>/dev/null || true
codesign --force --sign "$IDENTITY" "$APP/Contents/MacOS/koetype"
codesign --force --sign "$IDENTITY" "$APP"
codesign --verify --strict "$APP"

rm -rf "/Applications/こえタイプ.app"
cp -R "$APP" /Applications/
codesign --verify --strict "/Applications/こえタイプ.app"
echo "できました。指定要件:"
codesign -d -r- "/Applications/こえタイプ.app" 2>&1 | tail -1
open "/Applications/こえタイプ.app"
