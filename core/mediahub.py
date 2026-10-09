"""The media hub: what is playing on this PC, controlling the right player, and checking that it worked.

Sources, best first:

1. **Windows media sessions** (through the Windows helper): every player that shows up in Windows' volume
   flyout (Chrome / Edge tabs such as YouTube and YouTube Music, Spotify, VLC, Media Player...), with its
   title, artist, playing/paused state, position and the buttons it supports. Commands go to one session,
   and the session is read again afterwards: "Paused" is only said when Windows reports it paused.
2. **The YouTube tab itself**: for what sessions can't do (jump 10 seconds, restart, full screen, captions,
   the video's own mute and speed), JARVIS switches to the YouTube tab and uses YouTube's own keyboard
   shortcuts, then checks the result where Windows lets it.
3. **Media keys and window titles**: if the helper isn't available, the keyboard's media keys, with Spotify's
   window title as the check.

App volumes (Windows' mixer) let "turn the music down" lower the music, not JARVIS, and let JARVIS lower the
music while it speaks. The original volumes are written to disk first, so they come back even after a crash.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

log = logging.getLogger("jarvis.media")

BROWSER_PROCS = {"chrome": "Chrome", "msedge": "Edge", "firefox": "Firefox", "brave": "Brave", "opera": "Opera", "vivaldi": "Vivaldi"}
_APP_NAMES = [  # (pattern on the session's app id, friendly name, process name)
    (r"^chrome$|chrome\.exe$|google\.chrome", "Chrome", "chrome"),
    (r"^msedge$|msedge|microsoftedge", "Edge", "msedge"),
    (r"^308046b0af4a39cb$|firefox", "Firefox", "firefox"),
    (r"brave", "Brave", "brave"),
    (r"opera", "Opera", "opera"),
    (r"spotify", "Spotify", "spotify"),
    (r"vlc", "VLC", "vlc"),
    (r"zunemusic|zunevideo|media\.player|mediaplayer|wmplayer", "Media Player", "Microsoft.Media.Player"),
    (r"applemusic|itunes", "Apple Music", "AppleMusic"),
    (r"amazon.*music", "Amazon Music", "Amazon Music"),
    (r"deezer", "Deezer", "Deezer"),
    (r"tidal", "TIDAL", "TIDAL"),
]
ACTIONS = ("play", "pause", "toggle", "next", "previous", "stop", "seek")


def app_info(app_id: str) -> tuple[str, str]:
    """(friendly name, process name) for a media session's app id ("Chrome", "Spotify.exe", "Microsoft.ZuneMusic_...")."""
    low = (app_id or "").lower()
    for pattern, name, proc in _APP_NAMES:
        if re.search(pattern, low):
            return name, proc
    base = re.sub(r"\.exe$", "", (app_id or "").split("!")[0].split("_")[0].split(".")[-1], flags=re.I)
    return (base or "a player"), base.lower()


@dataclass
class MediaSession:
    app_id: str
    app: str  # "Chrome", "Spotify"...
    process: str  # "chrome", "spotify"... (for app volume)
    title: str = ""
    artist: str = ""
    album: str = ""
    status: str = "unknown"  # playing | paused | stopped | unknown
    position: float | None = None
    duration: float | None = None
    can: set = field(default_factory=set)
    index: int | None = None
    current: bool = False
    site: str = ""  # "YouTube" / "YouTube Music" when the browser tab is known to be that
    source: str = "sessions"  # sessions | window

    @property
    def key(self) -> str:
        return f"{self.app_id}|{self.title}"

    @property
    def browser(self) -> bool:
        return self.process in BROWSER_PROCS

    @property
    def label(self) -> str:
        """'YouTube', 'Spotify', 'Chrome'..."""
        return self.site or self.app

    @property
    def playing(self) -> bool:
        return self.status == "playing"

    def spoken(self) -> str:
        if self.title and self.artist and self.artist.lower() not in self.title.lower():
            return f"{self.title} by {self.artist}"
        return self.title or f"something in {self.app}"

    def to_dict(self) -> dict:
        return {"key": self.key, "app": self.app, "label": self.label, "title": self.title, "artist": self.artist,
                "album": self.album, "status": self.status, "position": self.position, "duration": self.duration,
                "can": sorted(self.can), "site": self.site, "source": self.source, "current": self.current}


