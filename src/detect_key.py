"""押した修飾キーのキーコードを読み取る。どのキーを使うか決めるための道具。"""
import sys, time
import Quartz

NAMES = {54:"右Command", 55:"左Command", 56:"左Shift", 57:"CapsLock", 58:"左Option",
         59:"左Control", 60:"右Shift", 61:"右Option", 62:"右Control", 63:"fn/地球儀"}
seen = []
END = time.time() + 40

def cb(proxy, etype, event, refcon):
    code = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
    name = NAMES.get(code, f"不明(keycode={code})")
    if not seen or seen[-1][0] != code:
        seen.append((code, name))
        print(f"押されたキー: {name}  (keycode={code})", flush=True)
    return event

tap = Quartz.CGEventTapCreate(
    Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap,
    Quartz.kCGEventTapOptionListenOnly,
    Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged), cb, None)
if not tap:
    print("イベントタップを作れませんでした（権限不足）", flush=True); sys.exit(1)

src = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
Quartz.CFRunLoopAddSource(Quartz.CFRunLoopGetCurrent(), src, Quartz.kCFRunLoopCommonModes)
Quartz.CGEventTapEnable(tap, True)
print("検出開始。使いたいキーを2〜3回押してください（40秒）", flush=True)
Quartz.CFRunLoopRunInMode(Quartz.kCFRunLoopDefaultMode, 40, False)
print("--- 検出終了 ---", flush=True)
