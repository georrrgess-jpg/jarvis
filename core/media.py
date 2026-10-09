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
    action: str  # play | pause | resume | stop | next | previous | now_playing | seek | seek_to | restart | fullscreen |
    #              captions | speed | volume | search | resume_history
    query: str = ""
    service: str = ""  # "youtube" | "spotify" | "" (the user's choice in Settings)
    vague: bool = False  # "play some music": nothing particular
    target: str = ""  # the player named: youtube | spotify | video | all | ... ("" = work it out)
    amount: float | None = None  # seconds to seek / percent of volume
    level: float | None = None  # volume 0..100 to set
    muted: bool | None = None
    on: bool | None = None  # fullscreen / captions on or off ("exit full screen")


_LEAD = (r"^(?:(?:please|can you|could you|would you|will you|go ahead and|i want you to|i'd like you to|now|just|ok(?:ay)?|hey|and|then)\s*,?\s+)*")
_END = r"(?:\s+(?:please|now|for me|right now|thanks|thank you))*[\s.!?]*$"
_THING = r"(?:music|song|songs|track|tune|video|videos|playback|audio|podcast|spotify|youtube(?:\s+music)?|you\s+tube|player|media|vlc|movie|film|stream)"
_WHICH = r"(?:the\s+|my\s+|this\s+|that\s+|whatever(?:'s|\s+is)\s+(?:playing|on)\s+(?:on\s+|in\s+)?)?"
_PAUSE_ALL = re.compile(_LEAD + r"(?:pause|stop|halt|silence)\s+(?:everything|all(?:\s+(?:the\s+)?(?:media|music|audio|videos|players|sound))?|all\s+of\s+it|whatever(?:'s|\s+is)\s+playing)" + _END, re.I)
_PAUSE = re.compile(_LEAD + r"(?:pause|stop|halt|freeze)\s+" + _WHICH + r"(?P<thing>" + _THING + r")(?:\s+(?:on|in)\s+(?P<where>youtube|you\s+tube|spotify|chrome|edge|vlc))?" + _END, re.I)
_PAUSE_ONLY = re.compile(_LEAD + r"(?:pause(?:\s+(?:it|that|this))?|hit\s+pause|press\s+pause)" + _END, re.I)
_RESUME = re.compile(_LEAD + r"(?:resume|unpause|un-pause|continue\s+playing|keep\s+playing|carry\s+on\s+playing|hit\s+play|press\s+play|start\s+playing\s+again|play\s+(?:it\s+)?again|"
                     r"(?:continue|resume|restart|unpause)\s+" + _WHICH + r"(?P<thing>" + _THING + r")|play)(?:\s+(?:the\s+)?(?:music|song|video))?(?:\s+(?:on|in)\s+(?P<where>youtube|spotify))?" + _END, re.I)
_RESUME_HISTORY = re.compile(_LEAD + r"(?:resume|continue|play|put\s+on|go\s+back\s+to)\s+(?:what|whatever|the\s+(?:song|music|video|thing))\s+(?:i\s+was|we\s+were)\s+(?:listening\s+to|watching|playing)"
                             r"(?:\s+(?:earlier|before|last\s+time|yesterday|this\s+morning|a\s+while\s+ago))?" + _END, re.I)
_NEXT = re.compile(_LEAD + r"(?:(?:play\s+)?(?:the\s+)?next\s+(?:song|track|tune|video|one|episode)|skip(?:\s+(?:this|the|that))?(?:\s+(?:song|track|tune|video|one|it|ad))?|"
                   r"skip\s+ahead|go\s+to\s+the\s+next\s+(?:song|track|video)|change\s+(?:the\s+)?(?:song|track))(?:\s+(?:on|in)\s+(?P<where>youtube|spotify))?" + _END, re.I)
_PREV = re.compile(_LEAD + r"(?:(?:play\s+)?(?:the\s+)?(?:previous|last)\s+(?:song|track|tune|video|one)(?:\s+again)?|go\s+back\s+(?:to\s+the\s+previous|a|one)\s+(?:song|track|video)|"
                   r"back\s+(?:a|one)\s+(?:song|track)|replay\s+(?:that|the\s+last)\s+(?:song|track))(?:\s+(?:on|in)\s+(?P<where>youtube|spotify))?" + _END, re.I)
_NOW = re.compile(_LEAD + r"(?:what(?:'s|\s+is)\s+(?:playing|this\s+(?:song|track|tune|video)|the\s+(?:song|track|video)\s+(?:called|playing)|on)(?:\s+(?:right\s+)?now)?|"
                  r"what\s+(?:song|track|tune|music|video)\s+is\s+(?:this|playing|on)(?:\s+(?:right\s+)?now)?|"
                  r"(?:who|which\s+(?:artist|band))\s+(?:is\s+)?(?:this|sings\s+this|is\s+singing|is\s+playing)|name\s+(?:this|that)\s+(?:song|tune)|"
                  r"what(?:'s|\s+is)\s+the\s+name\s+of\s+(?:this|the)\s+(?:song|track|video)|what\s+am\s+i\s+(?:listening\s+to|watching))" + _END, re.I)
