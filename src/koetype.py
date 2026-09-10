#!/usr/bin/env python3
"""こえタイプ — 押して喋ると、AIが清書してカーソル位置に貼り付ける音声入力ツール。

録音・圧縮ともmacOS標準の機能だけで動く（ffmpegなどの追加インストールは不要）。

清書はGeminiの無料枠で行う（モデルごとに枠が別なので、尽きたら次のモデルに回す）。

右Optionキーを押している間だけ録音し、離すと
  録音 → AIが文字起こし＋清書 → クリップボード経由でカーソル位置にペースト
まで自動で走る。Typeless / ぽちペタと同じ使い勝手をローカルで再現したもの。
"""

import array
import base64
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import wave
from pathlib import Path

import AVFoundation as AV
import requests
import rumps
import Quartz
from AppKit import (NSPasteboard, NSStringPboardType, NSPanel, NSView, NSColor,
                    NSImage, NSImageView, NSTextField, NSFont, NSScreen, NSMakeRect,
                    NSBackingStoreBuffered, NSAlert, NSTextView, NSScrollView,
                    NSApplication, NSEventModifierFlagShift,
                    NSMutableParagraphStyle, NSFontAttributeName,
                    NSForegroundColorAttributeName, NSParagraphStyleAttributeName)
from Foundation import NSObject, NSURL, NSMutableAttributedString
import objc
from PyObjCTools import AppHelper

APP_NAME = "こえタイプ"
SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "KoeType"
CONFIG_PATH = SUPPORT_DIR / "config.json"
LOG_PATH = SUPPORT_DIR / "koetype.log"
HISTORY_PATH = SUPPORT_DIR / "history.json"
HISTORY_MAX = 3      # メニューに残す直前の結果の数
PREVIEW_COLS = 30    # メニューの1行に入れる文字数（全角で数えたときの目安）
PREVIEW_LINES = 5    # 1件あたり何行まで見せるか

# 清書の指示。内容は足さない・要約しない、が絶対条件。
POLISH_PROMPT = """あなたは日本語の音声入力を清書する校正者です。
渡された音声を文字起こしし、読みやすい書き言葉に整えてください。

やること:
- 「えー」「あのー」「えっと」「まあ」などのフィラーを消す
- 言い直し・言い淀みは、最終的に言いたかった形に統合する
- 音声認識の誤変換、明らかな言い間違いを直す
- 句読点を打ち、読みやすい長さで文を切る
- 話し言葉の語尾は自然な書き言葉に整える

絶対にやらないこと:
- 話していない内容を足す
- 要約する・短くまとめる
- 箇条書きや見出しに勝手に整形する
- 前置き・後書き・説明を付ける（清書後の本文だけを出力する）

短い一言・単語だけでも、必ずそのまま整えて出力してください。
「意味をなさない」「文脈が分からない」という理由で捨ててはいけません。
本当に一言も聞き取れないときだけ、空文字を返してください。"""

VOCAB_HINT = """
なお、次の語はこの人がよく使う固有名詞です。似た音が出てきたらこの表記に直してください:
{words}"""

FIX_HINT = """
また、次の表記ゆれは必ず右側に直してください:
{fixes}"""

# 修飾キーは「押した」イベントが来ないので、フラグが立ったかどうかで押下を見る。
# キーコードごとに、見るべきフラグが違う。
KEY_FLAG = {
    58: Quartz.kCGEventFlagMaskAlternate,    # 左Option
    61: Quartz.kCGEventFlagMaskAlternate,    # 右Option
    55: Quartz.kCGEventFlagMaskCommand,      # 左Command
    54: Quartz.kCGEventFlagMaskCommand,      # 右Command
    59: Quartz.kCGEventFlagMaskControl,      # 左Control
    62: Quartz.kCGEventFlagMaskControl,      # 右Control
    63: Quartz.kCGEventFlagMaskSecondaryFn,  # fn
}

DEFAULT_CONFIG = {
    "provider": "auto",
    "gemini_api_key": "",
    "gemini_model": "gemini-3.1-flash-lite",
    # 無料枠はモデルごとに別。尽きたら順に回して使える回数を増やす
    "gemini_fallback_models": [
        "gemini-3.1-flash-lite-preview",
        "gemini-3-flash-preview",
        "gemini-flash-latest",
    ],
    "openai_api_key_file": str(
        Path.home() / "Documents" / "Claude" / "Projects" / "tools" / "openai" / ".env"
    ),
    "openai_transcribe_model": "gpt-4o-mini-transcribe",
    "openai_polish_model": "gpt-4.1-nano",
    "hold_key_code": 61,  # 61=右Option, 58=左Option, 63=fn, 54=右Command
    "hold_key_name": "右Option",
    "audio_device": "auto",
    "min_seconds": 0.4,
    "max_seconds": 1800,   # 30分
    "sound_feedback": True,    # 録音が始まった合図（マイクが開いた瞬間のポッ）
    "sound_on_done": False,    # 貼り付け終わりの音。うるさいので既定は鳴らさない
    "debug_keys": False,
    # 直した表記を覚えさせる表。「間違い」→「正しい」
    "corrections": {
        "ぽちぺた": "ぽちペタ",
        "げんきAI": "Genki AI",
        "げんき AI": "Genki AI",
    },
    # よく使う固有名詞。メニューの「言葉を覚えさせる…」から足せる
    "vocabulary": [
        "リール", "インスタ", "ストーリーズ", "フォロワー", "サムネ",
    ],
}


def log(msg):
    SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        # 1MBを超えたら1世代だけ残して切り替える
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 1_000_000:
            LOG_PATH.replace(LOG_PATH.with_suffix(".log.1"))
    except OSError:
        pass
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def load_history():
    """直前の結果を読み込む。アプリを閉じても消えないようにしてある。"""
    try:
        if HISTORY_PATH.exists():
            items = json.loads(HISTORY_PATH.read_text())
            return [i for i in items if isinstance(i, dict) and i.get("text")][:HISTORY_MAX]
    except Exception as e:
        log(f"履歴を読めませんでした: {e}")
    return []


def save_history(items):
    """本人しか読めない権限で保存する（喋った内容が入るため）。"""
    try:
        SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
        tmp = HISTORY_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(items[:HISTORY_MAX], ensure_ascii=False, indent=2))
        os.chmod(tmp, 0o600)
        os.replace(tmp, HISTORY_PATH)
    except Exception as e:
        log(f"履歴を保存できませんでした: {e}")


# 行の頭に来ると読みにくい記号。前の行の末尾に付けて逃がす
NO_LINE_HEAD = "、。，．」』）】〉…！？!?ゝ々ー"