_STATUS = {"playing": "playing", "paused": "paused", "stopped": "stopped", "closed": "stopped", "opened": "paused", "changing": "playing"}


def sessions_from_helper(data: dict) -> list[MediaSession]:
    out = []
    for raw in data.get("sessions") or []:
        app_id = str(raw.get("app") or "")
        name, proc = app_info(app_id)
        can = {k for k, v in (raw.get("can") or {}).items() if v}
        position = raw.get("position")
        duration = raw.get("duration")
        status = _STATUS.get(str(raw.get("status") or "").lower(), "unknown")
        if status == "playing" and isinstance(position, (int, float)) and isinstance(raw.get("updated_ago"), (int, float)):
            position = position + max(0.0, float(raw["updated_ago"]))  # Windows reports where it was at the last update
            if isinstance(duration, (int, float)) and duration > 0:
                position = min(position, duration)
        out.append(MediaSession(app_id, name, proc, str(raw.get("title") or ""), str(raw.get("artist") or ""),
                                str(raw.get("album") or ""), status, position if isinstance(position, (int, float)) else None,
                                duration if isinstance(duration, (int, float)) and duration > 0 else None, can,
                                raw.get("index"), bool(raw.get("current"))))
    return out


_YT_TITLE = re.compile(r"^(?:\(\d+\)\s*)?(?P<t>.+?)\s+-\s+YouTube(?P<music>\s+Music)?(?:\s+[-–—]\s+.*)?$")
_SPOTIFY_IDLE = re.compile(r"^(?:spotify(?:\s+(?:premium|free))?|advertisement|spotify\s+-\s+web\s+player.*)$", re.I)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def tag_sites(sessions: list[MediaSession], windows) -> None:
    """A browser session is 'YouTube' when a browser window's title shows that video ("<title> - YouTube - Chrome")."""
    yt = []
    for w in windows or []:
        m = _YT_TITLE.match((getattr(w, "title", "") or "").strip())
        if m:
            yt.append((_norm(m.group("t")), "YouTube Music" if m.group("music") else "YouTube"))
    for s in sessions:
        if not s.browser:
            continue
        t = _norm(s.title)
        for title, site in yt:
            if t and (t == title or t in title or title in t):
                s.site = site
                break
        if not s.site and s.artist and _norm(s.artist).endswith(" topic"):
            s.site = "YouTube Music"


def sessions_from_windows(windows) -> list[MediaSession]:
    """Without the helper: what window titles show (Spotify: 'Artist - Song'; a YouTube tab; VLC)."""
    out = []
    for w in windows or []:
        app = (getattr(w, "app", "") or "").lower().removesuffix(".exe")
        title = (getattr(w, "title", "") or "").strip()
        if not title:
            continue
        if app == "spotify":
            if _SPOTIFY_IDLE.match(title):
                out.append(MediaSession("Spotify.exe", "Spotify", "spotify", status="paused", can={"toggle", "next", "previous"}, source="window"))
            else:
                artist, sep, song = title.partition(" - ")
                out.append(MediaSession("Spotify.exe", "Spotify", "spotify", song.strip() if sep else title, artist.strip() if sep else "",
                                        status="playing", can={"toggle", "next", "previous"}, source="window"))
        elif app in BROWSER_PROCS:
            m = _YT_TITLE.match(title)
            if m and _norm(m.group("t")) not in ("youtube", "home", "subscriptions", "library", "history"):
                site = "YouTube Music" if m.group("music") else "YouTube"
                text = m.group("t").strip()
                song, artist = (text.split(" • ", 1) + [""])[:2] if site == "YouTube Music" and " • " in text else (text, "")
                out.append(MediaSession(app.title(), BROWSER_PROCS[app], app, song.strip(), artist.strip(), status="unknown",
                                        can={"toggle", "next", "previous"}, site=site, source="window"))
        elif app == "vlc":
            m = re.match(r"^(?P<t>.+?)\s+-\s+VLC media player$", title, re.I)
            if m:
                out.append(MediaSession("VLC", "VLC", "vlc", m.group("t").strip(), status="unknown", can={"toggle", "next", "previous"},
                                        source="window"))
    return out


