"""こえタイプが使うGeminiモデルの週1点検。
models.json の各モデルと、新しく出たflash-lite系の候補を、同じ10秒の音声で3回ずつ測る。
結果は 点検/結果/YYYY-MM-DD.json に残し、表を標準出力に出す。
無料枠を使う（1回の点検で20回前後）。"""
import base64, json, statistics, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
import koetype as k
import requests

ROUNDS = 3
cfg = k.load_config()
key = cfg["gemini_api_key"]
current = json.loads((HERE.parent / "models.json").read_text())["gemini_models"]

listed = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                      params={"key": key, "pageSize": 1000}, timeout=30).json().get("models", [])
names = [m["name"].split("/")[-1] for m in listed
         if "generateContent" in m.get("supportedGenerationMethods", [])]
# 新しく出た軽量モデル（画像・音声合成用は除く）
fresh = [n for n in names if "flash-lite" in n and not n.startswith("gemini-2")
         and not any(x in n for x in ("tts", "image", "preview"))]
candidates = list(dict.fromkeys(current + fresh))

sp, mime, tmp = k.compress_for_upload(str(HERE / "test10.wav"))
audio = base64.b64encode(Path(sp).read_bytes()).decode()

result = {}
for rnd in range(ROUNDS):
    for m in candidates:
        r = result.setdefault(m, {"秒": [], "失敗": [], "書き方": None})
        for style in ([r["書き方"]] if r["書き方"] else k.THINKING_STYLES):
            body = {"contents": [{"parts": [{"text": k.build_prompt(cfg)},
                                            {"inline_data": {"mime_type": mime, "data": audio}}]}],
                    "generationConfig": {"temperature": 0.2, "thinkingConfig": style}}
            t = time.time()
            try:
                res = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent",
                    params={"key": key}, json=body, timeout=60)
                code = res.status_code
            except requests.RequestException as e:
                code, res = f"通信:{type(e).__name__}", None
            dt = round(time.time() - t, 1)
            if code == 400 and style is not k.THINKING_STYLES[-1] and not r["書き方"]:
                continue
            if code == 200:
                r["秒"].append(dt); r["書き方"] = style
                if res is not None:
                    try:
                        r["中身"] = res.json().get("modelVersion")
                    except ValueError:
                        pass
            else:
                r["失敗"].append(code)
            break

out = HERE / "結果"; out.mkdir(exist_ok=True)
(out / f"{time.strftime('%Y-%m-%d')}.json").write_text(
    json.dumps({"今のmodels.json": current, "結果": result}, ensure_ascii=False, indent=2))

print(f"今の models.json: {current}")
print(f"{'モデル':30s} {'中央値':>6s} {'最大':>6s} 失敗  中身")
for m, r in result.items():
    s = r["秒"]
    med = f"{statistics.median(s):.1f}" if s else "-"
    mx = f"{max(s):.1f}" if s else "-"
    mark = "★今の一覧" if m in current else "  新候補"
    print(f"{m:30s} {med:>6s} {mx:>6s} {len(r['失敗'])}/{ROUNDS} {r.get('中身') or ''} {mark} {r['失敗'] or ''}")