def wrap_preview(text, cols=PREVIEW_COLS, lines=PREVIEW_LINES):
    """メニューに収まる幅で本文を折り返す。全角は2、半角は1として数える。"""
    flat = " ".join(text.split())
    limit = cols * 2
    out, cur, width = [], "", 0
    for ch in flat:
        w = 2 if unicodedata.east_asian_width(ch) in "WFA" else 1
        if width + w > limit:
            if ch in NO_LINE_HEAD:       # 句読点だけが次の行に落ちるのを防ぐ
                cur += ch
                continue
            out.append(cur)
            if len(out) == lines:
                return out[:-1] + [out[-1][:-1] + "…"]
            cur, width = "", 0
        cur += ch
        width += w
    if cur:
        out.append(cur)
    return out or [""]


def sweep_temp_files():
    """前回の異常終了などで残った録音ファイルを片づける。1時間より古いものだけ。"""
    limit = time.time() - 3600
    removed = 0
    for path in Path(tempfile.gettempdir()).glob("koetype_*"):
        try:
            if path.stat().st_mtime < limit:
                path.unlink()
                removed += 1
        except OSError:
            pass
    if removed:
        log(f"残っていた一時ファイルを{removed}件片づけました")


def save_config(cfg):
    """一時ファイルに書いてから差し替える。書き込み中に落ちても壊れない。
    APIキーが入るので本人だけが読める権限にする。"""
    SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG_PATH)


def load_config():
    SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text()))
        except Exception as e:
            # 壊れたファイルを消してしまうとAPIキーが失われる。必ず取っておく
            backup = CONFIG_PATH.with_name(
                f"config.json.broken-{time.strftime('%Y%m%d-%H%M%S')}")
            try:
                CONFIG_PATH.replace(backup)
                log(f"configが壊れていたので {backup.name} に退避しました: {e}")
            except OSError:
                log(f"config読み込み失敗（既定値で起動）: {e}")
    save_config(cfg)
    return cfg


# ---------------------------------------------------------------- 録音