# ----------------------------------------------------------------------------- choosing the player
_HINTS = {
    "youtube music": lambda s: s.site == "YouTube Music",
    "youtube": lambda s: s.site.startswith("YouTube") or (s.browser and not s.site),
    "video": lambda s: s.site == "YouTube" or s.browser or s.process in ("vlc", "microsoft.media.player"),
    "spotify": lambda s: s.process == "spotify",
    "vlc": lambda s: s.process == "vlc",
    "chrome": lambda s: s.process == "chrome", "edge": lambda s: s.process == "msedge", "firefox": lambda s: s.process == "firefox",
    "browser": lambda s: s.browser,
    "media player": lambda s: s.process == "microsoft.media.player",
    "music": lambda s: True, "song": lambda s: True, "podcast": lambda s: True, "audio": lambda s: True,
}


def hint_from(text: str) -> str:
    """The player named in a command ('pause YouTube' -> 'youtube'); '' if none."""
    low = (text or "").lower()
    for word in ("youtube music", "youtube", "you tube", "spotify", "vlc", "chrome", "edge", "firefox", "browser", "media player", "video"):
        if re.search(r"\b" + word + r"\b", low):
            return "youtube" if word == "you tube" else word
    if re.search(r"\b(?:everything|all\s+(?:the\s+)?(?:media|music|audio|players|sound|videos)|all\s+of\s+it)\b", low):
        return "all"
    return ""


@dataclass
class Outcome:
    ok: bool
    verified: bool = False
    sessions: list[MediaSession] = field(default_factory=list)  # what was acted on
    detail: str = ""  # nothing | already | unsupported | gone | failed | ambiguous | no_helper
    choices: list[MediaSession] = field(default_factory=list)  # when it isn't clear which player is meant
    after: list[MediaSession] = field(default_factory=list)


