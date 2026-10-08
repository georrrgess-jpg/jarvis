"""Functional check of JARVIS's eyes and hands on a real Windows desktop (run by the Windows CI job).

It launches a small test app in its own process, the way a user's app would be, half-covers it with
another window, and then uses JARVIS's real code to: notice it as the active window, capture it
(through the cover), read it with Windows OCR, locate the "Press me" button, click it, type into the
text box and press Enter. The test app reports what really happened to it, so nothing is assumed.

    python tests/windows_vision_check.py report.json evidence.png
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TARGET_APP = r'''
import json, sys, tkinter as tk
state_path = sys.argv[1]
state = {"clicked": 0, "entry": "", "enter": False}
root = tk.Tk()
root.title("JARVIS Vision Test")
root.geometry("760x420+220+160")
root.configure(bg="white")
tk.Label(root, text="JARVIS VISION TEST 42", font=("Segoe UI", 28, "bold"), bg="white", fg="black").pack(pady=18)
def press():
    state["clicked"] += 1; save()
button = tk.Button(root, text="Press me", font=("Segoe UI", 22), command=press, padx=20, pady=6)
button.pack(pady=10)
entry = tk.Entry(root, font=("Segoe UI", 20), width=26)
entry.pack(pady=16)
def changed(*_):
    state["entry"] = entry.get(); save()
def enter(_):
    state["enter"] = True; save()
entry.bind("<KeyRelease>", changed)
entry.bind("<Return>", enter)
def save():
    root.update_idletasks()
    state["button"] = [button.winfo_rootx() + button.winfo_width() // 2, button.winfo_rooty() + button.winfo_height() // 2]
    state["entry_xy"] = [entry.winfo_rootx() + entry.winfo_width() // 2, entry.winfo_rooty() + entry.winfo_height() // 2]
    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
root.after(400, save)
root.after(500, lambda: (root.lift(), root.focus_force()))
root.mainloop()
'''

COVER_APP = r'''
import tkinter as tk
root = tk.Tk()
root.title("Covering window")
root.geometry("420x260+560+300")
root.configure(bg="#203040")
tk.Label(root, text="I am covering the test app", font=("Segoe UI", 16), bg="#203040", fg="white").pack(expand=True)
root.after(300, root.lift)
root.mainloop()
'''


def main() -> int:
    report_path = Path(sys.argv[1] if len(sys.argv) > 1 else "vision-check.json")
    evidence_path = Path(sys.argv[2] if len(sys.argv) > 2 else "vision-check.png")
    ctypes.windll.user32.SetProcessDPIAware()  # same as the app (pywebview does this)

    from core.ocr import WindowsOcr, find_text
    from core.screen import ForegroundTracker, WindowsDesktop
    from core.vision import Observation, VisionEngine

    report: dict = {"checks": {}}
    failures: list[str] = []

    def check(name: str, ok: bool, detail=None) -> None:
        report["checks"][name] = {"ok": bool(ok), "detail": detail}
        print(("PASS " if ok else "FAIL ") + name + (f": {detail}" if detail is not None else ""), flush=True)
        if not ok:
            failures.append(name)

    work = Path(tempfile.mkdtemp(prefix="jarvis-vision-"))
    state_file = work / "state.json"
    (work / "target.py").write_text(TARGET_APP, encoding="utf-8")
    (work / "cover.py").write_text(COVER_APP, encoding="utf-8")
    target = subprocess.Popen([sys.executable, str(work / "target.py"), str(state_file)])
    cover = None
    try:
        desk = WindowsDesktop()
        find = ctypes.windll.user32.FindWindowW
        find.restype = ctypes.c_void_p
        hwnd = None
        for _ in range(100):
            hwnd = find(None, "JARVIS Vision Test")
            if hwnd and state_file.exists():
                break
            time.sleep(0.1)
        check("target window appeared", bool(hwnd))
        if not hwnd:
            return 1
        state = lambda: json.loads(state_file.read_text(encoding="utf-8"))  # noqa: E731
        window = desk.window_info(int(hwnd))
        desk.bring_to_front(window)
        time.sleep(0.6)

        tracker = ForegroundTracker(desk)
        current = tracker.current()
        check("noticed as the active app (not JARVIS itself)", current is not None and current.hwnd == window.hwnd,
              current.title if current else None)

        # half-cover it with another app, the way JARVIS's HUD might
        cover = subprocess.Popen([sys.executable, str(work / "cover.py")])
        time.sleep(2.0)
        shot = desk.capture(window)
        shot.image.save(evidence_path)
        small = shot.image.convert("L").resize((64, 36))
        brightness = sum(small.getdata()) / (64 * 36)
        check("captured the window through the cover", shot.size[0] >= 700 and brightness > 150,
              {"size": shot.size, "mean_brightness": round(brightness, 1)})

        ocr = WindowsOcr()
        ocr_ok = ocr.available()
        report["ocr"] = {"available": ocr_ok, "error": ocr.error, "language": ocr.language}
        lines = []
        if ocr_ok:
            started = time.time()
            lines = ocr.read(shot.image)
            text = " | ".join(l.text for l in lines)
            check("Windows OCR read the window", "VISION" in text.upper() and "42" in text,
                  {"text": text[:200], "seconds": round(time.time() - started, 2)})
            started = time.time()
            ocr.read(shot.image)
            check("second OCR read is fast (helper stays running)", time.time() - started < 3.0, round(time.time() - started, 2))
        else:
            print(f"NOTE Windows OCR unavailable on this machine: {ocr.error}", flush=True)

        true_button = state()["button"]
        if ocr_ok:
            engine = VisionEngine({}, llm=None, desktop=desk, ocr=ocr)
            located = engine.locate(Observation(shot, lines), "the Press me button")
            ok = located is not None and abs(located.x - true_button[0]) <= 25 and abs(located.y - true_button[1]) <= 20
            check("located the 'Press me' button from its text", ok,
                  {"found": [located.x, located.y] if located else None, "actual": true_button, "sure": getattr(located, "sure", None)})
            click_at = (located.x, located.y) if located else tuple(true_button)
        else:
            click_at = tuple(true_button)

        desk.click(*click_at, window=window)  # raises the app above the cover first
        for _ in range(30):
            if state()["clicked"]:
                break
            time.sleep(0.1)
        check("click reached the button (through the covering window)", state()["clicked"] == 1, state()["clicked"])

        entry_xy = state()["entry_xy"]
        desk.click(*entry_xy, window=window)
        time.sleep(0.3)
        phrase = "Hello JARVIS 123 ünï"
        desk.type_text(phrase, window=window)
        time.sleep(0.6)
        check("typing reached the text box (including accents)", state()["entry"] == phrase, state()["entry"])

        desk.press([0x0D], window=window)
        time.sleep(0.5)
        check("Enter key reached the app", state()["enter"] is True)

        desk.scroll(-3, window=window)  # must not raise
        check("scrolling works", True)
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        check("no unexpected errors", False, f"{exc.__class__.__name__}: {exc}")
    finally:
        for proc in (cover, target):
            if proc is not None:
                proc.kill()
        report["ok"] = not failures
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("ALL VISION CHECKS PASSED" if not failures else f"FAILED: {failures}", flush=True)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
