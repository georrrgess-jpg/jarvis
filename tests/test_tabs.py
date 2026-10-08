"""Browser tabs and apps by voice, against a simulated desktop (Chrome with several tabs, Edge, Spotify)."""

import pytest

from core import tabs
from core.screen import ScreenError, Window
from core.vision import parse_act


class Browser:
    """A browser window whose title follows its active tab, like the real thing."""

    def __init__(self, hwnd, exe, suffix, tab_titles):
        self.hwnd, self.exe, self.suffix = hwnd, exe, suffix
        self.tabs = list(tab_titles)
        self.active = 0
        self.closed = []

    @property
    def title(self):
        return f"{self.tabs[self.active]} - {self.suffix}" if self.tabs else self.suffix

    def key(self, keys):
        if keys == [0x11, ord("W")]:
            self.closed.append(self.tabs.pop(self.active))
            self.active = min(self.active, len(self.tabs) - 1)
        elif keys == [0x11, 0x09]:
            self.active = (self.active + 1) % len(self.tabs)
        elif keys == [0x11, 0x10, ord("T")]:
            self.tabs.insert(self.active + 1, self.closed.pop())
            self.active += 1


class FakeDesktop:
    def __init__(self):
        self.chrome = Browser(1, "chrome.exe", "Google Chrome", ["Inbox - Gmail", "Never Gonna Give You Up - YouTube", "Messi bio - Google Docs"])
        self.edge = Browser(2, "msedge.exe", "Microsoft​ Edge", ["Weather - BBC"])
        self.other = [Window(3, "Spotify Premium", "Spotify.exe", (0, 0, 800, 600)), Window(4, "Notes - Notepad", "notepad.exe", (0, 0, 500, 400))]
        self.closed_windows = []
        self.front = None
        self.presses = []

    def _win(self, b):
        return Window(b.hwnd, b.title, b.exe, (0, 0, 1200, 800))

    def windows(self, include_own=False):
        return [self._win(self.chrome), self._win(self.edge), *[w for w in self.other if w.hwnd not in self.closed_windows]]

    def window_info(self, hwnd):
        return next((w for w in self.windows() if w.hwnd == hwnd), None)

    def bring_to_front(self, window):
        self.front = window.hwnd

    def press(self, keys, window=None):
        self.presses.append((keys, window.hwnd if window else None))
        target = {1: self.chrome, 2: self.edge}.get(window.hwnd if window else self.front)
        if target:
            target.key(keys)

    def close(self, window):
        self.closed_windows.append(window.hwnd)


@pytest.fixture
def desk():
    return FakeDesktop()


def test_title_helpers():
    assert tabs.tab_title("Never Gonna Give You Up - YouTube - Google Chrome") == "Never Gonna Give You Up - YouTube"
    assert tabs.browser_key("google chrome") == "chrome" and tabs.browser_key("microsoft edge") == "edge" and tabs.browser_key("browser") == "browser"


def test_close_the_chrome_tab_closes_the_active_one(desk):
    tabs.close_current_tab(desk, "chrome")
    assert desk.chrome.closed == ["Inbox - Gmail"] and desk.edge.closed == []


def test_close_a_named_tab_finds_it_first(desk):
    title = tabs.close_named_tab(desk, "YouTube")
    assert title == "Never Gonna Give You Up - YouTube"
    assert desk.chrome.closed == ["Never Gonna Give You Up - YouTube"] and len(desk.chrome.tabs) == 2


def test_named_tab_in_another_browser(desk):
    tabs.close_named_tab(desk, "BBC weather", "edge")
    assert desk.edge.closed == ["Weather - BBC"]


def test_google_docs_tab(desk):
    tabs.close_named_tab(desk, "google docs")
    assert desk.chrome.closed == ["Messi bio - Google Docs"]


def test_missing_tab_or_browser_is_explained(desk):
    with pytest.raises(ScreenError, match="couldn't find a Netflix tab"):
        tabs.close_named_tab(desk, "Netflix")
    assert desk.chrome.closed == [] and desk.chrome.active == 0  # walked all the way round, closed nothing
    with pytest.raises(ScreenError, match="Firefox isn't open"):
        tabs.close_current_tab(desk, "firefox")


def test_reopen_brings_the_tab_back(desk):
    tabs.close_named_tab(desk, "gmail")
    tabs.reopen_tab(desk, "chrome")
    assert "Inbox - Gmail" in desk.chrome.tabs


def test_close_browser_closes_its_windows_only(desk):
    assert tabs.close_browser(desk, "chrome") == 1
    assert desk.closed_windows == [1]


# ----------------------------------------------------------------------------- through the assistant
@pytest.fixture
def jarvis(config, desk):
    from core.assistant import Assistant
    from tests.test_assistant import Events, FakeTTS

    events = Events()
    assistant = Assistant(config, events, tts=FakeTTS(), desktop=desk)
    assistant.tracker.current = lambda: None
    yield assistant
    assistant.shutdown()


def say(assistant, text):
    act = parse_act(text)
    assert act is not None, text
    return "".join(assistant._act(None, act))


def test_close_the_google_chrome_tab_never_opens_anything(jarvis, desk):
    reply = say(jarvis, "close the google chrome tab")
    assert reply.startswith("Tab closed") and "reopen the tab" in reply
    assert desk.chrome.closed == ["Inbox - Gmail"]


def test_close_the_youtube_tab(jarvis, desk):
    assert say(jarvis, "close the YouTube tab").startswith("Closed the Never Gonna Give You Up - YouTube tab")


def test_close_tab_uses_the_browser_you_were_in(jarvis, desk):
    jarvis.tracker.current = lambda: desk._win(desk.edge)
    say(jarvis, "close the tab in the browser")
    assert desk.edge.closed == ["Weather - BBC"] and desk.chrome.closed == []


def test_close_apps_by_name(jarvis, desk):
    assert say(jarvis, "close spotify") == "Closing spotify, sir."
    assert desk.closed_windows == [3]
    assert say(jarvis, "close notepad") == "Closing notepad, sir."
    assert "can't see photoshop open" in say(jarvis, "close photoshop")


def test_screen_control_switch_is_respected(jarvis, desk):
    jarvis.config.update({"allow_control": False})
    assert "switched off" in say(jarvis, "close the youtube tab")
    assert desk.chrome.closed == []


def test_close_requests_are_never_offered_open_tools():
    from core.assistant import _CLOSING

    assert _CLOSING.match("close the thing in the corner") and _CLOSING.match("please close it") and not _CLOSING.match("open chrome")


@pytest.mark.parametrize("text", ["close it", "close the door", "close my eyes", "close the deal", "close everything"])
def test_not_app_closing(text):
    act = parse_act(text)
    assert act is None or act.action != "close_app"
