"""Music and media: "play Bohemian Rhapsody", "play lo-fi on Spotify", "pause", "next song", "what's playing?".

* Playing something finds the top YouTube result for free (no API key: the public results page) and opens
  it, so it starts straight away; on Spotify it opens the search in the Spotify app (or the web player).
* Pause / resume / next / previous use the keyboard's media keys, which every player on Windows obeys
  (Spotify, YouTube in any browser, VLC, Windows Media Player...).
* "What's playing?" reads the player's window title: Spotify shows "Artist - Song", a browser shows the
  YouTube video's title.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import quote, quote_plus

YOUTUBE_SEARCH = "https://www.youtube.com/results?search_query={q}"
YOUTUBE_WATCH = "https://www.youtube.com/watch?v={id}"
SPOTIFY_URI = "spotify:search:{q}"
SPOTIFY_WEB = "https://open.spotify.com/search/{q}"
SERVICES = ("youtube", "spotify")


@dataclass
class MediaCommand:
    action: str  # play | pause | resume | toggle | next | previous | now_playing
    query: str = ""
    service: str = ""  # "youtube" | "spotify" | "" (the user's choice in Settings)
    vague: bool = False  # "play some music": nothing particular


_LEAD = (r"^(?:(?:please|can you|could you|would you|will you|go ahead and|i want you to|i'd like you to|now|just|ok(?:ay)?|hey)\s*,?\s+)*")
_END = r"(?:\s+(?:please|now|for me|right now|thanks|thank you))*[\s.!?]*$"
_MEDIA = r"(?:the\s+)?(?:music|song|track|tune|video|playback|audio|podcast|spotify|youtube|player|media)"
_PAUSE = re.compile(_LEAD + r"(?:pause|stop|halt|freeze)(?:\s+(?:the\s+|my\s+|this\s+)?(?:music|song|track|tune|video|playback|audio|podcast|spotify|youtube|player|media|it))?" + _END, re.I)
_PAUSE_ONLY = re.compile(_LEAD + r"(?:pause(?:\s+(?:it|that|this))?|hit\s+pause|press\s+pause)" + _END, re.I)
_RESUME = re.compile(_LEAD + r"(?:resume|unpause|un-pause|continue\s+playing|keep\s+playing|carry\s+on\s+playing|hit\s+play|press\s+play|start\s+playing\s+again|play\s+(?:it\s+)?again|"
                     r"resume\s+" + _MEDIA + r"|(?:continue|resume|restart)\s+(?:the\s+|my\s+)?(?:music|song|video|playback|podcast)|play)(?:\s+(?:the\s+)?(?:music|song|video))?" + _END, re.I)
_NEXT = re.compile(_LEAD + r"(?:(?:play\s+)?(?:the\s+)?next\s+(?:song|track|tune|video|one)|skip(?:\s+(?:this|the|that))?(?:\s+(?:song|track|tune|video|one|it|ad))?|"
                   r"skip\s+ahead|go\s+to\s+the\s+next\s+(?:song|track|video)|change\s+(?:the\s+)?(?:song|track))" + _END, re.I)
_PREV = re.compile(_LEAD + r"(?:(?:play\s+)?(?:the\s+)?(?:previous|last)\s+(?:song|track|tune|video|one)(?:\s+again)?|go\s+back\s+(?:a|one)\s+(?:song|track|video)|"
                   r"back\s+(?:a|one)\s+(?:song|track)|replay\s+(?:that|the\s+last)\s+(?:song|track))" + _END, re.I)
_NOW = re.compile(_LEAD + r"(?:what(?:'s|\s+is)\s+(?:playing|this\s+(?:song|track|tune)|the\s+(?:song|track)\s+(?:called|playing)|on)(?:\s+(?:right\s+)?now)?|"
                  r"what\s+(?:song|track|tune|music)\s+is\s+(?:this|playing|on)(?:\s+(?:right\s+)?now)?|"
                  r"(?:who|which\s+(?:artist|band))\s+(?:is\s+)?(?:this|sings\s+this|is\s+singing|is\s+playing)|name\s+(?:this|that)\s+(?:song|tune)|"
                  r"what(?:'s|\s+is)\s+the\s+name\s+of\s+(?:this|the)\s+(?:song|track))" + _END, re.I)
_PLAY = re.compile(_LEAD + r"(?:play|put\s+on|stream|blast|queue\s+up|listen\s+to|i\s+want\s+to\s+(?:hear|listen\s+to)|let\s+me\s+hear|can\s+i\s+hear)\s+"
                   r"(?P<what>.+?)" + _END, re.I | re.S)
_ON_SERVICE = re.compile(r"\s+(?:on|in|from|using|with|via)\s+(?:the\s+)?(?:my\s+)?(?P<service>youtube(?:\s+music)?|you\s+tube|spotify)(?:\s+app)?\s*$", re.I)
_VAGUE = re.compile(r"^(?:some\s+|any\s+|my\s+|a\s+|the\s+)?(?:music|tunes|songs?|something|anything|a\s+song|playlist|my\s+playlist|"
                    r"some(?:thing)?\s+(?:good|nice|fun|upbeat|relaxing|chill)|my\s+(?:music|songs|tunes))$", re.I)
_NOT_MUSIC = re.compile(r"^(?:(?:me|us|myself|him|her|them|this|that|it|you)(?:\s+again)?$|a\s+game\b|the\s+game\b|it\s+cool|it\s+safe|with\b|along\b|nice\b|dead\b|fair\b|a\s+trick|chess|tennis|football|"
                        r"golf|cards|hide\s+and\s+seek|rock\s+paper\s+scissors|tic\s*tac\s*toe|20\s+questions|twenty\s+questions|a\s+round\b)", re.I)


def parse_media(text: str) -> MediaCommand | None:
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not t or len(t) > 300:
        return None
    if _NOW.match(t):
        return MediaCommand("now_playing")
    if _NEXT.match(t):
        return MediaCommand("next")
    if _PREV.match(t):
        return MediaCommand("previous")
    if _PAUSE_ONLY.match(t):
        return MediaCommand("pause")
    m = _PAUSE.match(t)
    if m and re.search(r"\b(?:music|song|track|tune|video|playback|audio|podcast|spotify|youtube|player|media)\b", t, re.I):
        return MediaCommand("pause")  # "stop the music" (but a bare "stop" means "stop talking")
    if _RESUME.match(t):
        return MediaCommand("resume")
    m = _PLAY.match(t)
    if m:
        what = m.group("what").strip(" \"'“”")
        service = ""
        s = _ON_SERVICE.search(what)
        if s:
            service = "spotify" if "spotify" in s.group("service").lower() else "youtube"
            what = what[: s.start()].strip(" \"'“”")
        what = re.sub(r"^(?:the\s+song|the\s+track|the\s+album|the\s+video|the\s+playlist|a\s+song\s+called|the\s+song\s+called)\s+", "", what, flags=re.I)
        if not what or _NOT_MUSIC.match(what):
            return None
        vague = bool(_VAGUE.match(what))
        if not vague:
            what = re.sub(r"^(?:some|a\s+bit\s+of|a\s+little)\s+", "", what, flags=re.I)
        return MediaCommand("play", query=what, service=service, vague=vague)
    return None


# ----------------------------------------------------------------------------- YouTube
def youtube_search_url(query: str) -> str:
    return YOUTUBE_SEARCH.format(q=quote_plus(query))


def first_youtube_video(markup: str) -> tuple[str, str] | None:
    """(video id, title) of the first real result on a YouTube results page (ads and shorts skipped)."""
    for m in re.finditer(r'"videoRenderer":\{"videoId":"(?P<id>[\w-]{11})"', markup or ""):
        chunk = markup[m.end(): m.end() + 4000]
        title = ""
        t = re.search(r'"title":\{"runs":\[\{"text":"(?P<t>(?:[^"\\]|\\.)*)"', chunk)
        if t:
            try:
                title = json.loads('"' + t.group("t") + '"')
            except ValueError:
                title = t.group("t")
        return m.group("id"), title
    m = re.search(r'"videoId":"([\w-]{11})"', markup or "")
    return (m.group(1), "") if m else None


def find_youtube_video(client, query: str) -> tuple[str, str] | None:
    """Look the query up on YouTube (no key needed). Returns (watch URL, title) or None."""
    r = client.get(youtube_search_url(query), headers={"Accept-Language": "en-US,en;q=0.8", "Cookie": "CONSENT=YES+cb; SOCS=CAI"})
    r.raise_for_status()
    found = first_youtube_video(r.text)
    if not found:
        return None
    vid, title = found
    return YOUTUBE_WATCH.format(id=vid), title


def spotify_targets(query: str) -> tuple[str, str]:
    """(app URI, web URL) that open Spotify's search for ``query``."""
    return SPOTIFY_URI.format(q=quote(query)), SPOTIFY_WEB.format(q=quote(query))


