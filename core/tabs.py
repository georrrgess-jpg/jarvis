"""Browser tabs by voice: "close the Chrome tab", "close the YouTube tab", "reopen that tab".

Works with any Chromium browser or Firefox through the keyboard (Ctrl+W closes, Ctrl+Tab steps to the
next tab, Ctrl+Shift+T brings the last one back), so nothing is installed into the browser. A
browser window's title is always the title of its active tab ("YouTube - Google Chrome"), which is
how a named tab is found: step through the tabs until the title matches.
"""

from __future__ import annotations

import logging
import re
import time

from .screen import ScreenError, Window

log = logging.getLogger("jarvis.tabs")

VK_CONTROL, VK_SHIFT, VK_TAB, VK_W, VK_T = 0x11, 0x10, 0x09, ord("W"), ord("T")

BROWSERS: dict[str, tuple[str, ...]] = {
    "chrome": ("chrome.exe",),
    "edge": ("msedge.exe",),
    "firefox": ("firefox.exe",),
    "brave": ("brave.exe",),
    "opera": ("opera.exe", "opera_gx.exe"),
    "vivaldi": ("vivaldi.exe",),
}
ALL_BROWSERS = tuple(exe for exes in BROWSERS.values() for exe in exes)
BROWSER_NAMES = {"chrome": "Chrome", "edge": "Edge", "firefox": "Firefox", "brave": "Brave", "opera": "Opera",
                 "vivaldi": "Vivaldi", "browser": "your browser"}
_SUFFIX = re.compile(r"\s+[-–—]\s+(?:google chrome|microsoft​? edge|.*?mozilla firefox|brave|opera|vivaldi)\s*$", re.I)
MAX_TABS = 60


def browser_key(spoken: str) -> str:
    """'google chrome' -> 'chrome', 'microsoft edge' -> 'edge', 'web browser' -> 'browser'."""
    s = (spoken or "").lower()
    for key in BROWSERS:
        if key in s:
            return key
    return "browser"


def is_browser(window: Window | None, key: str = "browser") -> bool:
    if window is None:
        return False
    exes = ALL_BROWSERS if key == "browser" else BROWSERS.get(key, ())
    return (window.app or "").lower() in exes


def tab_title(window_title: str) -> str:
    """'Never Gonna Give You Up - YouTube - Google Chrome' -> 'Never Gonna Give You Up - YouTube'."""
    return _SUFFIX.sub("", window_title or "").strip()


def _matches(title: str, wanted: str) -> bool:
    t = re.sub(r"[^a-z0-9 ]", " ", tab_title(title).lower())
    words = [w for w in re.sub(r"[^a-z0-9 ]", " ", wanted.lower()).split() if w not in ("the", "my", "a", "tab", "page")]
    if not words:
        return False
    squashed = t.replace(" ", "")
    return all(w in t.split() or w in squashed for w in words) or "".join(words) in squashed


def browser_windows(desktop, key: str = "browser", current: Window | None = None) -> list[Window]:
    """Open windows of that browser, the one the user worked in last first."""
    found = [w for w in desktop.windows() if is_browser(w, key)]
    if current is not None and is_browser(current, key):
        found.sort(key=lambda w: w.hwnd != current.hwnd)
    return found


def close_current_tab(desktop, key: str = "browser", current: Window | None = None) -> Window:
    windows = browser_windows(desktop, key, current)
    if not windows:
        raise ScreenError(f"{BROWSER_NAMES.get(key, key.title())} isn't open.")
    window = windows[0]
    desktop.press([VK_CONTROL, VK_W], window=window)
    return window


def find_tab(desktop, wanted: str, key: str = "browser", current: Window | None = None, settle: float = 0.18) -> Window | None:
    """Bring the tab whose title matches ``wanted`` to the front; returns its window, or None."""
    windows = browser_windows(desktop, key, current)
    for window in windows:  # the active tab of any window first: no need to touch anything
        if _matches(window.title, wanted):
            desktop.bring_to_front(window)
            return window
    for window in windows:
        desktop.bring_to_front(window)
        start = (desktop.window_info(window.hwnd) or window).title
        for _ in range(MAX_TABS):
            desktop.press([VK_CONTROL, VK_TAB], window=window)
            time.sleep(settle)
            info = desktop.window_info(window.hwnd)
            if info is None:
                break
            if _matches(info.title, wanted):
                return info
            if info.title == start:
                break  # back where we started: it isn't in this window
    return None


def close_named_tab(desktop, wanted: str, key: str = "browser", current: Window | None = None) -> str:
    """Close the tab called ``wanted``; returns its title. Raises ScreenError when it can't be found."""
    if not browser_windows(desktop, key, current):
        raise ScreenError(f"{BROWSER_NAMES.get(key, key.title())} isn't open.")
    window = find_tab(desktop, wanted, key, current)
    if window is None:
        raise ScreenError(f"I couldn't find a {wanted} tab in {BROWSER_NAMES.get(key, 'your browser')}.")
    title = tab_title(window.title)
    desktop.press([VK_CONTROL, VK_W], window=window)
    return title


def reopen_tab(desktop, key: str = "browser", current: Window | None = None) -> Window:
    windows = browser_windows(desktop, key, current)
    if not windows:
        raise ScreenError(f"{BROWSER_NAMES.get(key, key.title())} isn't open.")
    desktop.press([VK_CONTROL, VK_SHIFT, VK_T], window=windows[0])
    return windows[0]


def close_browser(desktop, key: str = "browser", current: Window | None = None) -> int:
    windows = browser_windows(desktop, key, current)
    if not windows:
        raise ScreenError(f"{BROWSER_NAMES.get(key, key.title())} isn't open.")
    for w in windows:
        desktop.close(w)
    return len(windows)
