"""The browser manager: profiles, readiness states, verified navigation, and sign-in pages handled properly."""

import json
import threading
import time

import pytest

from core import browser as bm
from core.browser import BrowserManager, classify, matches, needs_account, pick_profile, read_profiles, site_name, tab_title
from core.screen import Window
from tests.test_assistant import make  # noqa: F401  (a fixture)
from tests.test_personas import ask


@pytest.mark.parametrize("title, url, state", [
    ("Sign in - Google Accounts - Google Chrome", "", bm.AUTHENTICATION_REQUIRED),
    ("Gmail - Google Chrome", "accounts.google.com/v3/signin/identifier?continue=https%3A%2F%2Fmail.google.com", bm.AUTHENTICATION_REQUIRED),
    ("Choose an account - Google Chrome", "", bm.AUTHENTICATION_REQUIRED),
    ("Who's using Chrome?", "", bm.PROFILE_SELECTION),
    ("Google Chrome", "chrome://profile-picker/", bm.PROFILE_SELECTION),
    ("Welcome to Chrome - Google Chrome", "", bm.FIRST_RUN_SETUP),
    ("Sign in to Chrome - Google Chrome", "", bm.FIRST_RUN_SETUP),
    ("Before you continue to YouTube - Google Chrome", "", bm.CONSENT),
    ("This site can’t be reached - Google Chrome", "", bm.ERROR),
    ("No internet - Google Chrome", "", bm.OFFLINE),
    ("New Tab - Google Chrome", "", bm.PAGE_LOADING),
    ("YouTube - Google Chrome", "", bm.PAGE_LOADING),
    ("Inbox (3) - me@gmail.com - Gmail - Google Chrome", "mail.google.com/mail/u/0/#inbox", bm.READY),
    ("Lofi beats - YouTube - Google Chrome", "youtube.com/watch?v=abc", bm.READY),
    ("Wikipedia - Personal - Microsoft\u200b Edge", "", bm.READY),
])
def test_classify(title, url, state):
    assert classify(title, url) == state


def test_titles_sites_and_accounts():
    assert tab_title("Lofi beats - YouTube - Google Chrome") == "Lofi beats - YouTube"
    assert tab_title("Wikipedia - Personal - Microsoft\u200b Edge") == "Wikipedia"
    assert site_name("https://www.youtube.com/watch?v=1") == "YouTube" and site_name("https://en.wikipedia.org/wiki/Cat") == "Wikipedia"
    assert needs_account("https://mail.google.com/mail/") and needs_account("docs.google.com/document/d/1")
    assert not needs_account("https://www.youtube.com") and not needs_account("https://www.google.com/search?q=x")
    look = bm.Look(bm.READY, "Lofi beats - YouTube - Google Chrome", "")
    assert matches(look, "youtube.com", "", "YouTube") and matches(look, "", "Lofi beats", "")
    assert not matches(bm.Look(bm.AUTHENTICATION_REQUIRED, "Gmail - Google Chrome", "accounts.google.com/signin"), "mail.google.com")
    assert matches(bm.Look(bm.READY, "Inbox - Gmail - Google Chrome", "mail.google.com/mail/u/0/"), "mail.google.com")


def test_profiles_from_local_state(tmp_path):
    folder = tmp_path / "Google" / "Chrome" / "User Data"
    folder.mkdir(parents=True)
    (folder / "Local State").write_text(json.dumps({"profile": {"last_used": "Profile 1", "info_cache": {
        "Default": {"name": "Person 1", "user_name": ""},
        "Profile 1": {"name": "Work", "user_name": "boss@company.com"},
        "Profile 2": {"name": "Home", "user_name": "me@gmail.com"}}}}), encoding="utf-8")
    profiles = read_profiles("chrome", str(tmp_path))
    assert [p.directory for p in profiles] == ["Profile 1", "Default", "Profile 2"]
    assert pick_profile(profiles, "me@gmail.com").directory == "Profile 2"  # signed in to the linked Google account
    assert pick_profile(profiles, "").directory == "Profile 1"  # else the last used
    assert pick_profile(profiles, "", "Home").directory == "Profile 2"  # or the one chosen in Settings
    assert read_profiles("chrome", str(tmp_path / "nothing")) == [] and read_profiles("firefox", str(tmp_path)) == []