_SEEK_START = re.compile(_LEAD + r"(?:skip|jump|go|fast[\s-]?forward|move|seek|rewind|back\s+up|wind\s+(?:it\s+)?(?:back|forward))\b", re.I)
_SEEK_AMOUNT = re.compile(r"\b(?P<n>\d+(?:\.\d+)?|a|an|one|two|three|four|five|ten|fifteen|twenty|thirty|forty|fifty|sixty|ninety|half\s+a)\s*(?P<unit>seconds?|secs?|minutes?|mins?)\b", re.I)
_SEEK_WORDS = re.compile(r"^(?:skip|jump|go|fast|forward|move|seek|rewind|back|backwards?|up|wind|it|the|a|an|by|ahead|on|in|of|this|video|song|track|"
                         r"clip|movie|bit|little|please|now|for|me|just|can|could|you|would|will|and|then)$", re.I)
_SEEK_TO = re.compile(_LEAD + r"(?:skip|jump|go|seek|fast[\s-]?forward|move)\s+(?:straight\s+)?to\s+(?:(?P<m>\d+)\s*(?::|minutes?(?:\s+and)?)\s*(?P<s>\d+)?\s*(?:seconds?)?|"
                      r"(?P<mo>\d+)\s+minutes?|the\s+(?P<where>middle|end))(?:\s+(?:of\s+)?(?:the\s+)?(?:video|song|track|it))?" + _END, re.I)
_RESTART = re.compile(_LEAD + r"(?:restart|replay|start\s+over)(?:\s+(?:the\s+|this\s+)?(?:video|song|track|it))?|(?:play|start|watch)\s+(?:it|this|the\s+(?:video|song))\s+(?:again\s+)?from\s+the\s+(?:beginning|start)|"
                      r"go\s+(?:back\s+)?to\s+the\s+(?:beginning|start)(?:\s+of\s+the\s+(?:video|song))?" + _END, re.I)
_FULLSCREEN = re.compile(_LEAD + r"(?:(?P<off>exit|leave|get\s+out\s+of|close|turn\s+off|stop|minimi[sz]e)\s+(?:the\s+)?full\s*-?\s*screen|"
                         r"(?:make\s+(?:it|the\s+video|this)\s+|go\s+|turn\s+on\s+|enter\s+|switch\s+to\s+|put\s+(?:it|the\s+video)\s+(?:in|on)\s+)?full\s*-?\s*screen(?:\s+(?:the\s+)?(?:video|it))?(?:\s+mode)?|"
                         r"(?:maximi[sz]e|enlarge)\s+the\s+video)" + _END, re.I)
_CAPTIONS = re.compile(_LEAD + r"(?:turn|switch|put)\s+(?P<onoff>on|off)\s+(?:the\s+)?(?:captions|subtitles|cc)|(?:turn|switch)\s+(?:the\s+)?(?:captions|subtitles|cc)\s+(?P<onoff2>on|off)|"
                       r"(?P<toggle>(?:show|hide|toggle)\s+(?:the\s+)?(?:captions|subtitles|cc))" + _END, re.I)
_SPEED = re.compile(_LEAD + r"(?:(?P<up>speed\s+(?:it\s+|the\s+video\s+)?up|play\s+(?:it\s+)?faster|faster)|(?P<down>slow\s+(?:it\s+|the\s+video\s+)?down|play\s+(?:it\s+)?slower|slower)|"
                    r"(?P<normal>(?:normal|regular)\s+speed|play\s+(?:it\s+)?at\s+normal\s+speed))(?:\s+(?:the\s+)?(?:video|playback))?" + _END, re.I)
_APP_VOL = re.compile(_LEAD + r"(?:(?:turn|put)\s+" + _WHICH + r"(?P<t1>" + _THING + r")\s+(?P<d1>up|down)|(?:turn|put)\s+(?P<d2>up|down)\s+" + _WHICH + r"(?P<t2>" + _THING + r")|"
                      r"(?P<d3>lower|raise|reduce|increase|boost|quieten|soften)\s+" + _WHICH + r"(?:volume\s+(?:of|on)\s+(?:the\s+)?)?(?P<t3>" + _THING + r")(?:\s+volume)?|"
                      r"(?:make\s+)?(?:the\s+)?(?P<t4>" + _THING + r")\s+(?P<d4>louder|quieter|softer)|"
                      r"(?:set\s+)?(?:the\s+)?(?P<t5>" + _THING + r")\s+volume\s+(?:to\s+)?(?P<lvl>\d{1,3})\s*(?:%|percent)?|"
                      r"(?:set|put)\s+(?:the\s+)?(?P<t6>" + _THING + r")\s+(?:volume\s+)?(?:to|at)\s+(?P<lvl2>\d{1,3})\s*(?:%|percent)?)" + _END, re.I)
