"""Music and media: play a song (YouTube or Spotify), pause / skip with media keys, and "what's playing?"."""

import httpx
import pytest

from core import osctl
from core.media import first_youtube_video, now_playing, parse_media
from core.screen import Window
from tests.test_assistant import make  # noqa: F401  (a fixture)
from tests.test_personas import ask


@pytest.mark.parametrize("text, action, query, service, vague", [
    ("play bohemian rhapsody", "play", "bohemian rhapsody", "", False),
    ("play lo-fi beats on spotify", "play", "lo-fi beats", "spotify", False),
    ("play the song Blinding Lights by the Weeknd on youtube", "play", "Blinding Lights by the Weeknd", "youtube", False),
    ("put on some jazz", "play", "jazz", "", False),
    ("I want to listen to Taylor Swift", "play", "Taylor Swift", "", False),
    ("play some music", "play", "some music", "", True),
    ("pause", "pause", "", "", False), ("pause the music", "pause", "", "", False), ("stop the music", "pause", "", "", False),
    ("resume", "resume", "", "", False), ("play", "resume", "", "", False), ("unpause the video", "resume", "", "", False),
    ("next song", "next", "", "", False), ("skip", "next", "", "", False), ("skip this track please", "next", "", "", False),
    ("previous song", "previous", "", "", False), ("go back a song", "previous", "", "", False),
    ("what's playing", "now_playing", "", "", False), ("what song is this", "now_playing", "", "", False),
    ("who sings this", "now_playing", "", "", False),
])
def test_understanding(text, action, query, service, vague):
    cmd = parse_media(text)
    assert cmd is not None and (cmd.action, cmd.query, cmd.service, cmd.vague) == (action, query, service, vague)


@pytest.mark.parametrize("text", ["stop", "continue", "next", "listen to me", "play chess", "play it cool", "play a game with me",
                                  "what's the weather", "next tab", "open spotify", "stop listening"])
def test_not_media(text):
    assert parse_media(text) is None


PAGE = ('<script>var ytInitialData = {"contents":[{"adSlotRenderer":{"videoId":"AAAAAAAAAAA"}},'
        '{"videoRenderer":{"videoId":"fJ9rUzIMcZQ","thumbnail":{},"title":{"runs":[{"text":"Queen \\u2013 Bohemian Rhapsody (Official Video)"}]}}},'
        '{"videoRenderer":{"videoId":"zzzzzzzzzzz","title":{"runs":[{"text":"Another"}]}}}]};</script>')


def test_reading_youtube_results():
    assert first_youtube_video(PAGE) == ("fJ9rUzIMcZQ", "Queen – Bohemian Rhapsody (Official Video)")
    assert first_youtube_video('{"videoId":"abcdefghijk"}') == ("abcdefghijk", "")
    assert first_youtube_video("<html>nothing</html>") is None


def test_whats_playing_from_window_titles():
    assert now_playing([Window(1, "Queen - Bohemian Rhapsody", "Spotify.exe")]).to_dict() == \
        {"title": "Bohemian Rhapsody", "artist": "Queen", "app": "Spotify", "playing": True}
    idle = now_playing([Window(1, "Spotify Premium", "Spotify.exe")])
    assert idle.app == "Spotify" and not idle.playing
    yt = now_playing([Window(2, "(3) Lofi Girl - beats to relax - YouTube - Google Chrome", "chrome.exe")])
    assert (yt.title, yt.app) == ("Lofi Girl - beats to relax", "YouTube")
    ytm = now_playing([Window(2, "Song Name • The Artist - YouTube Music - Microsoft Edge", "msedge.exe")])
    assert (ytm.title, ytm.artist, ytm.app) == ("Song Name", "The Artist", "YouTube Music")
    assert now_playing([Window(3, "Home - YouTube - Google Chrome", "chrome.exe")]) is None
    assert now_playing([Window(4, "Inbox - Gmail - Google Chrome", "chrome.exe")]) is None
    assert now_playing([Window(5, "movie.mkv - VLC media player", "vlc.exe")]).title == "movie.mkv"
    # Spotify playing wins over a YouTube tab
    both = now_playing([Window(2, "Talk - YouTube - Google Chrome", "chrome.exe"), Window(1, "Muse - Uprising", "Spotify.exe")])
    assert both.app == "Spotify"


