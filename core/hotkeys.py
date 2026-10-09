"""System-wide keyboard shortcuts that start protocols (Ctrl+Alt+G for "Gaming Mode"), even when JARVIS isn't in front.

Windows delivers registered hotkeys to the thread that registered them, so one small thread owns them all and
re-registers the set whenever protocols change. A combination another program already owns is reported back
rather than silently failing.
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
from typing import Callable

log = logging.getLogger("jarvis.hotkeys")

MOD = {"Alt": 0x1, "Ctrl": 0x2, "Shift": 0x4, "Win": 0x8}
MOD_NOREPEAT = 0x4000
WM_HOTKEY, WM_APP, WM_QUIT = 0x0312, 0x8000, 0x0012


def to_vk(hotkey: str) -> tuple[int, int] | None:
    """'Ctrl+Alt+G' -> (modifiers, virtual key)."""
    parts = [p for p in (hotkey or "").split("+") if p]
    if len(parts) < 2:
        return None
    mods = 0
    for p in parts[:-1]:
        if p not in MOD:
            return None
        mods |= MOD[p]
    key = parts[-1].upper()
    if len(key) == 1 and key.isalnum():
        vk = ord(key)
    elif key.startswith("F") and key[1:].isdigit() and 1 <= int(key[1:]) <= 12:
        vk = 0x70 + int(key[1:]) - 1
    else:
        return None
    return mods | MOD_NOREPEAT, vk


class HotkeyManager:
    def __init__(self, on_press: Callable[[str], None]) -> None:
        self._on_press = on_press
        self._thread: threading.Thread | None = None
        self._tid = 0
        self._ready = threading.Event()
        self._jobs: "queue.Queue[tuple[dict, dict, threading.Event]]" = queue.Queue()
        self._ids: dict[int, str] = {}

    def set(self, wanted: dict[str, str], timeout: float = 2.0) -> dict[str, str]:
        """Register exactly these {hotkey: protocol id}; returns {hotkey: why it failed}."""
        if sys.platform != "win32":
            return {}
        if not wanted and self._thread is None:
            return {}
        self._ensure()
        problems: dict = {}
        done = threading.Event()
        self._jobs.put((dict(wanted), problems, done))
        import ctypes

        ctypes.windll.user32.PostThreadMessageW(self._tid, WM_APP, 0, 0)
        done.wait(timeout)
        return problems

    def stop(self) -> None:
        if self._thread is not None and self._tid:
            import ctypes

            ctypes.windll.user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)

    def _ensure(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._ready.clear()
        self._thread = threading.Thread(target=self._loop, name="hotkeys", daemon=True)
        self._thread.start()
        self._ready.wait(2.0)

    def _loop(self) -> None:
        import ctypes
        import ctypes.wintypes as wt

        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        msg = wt.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)  # creates this thread's message queue
        self._tid = kernel32.GetCurrentThreadId()
        self._ready.set()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                pid = self._ids.get(int(msg.wParam))
                if pid:
                    threading.Thread(target=self._on_press, args=(pid,), daemon=True).start()
            elif msg.message == WM_APP:
                while not self._jobs.empty():
                    wanted, problems, done = self._jobs.get()
                    for hid in list(self._ids):
                        user32.UnregisterHotKey(None, hid)
                    self._ids.clear()
                    for n, (key, pid) in enumerate(sorted(wanted.items()), start=1):
                        parsed = to_vk(key)
                        if parsed is None:
                            problems[key] = "not a valid shortcut"
                            continue
                        if user32.RegisterHotKey(None, n, parsed[0], parsed[1]):
                            self._ids[n] = pid
                        else:
                            problems[key] = "another program already uses it"
                    done.set()
        for hid in list(self._ids):
            user32.UnregisterHotKey(None, hid)
