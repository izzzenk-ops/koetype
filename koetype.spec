# こえタイプ.app を PyInstaller で組み立てる設定。
# シェルスクリプト経由だとアプリがPython.appの身分を名乗ってしまい、
# システム設定のアクセシビリティに「こえタイプ」として並ばない。
# 独立した実行ファイルを持つ .app にするために PyInstaller を使う。

a = Analysis(
    ["src/koetype.py"],
    pathex=[],
    binaries=[],
    datas=[("icon/koetype_rec.png", "."), ("icon/koetype_blue.png", "."),
           ("icon/koetype_menu.png", "."), ("icon/koetype_menu_blue.png", ".")],
    hiddenimports=["rumps", "Quartz", "AppKit", "Foundation", "objc", "AVFoundation", "ApplicationServices", "PyObjCTools", "PyObjCTools.AppHelper"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "numpy", "PIL"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="koetype",
    debug=False,
    strip=False,
    upx=False,
    console=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="koetype")

app = BUNDLE(
    coll,
    name="こえタイプ.app",
    icon="icon/koetype.icns",
    bundle_identifier="com.otolab.koetype",
    version="1.2.0",   # 上げ忘れると受講生が新旧を見分けられない
    info_plist={
        "CFBundleName": "こえタイプ",
        "CFBundleDisplayName": "こえタイプ",
        "LSUIElement": True,          # Dockに出さない。メニューバーだけに常駐する
        "LSMinimumSystemVersion": "13.0",
        "NSMicrophoneUsageDescription": "話した内容を文字にするためにマイクを使います。",
        "NSAppleEventsUsageDescription": "清書した文章をカーソル位置に貼り付けるために使います。",
        "NSHighResolutionCapable": True,
    },
)
