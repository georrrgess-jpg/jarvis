"""Real-Windows check of the borderless window, with the real mouse and keyboard (run by the Windows CI job).

Jarvis.exe is started with --window-probe, which keeps writing where its title bar and buttons are. Then:
dragging the title bar moves it; dragging the bottom edge resizes it; double-clicking the title bar and the
maximise button maximise to the work area (taskbar still visible) and restore; F11 / the full-screen button cover
the whole monitor and Esc leaves; dragging a maximised window restores it under the cursor; minimise and close work.

    python tests/windows_window_check.py dist/Jarvis.exe window-check.json
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

user32 = ctypes.WinDLL("user32", use_last_error=True)
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x2, 0x4
KEYEVENTF_KEYUP = 0x2


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


def find_window(pid_hint=None, timeout=120):
    found = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        if buf.value == "J.A.R.V.I.S." and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    deadline = time.time() + timeout
    while time.time() < deadline:
        found.clear()
        user32.EnumWindows(WNDENUMPROC(cb), 0)
        if found:
            return found[0]
        time.sleep(0.5)
    return None


def rect(hwnd):
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return [r.left, r.top, r.right - r.left, r.bottom - r.top]


def monitor(hwnd, work=True):
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    user32.GetMonitorInfoW(user32.MonitorFromWindow(hwnd, 2), ctypes.byref(info))
    r = info.rcWork if work else info.rcMonitor
    return [r.left, r.top, r.right - r.left, r.bottom - r.top]


def move_to(x, y):
    user32.SetCursorPos(int(x), int(y))


def click(x, y, double=False):
    move_to(x, y)
    time.sleep(0.15)
    for _ in range(2 if double else 1):
        user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.04)
        user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        time.sleep(0.08)


def drag(x, y, dx, dy, steps=24):
    move_to(x, y)
    time.sleep(0.2)
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.15)
    for i in range(1, steps + 1):
        move_to(x + dx * i / steps, y + dy * i / steps)
        time.sleep(0.03)
    time.sleep(0.25)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(0.6)


def key(vk):
    user32.keybd_event(vk, 0, 0, 0)
    time.sleep(0.05)
    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.8)


def near(a, b, tol=8):
    return a is not None and b is not None and all(abs(x - y) <= tol for x, y in zip(a, b))


def main() -> int:
    ctypes.windll.user32.SetProcessDPIAware()
    exe = Path(sys.argv[1] if len(sys.argv) > 1 else "dist/Jarvis.exe").resolve()
    report_path = Path(sys.argv[2] if len(sys.argv) > 2 else "window-check.json")
    work = Path(tempfile.mkdtemp(prefix="jarvis-window-"))
    probe_path = work / "probe.json"
    report: dict = {"checks": {}, "steps": []}
    failures: list[str] = []

    def check(name, ok, detail=None):
        report["checks"][name] = {"ok": bool(ok), "detail": detail}
        print(("PASS " if ok else "FAIL ") + name + (f": {detail}" if detail is not None else ""), flush=True)
        if not ok:
            failures.append(name)

    def probe():
        try:
            return json.loads(probe_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def screen_point(p, name, fx=0.5, fy=0.5):
        """Centre of an element from the probe, in screen pixels."""
        box, dpr, r = p.get(name), p.get("dpr") or 1, p.get("rect") or [0, 0, 0, 0]
        if not box:
            return None
        return r[0] + (box[0] + box[2] * fx) * dpr, r[1] + (box[1] + box[3] * fy) * dpr

    env = {**os.environ, "JARVIS_HOME": str(work / "home"), "JARVIS_NO_DIALOGS": "1", "SDL_AUDIODRIVER": "dummy"}
    proc = subprocess.Popen([str(exe), "--window-probe", str(probe_path)], env=env)
    try:
        hwnd = find_window()
        check("window appeared", hwnd is not None)
        deadline = time.time() + 120
        while time.time() < deadline and not probe().get("booted"):
            time.sleep(0.5)
        time.sleep(2.0)
        p = probe()
        check("HUD booted and the probe reports the title bar", p.get("booted") and p.get("drag") and p.get("max"), {k: p.get(k) for k in ("dpr", "w", "h", "rect")})
        workarea, screen = monitor(hwnd), monitor(hwnd, work=False)
        report["screen"], report["workarea"] = screen, workarea
        for name in ("min", "max", "close"):
            box = p.get(name)
            check(f"the {name} button is inside the window", box and box[0] + box[2] <= p.get("w", 0) + 1, box)

        # Small CI screens start maximised (to the work area, not over the taskbar).
        r0 = rect(hwnd)
        if p.get("state", {}).get("maximized"):
            check("starts maximised to the work area (taskbar visible)", near(r0, workarea), {"rect": r0, "work": workarea})
            dx, dy = screen_point(p, "drag")
            click(dx, dy, double=True)
            time.sleep(1.0)
            r0 = rect(hwnd)
            check("double-clicking the title bar restores it", not probe().get("state", {}).get("maximized") and r0[2] < workarea[2], r0)
        time.sleep(0.5)

        # 1. move by dragging the title bar
        p = probe()
        x, y = screen_point(p, "drag")
        before = rect(hwnd)
        drag(x, y, -60, 40)
        after = rect(hwnd)
        moved = (after[0] - before[0], after[1] - before[1])
        check("dragging the title bar moves the window", abs(moved[0] + 60) <= 10 and abs(moved[1] - 40) <= 10 and after[2:] == before[2:],
              {"before": before, "after": after, "moved": moved})

        # 2. resize from the bottom edge
        p = probe()
        gx, gy = screen_point(p, "grip", 0.5, 0.5)
        before = rect(hwnd)
        drag(gx, gy, 0, -50)
        after = rect(hwnd)
        check("dragging the bottom edge resizes the window", abs((after[3] - before[3]) + 50) <= 10 and after[:3] == before[:3],
              {"before": before, "after": after})

        # 3. maximise button -> work area; again -> back where it was
        normal = rect(hwnd)
        p = probe()
        click(*screen_point(p, "max"))
        time.sleep(1.0)
        check("the maximise button fills the work area (taskbar stays visible)", near(rect(hwnd), workarea) and probe().get("state", {}).get("maximized"),
              {"rect": rect(hwnd), "work": workarea})
        p = probe()
        click(*screen_point(p, "max"))
        time.sleep(1.0)
        check("pressing it again restores the previous size and place", near(rect(hwnd), normal), {"rect": rect(hwnd), "was": normal})

        # 4. full screen with F11, Esc to leave
        p = probe()
        click(*screen_point(p, "drag"))  # focus the window (a click, not a drag)
        key(0x7A)  # F11
        check("F11 covers the whole monitor", near(rect(hwnd), screen) and probe().get("state", {}).get("fullscreen"), {"rect": rect(hwnd), "screen": screen})
        key(0x1B)  # Esc
        check("Esc leaves full screen", near(rect(hwnd), normal) and not probe().get("state", {}).get("fullscreen"), {"rect": rect(hwnd), "was": normal})

        # 5. dragging a maximised window restores it under the cursor
        p = probe()
        click(*screen_point(p, "max"))
        time.sleep(1.0)
        p = probe()
        x, y = screen_point(p, "drag")
        drag(x, y, 0, 120)
        after = rect(hwnd)
        check("dragging a maximised window restores it and moves it", not probe().get("state", {}).get("maximized") and after[2] == normal[2]
              and after[3] == normal[3] and after[0] <= x <= after[0] + after[2], {"rect": after, "normal": normal, "cursor": [x, y + 120]})

        # 6. minimise, bring back, close
        p = probe()
        click(*screen_point(p, "min"))
        time.sleep(1.0)
        check("minimise works", bool(user32.IsIconic(hwnd)))
        user32.ShowWindow(hwnd, 9)
        time.sleep(1.0)
        check("it comes back from the taskbar", not user32.IsIconic(hwnd) and rect(hwnd)[2] == normal[2], rect(hwnd))
        p = probe()
        click(*screen_point(p, "close"))
        try:
            proc.wait(30)
        except subprocess.TimeoutExpired:
            pass
        check("the close button closes JARVIS", proc.poll() is not None, proc.poll())
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        check("no unexpected errors", False, f"{exc.__class__.__name__}: {exc}")
    finally:
        if proc.poll() is None:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        try:
            report["log_tail"] = (work / "home" / "jarvis.log").read_text(encoding="utf-8", errors="replace")[-2500:]
        except OSError:
            pass
        report["ok"] = not failures
        report_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print("ALL WINDOW CHECKS PASSED" if not failures else f"FAILED: {failures}", flush=True)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
