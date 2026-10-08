"""Seeing and touching the desktop: window tracking, screen capture, mouse and keyboard.

Windows is the real target and uses only the Win32 API through ctypes (no extra packages):

* the app the user is working in is tracked in the background, so "look at this" means that app,
  not JARVIS's own window;
* windows are captured with ``PrintWindow``, which works even when JARVIS's HUD covers them;
* clicks, typing and keys go through ``SendInput``, exactly like a real mouse and keyboard.

Everything else (Linux, tests) uses ``FakeDesktop``-style objects with the same small interface.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger("jarvis.screen")


class ScreenError(Exception):
    """Something the user should hear about ("that window is minimised", "screen control is off")."""


@dataclass
class Window:
    hwnd: int
    title: str
    app: str = ""  # executable name, e.g. "chrome.exe"
    rect: tuple[int, int, int, int] = (0, 0, 0, 0)  # left, top, right, bottom in screen pixels

    @property
    def label(self) -> str:
        app = re.sub(r"\.exe$", "", self.app or "", flags=re.I)
        return self.title or app or "the active window"


@dataclass
class Shot:
    """A captured image and how its pixels map back onto the screen."""

    image: object  # PIL.Image
    left: int = 0
    top: int = 0
    scale: float = 1.0  # image pixels per screen pixel
    window: Window | None = None
    taken: float = field(default_factory=time.time)

    def to_screen(self, x: float, y: float) -> tuple[int, int]:
        return int(round(self.left + x / self.scale)), int(round(self.top + y / self.scale))

    @property
    def size(self) -> tuple[int, int]:
        return self.image.size


_SENSITIVE_DEFAULT = "password, 1password, bitwarden, lastpass, keepass, dashlane, bank, banking, paypal, credit card, sign in, log in"


def is_private(window: Window | None, exclusions: str = _SENSITIVE_DEFAULT) -> bool:
    """Windows JARVIS must never look at or touch (password managers, banking, sign-in pages)."""
    if window is None:
        return False
    hay = f"{window.title} {window.app}".lower()
    words = [w.strip().lower() for w in re.split(r"[,;\n]+", exclusions or "") if w.strip()]
    return any(re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", hay) for w in words)


# =============================================================================== Windows implementation
if sys.platform == "win32":  # pragma: no cover - exercised on the Windows CI runner
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    kernel32 = ctypes.windll.kernel32
    try:
        dwmapi = ctypes.windll.dwmapi
    except OSError:
        dwmapi = None

    ULONG_PTR = ctypes.c_size_t

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                    ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                    ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]

    user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.WindowFromPoint.argtypes = (wintypes.POINT,)
    user32.WindowFromPoint.restype = wintypes.HWND
    user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
    user32.GetAncestor.restype = wintypes.HWND
    user32.GetWindowDC.argtypes = (wintypes.HWND,)
    user32.GetWindowDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = (wintypes.HWND, wintypes.HDC)
    user32.PrintWindow.argtypes = (wintypes.HWND, wintypes.HDC, wintypes.UINT)
    user32.SetWindowPos.argtypes = (wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT)
    gdi32.CreateCompatibleDC.argtypes = (wintypes.HDC,)
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = (wintypes.HDC, ctypes.c_int, ctypes.c_int)
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = (wintypes.HDC, wintypes.HGDIOBJ)
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.DeleteObject.argtypes = (wintypes.HGDIOBJ,)
    gdi32.DeleteDC.argtypes = (wintypes.HDC,)
    gdi32.GetDIBits.argtypes = (wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
                                ctypes.c_void_p, wintypes.UINT)

    for _name, _args in {
        "IsWindow": (wintypes.HWND,), "IsIconic": (wintypes.HWND,), "GetWindowTextLengthW": (wintypes.HWND,),
        "GetWindowTextW": (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int), "GetClassNameW": (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int),
        "GetWindowThreadProcessId": (wintypes.HWND, ctypes.POINTER(wintypes.DWORD)),
        "GetWindowRect": (wintypes.HWND, ctypes.POINTER(wintypes.RECT)), "ShowWindow": (wintypes.HWND, ctypes.c_int),
        "BringWindowToTop": (wintypes.HWND,), "SetForegroundWindow": (wintypes.HWND,),
        "PostMessageW": (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM),
        "AttachThreadInput": (wintypes.DWORD, wintypes.DWORD, wintypes.BOOL), "SetCursorPos": (ctypes.c_int, ctypes.c_int),
    }.items():
        getattr(user32, _name).argtypes = _args
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
    KEYEVENTF_KEYUP, KEYEVENTF_UNICODE, KEYEVENTF_EXTENDEDKEY = 0x0002, 0x0004, 0x0001
    MOUSE = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040)}
    MOUSEEVENTF_WHEEL = 0x0800
    _SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Windows.UI.Core.CoreWindow",
                      "NotifyIconOverflowWindow", "TopLevelWindowForOverflowXamlIsland", "XamlExplorerHostIslandWindow"}
    _EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5C}  # nav keys, Win keys

    def _send(inputs: list) -> None:
        array = (INPUT * len(inputs))(*inputs)
        sent = user32.SendInput(len(inputs), array, ctypes.sizeof(INPUT))
        if sent != len(inputs):
            raise ScreenError("Windows blocked the simulated input (is an administrator window in front?).")

    def _key(vk: int, up: bool = False) -> INPUT:
        flags = (KEYEVENTF_KEYUP if up else 0) | (KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED else 0)
        return INPUT(type=INPUT_KEYBOARD, u=_INPUTUNION(ki=KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=0)))

    def _char(code: int, up: bool = False) -> INPUT:
        flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
        return INPUT(type=INPUT_KEYBOARD, u=_INPUTUNION(ki=KEYBDINPUT(wVk=0, wScan=code, dwFlags=flags, time=0, dwExtraInfo=0)))

    def _mouse(flags: int, data: int = 0) -> INPUT:
        return INPUT(type=INPUT_MOUSE, u=_INPUTUNION(mi=MOUSEINPUT(dx=0, dy=0, mouseData=data & 0xFFFFFFFF, dwFlags=flags,
                                                                   time=0, dwExtraInfo=0)))


class WindowsDesktop:
    """The real desktop on Windows."""

    def __init__(self) -> None:
        self._own_pid = os.getpid()

    # -- windows -----------------------------------------------------------------
    def _class(self, hwnd: int) -> str:
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        return buf.value

    def window_info(self, hwnd: int) -> Window | None:
        if not hwnd or not user32.IsWindow(hwnd):
            return None
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        app = ""
        handle = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
        if handle:
            try:
                size = wintypes.DWORD(1024)
                path = ctypes.create_unicode_buffer(1024)
                if kernel32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
                    app = os.path.basename(path.value)
            finally:
                kernel32.CloseHandle(handle)
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        return Window(int(hwnd), buf.value, app, (rect.left, rect.top, rect.right, rect.bottom))

    def is_own(self, hwnd: int) -> bool:
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value == self._own_pid

    def foreground(self) -> Window | None:
        """The window the user is working in, ignoring JARVIS itself and the desktop/taskbar."""
        hwnd = user32.GetForegroundWindow()
        if not hwnd or self.is_own(hwnd) or self._class(hwnd) in _SHELL_CLASSES:
            return None
        return self.window_info(hwnd)

    def alive(self, window: Window) -> bool:
        return bool(user32.IsWindow(window.hwnd))

    # -- capture -----------------------------------------------------------------
    def capture(self, window: Window | None = None):
        from PIL import Image, ImageGrab

        if window is None:
            image = ImageGrab.grab(all_screens=True)
            left = user32.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
            top = user32.GetSystemMetrics(77)
            return Shot(image, left, top, 1.0, None)
        if user32.IsIconic(window.hwnd):
            raise ScreenError(f"{window.label} is minimised, so I can't see it.")
        info = self.window_info(window.hwnd) or window
        left, top, right, bottom = info.rect
        width, height = right - left, bottom - top
        if width <= 1 or height <= 1:
            raise ScreenError(f"I can't see {window.label}.")
        image = self._print_window(window.hwnd, width, height)
        if image is None or _mostly_black(image):  # some GPU-rendered apps draw nothing for PrintWindow
            image = ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True)
        return Shot(image, left, top, 1.0, info)

    @staticmethod
    def _print_window(hwnd: int, width: int, height: int):
        from PIL import Image

        hwnd_dc = user32.GetWindowDC(hwnd)
        mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
        bitmap = gdi32.CreateCompatibleBitmap(hwnd_dc, width, height)
        old = gdi32.SelectObject(mem_dc, bitmap)
        try:
            if not user32.PrintWindow(hwnd, mem_dc, 2):  # PW_RENDERFULLCONTENT
                return None
            header = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=width, biHeight=-height,
                                      biPlanes=1, biBitCount=32, biCompression=0)
            buffer = ctypes.create_string_buffer(width * height * 4)
            if not gdi32.GetDIBits(mem_dc, bitmap, 0, height, buffer, ctypes.byref(header), 0):
                return None
            return Image.frombuffer("RGB", (width, height), buffer, "raw", "BGRX", 0, 1)
        finally:
            gdi32.SelectObject(mem_dc, old)
            gdi32.DeleteObject(bitmap)
            gdi32.DeleteDC(mem_dc)
            user32.ReleaseDC(hwnd, hwnd_dc)

    # -- input -------------------------------------------------------------------
    def bring_to_front(self, window: Window) -> None:
        """Raise and focus ``window`` so clicks and keys reach it (Windows normally blocks background apps)."""
        hwnd = window.hwnd
        if not user32.IsWindow(hwnd):
            raise ScreenError(f"{window.label} has been closed.")
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        flags = 0x0001 | 0x0002 | 0x0040  # SWP_NOSIZE | SWP_NOMOVE | SWP_SHOWWINDOW
        user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, flags)  # topmost for a moment...
        user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, flags)  # ...then back to normal, but now on top
        fg = user32.GetForegroundWindow()
        this_thread = kernel32.GetCurrentThreadId()
        fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        attached = bool(fg_thread and fg_thread != this_thread and user32.AttachThreadInput(this_thread, fg_thread, True))
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(this_thread, fg_thread, False)
        time.sleep(0.12)

    def window_at(self, x: int, y: int) -> int:
        hwnd = user32.WindowFromPoint(wintypes.POINT(x, y))
        return int(user32.GetAncestor(hwnd, 2) or 0) if hwnd else 0  # GA_ROOT

    def click(self, x: int, y: int, button: str = "left", double: bool = False, window: Window | None = None) -> None:
        if window is not None:
            self.bring_to_front(window)
            root = self.window_at(x, y)
            if root and root != window.hwnd and not self._same_app(root, window):
                raise ScreenError(f"something is covering {window.label} where I need to click.")
        down, up = MOUSE.get(button, MOUSE["left"])
        user32.SetCursorPos(int(x), int(y))
        time.sleep(0.05)
        for _ in range(2 if double else 1):
            _send([_mouse(down), _mouse(up)])
            time.sleep(0.06)

    def _same_app(self, hwnd: int, window: Window) -> bool:
        other = self.window_info(hwnd)
        return bool(other and other.app and other.app == window.app)  # popups and menus of the same app are fine

    def type_text(self, text: str, window: Window | None = None) -> None:
        if window is not None and user32.GetForegroundWindow() != window.hwnd:
            self.bring_to_front(window)
        for ch in text:
            if ch == "\n":
                _send([_key(0x0D), _key(0x0D, True)])
                continue
            for unit in _utf16_units(ch):
                _send([_char(unit), _char(unit, True)])
            time.sleep(0.004)

    def press(self, keys: list[int], window: Window | None = None) -> None:
        if window is not None and user32.GetForegroundWindow() != window.hwnd:
            self.bring_to_front(window)
        _send([_key(k) for k in keys] + [_key(k, True) for k in reversed(keys)])

    def scroll(self, clicks: int, x: int | None = None, y: int | None = None, window: Window | None = None) -> None:
        if window is not None:
            self.bring_to_front(window)
            left, top, right, bottom = (self.window_info(window.hwnd) or window).rect
            x = (left + right) // 2 if x is None else x
            y = (top + bottom) // 2 if y is None else y
        if x is not None and y is not None:
            user32.SetCursorPos(int(x), int(y))
        _send([_mouse(MOUSEEVENTF_WHEEL, 120 * int(clicks))])

    def close(self, window: Window) -> None:
        user32.PostMessageW(window.hwnd, 0x0010, 0, 0)  # WM_CLOSE: the app still asks to save unsaved work


def _utf16_units(ch: str) -> list[int]:
    data = ch.encode("utf-16-le")
    return [int.from_bytes(data[i:i + 2], "little") for i in range(0, len(data), 2)]


def _mostly_black(image) -> bool:
    small = image.convert("L").resize((32, 18))
    return max(small.getdata()) < 12


# =============================================================================== keys
VK = {
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "escape": 0x1B, "esc": 0x1B, "space": 0x20, "spacebar": 0x20,
    "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "page up": 0x21, "pageup": 0x21, "page down": 0x22, "pagedown": 0x22, "home": 0x24, "end": 0x23, "insert": 0x2D,
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "windows": 0x5B, "win": 0x5B,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75, "f7": 0x76, "f8": 0x77, "f9": 0x78,
    "f10": 0x79, "f11": 0x7A, "f12": 0x7B, "play": 0xB3, "pause": 0xB3, "next track": 0xB0, "previous track": 0xB1,
}


def parse_keys(spec: str) -> list[int] | None:
    """'ctrl+c', 'control shift t', 'alt tab', 'enter', 'f5' -> virtual-key codes (None if not keys)."""
    spec = re.sub(r"\s*(?:\+|plus|-)\s*", " ", (spec or "").lower()).strip()
    spec = re.sub(r"\b(?:the|key|keys|button)\b", " ", spec).strip()
    for name in ("page up", "page down", "next track", "previous track"):
        spec = spec.replace(name, name.replace(" ", "_"))
    keys = []
    for token in spec.split():
        token = token.replace("_", " ")
        if token in VK:
            keys.append(VK[token])
        elif len(token) == 1 and token.isalnum():
            keys.append(ord(token.upper()))
        else:
            return None
    return keys or None


# =============================================================================== the active app, tracked continuously
class ForegroundTracker:
    """Remembers the last app the user worked in (not JARVIS), so "look at this" always means that app."""

    def __init__(self, desktop, interval: float = 0.4) -> None:
        self._desktop = desktop
        self._interval = interval
        self._last: Window | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is None and hasattr(self._desktop, "foreground"):
            self._thread = threading.Thread(target=self._run, name="foreground", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                window = self._desktop.foreground()
                if window is not None:
                    self._last = window
            except Exception:
                log.debug("foreground poll failed", exc_info=True)

    def current(self) -> Window | None:
        try:
            window = self._desktop.foreground()
        except Exception:
            window = None
        if window is not None:
            self._last = window
            return window
        if self._last is not None and getattr(self._desktop, "alive", lambda w: True)(self._last):
            return self._last
        return None


def default_desktop():
    if sys.platform == "win32":
        return WindowsDesktop()
    return BasicDesktop()


class BasicDesktop:
    """Non-Windows fallback: whole-screen capture only (enough for "what's on my screen")."""

    def foreground(self) -> Window | None:
        return None

    def alive(self, window: Window) -> bool:
        return True

    def capture(self, window: Window | None = None):
        try:
            from PIL import ImageGrab

            return Shot(ImageGrab.grab(), 0, 0, 1.0, None)
        except Exception as exc:
            raise ScreenError(f"I can't capture the screen on this system ({exc.__class__.__name__}).") from exc

    def _unsupported(self, *args, **kwargs):
        raise ScreenError("controlling the mouse and keyboard is only supported on Windows.")

    click = type_text = press = scroll = close = bring_to_front = _unsupported