class Desk:
    def __init__(self, windows=()):
        self.list = list(windows)
        self.pressed = []

    def windows(self):
        return list(self.list)

    def foreground(self):
        return self.list[0] if self.list else None

    def monitors(self):
        return []

    def bring_to_front(self, w):
        pass

    def press(self, keys, window=None):
        self.pressed.append(tuple(keys))


class FakeHelper:
    """Windows' media sessions, mixer and address bar, simulated."""

    def __init__(self):
        self.sessions = []  # dicts: app, title, artist, status, position, duration, can, playlist, ignore
        self.mixer = []  # dicts: pid, name, state, volume, muted
        self.calls = []
        self.caps = {"media": True, "audio": True, "uia": True}
        self.urls = {}

    def has(self, cap):
        return bool(self.caps.get(cap))

    def available(self):
        return True

    def status(self):
        return {"running": True, "caps": dict(self.caps), "errors": {}, "error": None}

    def add(self, app, title, artist="", status="Playing", position=10.0, duration=200.0, can=None, **extra):
        can = can or {"play": True, "pause": True, "toggle": True, "next": True, "previous": True, "stop": True, "seek": True}
        self.sessions.append({"app": app, "title": title, "artist": artist, "status": status, "position": position,
                              "duration": duration, "can": can, **extra})
        return self.sessions[-1]

    def call(self, cmd, timeout=8.0, **a):
        self.calls.append((cmd, a))
        if cmd == "media.sessions":
            return {"ok": True, "sessions": [{**{k: v for k, v in s.items() if k not in ("playlist", "ignore")}, "index": i,
                                               "current": i == 0, "updated_ago": 0} for i, s in enumerate(self.sessions)]}
        if cmd == "media.control":
            s = next((x for x in self.sessions if x["app"] == a["app"] and x["title"] == a.get("title")), None) or \
                next((x for x in self.sessions if x["app"] == a["app"]), None)
            if s is None:
                return {"ok": False, "error": "gone"}
            if s.get("ignore"):
                return {"ok": True}  # says yes, does nothing (some players)
            act = a["action"]
            if act == "pause" or (act == "toggle" and s["status"] == "Playing") or act == "stop":
                s["status"] = "Paused"
            elif act in ("play", "toggle"):
                s["status"] = "Playing"
            elif act in ("next", "previous") and s.get("playlist"):
                s["title"] = s["playlist"].pop(0)
                s["position"] = 0
            elif act == "seek":
                s["position"] = float(a["position"])
            return {"ok": True}
        if cmd == "audio.sessions":
            return {"ok": True, "sessions": [dict(m) for m in self.mixer]}
        if cmd == "audio.set":
            for m in self.mixer:
                if m["pid"] == a["pid"]:
                    if "volume" in a:
                        m["volume"] = a["volume"]
                    if "muted" in a:
                        m["muted"] = a["muted"]
            return {"ok": True, "changed": 1}
        if cmd == "browser.url":
            return {"ok": True, "url": self.urls.get(a["hwnd"], "")}
        return {"ok": False}


@pytest.fixture
def media_env(make, monkeypatch, no_real_browser):
    assistant, events = make()
    keys = []
    monkeypatch.setattr(osctl, "media_key", lambda action, **_: keys.append(action))
    assistant.desktop = Desk()
    helper = FakeHelper()
    assistant.winhelper = assistant.media.helper = assistant.browser.helper = helper
    assistant.media._sleep = lambda s: None

    def youtube(request):
        if request.url.host == "www.youtube.com":
            return httpx.Response(200, text=PAGE)
        return httpx.Response(404)

    assistant.tools._http = httpx.Client(transport=httpx.MockTransport(youtube))
    assistant.tools.open_target = lambda *a, **k: (_ for _ in ()).throw(__import__("core.tools", fromlist=["ToolError"]).ToolError("no such game"))
    return assistant, events, keys, no_real_browser, helper


def statuses(helper):
    return {s["title"]: s["status"] for s in helper.sessions}