# ----------------------------------------------------------------------------- what's playing
_SPOTIFY_IDLE = re.compile(r"^(?:spotify(?:\s+(?:premium|free))?|advertisement|spotify\s+-\s+web\s+player.*)$", re.I)
_BROWSERS = ("chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe")
_YOUTUBE_TITLE = re.compile(r"^(?:\(\d+\)\s*)?(?P<t>.+?)\s+-\s+YouTube(?P<music>\s+Music)?(?:\s+[-–—]\s+.*)?$")
_VLC_TITLE = re.compile(r"^(?P<t>.+?)\s+-\s+VLC media player$", re.I)


@dataclass
class NowPlaying:
    title: str
    artist: str = ""
    app: str = ""  # "Spotify" | "YouTube" | "YouTube Music" | "VLC"
    playing: bool = True

    def spoken(self) -> str:
        return f"{self.title} by {self.artist}" if self.artist else self.title

    def to_dict(self) -> dict:
        return {"title": self.title, "artist": self.artist, "app": self.app, "playing": self.playing}


def now_playing(windows) -> NowPlaying | None:
    """What a media player is showing, from window titles (Spotify first, then YouTube, then VLC)."""
    spotify_idle = None
    youtube = vlc = None
    for w in windows or []:
        app, title = (getattr(w, "app", "") or "").lower(), (getattr(w, "title", "") or "").strip()
        if not title:
            continue
        if app == "spotify.exe":
            if _SPOTIFY_IDLE.match(title):
                spotify_idle = NowPlaying("", app="Spotify", playing=False)
                continue
            artist, sep, song = title.partition(" - ")
            return NowPlaying(song.strip() if sep else title, artist.strip() if sep else "", "Spotify")
        if app in _BROWSERS and youtube is None:
            m = _YOUTUBE_TITLE.match(title)
            if m and m.group("t").strip().lower() not in ("youtube", "home", "subscriptions", "library", "history"):
                text = m.group("t").strip()
                if m.group("music") and " • " in text:
                    song, _, artist = text.partition(" • ")
                    youtube = NowPlaying(song.strip(), artist.strip(), "YouTube Music")
                else:
                    youtube = NowPlaying(text, "", "YouTube Music" if m.group("music") else "YouTube")
        if app == "vlc.exe" and vlc is None:
            m = _VLC_TITLE.match(title)
            if m:
                vlc = NowPlaying(m.group("t").strip(), "", "VLC")
    return youtube or vlc or spotify_idle