class Screen:
    """Browser windows whose titles change over time, as the script says."""

    def __init__(self, script):
        self.script = list(script)  # list of (after_n_looks, [(hwnd, title, app)])
        self.looks = 0
        self.list = []
        self.brought = []

    def windows(self):
        self.looks += 1
        while self.script and self.looks >= self.script[0][0]:
            self.list = [Window(h, t, a) for h, t, a in self.script.pop(0)[1]]
        return list(self.list)


def manager(config, screen, urls=None, profiles=None):
    launched = []

    class Helper:
        caps = {"uia": True}

        def has(self, cap):
            return True

        def call(self, cmd, timeout=8.0, **a):
            return {"ok": True, "url": (urls or {}).get(a["hwnd"], lambda: "")()} if urls else {"ok": False}

    clock = [0.0]
    m = BrowserManager(config, windows=screen.windows, helper=Helper() if urls else None, popen=lambda args, **k: launched.append(args),
                       sleep=lambda s: clock.__setitem__(0, clock[0] + max(s, 0.05)), profiles=lambda key: profiles or [], watch=True,
                       bring_to_front=lambda w: screen.brought.append(w.hwnd), clock=lambda: clock[0])
    m._launch = lambda url, key: launched.append(url)
    return m, launched


def test_open_a_public_site_is_verified(config):
    screen = Screen([(1, [(1, "New Tab - Google Chrome", "chrome.exe")]), (3, [(1, "Wikipedia - Google Chrome", "chrome.exe")])])
    m, launched = manager(config, screen)
    nav = m.open("https://www.wikipedia.org", wait=5)
    assert nav.ok and nav.state == bm.PAGE_READY and nav.verified and launched == ["https://www.wikipedia.org"]


def test_gmail_behind_a_sign_in_page_is_reported(config):
    screen = Screen([(1, [(1, "New Tab - Google Chrome", "chrome.exe")]), (3, [(1, "Sign in - Google Accounts - Google Chrome", "chrome.exe")])])
    m, _ = manager(config, screen)
    nav = m.open("https://mail.google.com/mail/", wait=5)
    assert not nav.ok and nav.blocked and nav.state == bm.AUTHENTICATION_REQUIRED


def test_a_stray_sign_in_page_doesnt_block_a_public_site(config):
    """Chrome opens on a sign-in page first, then our YouTube tab: a public site must not wait for a sign-in."""
    screen = Screen([(1, [(1, "Sign in - Google Accounts - Google Chrome", "chrome.exe")]),
                     (4, [(1, "YouTube - Google Chrome", "chrome.exe")]), (6, [(1, "Home - YouTube - Google Chrome", "chrome.exe")])])
    m, _ = manager(config, screen)
    nav = m.open("https://www.youtube.com", wait=30)
    assert nav.ok and nav.state == bm.PAGE_READY


def test_a_page_still_loading_is_not_reported_open(config):
    """Real Chrome: the tab is "Untitled" (or shows the bare address) while the address bar already has the new URL."""
    assert bm.still_loading(bm.Look(bm.READY, "Untitled - Google Chrome", "127.0.0.1:5000/p1.html"))
    assert bm.still_loading(bm.Look(bm.READY, "mail.google.com/mail/ - Google Chrome", "mail.google.com/mail/"))
    assert not bm.still_loading(bm.Look(bm.READY, "Inbox (3) - me@gmail.com - Gmail - Google Chrome", "mail.google.com/mail/u/0/#inbox"))
    assert not matches(bm.Look(bm.READY, "Untitled - Google Chrome", "mail.google.com/mail/"), "mail.google.com")


def test_gmail_redirecting_to_sign_in_is_not_reported_open(config):
    """Seen on the Windows CI: a signed-out Gmail first shows mail.google.com in the address bar, then redirects to the sign-up page."""
    screen = Screen([(1, [(9, "New Tab - Google Chrome", "chrome.exe")]), (2, [(9, "Gmail - Google Chrome", "chrome.exe")]),
                     (4, [(9, "Gmail: Private and Secure Email | Google Workspace - Google Chrome", "chrome.exe")])])
    url = lambda: "mail.google.com/mail/u/0/" if screen.looks < 4 else "workspace.google.com/intl/en/gmail/"  # noqa: E731
    m, _ = manager(config, screen, urls={9: url})
    nav = m.open("https://mail.google.com/mail/", wait=6)
    assert not nav.ok and nav.state == bm.AUTHENTICATION_REQUIRED