def test_pause_youtube_is_checked_not_assumed(media_env):
    assistant, events, keys, opened, helper = media_env
    helper.add("Chrome", "Lofi beats to study to", "Lofi Girl")
    assistant.desktop.list = [Window(1, "Lofi beats to study to - YouTube - Google Chrome", "chrome.exe")]
    assert ask(assistant, events, "pause YouTube") == "Paused YouTube, sir."
    assert statuses(helper) == {"Lofi beats to study to": "Paused"} and keys == []  # a real session command, not a key press
    assert assistant._activities[-1]["status"] == "ok"
    assert ask(assistant, events, "resume the video") == "Resuming Lofi beats to study to by Lofi Girl on YouTube, sir."
    assert ask(assistant, events, "pause it") == "Paused YouTube, sir."
    assert ask(assistant, events, "pause") == "It's already paused, sir."


def test_a_player_that_ignores_the_command_is_reported(media_env):
    assistant, events, keys, opened, helper = media_env
    helper.add("Spotify.exe", "Uprising", "Muse", ignore=True)
    reply = ask(assistant, events, "pause the music")
    assert "didn't respond" in reply and assistant._activities[-1]["status"] == "failed"


def test_two_players_asks_which_then_does_that_one(media_env):
    assistant, events, keys, opened, helper = media_env
    helper.add("Spotify.exe", "Uprising", "Muse")
    helper.add("Chrome", "Cat video", "Cats")
    assistant.desktop.list = [Window(1, "Cat video - YouTube - Google Chrome", "chrome.exe")]
    assert ask(assistant, events, "pause") == "Spotify and YouTube are both playing, sir. Which one?"
    assert ask(assistant, events, "Spotify") == "Paused Spotify, sir."
    assert statuses(helper) == {"Uprising": "Paused", "Cat video": "Playing"}
    assert ask(assistant, events, "resume") .startswith("Resuming Uprising by Muse on Spotify")


def test_pause_everything(media_env):
    assistant, events, keys, opened, helper = media_env
    helper.add("Spotify.exe", "Uprising", "Muse")
    helper.add("Chrome", "Cat video")
    assert ask(assistant, events, "stop all media") == "Stopped Chrome and Spotify, sir."
    assert set(statuses(helper).values()) == {"Paused"}


def test_skip_reports_the_new_song(media_env):
    assistant, events, keys, opened, helper = media_env
    helper.add("Spotify.exe", "Uprising", "Muse", playlist=["Starlight"])
    assert ask(assistant, events, "skip this song") == "Skipped, sir. Now playing Starlight by Muse."
    helper.sessions[0]["can"]["next"] = False
    assert "doesn't let me skip" in ask(assistant, events, "next song")


def test_seeking(media_env):
    assistant, events, keys, opened, helper = media_env
    helper.add("Chrome", "Lecture", position=60.0)
    assert ask(assistant, events, "skip forward 30 seconds") == "Jumped to 1:30, sir."
    assert helper.sessions[0]["position"] == 90.0
    assert ask(assistant, events, "restart the video") == "Back to the start, sir."
    assert ask(assistant, events, "jump to 2 minutes") == "Jumped to 2:00, sir."
    helper.sessions[0]["can"]["seek"] = False  # YouTube without seeking through Windows: its own shortcuts in its tab
    assistant.desktop.list = [Window(1, "Lecture - YouTube - Google Chrome", "chrome.exe")]
    assert ask(assistant, events, "go back 30 seconds") == "Back 30 seconds, sir."
    assert assistant.desktop.pressed[-3:] == [(ord("J"),)] * 3


def test_a_seek_the_player_ignores_is_not_claimed(media_env):
    """Seen on real Windows: Chrome accepts the request for a plain page but the position never moves."""
    assistant, events, keys, opened, helper = media_env
    helper.add("Chrome", "Podcast episode", position=30.0, ignore=True)
    reply = ask(assistant, events, "skip forward 20 seconds")
    assert reply == "I asked Chrome to jump to 0:50, sir, but it didn't confirm the change."
    assert assistant._activities[-1]["status"] == "unverified"


def test_youtube_shortcuts(media_env):
    assistant, events, keys, opened, helper = media_env
    assistant.desktop.list = [Window(1, "Lecture - YouTube - Google Chrome", "chrome.exe")]
    assert ask(assistant, events, "full screen") == "Full screen, sir."
    assert ask(assistant, events, "turn on captions") == "Captions on, sir."
    assert ask(assistant, events, "speed it up") == "Faster, sir."
    assert assistant.desktop.pressed == [(ord("F"),), (ord("C"),), (0x10, 0xBE)]
    assistant.desktop.list = []
    assert "can't see one open" in ask(assistant, events, "exit full screen")