class MediaHub:
    def __init__(self, helper=None, windows: Callable[[], list] | None = None, media_key: Callable[[str], None] | None = None,
                 youtube_keys: Callable[[list, int], bool] | None = None, state_dir: Path | None = None,
                 sleep: Callable[[float], None] = time.sleep, own_pid: int | None = None) -> None:
        self.helper = helper
        self._windows = windows or (lambda: [])
        self._media_key = media_key
        self._youtube_keys = youtube_keys  # (keys, times) -> pressed? (switches to the YouTube tab first)
        self._sleep = sleep
        self._state_dir = state_dir
        self._own_pid = own_pid if own_pid is not None else os.getpid()
        self.known_sites: dict[str, str] = {}  # session key -> "YouTube" for videos JARVIS started
        self.last: str = ""  # key of the session JARVIS controlled last ("pause it" after "play X")
        self.last_at = 0.0
        self._lock = threading.RLock()
        self._duck_depth = 0
        self._ducked: dict[int, dict] = {}
        self._duck_timer: threading.Timer | None = None

    # -- reading
    def can_read(self) -> bool:
        return bool(self.helper) and self.helper.has("media")

    def sessions(self) -> list[MediaSession]:
        windows = self._safe_windows()
        if self.can_read():
            try:
                data = self.helper.call("media.sessions")
                if data.get("ok"):
                    found = [s for s in sessions_from_helper(data) if s.status != "stopped" or s.title]
                    tag_sites(found, windows)
                    for s in found:
                        if not s.site and s.key in self.known_sites:
                            s.site = self.known_sites[s.key]  # a tab JARVIS opened itself, even when it isn't the one showing
                    return found
                log.info("media sessions: %s", data.get("error"))
            except Exception as exc:  # noqa: BLE001
                log.info("media sessions unavailable: %s", exc)
        return sessions_from_windows(windows)

    def _safe_windows(self) -> list:
        try:
            return list(self._windows())
        except Exception:
            return []

    def now_playing(self) -> MediaSession | None:
        found = self.sessions()
        playing = [s for s in found if s.playing]
        if playing:
            return self._prefer(playing)
        paused = [s for s in found if s.title]
        return self._prefer(paused) if paused else None

    def _prefer(self, options: list[MediaSession]) -> MediaSession:
        for s in options:
            if s.key == self.last:
                return s
        for s in options:
            if s.current:
                return s
        return options[0]

    # -- choosing
    def choose(self, action: str, hint: str = "", found: list[MediaSession] | None = None) -> tuple[list[MediaSession], list[MediaSession]]:
        """(targets, ambiguous choices) for an action, using the player named, then context, then what's playing."""
        found = self.sessions() if found is None else found
        if hint == "all":
            if action in ("pause", "stop"):
                return [s for s in found if s.playing] or [], []
            return [s for s in found if s.status == "paused"], []
        pool = [s for s in found if _HINTS.get(hint, lambda s: True)(s)] if hint and hint not in ("music", "song", "audio", "podcast") else found
        if hint and not pool:
            return [], []
        if action == "pause" or action == "stop":
            live = [s for s in pool if s.playing or s.status == "unknown"]
        elif action == "play":
            live = [s for s in pool if s.status in ("paused", "unknown")] or [s for s in pool if s.status == "stopped" and s.title]
        else:
            live = [s for s in pool if s.playing] or [s for s in pool if s.status in ("paused", "unknown")]
        if not live:
            return [], []
        if len(live) == 1:
            return live, []
        for s in live:
            if s.key == self.last:
                return [s], []
        if action == "play":  # resume: the one Windows considers current, else the last one paused
            current = [s for s in live if s.current]
            if current:
                return current[:1], []
        known = [s for s in live if s.status != "unknown"]
        if len(known) == 1:
            return known, []
        return [], live  # e.g. Spotify and a YouTube video both playing, and the user said just "pause"

    # -- controlling
    def control(self, action: str, hint: str = "", position: float | None = None, targets: list[MediaSession] | None = None) -> Outcome:
        """Pause / play / toggle / next / previous / stop / seek, and check it worked."""
        with self._lock:
            found = self.sessions()
            if targets is None:
                targets, choices = self.choose(action, hint, found)
                if choices:
                    return Outcome(False, detail="ambiguous", choices=choices)
            if not targets:
                if action == "pause" and any(s.status == "paused" for s in found if not hint or _HINTS.get(hint, lambda s: True)(s)):
                    return Outcome(True, verified=True, detail="already")
                if action == "play" and any(s.playing for s in found if not hint or _HINTS.get(hint, lambda s: True)(s)):
                    return Outcome(True, verified=True, detail="already", sessions=[s for s in found if s.playing][:1])
                return Outcome(False, detail="nothing")
            if targets[0].source == "window" or not self.can_read():
                return self._keys(action, targets)
            done = []
            for s in targets:
                ok = self._supports(s, action) and self._session_action(s, action, position)
                if not ok and action in ("pause", "play", "toggle", "next", "previous") and s.site.startswith("YouTube") and self._youtube_keys:
                    keys = {"pause": ["k"], "play": ["k"], "toggle": ["k"], "next": ["shift", "n"], "previous": ["shift", "p"]}[action]
                    ok = bool(self._youtube_keys(keys, 1))
                if ok:
                    done.append(s)
            if not done:
                return Outcome(False, detail="unsupported" if all(not self._supports(s, action) for s in targets) else "failed", sessions=targets)
            after = self._verify(action, done, position)
            verified = all(self._worked(action, s, after, position) for s in done)
            if verified or action in ("next", "previous"):
                self._remember_target(done[-1] if action != "pause" or len(done) == 1 else done[0])
            return Outcome(verified or action in ("next", "previous", "seek"), verified, done, after=after)

    def _supports(self, s: MediaSession, action: str) -> bool:
        need = {"play": "play", "pause": "pause", "toggle": "toggle", "next": "next", "previous": "previous", "stop": "stop", "seek": "seek"}[action]
        return need in s.can or (action in ("play", "pause", "stop") and ("toggle" in s.can or "pause" in s.can))

    def _session_action(self, s: MediaSession, action: str, position: float | None) -> bool:
        act = action
        if action in ("play", "pause") and action not in s.can and "toggle" in s.can:
            act = "toggle"
        if action == "stop" and "stop" not in s.can:
            act = "pause"
        try:
            reply = self.helper.call("media.control", app=s.app_id, title=s.title, index=s.index, action=act,
                                     **({"position": position} if position is not None else {}))
            return bool(reply.get("ok"))
        except Exception as exc:  # noqa: BLE001
            log.info("media control failed: %s", exc)
            return False

    def _find(self, s: MediaSession, found: list[MediaSession]) -> MediaSession | None:
        same = [x for x in found if x.app_id == s.app_id]
        return next((x for x in same if x.title == s.title), None) or (same[0] if len(same) == 1 else None)

    def _worked(self, action: str, s: MediaSession, after: list[MediaSession], position: float | None) -> bool:
        now = self._find(s, after)
        if now is None:
            return action == "stop"
        if action in ("pause", "stop"):
            return now.status in ("paused", "stopped")
        if action == "play":
            return now.status == "playing"
        if action == "toggle":
            return now.status != s.status
        if action in ("next", "previous"):
            return now.title != s.title or (now.position is not None and now.position < 5)
        if action == "seek" and position is not None and now.position is not None:
            return abs(now.position - position) < 6
        return False

    def _verify(self, action: str, done: list[MediaSession], position: float | None, wait: float = 3.0) -> list[MediaSession]:
        deadline = time.monotonic() + wait
        after: list[MediaSession] = []
        while True:
            self._sleep(0.25)
            after = self.sessions()
            if all(self._worked(action, s, after, position) for s in done) or time.monotonic() >= deadline:
                return after

    def _keys(self, action: str, targets: list[MediaSession]) -> Outcome:
        """No media sessions to talk to: the media keys, checked against Spotify's window title where possible."""
        if not self._media_key:
            return Outcome(False, detail="no_helper")
        key = {"pause": "toggle", "play": "toggle", "toggle": "toggle", "next": "next", "previous": "previous", "stop": "toggle"}.get(action)
        if key is None:
            return Outcome(False, detail="unsupported", sessions=targets)
        before = targets[0]
        self._media_key(key)
        if before.process != "spotify" or before.source != "window":
            return Outcome(True, verified=False, sessions=targets)
        self._sleep(0.8)
        after = self.sessions()
        now = next((s for s in after if s.process == "spotify"), None)
        ok = now is not None and self._worked(action, before, [now], None)
        return Outcome(ok, verified=ok, sessions=targets, after=after)

    def _remember_target(self, s: MediaSession) -> None:
        self.last, self.last_at = s.key, time.time()

    def remember(self, s: MediaSession | None, site: str = "") -> None:
        if s is not None:
            if site:
                s.site = site
                self.known_sites[s.key] = site
                if len(self.known_sites) > 50:
                    self.known_sites.pop(next(iter(self.known_sites)))
            self._remember_target(s)

    # -- YouTube's own controls
    def youtube(self, keys: list, times: int = 1, verify: str = "", before: MediaSession | None = None) -> Outcome:
        """Press YouTube shortcuts in its tab (j / l = 10 s back / forward, 0 = start, f = full screen, m = mute, c = captions)."""
        if not self._youtube_keys:
            return Outcome(False, detail="no_helper")
        before = before or next((s for s in self.sessions() if s.site.startswith("YouTube")), None)
        if not self._youtube_keys(keys, times):
            return Outcome(False, detail="nothing")
        if not verify or before is None or not self.can_read():
            return Outcome(True, verified=False, sessions=[before] if before else [])
        self._sleep(0.6)
        after = self.sessions()
        now = self._find(before, after)
        ok = False
        if now is not None and now.position is not None and before.position is not None:
            if verify == "forward":
                ok = now.position > before.position + 2
            elif verify == "back":
                ok = now.position < before.position - 2
            elif verify == "start":
                ok = now.position < 5
        return Outcome(True, verified=ok, sessions=[before], after=after)

    # -- app volumes
    def can_mix(self) -> bool:
        return bool(self.helper) and self.helper.has("audio")

    def mixer(self) -> list[dict]:
        if not self.can_mix():
            return []
        try:
            data = self.helper.call("audio.sessions")
            return [s for s in data.get("sessions") or [] if not s.get("system")] if data.get("ok") else []
        except Exception:  # noqa: BLE001
            return []

    def app_volume(self, process: str, *, delta: float | None = None, level: float | None = None, muted: bool | None = None) -> dict | None:
        """Change one app's own volume (0..1); returns {before, after, muted} or None when it can't."""
        rows = [r for r in self.mixer() if str(r.get("name") or "").lower() == process.lower()]
        if not rows:
            return None
        before = max(float(r.get("volume") or 0) for r in rows)
        target = before if level is None and delta is None else max(0.0, min(1.0, level if level is not None else before + delta))
        for r in rows:
            args = {"pid": r["pid"]}
            if level is not None or delta is not None:
                args["volume"] = round(target, 3)
            if muted is not None:
                args["muted"] = muted
            try:
                self.helper.call("audio.set", **args)
            except Exception:  # noqa: BLE001
                return None
        after_rows = [r for r in self.mixer() if str(r.get("name") or "").lower() == process.lower()]
        after = max((float(r.get("volume") or 0) for r in after_rows), default=target)
        return {"before": before, "after": after, "muted": any(r.get("muted") for r in after_rows)}

    # -- lowering music while JARVIS speaks
    def _duck_file(self) -> Path | None:
        return self._state_dir / "ducked.json" if self._state_dir else None

    def duck(self, level: float = 0.3) -> None:
        """Lower every other app that's making sound to ``level`` of its volume (no-op without app volumes)."""
        with self._lock:
            self._duck_depth += 1
            if self._duck_timer is not None:
                self._duck_timer.cancel()
                self._duck_timer = None
            if self._ducked or not self.can_mix():
                return
            rows = [r for r in self.mixer() if int(r.get("state") or 0) == 1 and int(r.get("pid") or 0) != self._own_pid
                    and float(r.get("volume") or 0) > 0.05 and not r.get("muted")
                    and str(r.get("name") or "").lower() not in ("jarvis", "python", "pythonw")]
            if not rows:
                return
            for r in rows:
                self._ducked[int(r["pid"])] = {"name": r.get("name"), "volume": float(r["volume"]), "ducked": round(float(r["volume"]) * level, 3)}
            self._save_ducked()
            for pid, d in self._ducked.items():
                try:
                    self.helper.call("audio.set", pid=pid, volume=d["ducked"])
                except Exception:  # noqa: BLE001
                    pass

    def unduck(self, delay: float = 0.6) -> None:
        """Put the volumes back (after a short pause, so a reply in several sentences doesn't pump the music)."""
        with self._lock:
            self._duck_depth = max(0, self._duck_depth - 1)
            if self._duck_depth or not self._ducked:
                return
            if self._duck_timer is not None:
                self._duck_timer.cancel()
            self._duck_timer = threading.Timer(delay, self.restore)
            self._duck_timer.daemon = True
            self._duck_timer.start()

    def restore(self) -> None:
        with self._lock:
            if self._duck_depth:
                return
            ducked, self._ducked = self._ducked, {}
            self._duck_timer = None
            if not ducked:
                return
            current = {int(r["pid"]): float(r.get("volume") or 0) for r in self.mixer()}
            for pid, d in ducked.items():
                now = current.get(pid)
                if now is not None and abs(now - d["ducked"]) > 0.03:
                    continue  # the user changed it meanwhile: leave their choice alone
                try:
                    self.helper.call("audio.set", pid=pid, volume=d["volume"])
                except Exception:  # noqa: BLE001
                    pass
            self._save_ducked()

    def recover(self) -> int:
        """At start-up: put back volumes left lowered by a crash. Returns how many were restored."""
        path = self._duck_file()
        if path is None or not path.exists():
            return 0
        try:
            saved = {int(k): v for k, v in json.loads(path.read_text(encoding="utf-8")).items()}
        except (OSError, ValueError):
            saved = {}
        restored = 0
        if saved and self.can_mix():
            rows = self.mixer()
            for pid, d in saved.items():
                same = [r for r in rows if int(r["pid"]) == pid and str(r.get("name")) == str(d.get("name"))]
                for r in same:
                    if abs(float(r.get("volume") or 0) - float(d.get("ducked", -1))) <= 0.03:
                        self.helper.call("audio.set", pid=pid, volume=d["volume"])
                        restored += 1
        try:
            path.unlink()
        except OSError:
            pass
        return restored

    def _save_ducked(self) -> None:
        path = self._duck_file()
        if path is None:
            return
        try:
            if self._ducked:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({str(k): v for k, v in self._ducked.items()}), encoding="utf-8")
            elif path.exists():
                path.unlink()
        except OSError:
            log.debug("couldn't save ducking state", exc_info=True)

    def status(self) -> dict:
        helper = self.helper.status() if self.helper else {}
        return {"sessions": bool(helper.get("caps", {}).get("media")), "mixer": bool(helper.get("caps", {}).get("audio")),
                "keys": bool(self._media_key), "errors": helper.get("errors") or {}, "error": helper.get("error")}


def describe_position(seconds: float | None) -> str:
    if seconds is None:
        return ""
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"
