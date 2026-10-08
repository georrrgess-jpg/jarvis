"""Opening links in the right browser, signed in as the right Google account.

The default browser is often not the one the user is signed in to Google with, so Google links
(Docs, Gmail...) can land on a login page. Two remedies: pick the browser in Settings, and always
tell Google which account the link is for (``authuser=<email>``), which also avoids the
"wrong account" page when several accounts are signed in.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

log = logging.getLogger("jarvis.browsers")

BROWSERS = {  # setting value -> (label, Windows exe, executable names elsewhere)
    "chrome": ("Google Chrome", "chrome.exe", ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")),
    "edge": ("Microsoft Edge", "msedge.exe", ("microsoft-edge", "microsoft-edge-stable")),
    "firefox": ("Firefox", "firefox.exe", ("firefox",)),
    "brave": ("Brave", "brave.exe", ("brave-browser", "brave")),
}
GOOGLE_HOSTS = {
    "docs.google.com", "drive.google.com", "mail.google.com", "calendar.google.com", "meet.google.com",
    "keep.google.com", "photos.google.com", "contacts.google.com", "script.google.com", "sheets.google.com",
    "slides.google.com", "forms.google.com",
}


def with_account(url: str, email: str | None) -> str:
    """Add ``authuser`` to Google app links so they open as ``email`` (no-op for other sites)."""
    if not email or "@" not in email:
        return url
    parts = urlparse(url)
    if parts.scheme != "https" or (parts.hostname or "") not in GOOGLE_HOSTS:
        return url
    query = parse_qsl(parts.query, keep_blank_values=True)
    if any(k == "authuser" for k, _ in query):
        return url
    path = parts.path
    if parts.hostname == "mail.google.com" and path in ("", "/"):
        path = "/mail/"
    query.append(("authuser", email))
    return urlunparse(parts._replace(path=path, query=urlencode(query)))


def browser_path(name: str) -> str | None:
    """Full path of an installed browser, or None."""
    entry = BROWSERS.get(name)
    if not entry:
        return None
    _label, exe, names = entry
    if sys.platform == "win32":
        import winreg

        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}") as key:
                    path = os.path.expandvars(str(winreg.QueryValueEx(key, "")[0] or "")).strip().strip('"')
                    if path and Path(path).is_file():
                        return path
            except OSError:
                continue
        roots = [os.environ.get(v) for v in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")]
        subdirs = {"chrome.exe": r"Google\Chrome\Application", "msedge.exe": r"Microsoft\Edge\Application",
                   "firefox.exe": r"Mozilla Firefox", "brave.exe": r"BraveSoftware\Brave-Browser\Application"}
        for root in filter(None, roots):
            candidate = Path(root) / subdirs[exe] / exe
            if candidate.is_file():
                return str(candidate)
        return None
    return next((p for p in map(shutil.which, names) if p), None)


def installed_browsers() -> dict[str, str]:
    return {name: BROWSERS[name][0] for name in BROWSERS if browser_path(name)}


def open_in_browser(url: str, browser: str = "default", popen=subprocess.Popen) -> str:
    """Open ``url``; returns the label of the browser used. Falls back to the default browser."""
    if browser in BROWSERS:
        path = browser_path(browser)
        if path:
            try:
                popen([path, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return BROWSERS[browser][0]
            except OSError:
                log.warning("Could not start %s; using the default browser", path, exc_info=True)
    webbrowser.open(url)
    return "your default browser"