def test_music_volume_is_the_apps_own(media_env):
    assistant, events, keys, opened, helper = media_env
    helper.add("Spotify.exe", "Uprising", "Muse")
    helper.mixer = [{"pid": 50, "name": "Spotify", "state": 1, "volume": 0.9, "muted": False}]
    assert ask(assistant, events, "turn the music down") == "Spotify is at 70 percent now, sir. My voice stays where it was."
    assert ask(assistant, events, "set the music volume to 25") == "Spotify is at 25 percent now, sir. My voice stays where it was."
    assert ask(assistant, events, "mute the music") == "Muted Spotify, sir."
    assert helper.mixer[0]["muted"] is True


def test_music_volume_without_a_mixer_uses_the_system_volume(media_env, monkeypatch):
    assistant, events, keys, opened, helper = media_env
    helper.caps["audio"] = False
    calls = []
    monkeypatch.setattr(osctl, "change_volume", lambda d: calls.append(d))
    assert ask(assistant, events, "turn the music down") == "Volume down, sir."
    assert calls == [-10]


def test_whats_playing_with_position(media_env):
    assistant, events, keys, opened, helper = media_env
    assert ask(assistant, events, "what's playing") == "Nothing's playing right now, sir."
    helper.add("Spotify.exe", "Uprising", "Muse", position=75.0, duration=305.0)
    assert ask(assistant, events, "what's playing") == "That's Uprising by Muse, on Spotify, 1:15 of 5:05, sir."


def test_play_a_song_starts_the_top_youtube_video(media_env, monkeypatch):
    assistant, events, keys, opened, helper = media_env
    from core.browser import BrowserManager

    def launch(self, url, key):
        opened.append(url)
        helper.add("Chrome", "Queen – Bohemian Rhapsody (Official Video)", "Queen Official")

    monkeypatch.setattr(BrowserManager, "_launch", launch)
    helper.add("Spotify.exe", "Uprising", "Muse")
    reply = ask(assistant, events, "play bohemian rhapsody")
    assert reply == "Playing Queen – Bohemian Rhapsody (Official Video) on YouTube, sir."
    assert opened == ["https://www.youtube.com/watch?v=fJ9rUzIMcZQ"]
    assert statuses(helper)["Uprising"] == "Paused"  # Spotify paused first: one thing at a time
    assert assistant.config.get("media_history")[-1]["url"] == "https://www.youtube.com/watch?v=fJ9rUzIMcZQ"
    assert ask(assistant, events, "pause it") == "Paused YouTube, sir."


def test_autoplay_blocked_then_pressed_play(media_env, monkeypatch):
    assistant, events, keys, opened, helper = media_env
    from core.browser import BrowserManager

    monkeypatch.setattr(BrowserManager, "_launch", lambda self, url, key: helper.add("Chrome", "Queen – Bohemian Rhapsody (Official Video)", status="Paused"))
    assert ask(assistant, events, "find a video explaining how neural networks work and play it on youtube") == \
        "Playing Queen – Bohemian Rhapsody (Official Video) on YouTube, sir."
    assert helper.sessions[0]["status"] == "Playing"


def test_resume_what_i_was_listening_to(media_env, monkeypatch):
    assistant, events, keys, opened, helper = media_env
    assert "don't have a record" in ask(assistant, events, "resume what I was listening to earlier")
    assistant.config.update({"media_history": [{"title": "Starlight", "app": "YouTube", "url": "https://www.youtube.com/watch?v=abcdefghijk",
                                                "position": 42, "at": 1}]})
    assert ask(assistant, events, "resume what I was listening to earlier") == "Back to Starlight, sir."
    assert opened[-1] == "https://www.youtube.com/watch?v=abcdefghijk&t=42s"
    helper.add("Spotify.exe", "Uprising", "Muse", status="Paused")
    assert ask(assistant, events, "resume what I was listening to") == "Picking up Uprising by Muse on Spotify, sir."


def test_play_music_resumes_or_starts_something(media_env, monkeypatch):
    assistant, events, keys, opened, helper = media_env
    helper.add("Spotify.exe", "Uprising", "Muse", status="Paused")
    assert ask(assistant, events, "play music") == "Resuming Uprising by Muse on Spotify, sir."


