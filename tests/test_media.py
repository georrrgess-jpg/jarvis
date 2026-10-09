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

    def windows(self):
        return list(self.list)

    def foreground(self):
        return None

    def monitors(self):
        return []


@pytest.fixture
def media_env(make, monkeypatch):
    assistant, events = make()
    keys, opened = [], []
    monkeypatch.setattr(osctl, "media_key", lambda action, **_: keys.append(action))
    assistant.desktop = Desk()
    assistant.tools.open_website = lambda url: opened.append(url) or {"opened": url}

    def youtube(request):
        if request.url.host == "www.youtube.com":
            return httpx.Response(200, text=PAGE)
        return httpx.Response(404)

    assistant.tools._http = httpx.Client(transport=httpx.MockTransport(youtube))
    assistant.tools.open_target = lambda *a, **k: (_ for _ in ()).throw(__import__("core.tools", fromlist=["ToolError"]).ToolError("no such game"))
    return assistant, events, keys, opened


def test_play_a_song_starts_the_top_youtube_video(media_env):
    assistant, events, keys, opened = media_env
    reply = ask(assistant, events, "play bohemian rhapsody")
    assert reply == "Playing Queen – Bohemian Rhapsody (Official Video) on YouTube, sir."
    assert opened == ["https://www.youtube.com/watch?v=fJ9rUzIMcZQ"]


def test_play_in_a_chain(media_env):
    assistant, events, keys, opened = media_env
    assistant.tools.open_target = lambda target, **k: {"name": "Notepad", "path": "x"} if "notepad" in target.lower() else \
        (_ for _ in ()).throw(__import__("core.tools", fromlist=["ToolError"]).ToolError("no"))
    reply = ask(assistant, events, "open notepad and play some jazz")
    assert "Opening Notepad" in reply and "Playing" in reply and "couldn't work out" not in reply
    assert opened == ["https://www.youtube.com/watch?v=fJ9rUzIMcZQ"]


def test_spotify_opens_its_search(media_env, monkeypatch):
    assistant, events, keys, opened = media_env
    uris = []
    assistant.tools.open_uri = lambda uri: uris.append(uri)
    reply = ask(assistant, events, "play lo-fi beats on spotify")
    assert uris == ["spotify:search:lo-fi%20beats"] and "Spotify" in reply and not opened


def test_media_keys(media_env):
    assistant, events, keys, opened = media_env
    assert ask(assistant, events, "pause the music") == "Paused, sir."
    assert ask(assistant, events, "next song") == "Skipping ahead, sir."
    assert ask(assistant, events, "go back a song") == "Going back a track, sir."
    assert ask(assistant, events, "resume") == "Resuming, sir."
    assert keys == ["toggle", "next", "previous", "toggle"]


def test_spotify_state_is_respected(media_env):
    assistant, events, keys, opened = media_env
    assistant.desktop.list = [Window(1, "Spotify Premium", "Spotify.exe")]
    assert ask(assistant, events, "pause") == "Spotify is already paused, sir."
    assert ask(assistant, events, "play some music") == "Resuming Spotify, sir."
    assistant.desktop.list = [Window(1, "Queen - Bohemian Rhapsody", "Spotify.exe")]
    assert ask(assistant, events, "resume").startswith("It's already playing")
    assert ask(assistant, events, "what's playing") == "That's Bohemian Rhapsody by Queen, on Spotify, sir."
    keys.clear()
    ask(assistant, events, "play bohemian rhapsody on youtube")
    assert keys == ["toggle"]  # Spotify paused first so two songs don't play at once
    assistant.desktop.list = []
    assert ask(assistant, events, "what's playing").startswith("I can't see anything playing")


def test_now_playing_strip_and_buttons(media_env):
    assistant, events, keys, opened = media_env
    assistant.desktop.list = [Window(1, "Muse - Uprising", "Spotify.exe")]
    assistant._media_refresh()
    assert events.of("media")[-1]["playing"] == {"title": "Uprising", "artist": "Muse", "app": "Spotify", "playing": True}
    assistant._media_refresh()
    assert len(events.of("media")) == 1  # only on changes
    assert assistant.media_control("next")["ok"] and keys == ["next"]
    assert assistant.media_control("format c:")["ok"] is False
    assistant.desktop.list = []
    assistant._media_refresh()
    assert events.of("media")[-1]["playing"] is None


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