def test_profile_picker_and_first_run(config):
    m, _ = manager(config, Screen([(1, [(1, "Who's using Chrome?", "chrome.exe")])]))
    assert m.open("https://www.youtube.com", wait=3).state == bm.PROFILE_SELECTION
    m, _ = manager(config, Screen([(1, [(1, "Welcome to Chrome - Google Chrome", "chrome.exe")])]))
    assert m.open("https://www.youtube.com", wait=3).state == bm.FIRST_RUN_SETUP


def test_address_bar_beats_the_title(config):
    """Gmail's sign-in page is titled just 'Gmail': the address bar shows it's really accounts.google.com."""
    screen = Screen([(1, [(9, "Gmail - Google Chrome", "chrome.exe")])])
    m, _ = manager(config, screen, urls={9: lambda: "accounts.google.com/v3/signin/identifier?service=mail"})
    assert m.open("https://mail.google.com/", wait=3).state == bm.AUTHENTICATION_REQUIRED


def test_launch_reuses_a_running_browser(config):
    screen = Screen([(1, [(1, "Wikipedia - Google Chrome", "chrome.exe")])])
    m, launched = manager(config, screen)
    nav = m.launch_browser()
    assert nav.ok and launched == [] and screen.brought == [1]
    screen = Screen([(1, []), (3, [(2, "New Tab - Google Chrome", "chrome.exe")])])
    m, launched = manager(config, screen)
    nav = m.launch_browser(wait=30)
    assert launched == [None] and nav.state == bm.READY


def test_chrome_starts_in_the_linked_accounts_profile(config, monkeypatch):
    config.update({"google_user_email": "me@gmail.com", "link_browser": "chrome"})
    monkeypatch.setattr(bm, "browser_path", lambda key: "C:/chrome.exe")
    calls = []
    m = BrowserManager(config, windows=lambda: [], popen=lambda args, **k: calls.append(args), watch=False,
                       profiles=lambda key: [bm.Profile("Default", "Me", "", True), bm.Profile("Profile 3", "Home", "me@gmail.com")])
    BrowserManager.real_launch(m, "https://mail.google.com/mail/?authuser=me%40gmail.com", "chrome")
    assert calls == [["C:/chrome.exe", "--profile-directory=Profile 3", "https://mail.google.com/mail/?authuser=me%40gmail.com"]]
    calls.clear()
    BrowserManager.real_launch(m, None, "chrome")  # "open Chrome": a window in that profile, no profile picker
    assert calls == [["C:/chrome.exe", "--profile-directory=Profile 3", "--new-window"]]


def test_waiting_for_the_user_to_sign_in(config):
    screen = Screen([(1, [(1, "Sign in - Google Accounts - Google Chrome", "chrome.exe")]),
                     (5, [(1, "Inbox (2) - me@gmail.com - Gmail - Google Chrome", "chrome.exe")])])
    m, _ = manager(config, screen)
    result = m.wait_until("mail.google.com", "", "Gmail", timeout=60, poll=0)
    assert result.ok and result.state == bm.PAGE_READY
    cancel = threading.Event()
    cancel.set()
    assert not m.wait_until("mail.google.com", timeout=60, cancel=cancel, poll=0).ok


# ----------------------------------------------------------------------------- through JARVIS
@pytest.fixture
def jarvis(make, no_real_browser):
    assistant, events = make()
    assistant.browser.watch = True
    assistant.browser._sleep = lambda s: time.sleep(0.01)
    return assistant, events, no_real_browser


def test_open_youtube_says_open_only_once_it_is(jarvis):
    assistant, events, launched = jarvis
    screen = Screen([(1, [(1, "New Tab - Google Chrome", "chrome.exe")]), (3, [(1, "YouTube - Google Chrome", "chrome.exe")]),
                     (5, [(1, "Home - YouTube - Google Chrome", "chrome.exe")])])
    assistant.desktop = type("D", (), {"windows": lambda self: screen.windows(), "foreground": lambda self: None,
                                       "bring_to_front": lambda self, w: None, "monitors": lambda self: []})()
    assert ask(assistant, events, "open youtube") == "YouTube is open, sir."
    assert launched == ["https://www.youtube.com"] and assistant._activities[-1]["status"] == "ok"


