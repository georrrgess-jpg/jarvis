"""The browser manager: opening sites in the right browser profile and checking where the browser really ended up.

Chrome is not ready the moment it launches. It can open on a profile picker ("Who's using Chrome?"), a
first-run welcome page, a Google sign-in page or account chooser, a consent page, or an error page instead of
the site that was asked for. JARVIS therefore:

* starts Chrome / Edge / Brave **in a specific profile** (the one signed in to the Google account linked in
  Settings, else the one used last), which skips the profile picker and keeps Google links in the right account;
* reuses the running browser (a new tab in the existing window, never a pile of new windows);
* after navigating, **watches the active tab** (window title, and the address bar read through Windows
  accessibility) until it is the page asked for, or a blocker is recognised, within a bounded time;
* tells a page that needs the user's Google account (Gmail, Drive, Docs...) apart from a public one (YouTube,
  Wikipedia): only the former can legitimately be blocked by a sign-in page;
* when the user really must sign in, says so, waits for them to do it themselves (passwords are never typed
  or touched), and carries on once the page has loaded.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from .browsers import BROWSERS, GOOGLE_HOSTS, browser_path, open_in_browser, with_account

log = logging.getLogger("jarvis.browser")

# states of the browser, as the HUD and the log show them
NOT_RUNNING, LAUNCHING, PROFILE_SELECTION, AUTHENTICATION_REQUIRED, FIRST_RUN_SETUP = (
    "NOT_RUNNING", "LAUNCHING", "PROFILE_SELECTION", "AUTHENTICATION_REQUIRED", "FIRST_RUN_SETUP")
READY, NAVIGATING, PAGE_LOADING, PAGE_READY, CONSENT, OFFLINE, ERROR = (
    "READY", "NAVIGATING", "PAGE_LOADING", "PAGE_READY", "CONSENT_REQUIRED", "OFFLINE", "ERROR")
BLOCKERS = (PROFILE_SELECTION, AUTHENTICATION_REQUIRED, FIRST_RUN_SETUP, CONSENT, OFFLINE, ERROR)

_EXES = {"chrome": "chrome", "edge": "msedge", "brave": "brave", "firefox": "firefox", "opera": "opera", "vivaldi": "vivaldi"}
_TITLE_SUFFIX = re.compile(r"\s+[-–—]\s+(?:Google Chrome|Mozilla Firefox|Firefox|Brave|Opera|Vivaldi|Chromium|"
                           r"(?:[^-–—]+?\s+[-–—]\s+)?Microsoft\u200b?\s*Edge)$")  # Edge puts the profile name in the middle
_USER_DATA = {  # Chromium browsers keep their profiles here (relative to %LOCALAPPDATA%)
    "chrome": r"Google\Chrome\User Data", "edge": r"Microsoft\Edge\User Data", "brave": r"BraveSoftware\Brave-Browser\User Data",
}

# what the active tab tells us (title first, then the address when it can be read)
_TITLE_RULES = [
    (PROFILE_SELECTION, re.compile(r"who'?s using (?:chrome|edge|brave)|choose a (?:chrome |browser )?profile|^(?:profile picker|select a profile)$", re.I)),
    (FIRST_RUN_SETUP, re.compile(r"^(?:welcome to (?:google )?chrome|welcome to microsoft edge|sign in to chrome|make chrome your own|turn on sync\b|"
                                 r"set up your new (?:chrome )?profile|customi[sz]e your (?:chrome )?profile|get started with chrome|"
                                 r"chrome is ready|let'?s get you set up)", re.I)),
    (AUTHENTICATION_REQUIRED, re.compile(r"^(?:sign in|sign-in|log in)\s*[-–—:]\s*google accounts?$|^google accounts?$|^choose an account(?:\s*[-–—].*)?$|"
                                         r"^sign in\s*[-–—]\s*google|^sign in to (?:your )?google|^verify it'?s you|^2-step verification|"
                                         r"^gmail$|^google drive:? sign-?in$|^sign in(?: to continue)?(?: to (?:gmail|youtube|google drive|google docs))?$", re.I)),
    (AUTHENTICATION_REQUIRED, re.compile(r"^(?:gmail: (?:private and secure|free,? private|email from google)|google drive: (?:share files|cloud storage|free cloud)|"
                                         r"google docs: (?:sign-?in|online document)|google workspace)", re.I)),  # signed-out sales pages
    (CONSENT, re.compile(r"^before you continue(?: to (?:google|youtube).*)?$", re.I)),
    (OFFLINE, re.compile(r"^(?:no internet|you'?re offline|there is no internet connection|log in to (?:the )?network|captive portal)", re.I)),
    (ERROR, re.compile(r"^(?:this site can.?t be reached|this page isn.?t working|privacy error|your connection is(?: not|n.?t) private|"
                       r"err_[a-z_]+|problem loading page|server not found|hmm\.{0,3} can.?t reach this page|aw,? snap)", re.I)),
]
_LOADING_TITLES = re.compile(r"^(?:new tab|untitled|loading\.*|about:blank|start page|youtube|google)$", re.I)


@dataclass
class Look:
    state: str
    title: str = ""
    url: str = ""
    window: object = None


def tab_title(window_title: str) -> str:
    return _TITLE_SUFFIX.sub("", (window_title or "").strip()).strip()


def classify(title: str, url: str = "") -> str:
    """The state a tab is in, from its title (and address when known)."""
    u = (url or "").lower().strip()
    if u:
        if not re.match(r"^[a-z-]+://", u):
            u = "https://" + u
        host = urlparse(u).hostname or ""
        if u.startswith(("chrome://profile-picker", "edge://profile-picker")):
            return PROFILE_SELECTION
        if u.startswith(("chrome://welcome", "chrome://intro", "chrome://signin", "edge://welcome", "chrome://newtab/#signin")):
            return FIRST_RUN_SETUP
        if u.startswith(("chrome-error://", "edge-error://")):
            return ERROR
        if host == "accounts.google.com" and re.search(r"/(?:v\d/)?(?:signin|servicelogin|accountchooser|interactivelogin|signup|challenge|speedbump)", u):
            return AUTHENTICATION_REQUIRED
        if host in ("consent.google.com", "consent.youtube.com"):
            return CONSENT
        if host == "workspace.google.com" or (host == "www.google.com" and re.search(r"/(?:gmail|drive|docs|sheets|slides)/about", u)):
            return AUTHENTICATION_REQUIRED  # signed out: Gmail / Drive send you to their sales page instead
    t = tab_title(title)
    for state, pattern in _TITLE_RULES:
        if pattern.search(t):
            return state
    if not t or _LOADING_TITLES.match(t):
        return PAGE_LOADING
    return READY


def needs_account(url: str) -> bool:
    """Gmail, Drive, Docs... are private to a Google account; YouTube, Google Search, Wikipedia are public."""
    host = (urlparse(url if "://" in url else "https://" + url).hostname or "").lower()
    return host in GOOGLE_HOSTS or host in ("myaccount.google.com", "accounts.google.com", "one.google.com", "admin.google.com")


def site_name(url: str) -> str:
    host = (urlparse(url if "://" in url else "https://" + url).hostname or "").lower().removeprefix("www.").removeprefix("m.")
    known = {"youtube.com": "YouTube", "music.youtube.com": "YouTube Music", "mail.google.com": "Gmail", "drive.google.com": "Google Drive",
             "docs.google.com": "Google Docs", "calendar.google.com": "Google Calendar", "google.com": "Google", "wikipedia.org": "Wikipedia",
             "github.com": "GitHub", "reddit.com": "Reddit", "netflix.com": "Netflix", "x.com": "X", "twitch.tv": "Twitch",
             "maps.google.com": "Google Maps", "news.google.com": "Google News", "translate.google.com": "Google Translate"}
    if host in known:
        return known[host]
    for h, name in known.items():
        if host.endswith("." + h):
            return name
    return host.split(".")[-2].title() if host.count(".") >= 1 else host


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


_PLACEHOLDER_TITLES = {"", "untitled", "new tab", "loading", "loading...", "loading…", "about:blank"}


def still_loading(look: Look) -> bool:
    """Chrome shows "Untitled" or the bare address as the tab title until the page itself has a title."""
    t = tab_title(look.title).strip().lower()
    if t in _PLACEHOLDER_TITLES:
        return True
    u = look.url.lower().split("://")[-1].rstrip("/")
    return bool(u and t.split("://")[-1].rstrip("/") in (u, u.removeprefix("www."))) or bool(re.fullmatch(r"[\w.-]+\.[a-z]{2,}(?::\d+)?(?:/\S*)?", t))


def matches(look: Look, expect_host: str = "", expect_title: str = "", expect_site: str = "") -> bool:
    """Has the active tab become the page we asked for (and finished loading it)?"""
    u = look.url.lower()
    if u and expect_host:
        host = (urlparse(u if "://" in u else "https://" + u).hostname or "").removeprefix("www.")
        want = expect_host.lower().removeprefix("www.")
        if host == want or host.endswith("." + want):
            return look.state not in BLOCKERS and not still_loading(look)
    t = _norm(tab_title(look.title))
    if expect_title:
        want = _norm(expect_title)
        if want and t and (want in t or (len(t) > 12 and t in want)):
            return True
    if expect_site and t:
        site = _norm(expect_site)
        if site and (t.endswith(" " + site) or t == site or t.startswith(site + " ")) and look.state == READY:
            return True
    return False


# ----------------------------------------------------------------------------- profiles
@dataclass
class Profile:
    directory: str  # "Default", "Profile 1"
    name: str
    email: str = ""
    last_used: bool = False


def read_profiles(browser: str, local_app_data: str | None = None) -> list[Profile]:
    """The profiles of a Chromium browser, from its 'Local State' file (no browser needed)."""
    root = local_app_data if local_app_data is not None else os.environ.get("LOCALAPPDATA", "")
    sub = _USER_DATA.get(browser)
    if not root or not sub:
        return []
    path = Path(root).joinpath(*sub.split("\\")) / "Local State"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    info = (data.get("profile") or {}).get("info_cache") or {}
    last = (data.get("profile") or {}).get("last_used") or ""
    out = [Profile(d, str(v.get("name") or v.get("gaia_name") or d), str(v.get("user_name") or ""), d == last)
           for d, v in info.items() if isinstance(v, dict)]
    out.sort(key=lambda p: (not p.last_used, p.directory != "Default", p.directory))
    return out


def pick_profile(profiles: list[Profile], email: str = "", wanted: str = "") -> Profile | None:
    """The profile to open links in: the one chosen in Settings, else the one signed in to the linked Google
    account, else the one used last."""
    if not profiles:
        return None
    if wanted and wanted != "auto":
        chosen = next((p for p in profiles if p.directory == wanted or p.name.lower() == wanted.lower()), None)
        if chosen:
            return chosen
    if email:
        chosen = next((p for p in profiles if p.email.lower() == email.lower()), None)
        if chosen:
            return chosen
    return next((p for p in profiles if p.last_used), profiles[0])


# ----------------------------------------------------------------------------- the manager
@dataclass
class NavResult:
    ok: bool
    state: str
    title: str = ""
    url: str = ""
    verified: bool = False
    browser: str = ""
    seconds: float = 0.0
    transitions: list = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.state in BLOCKERS

    def to_dict(self) -> dict:
        return {"ok": self.ok, "state": self.state, "title": self.title, "url": self.url, "verified": self.verified,
                "browser": self.browser, "seconds": round(self.seconds, 1), "transitions": self.transitions[-8:]}


class BrowserManager:
    def __init__(self, config, windows: Callable[[], list], helper=None, popen=subprocess.Popen,
                 sleep: Callable[[float], None] = time.sleep, profiles: Callable[[str], list[Profile]] | None = None,
                 front: Callable[[], object] | None = None, bring_to_front: Callable[[object], None] | None = None,
                 on_state: Callable[[dict], None] | None = None, watch: bool | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.config = config
        self.watch = (sys.platform == "win32") if watch is None else watch  # can we see the browser's windows?
        self._clock = clock
        self.url_launcher: Callable[[str], None] | None = None  # an embedder's own way of opening links (tests, tools)
        self._windows = windows
        self.helper = helper
        self._popen = popen
        self._sleep = sleep
        self._profiles = profiles or read_profiles
        self._front = front
        self._bring = bring_to_front
        self._on_state = on_state
        self.state = NOT_RUNNING
        self.last: Look = Look(NOT_RUNNING)
        self.history: list[dict] = []  # state transitions, for diagnostics
        self._url_cache: dict[int, tuple[float, str]] = {}
        self._lock = threading.Lock()
        self.waiting: dict | None = None  # a sign-in JARVIS is waiting for the user to finish

    # -- which browser
    def key(self) -> str:
        chosen = str(self.config.get("link_browser") or "default")
        if chosen in BROWSERS:
            return chosen
        running = self.running_keys()
        if running:
            return running[0]
        return "chrome" if sys.platform == "win32" and browser_path("chrome") else "default"

    def running_keys(self) -> list[str]:
        seen = []
        for w in self._safe_windows():
            exe = (getattr(w, "app", "") or "").lower().removesuffix(".exe")
            key = next((k for k, v in _EXES.items() if v == exe), None)
            if key and key not in seen:
                seen.append(key)
        return seen

    def label(self, key: str | None = None) -> str:
        key = key or self.key()
        return BROWSERS[key][0] if key in BROWSERS else "your browser"

    def profile(self, key: str | None = None) -> Profile | None:
        key = key or self.key()
        if key not in _USER_DATA:
            return None
        try:
            return pick_profile(self._profiles(key), str(self.config.get("google_user_email") or ""),
                                str(self.config.get("browser_profile") or "auto"))
        except Exception:
            log.debug("couldn't read browser profiles", exc_info=True)
            return None

    def profiles(self, key: str | None = None) -> list[dict]:
        key = key or self.key()
        try:
            return [{"directory": p.directory, "name": p.name, "email": p.email, "last_used": p.last_used} for p in self._profiles(key)]
        except Exception:
            return []

    # -- looking
    def _safe_windows(self) -> list:
        try:
            return list(self._windows())
        except Exception:
            return []

    def windows(self, key: str | None = None) -> list:
        key = key or self.key()
        exes = {_EXES[key]} if key in _EXES else set(_EXES.values())
        return [w for w in self._safe_windows() if (getattr(w, "app", "") or "").lower().removesuffix(".exe") in exes and (getattr(w, "title", "") or "").strip()]

    def read_url(self, window, max_age: float = 1.0) -> str:
        """The address in a browser window's address bar ('' if it can't be read)."""
        if not self.helper or window is None:
            return ""
        hwnd = int(getattr(window, "hwnd", 0) or 0)
        cached = self._url_cache.get(hwnd)
        if cached and time.monotonic() - cached[0] < max_age:
            return cached[1]
        url = ""
        try:
            if self.helper.has("uia"):
                reply = self.helper.call("browser.url", hwnd=hwnd, timeout=6.0)
                url = str(reply.get("url") or "") if reply.get("ok") else ""
        except Exception:
            log.debug("couldn't read the address bar", exc_info=True)
        self._url_cache[hwnd] = (time.monotonic(), url)
        return url

    def look(self, key: str | None = None, read_url: bool = True) -> Look:
        """What the browser is showing right now (its front window's active tab)."""
        wins = self.windows(key)
        if not wins:
            return self._set(Look(NOT_RUNNING))
        front = None
        if self._front:
            try:
                f = self._front()
                front = next((w for w in wins if getattr(w, "hwnd", None) == getattr(f, "hwnd", -1)), None)
            except Exception:
                front = None
        w = front or wins[0]
        url = self.read_url(w) if read_url else ""
        return self._set(Look(classify(w.title, url), w.title, url, w))

    def _set(self, look: Look) -> Look:
        if look.state != self.state or tab_title(look.title) != tab_title(self.last.title):
            if look.state != self.state:
                self.history.append({"at": time.time(), "from": self.state, "to": look.state, "title": tab_title(look.title)[:80]})
                self.history = self.history[-40:]
            self.state = look.state
            self.last = look
            if self._on_state:
                try:
                    self._on_state(self.status())
                except Exception:
                    pass
        self.last = look
        return look

    def status(self) -> dict:
        p = self.profile() if self.key() in _USER_DATA else None
        return {"state": self.state, "title": tab_title(self.last.title), "url": self.last.url, "browser": self.label(),
                "profile": ({"name": p.name, "email": p.email, "directory": p.directory} if p else None),
                "waiting": dict(self.waiting) if self.waiting else None, "history": self.history[-6:],
                "address_bar": bool(self.helper and self.helper.caps.get("uia"))}

    # -- launching and navigating
    def _launch(self, url: str | None, key: str) -> None:
        path = browser_path(key) if key in BROWSERS else None
        if path and key in _USER_DATA:
            args = [path] + os.environ.get("JARVIS_BROWSER_ARGS", "").split()  # extra switches (used by the CI checks)
            p = self.profile(key)
            if p:
                args.append(f"--profile-directory={p.directory}")  # straight into the right profile: no picker
            if url:
                args.append(url)
            else:
                args.append("--new-window")
            self._popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        if url:
            open_in_browser(url, key if key in BROWSERS else "default", popen=self._popen)
        elif path:
            self._popen([path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def open(self, url: str, expect_host: str = "", expect_title: str = "", wait: float = 12.0, account: bool | None = None) -> NavResult:
        """Open ``url`` and watch until it has loaded, or a blocker shows, or ``wait`` seconds pass."""
        key = self.key()
        email = str(self.config.get("google_user_email") or "")
        target = with_account(url, email)
        account = needs_account(url) if account is None else account
        host = expect_host or (urlparse(url).hostname or "").removeprefix("www.")
        site = site_name(url)
        before = {getattr(w, "hwnd", None): getattr(w, "title", "") for w in self.windows(key)}
        started = self._clock()
        transitions = [NOT_RUNNING if not before else READY]
        with self._lock:
            self.state = NAVIGATING if before else LAUNCHING
            try:
                if self.url_launcher is not None:
                    self.url_launcher(target)
                else:
                    self._launch(target, key)
            except OSError as exc:
                log.warning("couldn't start the browser: %s", exc)
                return NavResult(False, ERROR, browser=self.label(key), transitions=transitions + [ERROR])
        if not self.watch:
            return NavResult(True, NAVIGATING, browser=self.label(key), transitions=transitions)  # nothing to watch here
        last = Look(LAUNCHING)
        blocker_since = None
        seen_match, seen_at = "", 0.0  # account pages (Gmail, Drive) redirect to sign-in after a moment: the match has to hold for 1.5 s
        while self._clock() - started < wait:
            self._sleep(0.4)
            look = self.look(key)
            if look.state != transitions[-1]:
                transitions.append(look.state)
            last = look
            if look.state == NOT_RUNNING:
                continue  # still starting
            changed = not before or before.get(getattr(look.window, "hwnd", None)) != look.title
            if matches(look, host, expect_title, site):
                if account and (seen_match != f"{look.url}|{look.title}" or self._clock() - seen_at < 1.5):
                    if seen_match != f"{look.url}|{look.title}":
                        seen_match, seen_at = f"{look.url}|{look.title}", self._clock()
                    continue
                return self._done(NavResult(True, PAGE_READY, look.title, look.url, True, self.label(key), self._clock() - started, transitions + [PAGE_READY]))
            if look.state in BLOCKERS and (changed or look.state in (PROFILE_SELECTION, FIRST_RUN_SETUP)):
                if look.state == AUTHENTICATION_REQUIRED and not account:
                    blocker_since = blocker_since or self._clock()
                    if self._clock() - blocker_since < 4.0:
                        continue  # a public page: the sign-in tab may be something else that opened first; give ours a moment
                elif look.state in (OFFLINE, ERROR):
                    if blocker_since is None:
                        blocker_since = self._clock()
                        continue  # error pages can flash up during redirects
                return self._done(NavResult(False, look.state, look.title, look.url, True, self.label(key), self._clock() - started, transitions))
            seen_match = ""
        state = last.state if last.state in BLOCKERS else PAGE_LOADING
        return self._done(NavResult(False, state, last.title, last.url, False, self.label(key), self._clock() - started, transitions))

    def _done(self, result: NavResult) -> NavResult:
        log.info("navigation: %s", result.to_dict())
        return result

    def launch_browser(self, wait: float = 15.0) -> NavResult:
        """"Open Chrome": bring the running one forward, or start it (in the right profile) and wait until it's usable."""
        key = self.key()
        wins = self.windows(key)
        if wins:
            if self._bring:
                try:
                    self._bring(wins[0])
                except Exception:
                    pass
            look = self.look(key)
            return NavResult(look.state not in BLOCKERS, look.state, look.title, look.url, True, self.label(key), 0.0, ["RUNNING", look.state])
        started = self._clock()
        transitions = [NOT_RUNNING, LAUNCHING]
        try:
            self._launch(None, key)
        except OSError:
            return NavResult(False, ERROR, browser=self.label(key), transitions=transitions + [ERROR])
        if not self.watch:
            return NavResult(True, LAUNCHING, browser=self.label(key), transitions=transitions)
        last = Look(LAUNCHING)
        while self._clock() - started < wait:
            self._sleep(0.5)
            look = self.look(key)
            if look.state == NOT_RUNNING:
                continue
            if look.state != transitions[-1]:
                transitions.append(look.state)
            last = look
            if look.state in (PROFILE_SELECTION, FIRST_RUN_SETUP, AUTHENTICATION_REQUIRED):
                return NavResult(False, look.state, look.title, look.url, True, self.label(key), self._clock() - started, transitions)
            if look.state in (READY, PAGE_LOADING) and self._clock() - started > 1.5:
                return NavResult(True, READY, look.title, look.url, True, self.label(key), self._clock() - started, transitions + [READY])
        return NavResult(False, last.state if last.state != LAUNCHING else NOT_RUNNING, last.title, last.url, False, self.label(key),
                         self._clock() - started, transitions)

    def wait_until(self, expect_host: str, expect_title: str = "", expect_site: str = "", timeout: float = 300.0,
                   cancel: threading.Event | None = None, poll: float = 2.0) -> NavResult:
        """Wait (in the background) for the user to get past a blocker (sign in, pick a profile) to the page."""
        started = self._clock()
        last = Look(self.state)
        while self._clock() - started < timeout:
            if cancel is not None and cancel.is_set():
                break
            self._sleep(poll)
            look = self.look()
            last = look
            if matches(look, expect_host, expect_title, expect_site):
                return NavResult(True, PAGE_READY, look.title, look.url, True, self.label(), self._clock() - started)
        return NavResult(False, last.state, last.title, last.url, False, self.label(), self._clock() - started)