_APP_MUTE = re.compile(_LEAD + r"(?P<verb>mute|unmute|silence)\s+" + _WHICH + r"(?P<thing>" + _THING + r")" + _END, re.I)
_FIND_VIDEO = re.compile(_LEAD + r"(?:find|search\s+for|look\s+up|get)\s+(?:me\s+)?(?:a\s+|an\s+|the\s+|some\s+)?(?:good\s+|short\s+)?(?:youtube\s+)?(?:video|clip|tutorial|song|music\s+video)s?\s+"
                         r"(?:explaining|about|on|of|showing|that\s+explains|that\s+shows|for|called|where|teaching|with)\s+(?P<what>.+?)\s+and\s+(?:play|put\s+on|start|show)\s+(?:it|that|one|the\s+(?:best|first|top)\s+one)"
                         r"(?:\s+(?:on|in)\s+(?:youtube|you\s+tube))?" + _END, re.I | re.S)
_SEARCH_YT = re.compile(_LEAD + r"(?:(?:search|look)\s+(?:on\s+)?(?:youtube|you\s+tube)\s+for\s+(?P<what>.+?)|(?:search\s+for|search|look\s+up|find)\s+(?P<what2>.+?)\s+on\s+(?:youtube|you\s+tube))" + _END, re.I | re.S)
_PLAY = re.compile(_LEAD + r"(?:play|put\s+on|stream|blast|queue\s+up|listen\s+to|watch|i\s+want\s+to\s+(?:hear|listen\s+to|watch)|let\s+me\s+hear|can\s+i\s+hear|let'?s\s+(?:hear|listen\s+to|watch))\s+"
                   r"(?P<what>.+?)" + _END, re.I | re.S)
_ON_SERVICE = re.compile(r"\s+(?:on|in|from|using|with|via)\s+(?:the\s+)?(?:my\s+)?(?P<service>youtube(?:\s+music)?|you\s+tube|spotify)(?:\s+app)?\s*$", re.I)
_VAGUE = re.compile(r"^(?:some\s+|any\s+|my\s+|a\s+|the\s+)?(?:music|tunes|songs?|something|anything|a\s+song|playlist|my\s+playlist|"
                    r"some(?:thing)?\s+(?:good|nice|fun|upbeat|relaxing|chill)|my\s+(?:music|songs|tunes))$", re.I)
_NOT_MUSIC = re.compile(r"^(?:(?:me|us|myself|him|her|them|this|that|it|you)(?:\s+again)?$|a\s+game\b|the\s+game\b|it\s+cool|it\s+safe|with\b|along\b|nice\b|dead\b|fair\b|a\s+trick|chess|tennis|football|"
                        r"golf|cards|hide\s+and\s+seek|rock\s+paper\s+scissors|tic\s*tac\s*toe|20\s+questions|twenty\s+questions|a\s+round\b|out\b|my\s+(?:back|step)|"
                        r"(?:the\s+)?(?:news|weather)$|my\s+screen|the\s+screen|it\s+safe|your\s+step|a\s+movie\s+night)", re.I)
_WORDNUM = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30,
            "forty": 40, "fifty": 50, "sixty": 60, "ninety": 90, "half a": 0.5}


def _target(thing: str | None, where: str | None = None) -> str:
    text = f"{where or ''} {thing or ''}".lower()
    for word, key in (("youtube music", "youtube music"), ("youtube", "youtube"), ("you tube", "youtube"), ("spotify", "spotify"),
                      ("vlc", "vlc"), ("chrome", "chrome"), ("edge", "edge"), ("video", "video"), ("movie", "video"), ("film", "video")):
        if word in text:
            return key
    return ""


def _number(text: str | None) -> float | None:
    if not text:
        return None
    t = text.lower().strip()
    return float(t) if re.fullmatch(r"\d+(?:\.\d+)?", t) else _WORDNUM.get(t)