def test_open_gmail_waits_for_sign_in_then_says_so(jarvis):
    assistant, events, launched = jarvis
    screen = Screen([(1, [(1, "Sign in - Google Accounts - Google Chrome", "chrome.exe")])])
    assistant.desktop = type("D", (), {"windows": lambda self: screen.windows(), "foreground": lambda self: None,
                                       "bring_to_front": lambda self, w: None, "monitors": lambda self: []})()
    original = assistant.browser.wait_until
    assistant.browser.wait_until = lambda *a, **k: original(*a, **{**k, "poll": 0.05})
    reply = ask(assistant, events, "open gmail")
    assert reply.startswith("Gmail needs you to sign in to your Google account first") and "never type passwords" in reply
    assert assistant._activities[-1]["status"] == "waiting" and assistant.browser.status()["waiting"]["label"] == "Gmail"
    screen.list = [Window(1, "Inbox - me@gmail.com - Gmail - Google Chrome", "chrome.exe")]  # the user signs in
    events.wait_for(lambda: any("Gmail is open now" in s for s in assistant.tts.spoken), 10)
    assert assistant._activities[-1]["status"] == "ok" and assistant.browser.waiting is None


def test_open_chrome_and_search_google(jarvis):
    assistant, events, launched = jarvis
    screen = Screen([(1, [(1, "New Tab - Google Chrome", "chrome.exe")]), (3, [(1, "latest tech news - Google Search - Google Chrome", "chrome.exe")])])
    assistant.desktop = type("D", (), {"windows": lambda self: screen.windows(), "foreground": lambda self: None,
                                       "bring_to_front": lambda self, w: None, "monitors": lambda self: []})()
    assert ask(assistant, events, "open chrome") == "Google Chrome is up, sir."
    assert ask(assistant, events, "search google for latest tech news") == "Here are Google's results for latest tech news, sir."
    assert launched[-1] == "https://www.google.com/search?q=latest+tech+news"
    assert ask(assistant, events, "what page am I on").startswith("You're on “latest tech news - Google Search”")


def test_favourite_website(jarvis):
    assistant, events, launched = jarvis
    assistant.browser.watch = False
    assert "haven't told me" in ask(assistant, events, "open my favourite website")
    ask(assistant, events, "my favourite website is github.com")
    assert ask(assistant, events, "open my favourite website") == "Opening GitHub, sir."
    assert launched[-1] == "https://github.com"


def test_an_app_is_only_called_open_once_its_window_shows(jarvis):
    assistant, events, _ = jarvis
    wins = []
    assistant.desktop = type("D", (), {"windows": lambda self: list(wins), "foreground": lambda self: None,
                                       "bring_to_front": lambda self, w: None, "monitors": lambda self: []})()
    assistant.tools.open_target = lambda target, **k: {"name": "Discord", "kind": "app"}
    assert ask(assistant, events, "open discord") == "Opening Discord, sir."  # launched, but no window yet: not claimed
    assert assistant._activities[-1]["status"] == "unverified"
    wins.append(Window(5, "Friends - Discord", "Discord.exe"))
    assert ask(assistant, events, "open discord") == "Discord is open, sir."
    assert assistant._activities[-1]["status"] == "ok" and assistant._activities[-1]["kind"] == "app"


def test_a_slow_app_is_watched_until_its_window_appears(jarvis, monkeypatch):
    assistant, events, _ = jarvis
    from core.assistant import Assistant

    monkeypatch.setattr(Assistant, "LAUNCH_WAIT", 0.2)
    wins = []
    assistant.desktop = type("D", (), {"windows": lambda self: list(wins), "foreground": lambda self: None,
                                       "bring_to_front": lambda self, w: None, "monitors": lambda self: []})()
    assistant.tools.open_target = lambda target, **k: {"name": "Steam", "kind": "app"}
    assert ask(assistant, events, "open steam") == "Opening Steam, sir."
    wins.append(Window(6, "Steam", "steam.exe"))  # the window shows up a moment later
    deadline = time.time() + 5
    while time.time() < deadline and not any(a["kind"] == "app" and a["status"] == "ok" for a in assistant._activities):
        time.sleep(0.05)
    done = [a for a in assistant._activities if a["kind"] == "app"]
    assert done and done[-1]["status"] == "ok" and "window appeared" in done[-1]["detail"]