class Recorder:
    """macOS標準の録音機能（AVFoundation）でマイクを録る。ffmpegは要らない。
    無音判定は今までどおりWAVを直接読むので、16kHz・16bit・モノラルで録る。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.session = None
        self.output = None
        self.path = None
        self.started_at = 0.0
        self.device_label = "?"
        self.ready = threading.Event()      # マイクが実際に開いたら立つ
        self._finished = threading.Event()  # ファイルが閉じたら立つ
        self._delegate = None

    def _pick_device(self):
        want = self.cfg["audio_device"]
        devices = AV.AVCaptureDevice.devicesWithMediaType_(AV.AVMediaTypeAudio) or []
        if want != "auto":
            for d in devices:
                if d.localizedName() == want:
                    return d
            log(f"指定のマイク「{want}」が見つからないので既定を使います")
        d = AV.AVCaptureDevice.defaultDeviceWithMediaType_(AV.AVMediaTypeAudio)
        return d or (devices[0] if devices else None)

    def start(self):
        if self.session:
            return
        fd, path = tempfile.mkstemp(suffix=".wav", prefix="koetype_")
        os.close(fd)
        os.unlink(path)      # 録音先に既存ファイルがあると失敗する
        self.path = path
        self.started_at = time.time()
        self.ready.clear()
        self._finished.clear()

        device = self._pick_device()
        if device is None:
            raise RuntimeError("マイクが見つかりません")
        self.device_label = device.localizedName()

        session = AV.AVCaptureSession.alloc().init()
        dev_in, err = AV.AVCaptureDeviceInput.deviceInputWithDevice_error_(device, None)
        if dev_in is None or not session.canAddInput_(dev_in):
            raise RuntimeError(f"マイクを開けません: {err}")
        session.addInput_(dev_in)

        output = AV.AVCaptureAudioFileOutput.alloc().init()
        if not session.canAddOutput_(output):
            raise RuntimeError("録音先を作れません")
        session.addOutput_(output)
        # 無音判定がWAVを直接読むので、16bit・16kHz・モノラルで固定する
        output.setAudioSettings_({
            "AVFormatIDKey": 1819304813,      # kAudioFormatLinearPCM
            "AVSampleRateKey": 16000.0,
            "AVNumberOfChannelsKey": 1,
            "AVLinearPCMBitDepthKey": 16,
            "AVLinearPCMIsFloatKey": False,
            "AVLinearPCMIsBigEndianKey": False,
            "AVLinearPCMIsNonInterleaved": False,
        })

        self._delegate = _RecordingDelegate.alloc().initWithRecorder_(self)
        session.startRunning()
        output.startRecordingToOutputFileURL_outputFileType_recordingDelegate_(
            NSURL.fileURLWithPath_(path), AV.AVFileTypeWAVE, self._delegate)
        self.session, self.output = session, output

    def stop(self):
        """録音を止めて wav のパスを返す。短すぎる場合は None。"""
        if not self.session:
            return None
        duration = time.time() - self.started_at
        try:
            self.output.stopRecording()
            self._finished.wait(timeout=5)   # ファイルが閉じるまで待つ
        finally:
            try:
                self.session.stopRunning()
            except Exception as e:
                log(f"録音の後始末で問題: {e}")
            self.session = self.output = self._delegate = None

        path = self.path
        self.path = None
        if duration < self.cfg["min_seconds"]:
            _unlink(path)
            return None
        if not path or not os.path.exists(path) or os.path.getsize(path) < 2000:
            _unlink(path)
            return None
        return path


class _RecordingDelegate(NSObject):
    """録音が本当に始まった／終わったを受け取る。"""

    def initWithRecorder_(self, recorder):
        self = objc.super(_RecordingDelegate, self).init()
        self._rec = recorder
        return self

    def captureOutput_didStartRecordingToOutputFileAtURL_fromConnections_(
            self, output, url, connections):
        self._rec.ready.set()

    def captureOutput_didFinishRecordingToOutputFileAtURL_fromConnections_error_(
            self, output, url, connections, error):
        if error is not None:
            log(f"録音の終了時にエラー: {error}")
        self._rec.ready.set()
        self._rec._finished.set()


# 無音を投げると文字起こしAIが「ご視聴ありがとうございました」等を捏造するので、
# 音量を測って手前で弾く。それでも出た定型句は最後に落とす。
HALLUCINATIONS = {
    "ご視聴ありがとうございました", "ご視聴ありがとうございました。",
    "ご清聴ありがとうございました", "ご清聴ありがとうございました。",
    "チャンネル登録お願いします", "チャンネル登録よろしくお願いします",
    "おわり", "終わり", "。", "ありがとうございました", "ありがとうございました。",
}
class SilentAudio(Exception):
    """録れた音に声が入っていなかった。マイクの選択ミスもここで気づける。"""

    def __init__(self, db, spread):
        self.db = db
        self.spread = spread
        super().__init__(f"{db:.1f}dB / 起伏{spread:.1f}dB")


# 平均音量だけでは声と暗騒音を分けられない。MacBook Air本体マイクは自動ゲインが効くため、
# 無音でも平均 -43dB まで持ち上がり、実測では発話（-42dB）と1dBしか違わなかった。
# 代わりに「静かなときと大きいときの差（起伏）」を見る。
# 実測: 無音3.8dB / スピーカー越しの音声14.6dB。
SILENCE_DB = -60.0       # これより静かなら文字通りの無音（ゲインが効かないマイク用）
SPEECH_SPREAD_DB = 7.0   # 起伏がこれ未満なら「声が入っていない」と判断する
FRAME_MS = 30


def audio_levels(path):
    """(平均dB, 起伏dB) を返す。測れなければ (None, None)。

    起伏 = 上位フレーム － 下から2割のフレーム。
    上位は「フレーム数の5%か3個の多い方」番目を採る。キー打鍵1発（1〜2フレーム）では
    立たず、0.1秒以上の発声で立つ。
    """
    try:
        with wave.open(path) as w:
            if w.getsampwidth() != 2:
                raise ValueError("16bit以外")
            rate = w.getframerate()
            data = array.array("h", w.readframes(w.getnframes()))
        if not data:
            return None, None
        # 長時間の録音でも一瞬で終わるよう、最大20万点に間引いて測る
        step = max(1, len(data) // 200_000)
        sample = data[::step]
        rms = math.sqrt(sum(x * x for x in sample) / len(sample))
        mean = 20 * math.log10(rms / 32768) if rms else -99.0

        n = max(1, int(rate * FRAME_MS / 1000))
        frames = []
        for i in range(0, len(data) - n + 1, n):
            chunk = data[i:i + n]
            r = math.sqrt(sum(x * x for x in chunk) / len(chunk))
            frames.append(20 * math.log10(r / 32768) if r else -99.0)
        if len(frames) < 6:          # 0.2秒未満。起伏では判定できないので通す
            return mean, 99.0
        frames.sort()
        high = frames[-max(3, len(frames) // 20)]
        low = frames[len(frames) // 5]
        return mean, high - low
    except Exception as e:
        log(f"音量を測れませんでした: {e}")
        return None, None


def drop_hallucination(text):
    return "" if text.strip() in HALLUCINATIONS else text


MIC_STATUS = {0: "未確定", 1: "制限あり", 2: "拒否", 3: "許可済み"}


def check_mic_permission():
    """マイクの許可状態を返す。拒否されていると録音は無音になる（エラーにならない）。"""
    try:
        import AVFoundation as AV
        st = AV.AVCaptureDevice.authorizationStatusForMediaType_(AV.AVMediaTypeAudio)
        log(f"マイクの許可: {MIC_STATUS.get(st, st)}")
        if st == 0:
            AV.AVCaptureDevice.requestAccessForMediaType_completionHandler_(
                AV.AVMediaTypeAudio, lambda granted: log(f"マイク許可の応答: {granted}"))
        return st
    except Exception as e:
        log(f"マイクの許可状態を取れませんでした: {e}")
        return None


def list_input_devices():
    """使えるマイクの名前一覧。設定にはこの名前をそのまま入れる。"""
    try:
        import AVFoundation as AV
        return [d.localizedName()
                for d in AV.AVCaptureDevice.devicesWithMediaType_(AV.AVMediaTypeAudio)]
    except Exception as e:
        log(f"マイク一覧を取得できませんでした: {e}")
        return []


def default_mic_name():
    """システムが今使っている入力デバイス名。取れなければ None。"""
    try:
        import AVFoundation as AV
        d = AV.AVCaptureDevice.defaultDeviceWithMediaType_(AV.AVMediaTypeAudio)
        return d.localizedName() if d else None
    except Exception as e:
        log(f"既定マイクを取得できませんでした: {e}")
        return None


def _unlink(path):
    try:
        if path:
            os.unlink(path)
    except OSError:
        pass


# ---------------------------------------------------------------- AI（文字起こし＋清書）

def build_prompt(cfg):
    prompt = POLISH_PROMPT
    words = [w for w in cfg.get("vocabulary", []) if w.strip()]
    if words:
        prompt += VOCAB_HINT.format(words="、".join(words))
    fixes = cfg.get("corrections") or {}
    if fixes:
        lines = "\n".join(f"「{a}」→「{b}」" for a, b in fixes.items())
        prompt += FIX_HINT.format(fixes=lines)
    return prompt


def apply_corrections(text, cfg):
    """覚えさせた表記に置き換える。AIの気まぐれに任せず、最後に必ず通す。"""
    fixes = cfg.get("corrections") or {}
    # 長い語から先に置き換える（短い語が先だと途中で食い合う）
    for wrong in sorted(fixes, key=len, reverse=True):
        right = fixes[wrong]
        if wrong and wrong in text:
            text = text.replace(wrong, right)
            log(f"表記を直しました: {wrong} → {right}")
    return text


def pick_provider(cfg):
    p = cfg.get("provider", "auto")
    if p != "auto":
        return p
    if cfg.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY"):
        return "gemini"
    return "openai"


MAX_RAW_BYTES = 6 * 1024 * 1024   # これを超えたら圧縮してから送る


def compress_for_upload(wav_path):
    """大きなWAVをAAC(m4a)にする。10分の録音が19MB→約1MBになり、APIの上限に収まる。
    afconvertはmacOSに最初から入っているので、追加インストールは要らない。
    実測（2026-09-07）: 同じ音声のWAVとAAC32kで文字起こし結果は同一。
    戻り値は (送るファイル, MIME種別, 一時ファイルか)。"""
    try:
        size = os.path.getsize(wav_path)
    except OSError:
        return wav_path, "audio/wav", False
    if size <= MAX_RAW_BYTES:
        return wav_path, "audio/wav", False

    m4a = wav_path.rsplit(".", 1)[0] + ".m4a"
    t0 = time.time()
    r = subprocess.run(
        ["/usr/bin/afconvert", "-f", "m4af", "-d", "aac", "-b", "32000", "-c", "1",
         wav_path, m4a],
        capture_output=True,
    )
    if r.returncode != 0 or not os.path.exists(m4a):
        log(f"圧縮できなかったのでそのまま送ります: {r.stderr[:120]!r}")
        return wav_path, "audio/wav", False
    log(f"圧縮 {size // 1024}KB → {os.path.getsize(m4a) // 1024}KB "
        f"（{time.time() - t0:.1f}秒）")
    return m4a, "audio/mp4", True


def transcribe_and_polish(wav_path, cfg):
    db, spread = audio_levels(wav_path)
    log(f"録音の音量: {db if db is None else f'{db:.1f}'}dB / "
        f"起伏: {spread if spread is None else f'{spread:.1f}'}dB")
    if db is not None and (db < SILENCE_DB or spread < SPEECH_SPREAD_DB):
        log(f"声が入っていないので送信しませんでした"
            f"（実測 {db:.1f}dB・起伏 {spread:.1f}dB / 基準 起伏{SPEECH_SPREAD_DB}dB）")
        raise SilentAudio(db, spread)
    if pick_provider(cfg) == "openai":
        return apply_corrections(drop_hallucination(_via_openai(wav_path, cfg)), cfg)

    models = [cfg["gemini_model"]] + list(cfg.get("gemini_fallback_models") or [])
    deadline = time.time() + TOTAL_DEADLINE
    now = time.time()
    usable = [m for m in models if _model_blocked_until.get(m, 0) <= now] or models
    last = None
    for model in usable:
        if time.time() > deadline:
            log("時間切れのため打ち切りました（音声は残します）")
            break
        try:
            text = drop_hallucination(_via_gemini(wav_path, {**cfg, "gemini_model": model}))
            if model != models[0]:
                log(f"{model} で成功しました")
            return apply_corrections(text, cfg)
        except Exception as e:
            last = e
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                _model_blocked_until[model] = time.time() + GEMINI_COOLDOWN
                log(f"{model} は無料枠が尽きました。次のモデルを試します")
            else:
                log(f"{model} が失敗しました: {str(e)[:100]}")
    raise last if last else RuntimeError("Geminiのモデルがすべて使えませんでした")


# 混雑（503）や回数制限（429）は時間をおけば通ることが多い。黙って捨てない
# 429（枠切れ）はやり直しても無駄に枠を食うだけなので、粘らず次のモデルに回す
RETRY_CODES = {500, 502, 503, 504}
# Geminiの無料枠が尽きたら、しばらく試さない（毎回7秒待たされるのを防ぐ）
GEMINI_COOLDOWN = 1800.0
# モデルごとに無料枠が別なので、尽きたら次のモデルに回す
_model_blocked_until = {}
RETRY_WAITS = (2, 5)
CONNECT_TIMEOUT = 10      # つながらないと判断するまで
READ_TIMEOUT = 180        # 応答を待つ上限（10分の音声でも12秒程度）
TOTAL_DEADLINE = 240      # 全モデル・全やり直しを合わせた締め切り


def _post_with_retry(send, label):
    for attempt, wait in enumerate((0,) + RETRY_WAITS):
        if wait:
            log(f"{label}が混んでいます。{wait}秒待って{attempt}回目のやり直し")
            time.sleep(wait)
        t0 = time.time()
        try:
            r = send()
        except requests.RequestException as e:
            log(f"{label}に届きませんでした: {str(e)[:80]}")
            continue
        log(f"{label}応答 {time.time() - t0:.2f}秒 ({r.status_code})")
        if r.status_code not in RETRY_CODES:
            return r
    return r


def _via_gemini(wav_path, cfg):
    key = cfg.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY", "")
    if not key:
        raise RuntimeError(
            "Gemini APIキーが未設定です。メニューの「設定ファイルを開く」から "
            "gemini_api_key を入れてください。"
        )
    send_path, mime, temporary = compress_for_upload(wav_path)
    audio = base64.b64encode(Path(send_path).read_bytes()).decode()
    if temporary:
        _unlink(send_path)
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{cfg['gemini_model']}:generateContent"
    )
    body = {
        "contents": [
            {
                "parts": [
                    {"text": build_prompt(cfg)},
                    {"inline_data": {"mime_type": mime, "data": audio}},
                ]
            }
        ],
        "generationConfig": {"temperature": 0.2, "thinkingConfig": {"thinkingBudget": 0}},
    }
    r = _post_with_retry(
        lambda: requests.post(url, params={"key": key}, json=body,
                              headers={"Content-Type": "application/json"}, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)),
        "Gemini")
    if r.status_code != 200:
        raise RuntimeError(f"Gemini APIエラー {r.status_code}: {r.text[:300]}")
    data = r.json()
    try:
        parts = data["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts).strip()
    except (KeyError, IndexError):
        raise RuntimeError(f"Geminiの応答を読めませんでした: {json.dumps(data)[:300]}")


def _openai_key(cfg):
    key = os.environ.get("OPENAI_API_KEY", "")
    if key:
        return key
    env_file = Path(cfg.get("openai_api_key_file", ""))
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("OPENAI_API_KEY"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("OpenAI APIキーが見つかりませんでした。")


def _via_openai(wav_path, cfg):
    key = _openai_key(cfg)
    t0 = time.time()
    send_path, mime, temporary = compress_for_upload(wav_path)
    name = "audio.m4a" if mime == "audio/mp4" else "audio.wav"
    def send():
        with open(send_path, "rb") as f:
            return requests.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {key}"},
                files={"file": (name, f, "audio/mp4" if mime == "audio/mp4" else "audio/wav")},
                data={"model": cfg["openai_transcribe_model"], "language": "ja"},
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            )
    r = _post_with_retry(send, "文字起こし")
    if temporary:
        _unlink(send_path)
    if r.status_code != 200:
        raise RuntimeError(f"文字起こしエラー {r.status_code}: {r.text[:300]}")
    raw = r.json().get("text", "").strip()
    log(f"文字起こし {time.time() - t0:.2f}秒: {raw[:80]!r}")
    t1 = time.time()
    if not raw:
        return ""

    r = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        json={
            "model": cfg["openai_polish_model"],
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": build_prompt(cfg).replace("渡された音声を文字起こしし、", "渡された文字起こしを、")},
                {"role": "user", "content": raw},
            ],
        },
    )
    if r.status_code != 200:
        raise RuntimeError(f"清書エラー {r.status_code}: {r.text[:300]}")
    polished = (r.json()["choices"][0]["message"].get("content") or "").strip()
    log(f"清書 {time.time() - t1:.2f}秒")
    if not polished:
        # 清書が空を返しても、聞き取れている以上は捨てない
        log("清書が空だったので、文字起こしをそのまま使います")
        return raw
    return polished


# ---------------------------------------------------------------- 貼り付け

_paste_lock = threading.Lock()


def paste_text(text):
    """クリップボード経由でカーソル位置に貼る。元のクリップボードは戻す。
    連続で貼ったときに前回の貼付文を「元の内容」と誤認しないよう、ロックで直列化する。"""
    with _paste_lock:
        _paste_once(text)


def _paste_once(text):
    pb = NSPasteboard.generalPasteboard()
    # 文字以外（画像・ファイル）をコピーしていた場合も壊さないよう、全種類を退避する
    previous = []
    for t in (pb.types() or []):
        data = pb.dataForType_(t)
        if data is not None:
            previous.append((t, data))

    pb.clearContents()
    pb.setString_forType_(text, NSStringPboardType)
    mine = pb.changeCount()
    time.sleep(0.08)

    src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
    v_down = Quartz.CGEventCreateKeyboardEvent(src, 9, True)   # 9 = V
    v_up = Quartz.CGEventCreateKeyboardEvent(src, 9, False)
    Quartz.CGEventSetFlags(v_down, Quartz.kCGEventFlagMaskCommand)
    Quartz.CGEventSetFlags(v_up, Quartz.kCGEventFlagMaskCommand)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, v_down)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, v_up)

    # 貼り付けが終わるまで待ってから戻す。この間ロックを持ったままにして、
    # 次の貼り付けが「前回の貼付文」を元の内容と勘違いするのを防ぐ
    time.sleep(0.6)
    if pb.changeCount() != mine:
        log("クリップボードが他で変わったので元に戻しません")
        return
    pb.clearContents()
    for t, data in previous:
        pb.setData_forType_(data, t)


def beep(name):
    subprocess.Popen(
        ["afplay", f"/System/Library/Sounds/{name}.aiff"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )



# ---------------------------------------------------------------- 入力欄

# NSAlertの標準の入力欄は1行しか書けず、Returnが「保存」に取られて改行できない。
# NSTextView を自前で載せて、Shift+Return だけ改行に回す。
_SAVE = 1000  # NSAlertFirstButtonReturn

# 画面下の表示の濃さ。1.0が不透明、小さいほど透ける
_BRIGHT = 0.90
_FAINT = 0.11


class _EditorDelegate(NSObject):
    def textView_doCommandBySelector_(self, view, selector):
        name = selector if isinstance(selector, str) else str(selector)
        if isinstance(selector, bytes):
            name = selector.decode()
        if "insertNewline" not in name:
            return False
        event = NSApplication.sharedApplication().currentEvent()
        if event and (event.modifierFlags() & NSEventModifierFlagShift):
            return False          # Shiftあり → そのまま改行させる
        NSApplication.sharedApplication().stopModalWithCode_(_SAVE)
        return True               # Shiftなし → 保存して閉じる


def ask_multiline(title, message, default_text, width=420, height=200):
    """複数行を書けるダイアログ。(押されたか, 本文) を返す。"""
    alert = NSAlert.alloc().init()
    alert.setMessageText_(title)
    alert.setInformativeText_(message)
    alert.addButtonWithTitle_("保存")
    alert.addButtonWithTitle_("キャンセル")

    scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))
    scroll.setHasVerticalScroller_(True)
    scroll.setBorderType_(2)      # NSBezelBorder
    view = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))
    view.setFont_(NSFont.systemFontOfSize_(13))
    view.setRichText_(False)
    view.setString_(default_text)
    delegate = _EditorDelegate.alloc().init()
    view.setDelegate_(delegate)
    scroll.setDocumentView_(view)
    alert.setAccessoryView_(scroll)
    alert.window().setInitialFirstResponder_(view)

    code = alert.runModal()
    return code == _SAVE, str(view.string())


# ---------------------------------------------------------------- 画面表示

def resource_path(filename):
    """アプリに同梱した画像を探す。開発中はプロジェクト内のものを使う。"""
    cands = []
    try:
        from Foundation import NSBundle
        res = NSBundle.mainBundle().resourcePath()
        if res:
            cands.append(os.path.join(res, filename))
    except Exception:
        pass
    base = getattr(sys, "_MEIPASS", None)
    if base:
        cands.append(os.path.join(base, filename))
    cands.append(os.path.join(os.path.dirname(sys.executable), "..", "Resources", filename))
    cands.append(str(Path(__file__).resolve().parent.parent / "icon" / filename))
    for c in cands:
        if os.path.exists(c):
            return c
    log(f"画像が見つかりません: {filename}")
    return None


# 入力中のアプリからフォーカスを奪ってはいけない（奪うと貼り付け先が変わる）。
# 非アクティブなパネルとして出し、マウスも素通しにする。
_BORDERLESS = 0
_NONACTIVATING_PANEL = 1 << 7
_STATUS_LEVEL = 25
_ALL_SPACES = 1 << 0
_STATIONARY = 1 << 4
_FULLSCREEN_AUX = 1 << 8


class Indicator:
    """画面下に「アイコン＋文字」を点滅表示する。録音中=コーラル／整え中=青。"""

    W, H = 133, 50      # アイコン＋文字が入る大きさ
    ICON = 34

    def __init__(self):
        self.panel = None
        self.view = None
        self.label = None
        self.dim = False
        self.img_rec = None
        self.img_work = None
        self.mode = None

    def _load(self, filename):
        path = resource_path(filename)
        return NSImage.alloc().initWithContentsOfFile_(path) if path else None

    def _build(self):
        screen = NSScreen.mainScreen().frame()
        x = screen.origin.x + (screen.size.width - self.W) / 2
        y = screen.origin.y + 150
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, self.W, self.H),
            _BORDERLESS | _NONACTIVATING_PANEL, NSBackingStoreBuffered, False)
        panel.setLevel_(_STATUS_LEVEL)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setIgnoresMouseEvents_(True)
        panel.setHidesOnDeactivate_(False)
        panel.setCollectionBehavior_(_ALL_SPACES | _STATIONARY | _FULLSCREEN_AUX)

        bg = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, self.W, self.H))
        bg.setWantsLayer_(True)
        bg.layer().setCornerRadius_(self.H / 2)   # 丸ピル
        bg.layer().setBackgroundColor_(
            NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 0.965, 0.949, 0.97).CGColor())

        pad = (self.H - self.ICON) / 2
        view = NSImageView.alloc().initWithFrame_(
            NSMakeRect(pad, pad, self.ICON, self.ICON))
        view.setImageScaling_(3)  # ProportionallyUpOrDown
        bg.addSubview_(view)

        label = NSTextField.alloc().initWithFrame_(
            NSMakeRect(pad + self.ICON + 7, self.H / 2 - 10, self.W - self.ICON - pad * 2 - 7, 20))
        label.setBezeled_(False)
        label.setDrawsBackground_(False)
        label.setEditable_(False)
        label.setSelectable_(False)
        label.setFont_(NSFont.boldSystemFontOfSize_(13.5))
        label.setTextColor_(
            NSColor.colorWithCalibratedRed_green_blue_alpha_(0.20, 0.18, 0.17, 1.0))
        bg.addSubview_(label)

        self.img_rec = self._load("koetype_rec.png")
        self.img_work = self._load("koetype_blue.png")
        panel.setContentView_(bg)
        panel.setHasShadow_(True)
        self.panel, self.view, self.label = panel, view, label
        self.mode = None

    def _apply(self, alpha, blue):
        if self.panel is None:
            self._build()
        if self.mode != blue:
            img = self.img_work if blue else self.img_rec
            if img:
                self.view.setImage_(img)
            self.label.setStringValue_("整え中" if blue else "録音中")
            self.mode = blue
        self.panel.setAlphaValue_(alpha)
        self.panel.orderFrontRegardless()

    def _off(self):
        if self.panel is not None:
            self.panel.orderOut_(None)

    # 別スレッド（キー監視・清書）から呼ばれるので、必ずメインスレッドに渡す
    def show(self, alpha=1.0, blue=False):
        AppHelper.callAfter(self._apply, alpha, blue)

    def hide(self):
        AppHelper.callAfter(self._off)


# ---------------------------------------------------------------- アプリ本体

class KoeTypeApp(rumps.App):
    def __init__(self):
        super().__init__(APP_NAME, title=None, quit_button=None,
                         icon=resource_path("koetype_menu.png"), template=False)
        self.cfg = load_config()
        sweep_temp_files()
        self.recorder = Recorder(self.cfg)
        self.busy = False
        self.holding = False
        self.indicator = Indicator()
        self.tick = None
        self._toggle_lock = threading.RLock()
        self.phase = None      # None / "rec"（録音中）/ "work"（清書中）
        self.recording = False
        self.last_text = ""

        # 通知は環境によっては出ないので、状態はメニューに常時表示する
        self.status_item = self._info(f"{self.cfg['hold_key_name']}で録音開始・もう一度で終了")
        self.last_item = self._info("直前: まだ使っていません")
        self.retry_item = self._info("やり直せる録音はありません")
        self.pending = None
        self.mic_menu = rumps.MenuItem("マイク: 確認中…")
        self.copy_menu = rumps.MenuItem("結果をコピー")
        self.history = load_history()
        self.menu = [
            self.status_item,
            self.last_item,
            None,
            self.mic_menu,
            self.copy_menu,
            rumps.MenuItem("言葉を覚えさせる…", callback=self.edit_corrections),
            self.retry_item,
            None,
            rumps.MenuItem("終了", callback=rumps.quit_application),
        ]
        self.known_devices = None
        self._history_built = False
        self._rebuild_copy_menu()
        self._refresh_mic(None)
        self.status_timer = rumps.Timer(self._refresh_mic, 3)
        self.status_timer.start()
        self._start_hotkey()
        self._check_paste_permission()
        check_mic_permission()

    def _check_paste_permission(self):
        """アクセシビリティ未許可なら、macOSの許可ダイアログを出す。
        これを呼ばないとシステム設定の一覧にアプリ自体が現れない。"""
        try:
            from ApplicationServices import AXIsProcessTrustedWithOptions
            from ApplicationServices import kAXTrustedCheckOptionPrompt
            trusted = AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True})
        except Exception as e:
            log(f"権限確認に失敗: {e}")
            return
        try:
            listen = Quartz.CGPreflightListenEventAccess()
            post = Quartz.CGPreflightPostEventAccess()
            log(f"権限 → 入力監視(キー検知)={'OK' if listen else 'NG'} "
                f"アクセシビリティ(貼り付け)={'OK' if post else 'NG'} AX総合={'OK' if trusted else 'NG'}")
        except Exception as e:
            log(f"権限の内訳を取れませんでした: {e}")
        if trusted:
            return
        log("アクセシビリティ: 未許可。許可してからアプリを起動し直してください")
        rumps.notification(
            APP_NAME, "アクセシビリティを許可してください",
            "システム設定 → プライバシーとセキュリティ → アクセシビリティ で「こえタイプ」をオンにし、アプリを起動し直してください",
        )

    def _set(self, item, title):
        """メニューの文字はメインスレッドから変える。別スレッドから触ると崩れる。"""
        AppHelper.callAfter(lambda: setattr(item, "title", title))

    @staticmethod
    def _info(text):
        """押せない、読むためだけのメニュー項目。"""
        item = rumps.MenuItem(text)
        item.set_callback(None)
        return item

    AUTO = "auto"

    def _refresh_mic(self, _):
        """マイクのサブメニューを最新にする。抜き差しにも追従する。"""
        devices = list_input_devices()
        cur = self.cfg["audio_device"]

        if devices != self.known_devices:
            if self.known_devices is not None:
                self.mic_menu.clear()   # 初回はまだ中身が無く、消せない
            self.known_devices = devices
            auto = rumps.MenuItem("自動（システムの既定）", callback=self._pick_mic)
            auto.device_value = self.AUTO
            self.mic_menu.add(auto)
            for name in devices:
                item = rumps.MenuItem(name, callback=self._pick_mic)
                item.device_value = name
                self.mic_menu.add(item)

        # 見出しに今使うマイクを出し、選択中の項目にチェックを付ける
        using = default_mic_name() if cur == self.AUTO else cur
        suffix = "（自動）" if cur == self.AUTO else ""
        self.mic_menu.title = f"マイク: {using or '不明'}{suffix}"
        for key, item in self.mic_menu.items():
            value = getattr(item, "device_value", None)
            if value is not None:
                item.state = 1 if value == cur else 0

    def _pick_mic(self, sender):
        self.cfg["audio_device"] = sender.device_value
        save_config(self.cfg)
        self.recorder.cfg = self.cfg
        log(f"マイクを変更: {sender.device_value}")
        self._refresh_mic(None)

    # --- ホットキー監視（右Optionの押し下げ／離しを CGEventTap で拾う）

    def _start_hotkey(self):
        threading.Thread(target=self._tap_loop, daemon=True).start()

    def _tap_loop(self):
        mask = Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged)
        tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap,
            Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionListenOnly,
            mask,
            self._on_flags,
            None,
        )
        if not tap:
            log("イベントタップを作れませんでした（アクセシビリティ権限が必要です）")
            rumps.notification(
                APP_NAME, "アクセシビリティ権限が必要です",
                "システム設定 → プライバシーとセキュリティ → アクセシビリティ で許可してください",
            )
            return
        source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
        Quartz.CFRunLoopAddSource(
            Quartz.CFRunLoopGetCurrent(), source, Quartz.kCFRunLoopCommonModes
        )
        Quartz.CGEventTapEnable(tap, True)
        self._tap = tap
        log("ホットキー監視を開始しました")
        Quartz.CFRunLoopRun()

    _TAP_DISABLED = (Quartz.kCGEventTapDisabledByTimeout,
                     Quartz.kCGEventTapDisabledByUserInput)

    def _on_flags(self, proxy, etype, event, refcon):
        # 重い処理やスリープ復帰でタップが切られることがある。黙って死なせない
        if etype in self._TAP_DISABLED:
            log("キー監視が止められたので復帰させます")
            if getattr(self, "_tap", None):
                Quartz.CGEventTapEnable(self._tap, True)
            return event
        try:
            code = Quartz.CGEventGetIntegerValueField(
                event, Quartz.kCGKeyboardEventKeycode
            )
            if self.cfg.get("debug_keys"):
                log(f"キー検知: keycode={code}")
            if code != self.cfg["hold_key_code"]:
                return event
            flags = Quartz.CGEventGetFlags(event)
            down = bool(flags & KEY_FLAG.get(code, Quartz.kCGEventFlagMaskAlternate))
            # 押した瞬間だけ拾う。離した時は何もしない（1回押すごとに開始/終了が切り替わる）
            if down and not self.holding:
                self.holding = True
                self._toggle()
            elif not down:
                self.holding = False
        except Exception as e:
            log(f"キー監視で例外: {e}")
        return event

    def _signal_ready(self):
        """マイクが本当に開いてから、音と表示で「どうぞ」を伝える。"""
        self.recorder.ready.wait(timeout=3)
        if not self.recording:
            return
        log(f"マイクが開きました（{time.time() - self.rec_started:.2f}秒）")
        if self.cfg["sound_feedback"]:
            beep("Pop")
        self._blink("rec")

    def _blink(self, phase):
        """phase を切り替えて点滅を続ける。rec=コーラル / work=青。"""
        self.phase = phase
        self.indicator.dim = False
        self.indicator.show(_BRIGHT, blue=(phase == "work"))
        # タイマーは実行ループを持つメインスレッドで作らないと一度も発火しない
        AppHelper.callAfter(self._start_tick)

    def _start_tick(self):
        if self.tick is None and self.phase is not None:
            self.tick = rumps.Timer(self._on_tick, 0.75)
            self.tick.start()

    def _blink_stop(self):
        self.phase = None
        tick, self.tick = self.tick, None
        if tick:
            AppHelper.callAfter(tick.stop)
        self.indicator.hide()

    def _on_tick(self, _):
        # 止め忘れ対策。上限を超えたらこちらで打ち切る
        if self.phase == "rec" and time.time() - self.rec_started > self.cfg["max_seconds"]:
            log("上限時間に達したので録音を打ち切りました")
            self._toggle()
            return
        self.indicator.dim = not self.indicator.dim
        self.indicator.show(_FAINT if self.indicator.dim else _BRIGHT,
                            blue=(self.phase == "work"))

    def _toggle(self):
        """1回目の押下で録音開始、2回目で終了。清書中の押下は無視する。
        上限時間での自動停止（メインスレッド）とキー押下（監視スレッド）が
        同時に入りうるので、まとめて鍵をかける。"""
        with self._toggle_lock:
            if self.busy:
                return
            if self.recording:
                self.recording = False
                self._end()
            else:
                self.recording = True
                self._begin()

    # --- 録音〜貼り付け

    def _begin(self):
        if self.busy:
            return
        self.rec_started = time.time()
        try:
            self.recorder.start()
            log(f"録音開始（マイク: {getattr(self.recorder, 'device_label', '?')}）")
            # マイクが開くまで0.3〜0.5秒かかる。実際に録れ始めてから合図する
            threading.Thread(target=self._signal_ready, daemon=True).start()
        except Exception as e:
            _unlink(self.recorder.path)
            self.recorder.path = None
            self._blink_stop()
            self.recording = False
            log(f"録音開始に失敗: {e}")
            rumps.notification(APP_NAME, "録音を始められませんでした", str(e)[:180])

    def _end(self):
        try:
            wav = self.recorder.stop()
        except Exception as e:
            self._blink_stop()
            self.recording = False
            log(f"録音停止に失敗: {e}")
            return
        if not wav:
            log("録音が短すぎるか空でした")
            self._blink_stop()
            return
        self._blink("work")     # 整え中は青いアイコンで点滅を続ける
        self.busy = True
        threading.Thread(target=self._process, args=(wav,), daemon=True).start()

    def _process(self, wav):
        try:
            started = time.time()
            text = transcribe_and_polish(wav, self.cfg)
            elapsed = time.time() - started
            if text:
                self.last_text = text
                self._remember(text)
                paste_text(text)
                if self.cfg["sound_on_done"]:
                    beep("Tink")
                log(f"完了 {elapsed:.1f}秒 / {len(text)}文字")
                self._set(self.last_item, f"直前: {len(text)}文字を貼りました")
            else:
                keep = SUPPORT_DIR / "last_failed.wav"
                try:
                    if Path(wav) != keep:
                        shutil.copy(wav, keep)
                    self.pending = str(keep)
                    self.retry_item.set_callback(self.retry_last)
                    self._set(self.retry_item, "失敗した録音をやり直す")
                    log(f"聞き取れなかった音声を残しました: {keep}")
                except OSError as e:
                    log(f"音声を残せませんでした: {e}")
                self._set(self.last_item, "直前: 聞き取れませんでした")
        except SilentAudio as e:
            mic = getattr(self.recorder, "device_label", "?")
            self._set(self.last_item,
                      f"直前: 声が入っていませんでした（起伏{e.spread:.0f}dB / {mic}）")
            rumps.notification(
                APP_NAME, "声が入っていませんでした",
                f"何も貼っていません。話していれば、マイク「{mic}」が違う可能性があります"
                f"（{e.db:.0f}dB・起伏{e.spread:.0f}dB）。"
                f"メニューの「マイクを本体に固定」を試してください。")
        except Exception as e:
            log(f"エラー: {e}")
            # 長い録音を捨てないよう、失敗した音声は取っておく
            saved = SUPPORT_DIR / "last_failed.wav"
            try:
                if Path(wav) != saved:      # やり直し時は同じファイルなのでコピー不要
                    shutil.copy(wav, saved)
                self.pending = str(saved)
                log(f"音声を残しました。メニューからやり直せます: {saved}")
                self.retry_item.set_callback(self.retry_last)
                self._set(self.retry_item, "失敗した録音をやり直す")
            except OSError as ce:
                log(f"音声を残せませんでした: {ce}")
            self._set(self.last_item, f"直前: 失敗（{str(e)[:30]}）")
            rumps.notification(APP_NAME, "うまくいきませんでした", str(e)[:200])
        finally:
            if Path(wav) != SUPPORT_DIR / "last_failed.wav":
                _unlink(wav)        # 保存した音声は消さない（やり直せなくなる）
            self.busy = False
            self._blink_stop()

    # --- メニュー

    def edit_corrections(self, _):
        """1行1件で「間違い→正しい」を登録する。次回から必ずこの表記になる。"""
        fixes = self.cfg.get("corrections") or {}
        current = "\n".join(f"{a} → {b}" for a, b in fixes.items())
        clicked, text = ask_multiline(
            "言葉を覚えさせる",
            ("1行に1件、「聞き間違えられる形」と「正しい表記」を区切って書いてください。\n\n"
             "区切りは → -> > = 、 , のどれでもかまいません。\n"
             "例）ぽちぺた、ぽちペタ\n"
             "　　げんきAI=Genki AI\n\n"
             "改行は Shift ＋ Return。Return だけ押すと保存されます。\n\n"
             "消したい行は削除してください。"),
            current)
        if not clicked:
            return

        new = {}
        # 改行できない環境向けに、セミコロン区切りも1件として扱う
        lines = []
        for raw in text.splitlines():
            lines.extend(raw.replace("；", ";").split(";"))
        for line in lines:
            line = line.strip()
            if not line:
                continue
            # 区切りは何でも受ける。矢印を打つのが面倒なので記号1文字でよい
            for sep in ("→", "->", "=>", "＞", ">", "＝", "=", "、", ",", "\t"):
                if sep in line:
                    a, b = line.split(sep, 1)
                    a, b = a.strip(), b.strip()
                    if a and b:
                        new[a] = b
                    break
            else:
                log(f"区切りが無い行は読み飛ばしました: {line!r}")

        self.cfg["corrections"] = new
        save_config(self.cfg)
        log(f"言い換えを{len(new)}件 保存しました")
        self.last_item.title = f"直前: 言葉を{len(new)}件 覚えました"

    def retry_last(self, _):
        """混雑などで失敗した録音を、もう一度AIに通す。"""
        if not self.pending or not os.path.exists(self.pending) or self.busy:
            return
        self.busy = True
        self._blink("work")
        threading.Thread(target=self._process, args=(self.pending,), daemon=True).start()
        self.pending = None
        self.retry_item.set_callback(None)
        self.retry_item.title = "やり直せる録音はありません"

    def _remember(self, text):
        """新しい結果を履歴の先頭に足して、メニューを作り直す。"""
        item = {"time": time.strftime("%H:%M"), "text": text}
        self.history = [item] + [h for h in self.history if h.get("text") != text]
        self.history = self.history[:HISTORY_MAX]
        save_history(self.history)
        AppHelper.callAfter(self._rebuild_copy_menu)

    def _rebuild_copy_menu(self):
        """コピー用のサブメニューを作り直す。中身が読めるように本文を並べる。"""
        if self._history_built:
            self.copy_menu.clear()   # 初回は中身が無く clear できない
        self._history_built = True

        if not self.history:
            empty = rumps.MenuItem("まだ結果がありません")
            empty.set_callback(None)
            self.copy_menu.add(empty)
            self.copy_menu.title = "結果をコピー"
            return

        for i, h in enumerate(self.history):
            text = h.get("text", "")
            # 見出しは中身が重なっても別項目として扱われるよう番号を入れておく
            item = rumps.MenuItem(f"{i + 1}. {h.get('time', '')}",
                                  callback=self._copy_from_history)
            item.history_index = i
            self._show_preview(item, h.get("time", ""), text)
            self.copy_menu.add(item)
        self.copy_menu.title = f"結果をコピー（{len(self.history)}件）"

    def _show_preview(self, item, when, text):
        """本文を折り返して、複数行のまま項目に表示する。"""
        lines = wrap_preview(text)
        head = f"{when}  {len(text)}文字\n"
        body = "\n".join(lines)

        para = NSMutableParagraphStyle.alloc().init()
        para.setLineSpacing_(1.0)
        attr = NSMutableAttributedString.alloc().initWithString_(head + body)
        attr.addAttribute_value_range_(NSParagraphStyleAttributeName, para,
                                       (0, attr.length()))
        attr.addAttribute_value_range_(NSFontAttributeName,
                                       NSFont.systemFontOfSize_(13.0),
                                       (0, attr.length()))
        # 時刻と文字数は控えめに、本文は読みやすい大きさで
        attr.addAttribute_value_range_(NSFontAttributeName,
                                       NSFont.systemFontOfSize_(10.5), (0, len(head)))
        attr.addAttribute_value_range_(NSForegroundColorAttributeName,
                                       NSColor.secondaryLabelColor(), (0, len(head)))
        item._menuitem.setAttributedTitle_(attr)
        item._menuitem.setToolTip_(text)   # 全文はマウスを乗せると出る

    def _copy_from_history(self, sender):
        i = getattr(sender, "history_index", None)
        if i is None or i >= len(self.history):
            return
        text = self.history[i]["text"]
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        pb.setString_forType_(text, NSStringPboardType)
        log(f"履歴{i + 1}件目をコピーしました（{len(text)}文字）")
        self._set(self.last_item, f"直前: {len(text)}文字をコピーしました")

    def copy_last(self, _):
        if not self.last_text:
            rumps.notification(APP_NAME, "まだ結果がありません", "")
            return
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        pb.setString_forType_(self.last_text, NSStringPboardType)


# ---------------------------------------------------------------- CLI（動作確認用）

def cli():
    """--file <wav> で音声ファイルを1本流して清書結果を出す。UIなしで検証できる。"""
    cfg = load_config()
    args = sys.argv[1:]
    if "--provider" in args:
        cfg["provider"] = args[args.index("--provider") + 1]
    if "--models" in args:
        key = cfg.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY", "")
        r = requests.get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            params={"key": key}, timeout=30,
        )
        for m in r.json().get("models", []):
            if "generateContent" in m.get("supportedGenerationMethods", []):
                print(m["name"])
        return
    if "--file" in args:
        wav = args[args.index("--file") + 1]
        print(transcribe_and_polish(wav, cfg))
        return
    if "--record" in args:
        secs = float(args[args.index("--record") + 1])
        rec = Recorder(cfg)
        rec.start()
        print(f"{secs}秒録音中…", flush=True)
        time.sleep(secs)
        path = rec.stop()
        print("録音ファイル:", path)
        if path:
            print(transcribe_and_polish(path, cfg))
        return
    print("使い方: koetype.py [--provider gemini|openai] [--models | --file a.wav | --record 5]")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        cli()
    else:
        try:
            KoeTypeApp().run()
        except Exception:
            import traceback
            log("起動に失敗しました:\n" + traceback.format_exc())
            raise
