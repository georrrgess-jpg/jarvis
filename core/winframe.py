"""Moving, resizing, maximising and full-screening JARVIS's borderless window the way Windows itself does.

The HUD draws its own title bar, so Windows doesn't know where to grab the window. Instead of moving it from
JavaScript on every mouse move (which jumps and drifts on scaled screens), the page asks for a drag or a resize and
Windows runs its own move loop (WM_SYSCOMMAND SC_MOVE); resizing follows the cursor from here, keeping the minimum size.

* maximise = the screen's work area (the taskbar stays visible), restore = the size and place it had before;
* full screen = the whole monitor; leaving it returns to how the window was;
* dragging a maximised window restores it under the cursor first, like any Windows app.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time
from ctypes import wintypes
from typing import Callable

log = logging.getLogger("jarvis.window")

WM_SYSCOMMAND = 0x0112
SC_MOVE_CAPTION = 0xF012
EDGES = {"left": 1, "right": 2, "top": 3, "topleft": 4, "topright": 5, "bottom": 6, "bottomleft": 7, "bottomright": 8}
SWP_NOZORDER, SWP_NOACTIVATE, SWP_FRAMECHANGED, SWP_SHOWWINDOW = 0x4, 0x10, 0x20, 0x40
SW_RESTORE = 9


class _MonitorInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


def _box(r: wintypes.RECT) -> tuple[int, int, int, int]:
    return r.left, r.top, r.right - r.left, r.bottom - r.top


class WinFrame:
    """hwnd(): the window handle; run_ui(fn): run fn on the window's UI thread; emit(state): tell the page."""

    def __init__(self, hwnd: Callable[[], int], run_ui: Callable[[Callable[[], None]], None], emit: Callable[[dict], None]) -> None:
        self._hwnd, self._run_ui, self._emit = hwnd, run_ui, emit
        self.maximized = False
        self.fullscreen = False
        self._normal: tuple[int, int, int, int] | None = None
        self._before_full: tuple[tuple[int, int, int, int], bool] | None = None
        self._resizing = False
        self.min_size = (640, 480)  # set by app.py to the window's real minimum
        self.user32 = ctypes.WinDLL("user32", use_last_error=True) if sys.platform == "win32" else None
        if self.user32:
            u = self.user32
            u.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
            u.SetWindowPos.argtypes = (wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT)
            u.MonitorFromWindow.argtypes = (wintypes.HWND, wintypes.DWORD)
            u.MonitorFromWindow.restype = wintypes.HANDLE
            u.GetMonitorInfoW.argtypes = (wintypes.HANDLE, ctypes.POINTER(_MonitorInfo))
            u.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
            u.IsZoomed.argtypes = (wintypes.HWND,)
            u.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
            u.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
            u.GetAsyncKeyState.argtypes = (ctypes.c_int,)
            u.GetAsyncKeyState.restype = ctypes.c_short

    # -- state
    def state(self) -> dict:
        return {"maximized": self.maximized, "fullscreen": self.fullscreen}

    def _changed(self) -> None:
        try:
            self._emit(self.state())
        except Exception:
            log.debug("window state emit failed", exc_info=True)

    # -- geometry
    def rect(self) -> tuple[int, int, int, int]:
        r = wintypes.RECT()
        self.user32.GetWindowRect(self._hwnd(), ctypes.byref(r))
        return _box(r)

    def monitor(self, work: bool = True) -> tuple[int, int, int, int]:
        info = _MonitorInfo()
        info.cbSize = ctypes.sizeof(_MonitorInfo)
        self.user32.GetMonitorInfoW(self.user32.MonitorFromWindow(self._hwnd(), 2), ctypes.byref(info))  # 2 = nearest
        return _box(info.rcWork if work else info.rcMonitor)

    def _place(self, box: tuple[int, int, int, int]) -> None:
        x, y, w, h = box
        self.user32.SetWindowPos(self._hwnd(), None, int(x), int(y), int(w), int(h), SWP_NOZORDER | SWP_FRAMECHANGED | SWP_SHOWWINDOW)

    # -- actions
    def maximize(self) -> None:
        if not self.user32 or self.fullscreen:
            return
        if not self.maximized:
            self._normal = self.rect()
        self._run_ui(lambda: self._place(self.monitor(work=True)))
        self.maximized = True
        self._changed()

    def restore(self) -> None:
        if not self.user32:
            return
        if self.fullscreen:
            self.toggle_fullscreen()
            return
        if self.user32.IsZoomed(self._hwnd()):  # Windows itself maximised it (Win+Up)
            self._run_ui(lambda: self.user32.ShowWindow(self._hwnd(), SW_RESTORE))
        elif self.maximized and self._normal:
            normal = self._normal
            self._run_ui(lambda: self._place(normal))
        self.maximized = False
        self._changed()

    def toggle_maximize(self) -> None:
        if self.fullscreen:
            self.toggle_fullscreen()
        elif self.maximized or (self.user32 and self.user32.IsZoomed(self._hwnd())):
            self.restore()
        else:
            self.maximize()

    def toggle_fullscreen(self) -> None:
        if not self.user32:
            return
        if not self.fullscreen:
            self._before_full = (self.rect(), self.maximized)
            screen = self.monitor(work=False)
            self._run_ui(lambda: self._place(screen))
            self.fullscreen = True
        else:
            box, was_max = self._before_full or (self._normal or self.rect(), False)
            self._run_ui(lambda: self._place(box))
            self.fullscreen = False
            self.maximized = was_max
        self._changed()

    def start_drag(self) -> None:
        """The mouse is down on the title bar and moving: let Windows move the window."""
        if not self.user32 or self.fullscreen:
            return
        u = self.user32
        hwnd = self._hwnd()

        def go() -> None:
            if self.maximized and self._normal:
                pt = wintypes.POINT()
                u.GetCursorPos(ctypes.byref(pt))
                x, y, w, h = self.rect()
                frac = (pt.x - x) / max(1, w)
                nx, ny, nw, nh = self._normal
                work = self.monitor(work=True)
                self._place((int(pt.x - nw * frac), max(work[1], pt.y - 18), nw, nh))
                self.maximized = False
            u.ReleaseCapture()
            u.PostMessageW(hwnd, WM_SYSCOMMAND, SC_MOVE_CAPTION, 0)

        was_max = self.maximized
        self._run_ui(go)
        if was_max:
            self._changed()

    def start_resize(self, edge: str) -> None:
        """Resize from an edge while the left button is held. Windows' own sizing loop ignores borderless windows,
        so the cursor is followed here (about 60 times a second), keeping the window's minimum size."""
        if not self.user32 or str(edge) not in EDGES or self.maximized or self.fullscreen:
            return
        if self._resizing:
            return
        self._resizing = True
        threading.Thread(target=self._resize_loop, args=(str(edge),), name="window-resize", daemon=True).start()

    def _resize_loop(self, edge: str) -> None:
        u = self.user32
        try:
            start = wintypes.POINT()
            u.GetCursorPos(ctypes.byref(start))
            x0, y0, w0, h0 = self.rect()
            min_w, min_h = self.min_size
            work = self.monitor(work=False)
            last = None
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline and (u.GetAsyncKeyState(0x01) & 0x8000):  # left button still down
                pt = wintypes.POINT()
                u.GetCursorPos(ctypes.byref(pt))
                dx, dy = pt.x - start.x, pt.y - start.y
                x, y, w, h = x0, y0, w0, h0
                if "left" in edge:
                    w = max(min_w, w0 - dx)
                    x = x0 + w0 - w
                if "right" in edge:
                    w = max(min_w, w0 + dx)
                if "top" in edge:
                    h = max(min_h, h0 - dy)
                    y = y0 + h0 - h
                if "bottom" in edge:
                    h = max(min_h, h0 + dy)
                w, h = min(w, work[2] * 2), min(h, work[3] * 2)
                box = (x, y, w, h)
                if box != last:
                    last = box
                    self._run_ui(lambda b=box: self._place(b))
                time.sleep(0.016)
        except Exception:
            log.debug("resize loop failed", exc_info=True)
        finally:
            self._resizing = False