def _seek(t: str) -> float | None:
    """'skip forward 30 seconds' -> 30, 'rewind' -> -10, 'fast forward the video by 1 minute' -> 60 (seconds; None if not a seek)."""
    if not _SEEK_START.match(t) or re.search(r"\b(?:song|track|tune|tab|page|episode|ad|intro|chapter|window|screen)\b", t, re.I):
        return None
    amount = _SEEK_AMOUNT.search(t)
    rest = _SEEK_AMOUNT.sub(" ", t) if amount else t
    if any(not _SEEK_WORDS.match(w) for w in re.findall(r"[a-z']+", rest.lower())):
        return None  # other words: "go back to the menu", "skip the intro"
    explicit = re.search(r"\b(?:rewind|fast[\s-]?forward|wind)\b", t, re.I) or re.search(r"\b(?:video|clip|movie|in\s+it)\b", t, re.I)
    if not amount and not explicit:
        return None  # a bare "go back" is the browser's back button
    n = (_number(amount.group("n")) if amount else 10.0) or 10.0
    if amount and amount.group("unit").lower().startswith("m"):
        n *= 60
    return -n if re.search(r"\b(?:back|backwards?|rewind)\b", t, re.I) else n


def parse_media(text: str) -> MediaCommand | None:
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not t or len(t) > 300:
        return None
    if _NOW.match(t):
        return MediaCommand("now_playing")
    if _RESUME_HISTORY.match(t):
        return MediaCommand("resume_history")
    m = _FIND_VIDEO.match(t)
    if m:
        return MediaCommand("play", query=m.group("what").strip(" .\"'"), service="youtube")
    m = _SEARCH_YT.match(t)
    if m:
        return MediaCommand("search", query=(m.group("what") or m.group("what2")).strip(" .\"'"), service="youtube")
    m = _NEXT.match(t)
    if m:
        return MediaCommand("next", target=_target(t))
    m = _PREV.match(t)
    if m:
        return MediaCommand("previous", target=_target(t))
    if _PAUSE_ALL.match(t):
        return MediaCommand("pause", target="all", query="stop" if re.match(_LEAD + r"stop", t, re.I) else "")
    if _PAUSE_ONLY.match(t):
        return MediaCommand("pause")
    m = _PAUSE.match(t)
    if m:  # "stop the music", "pause YouTube" (but a bare "stop" means "stop talking")
        return MediaCommand("pause", target=_target(t))
    if _RESTART.match(t):
        return MediaCommand("restart")
    m = _RESUME.match(t)
    if m:
        return MediaCommand("resume", target=_target(t))
    m = _APP_MUTE.match(t)
    if m:
        return MediaCommand("volume", target=_target(m.group("thing")) or "music", muted=m.group("verb").lower() != "unmute")
    m = _APP_VOL.match(t)
    if m:
        g = {k: v for k, v in m.groupdict().items() if v}
        thing = g.get("t1") or g.get("t2") or g.get("t3") or g.get("t4") or g.get("t5") or g.get("t6") or ""
        level = g.get("lvl") or g.get("lvl2")
        if level is not None:
            return MediaCommand("volume", target=_target(thing) or "music", level=min(100.0, float(level)))
        word = (g.get("d1") or g.get("d2") or g.get("d3") or g.get("d4") or "").lower()
        down = word in ("down", "lower", "reduce", "quieten", "soften", "quieter", "softer")
        return MediaCommand("volume", target=_target(thing) or "music", amount=-20.0 if down else 20.0)
    m = _SEEK_TO.match(t)
    if m:
        if m.group("where"):
            return MediaCommand("seek_to", query=m.group("where").lower())
        minutes = float(m.group("m") or m.group("mo") or 0)
        return MediaCommand("seek_to", amount=minutes * 60 + float(m.group("s") or 0))
    if _RESTART.match(t):
        return MediaCommand("restart")
    seek = _seek(t)
    if seek is not None:
        return MediaCommand("seek", amount=seek)
    m = _FULLSCREEN.match(t)
    if m:
        return MediaCommand("fullscreen", on=not bool(m.group("off")))
    m = _CAPTIONS.match(t)
    if m:
        onoff = (m.group("onoff") or m.group("onoff2") or "").lower()
        return MediaCommand("captions", on=None if not onoff else onoff == "on")
    m = _SPEED.match(t)
    if m and (re.search(r"\b(?:video|playback|it|speed)\b", t, re.I) or m.group("normal")):
        return MediaCommand("speed", query="up" if m.group("up") else "down" if m.group("down") else "normal")
    m = _PLAY.match(t)
    if m:
        what = m.group("what").strip(" \"'“”")
        service = ""
        s = _ON_SERVICE.search(what)
        if s:
            service = "spotify" if "spotify" in s.group("service").lower() else "youtube"
            what = what[: s.start()].strip(" \"'“”")
        what = re.sub(r"^(?:the\s+song|the\s+track|the\s+album|the\s+video|the\s+playlist|a\s+song\s+called|the\s+song\s+called|a\s+video\s+(?:of|about|on))\s+", "", what, flags=re.I)
        if not what or _NOT_MUSIC.match(what):
            return None
        if re.match(r"^(?:it|that|this|the\s+(?:music|song|video))(?:\s+again)?$", what, re.I):
            return MediaCommand("resume", target=service)
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