def test_without_the_helper_media_keys_and_window_titles(media_env):
    assistant, events, keys, opened, helper = media_env
    helper.caps = {}
    assistant.desktop.list = [Window(1, "Muse - Uprising", "Spotify.exe")]
    reply = ask(assistant, events, "pause the music")
    assert keys == ["toggle"] and "can't confirm" not in reply or "didn't respond" in reply
    assistant.desktop.list = [Window(1, "Lofi - YouTube - Google Chrome", "chrome.exe")]
    assert ask(assistant, events, "next song").endswith("I can't confirm it from here, though.")


def test_hud_strip_and_buttons(media_env):
    assistant, events, keys, opened, helper = media_env
    helper.add("Spotify.exe", "Uprising", "Muse")
    helper.mixer = [{"pid": 50, "name": "Spotify", "state": 1, "volume": 0.6, "muted": False}]
    assistant._media_refresh()
    ev = events.of("media")[-1]
    assert ev["playing"]["title"] == "Uprising" and ev["sessions"][0]["volume"] == 0.6
    assistant._media_refresh()
    assert len(events.of("media")) == 1  # only on changes
    key = ev["sessions"][0]["key"]
    assert assistant.media_control("toggle", key)["verified"] and helper.sessions[0]["status"] == "Paused"
    assert assistant.media_volume(key, 0.3)["after"] == 0.3
    assert assistant.media_control("format c:")["ok"] is False


# ----------------------------------------------------------------------------- lowering music while JARVIS speaks
def test_ducking_restores_volumes_even_after_a_crash(tmp_path):
    from core.mediahub import MediaHub

    helper = FakeHelper()
    helper.mixer = [{"pid": 50, "name": "Spotify", "state": 1, "volume": 0.8, "muted": False},
                    {"pid": 60, "name": "chrome", "state": 0, "volume": 1.0, "muted": False},  # not making sound: left alone
                    {"pid": 70, "name": "Jarvis", "state": 1, "volume": 1.0, "muted": False}]  # our own voice: never
    hub = MediaHub(helper, state_dir=tmp_path, own_pid=70)
    hub.duck(0.25)
    assert [m["volume"] for m in helper.mixer] == [0.2, 1.0, 1.0] and (tmp_path / "ducked.json").exists()
    hub.duck(0.25)  # a second sentence: no double ducking
    hub.unduck(delay=0)
    assert helper.mixer[0]["volume"] == 0.2
    hub.unduck(delay=0)
    import time as _t
    _t.sleep(0.2)
    assert helper.mixer[0]["volume"] == 0.8 and not (tmp_path / "ducked.json").exists()
    # crash while ducked: the next start puts it back
    hub.duck(0.25)
    assert helper.mixer[0]["volume"] == 0.2
    fresh = MediaHub(helper, state_dir=tmp_path, own_pid=70)
    assert fresh.recover() == 1 and helper.mixer[0]["volume"] == 0.8
    # the user turned it up themselves while JARVIS spoke: their choice stays
    hub2 = MediaHub(helper, state_dir=tmp_path, own_pid=70)
    hub2.duck(0.25)
    helper.mixer[0]["volume"] = 0.5
    hub2.unduck(delay=0)
    _t.sleep(0.2)
    assert helper.mixer[0]["volume"] == 0.5


def test_speaking_ducks_and_restores(make, no_real_browser):
    assistant, events = make()
    helper = FakeHelper()
    helper.mixer = [{"pid": 50, "name": "Spotify", "state": 1, "volume": 0.8, "muted": False}]
    assistant.winhelper = assistant.media.helper = helper
    seen = []
    original = assistant.media.duck
    assistant.media.duck = lambda level: (original(level), seen.append(helper.mixer[0]["volume"]))
    ask(assistant, events, "explain the arc reactor")
    import time as _t
    deadline = _t.time() + 5
    while helper.mixer[0]["volume"] != 0.8 and _t.time() < deadline:
        _t.sleep(0.05)
    assert seen and seen[0] == 0.24 and helper.mixer[0]["volume"] == 0.8


def test_media_key_platforms():
    pressed = []

    class U32:
        def keybd_event(self, key, scan, flags, extra):
            pressed.append((key, flags))

    osctl.media_key("next", platform="win32", user32=U32())
    assert pressed == [(0xB0, 0), (0xB0, 2)]
    calls = []
    osctl.media_key("toggle", platform="linux", runner=lambda cmd, **k: calls.append(cmd))
    assert calls == [["playerctl", "play-pause"]]
