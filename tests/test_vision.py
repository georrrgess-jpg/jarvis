"""JARVIS's eyes: looking at the screen, finding things on it, watching it and acting on it.

The desktop and OCR are faked (real screenshots are tested on the Windows CI runner by
tests/windows_vision_check.py); everything else is the real code talking to a mock Ollama server.
"""

import json
import threading
import time

import pytest
from PIL import Image, ImageDraw

from core.ocr import Line, Word, find_text, lines_from_json
from core.screen import Shot, Window, is_private, parse_keys
from core.vision import (VisionEngine, Watcher, data_url, first_number, frame_change, frame_signature, is_risky, parse_act,
                         parse_look, parse_watch, wants_vision_install)

WINDOW = Window(4242, "Inbox - Mail", "mail.exe", (100, 50, 1300, 850))


def frame(color=(30, 30, 40), text_lines=(), mark=None, size=(1200, 800)):
    """A fake screenshot. ``text_lines`` is what the fake OCR will 'read' from it."""
    image = Image.new("RGB", size, color)
    if mark:
        ImageDraw.Draw(image).rectangle(mark, fill=(240, 240, 240))
    image.info["ocr"] = list(text_lines)
    return image


def line(text, x, y, word_w=60, h=20):
    words, cx = [], x
    for part in text.split():
        words.append(Word(part, cx, y, word_w, h))
        cx += word_w + 8
    return Line(text, words)


class FakeDesktop:
    def __init__(self, frames, window=WINDOW):
        self.frames = list(frames)
        self.window = window
        self.index = 0
        self.actions = []
        self.is_alive = True

    def foreground(self):
        return self.window

    def alive(self, window):
        return self.is_alive

    def capture(self, window=None):
        image = self.frames[min(self.index, len(self.frames) - 1)]
        self.index += 1
        left, top = (window.rect[0], window.rect[1]) if window else (0, 0)
        return Shot(image, left, top, 1.0, window)

    def click(self, x, y, button="left", double=False, window=None):
        self.actions.append(("click", x, y, button, double))

    def type_text(self, text, window=None):
        self.actions.append(("type", text))

    def press(self, keys, window=None):
        self.actions.append(("press", list(keys)))

    def scroll(self, clicks, x=None, y=None, window=None):
        self.actions.append(("scroll", clicks))

    def close(self, window):
        self.actions.append(("close", window.hwnd))


class FakeOcr:
    name = "fake OCR"
    error = None

    def __init__(self, ready=True):
        self.ready = ready
        self.reads = 0

    def available(self):
        return self.ready

    def read(self, image):
        self.reads += 1
        return list(image.info.get("ocr", []))


# ------------------------------------------------------------------ understanding requests


@pytest.mark.parametrize("text", ["what's on my screen", "what am I looking at", "can you see my screen", "look at this",
                                  "summarize this article", "read this email to me", "explain this error", "what does this error mean",
                                  "translate this page", "how do I fix this", "help me with this", "which one is cheaper on my screen",
                                  "is this website safe?", "what's on the whole screen"])
def test_look_requests(text):
    assert parse_look(text) is not None


@pytest.mark.parametrize("text", ["what is the capital of France", "open spotify", "read my Messi doc", "write a bio on Lionel Messi",
                                  "what time is it", "tell me a joke"])
def test_not_look_requests(text):
    assert parse_look(text) is None


def test_full_screen_only_when_asked():
    assert parse_look("what's on the whole screen").full_screen and not parse_look("what's on my screen").full_screen


@pytest.mark.parametrize("text, action, condition", [
    ("watch my screen", "start", ""), ("keep an eye on my screen", "start", ""), ("stop watching", "stop", ""),
    ("tell me when my download finishes", "start", "my download finishes"), ("let me know when the render is done", "start", "the render is done"),
    ("notify me if an error appears", "start", "an error appears"), ("watch for new messages", "start", "new messages"),
    ("are you watching", "status", ""),
])
def test_watch_requests(text, action, condition):
    req = parse_watch(text)
    assert (req.action, req.condition) == (action, condition)


@pytest.mark.parametrize("text", ["tell me when 5 minutes are up", "set a timer for 5 minutes", "tell me a joke"])
def test_not_watch_requests(text):
    assert parse_watch(text) is None


@pytest.mark.parametrize("text, action, target, extra", [
    ("click send", "click", "send", None), ("double click the folder", "double", "the folder", None),
    ("right click the file", "right", "the file", None), ("press enter", "keys", "", [13]), ("hit control c", "keys", "", [17, 67]),
    ("press the submit button", "click", "the submit button", None), ("type hello world", "type", "", "hello world"),
    ("type my name into the search box", "type", "search box", "my name"), ("scroll down", "scroll", "", -5),
    ("scroll to the top", "keys", "", [17, 36]), ("close this window", "close", "", None), ("close that tab", "close_tab", "", None),
    ("go back", "keys", "", [18, 37]), ("refresh the page", "keys", "", [116]), ("select the second option", "click", "the second option", None),
])
def test_act_requests(text, action, target, extra):
    act = parse_act(text)
    assert (act.action, act.target) == (action, target)
    if isinstance(extra, list):
        assert act.keys == extra
    elif isinstance(extra, int):
        assert act.amount == extra
    elif isinstance(extra, str):
        assert act.text == extra


@pytest.mark.parametrize("text", ["choose a number between 1 and 10", "check the weather", "click", "open spotify", "what's on my screen"])
def test_not_act_requests(text):
    assert parse_act(text) is None


def test_risky_actions_and_privacy():
    assert is_risky(parse_act("click send")) and is_risky(parse_act("press the delete button")) and is_risky(parse_act("close this window"))
    assert not is_risky(parse_act("click sign up")) and not is_risky(parse_act("scroll down"))
    assert is_private(Window(1, "Bitwarden - Vault", "bitwarden.exe")) and is_private(Window(2, "Barclays online banking", "chrome.exe"))
    assert not is_private(Window(3, "Inbox - Mail", "mail.exe")) and not is_private(None)
    assert wants_vision_install("install vision") and not wants_vision_install("install spotify")


def test_keys_ocr_parsing_and_text_matching():
    assert parse_keys("ctrl+shift+t") == [0x11, 0x10, ord("T")] and parse_keys("page down") == [0x22] and parse_keys("hello world") is None
    lines = lines_from_json({"lines": [{"text": "Sign in to continue", "words": [
        {"t": "Sign", "x": 20, "y": 40, "w": 60, "h": 30}, {"t": "in", "x": 90, "y": 40, "w": 30, "h": 30},
        {"t": "to", "x": 130, "y": 40, "w": 30, "h": 30}, {"t": "continue", "x": 170, "y": 40, "w": 120, "h": 30}]}]}, scale=2.0)
    assert lines[0].words[0].x == 10 and lines[0].words[0].h == 15, "coordinates are mapped back from the enlarged image"
    hits = find_text(lines, "the sign in button")
    assert hits and hits[0].text == "Sign in" and hits[0].score == 1.0
    assert hits[0].center == (pytest.approx(35.0), pytest.approx(27.5))
    assert find_text(lines, "checkout") == []
    assert Shot(None, 100, 50, 2.0).to_screen(40, 20) == (120, 60)
    assert first_number("Mark 7.") == 7 and first_number("none") is None


def test_frame_change_detects_real_changes_only():
    a, b = frame(), frame()
    c = frame(mark=(100, 100, 600, 400))
    assert frame_change(frame_signature(a), frame_signature(b)) < 1.0
    assert frame_change(frame_signature(a), frame_signature(c)) > 5.0


# ------------------------------------------------------------------ the assistant with a fake desktop and a mock model

INBOX = [line("Inbox", 40, 40), line("Sign up", 200, 300), line("Send", 900, 700), line("Search mail", 300, 100)]


@pytest.fixture
def eyes(config, mock_ollama):
    from core.assistant import Assistant
    from tests.test_assistant import Events, FakeTTS

    mock_ollama.models = ["llama3.2:latest", "qwen2.5vl:7b"]
    mock_ollama.vision_models = {"qwen2.5vl:7b"}
    config.update({"ollama_host": mock_ollama.url, "voice_enabled": False, "watch_interval": 1.0})
    made = []

    def build(frames=None, ocr_ready=True, window=WINDOW):
        desktop = FakeDesktop(frames or [frame(text_lines=INBOX)], window)
        events = Events()
        assistant = Assistant(config, events, tts=FakeTTS(), desktop=desktop, ocr=FakeOcr(ocr_ready))
        assistant.start()
        made.append(assistant)

        def say(text, timeout=20):
            n = len(events.of("assistant_end"))
            assistant.submit_text(text)
            events.wait_for(lambda: len(events.of("assistant_end")) == n + 1 and events.states()[-1] == "IDLE", timeout=timeout)
            return "".join(t["text"] for t in events.of("assistant_token")[-80:]).split("\x00")[-1]

        return assistant, events, desktop, say

    yield build
    for a in made:
        a.shutdown()


def chats(mock):
    return [b for p, b in mock.requests if p == "/api/chat"]


def test_look_sends_the_screenshot_and_ocr_text_to_the_vision_model(eyes, mock_ollama):
    assistant, events, desktop, say = eyes()
    mock_ollama.responder = lambda body: "You're looking at your inbox, sir. There's a Send button at the bottom right."
    said = say("what's on my screen")
    assert "inbox" in said.lower()
    request = chats(mock_ollama)[-1]
    assert request["model"] == "qwen2.5vl:7b", "the vision model answers"
    user = request["messages"][-1]
    assert user["images"] and len(user["images"][0]) > 1000, "a real JPEG goes to the model"
    assert "Sign up" in user["content"] and "Inbox - Mail" in request["messages"][0]["content"]
    look = events.of("vision_look")[-1]
    assert look["image"].startswith("data:image/jpeg;base64,") and look["title"] == "Inbox - Mail" and look["ocr"]
    assert "Looking at" in assistant.llm._history[-1]["content"], "the conversation remembers what was seen"


def test_without_a_vision_model_ocr_text_still_answers(eyes, mock_ollama):
    mock_ollama.models = ["llama3.2:latest"]
    assistant, events, desktop, say = eyes()
    mock_ollama.responder = lambda body: "It's your mail inbox, sir."
    said = say("what does this error mean")
    request = chats(mock_ollama)[-1]
    assert request["model"] == "llama3.2:latest" and "images" not in request["messages"][-1]
    assert "Search mail" in request["messages"][-1]["content"] and "inbox" in said
    assert any("install vision" in m["text"] for m in events.of("system_message"))


def test_click_on_exact_text_happens_at_once(eyes, mock_ollama):
    assistant, events, desktop, say = eyes()
    said = say("click sign up")
    # "Sign up" spans x 200..328, y 300..320 in the window, which starts at (100, 50) on screen
    assert desktop.actions == [("click", 100 + 264, 50 + 310, "left", False)]
    assert said.startswith("Done") and not events.of("act_confirm") and not chats(mock_ollama)


def test_risky_clicks_wait_for_yes(eyes, mock_ollama):
    assistant, events, desktop, say = eyes()
    said = say("click send")
    card = events.of("act_confirm")[-1]
    assert card["reason"] == "risky" and card["image"].startswith("data:image/jpeg") and "Say yes" in said
    assert desktop.actions == [], "nothing happens before the user agrees"
    say("yes")
    assert desktop.actions == [("click", 100 + 930, 50 + 710, "left", False)]
    assert events.of("act_status")[-1]["status"] == "done"
    say("click send")
    say("no")
    assert len(desktop.actions) == 1, "'no' cancels"


def test_the_model_picks_from_numbered_marks_when_text_doesnt_match(eyes, mock_ollama):
    assistant, events, desktop, say = eyes()
    asked = []

    def responder(body):
        asked.append(body["messages"][-1]["content"])
        return "4" if "numbered marks" in asked[-1] else "?"

    mock_ollama.responder = responder
    say("click the magnifying glass")
    card = events.of("act_confirm")[-1]
    assert card["reason"] == "unsure" and chats(mock_ollama)[-1]["messages"][-1]["images"]
    assert assistant.confirm_act(card["id"], True)["ok"]
    # mark 4 is the 4th candidate: "Search mail" at x 300..428, y 100..120
    assert desktop.actions == [("click", 100 + 364, 50 + 110, "left", False)]


def test_grid_fallback_for_things_without_text(eyes, mock_ollama):
    assistant, events, desktop, say = eyes(frames=[frame(text_lines=[])])
    replies = iter(["8", "5"])
    mock_ollama.responder = lambda body: next(replies)
    say("click the red icon")
    card = events.of("act_confirm")[-1]
    assistant.confirm_act(card["id"], True)
    # 6x4 grid on 1200x800 -> cell 8 is x 200..400, y 200..400 (+15% margin); its middle sub-cell is centred at (300, 300)
    assert desktop.actions == [("click", 100 + 300, 50 + 300, "left", False)]


def test_keys_scrolling_and_typing(eyes, mock_ollama):
    assistant, events, desktop, say = eyes()
    say("press enter")
    say("scroll down")
    say("type hello there")
    say("type cats into the search box")
    assert desktop.actions[:3] == [("press", [13]), ("scroll", -5), ("type", "hello there")]
    assert desktop.actions[3][0] == "click" and desktop.actions[4] == ("type", "cats"), "clicks the box, then types"
    assert not chats(mock_ollama), "none of this needs the model"


def test_private_windows_and_the_off_switch(eyes, mock_ollama, config):
    assistant, events, desktop, say = eyes(window=Window(7, "Bitwarden - Vault", "bitwarden.exe", (0, 0, 800, 600)))
    assert "privacy list" in say("what's on my screen") and "privacy list" in say("click unlock")
    assert desktop.actions == [] and desktop.index == 0, "it never even took a screenshot"
    desktop.window = WINDOW
    config.update({"allow_control": False})
    assert "switched off" in say("click sign up") and desktop.actions == []
    config.update({"allow_control": True})
    assert "rather not type into password" in say("type hunter2 into the password field")


def test_install_vision_downloads_the_model(eyes, mock_ollama, config):
    mock_ollama.models = ["llama3.2:latest"]
    assistant, events, desktop, say = eyes()
    said = say("install vision")
    assert "Downloading my vision model" in said
    events.wait_for(lambda: any(e.get("role") == "vision" and e.get("ok") for e in events.of("pull_done")), timeout=20)
    assert config["vision_model"] == "qwen2.5vl:7b"
    assert any(p == "/api/pull" for p, _ in mock_ollama.requests)


# ------------------------------------------------------------------ watching


def run_watcher(engine, frames_lines, condition="", responder=None, mock=None):
    notes, ended = [], threading.Event()
    reasons = []
    w = Watcher(engine, lambda: WINDOW, notify=notes.append, on_end=lambda r: (reasons.append(r), ended.set()),
                condition=condition, interval=0.03, cooldown=0.0)
    w.start()
    ended.wait(8) if condition else time.sleep(1.2)
    w.stop()
    ended.wait(3)
    return notes, reasons


def make_engine(config, mock_ollama, frames, ocr_ready=True, vision=True):
    from core.llm import LLMEngine

    mock_ollama.models = ["llama3.2:latest"] + (["qwen2.5vl:7b"] if vision else [])
    mock_ollama.vision_models = {"qwen2.5vl:7b"}
    config.update({"ollama_host": mock_ollama.url})
    llm = LLMEngine(config)
    status = llm.check()
    engine = VisionEngine(config, llm, FakeDesktop(frames), FakeOcr(ocr_ready))
    engine.refresh(status.models)
    return engine


def test_watch_tells_you_when_the_condition_comes_true(config, mock_ollama):
    downloading = frame(text_lines=[line("Downloading 45%", 50, 50)])
    done = frame(text_lines=[line("Download complete", 50, 50)], mark=(0, 0, 600, 400))
    engine = make_engine(config, mock_ollama, [downloading, downloading, downloading, done, done, done])
    mock_ollama.responder = lambda body: "YES" if "complete" in body["messages"][-1]["content"] else "NO"
    notes, reasons = run_watcher(engine, None, condition="my download finishes")
    assert notes == ["my download finishes"] and reasons == ["met"]
    asked = [b for p, b in mock_ollama.requests if p == "/api/chat"]
    assert len(asked) == 2, "checked once at the start and once after the screen changed, not every frame"


def test_general_watch_speaks_up_about_errors_only(config, mock_ollama):
    frames = [frame(text_lines=[line("Editor", 10, 10)])] * 3 + \
             [frame(text_lines=[line("Editor", 10, 10), line("Saved draft notes", 10, 40)], mark=(0, 0, 300, 300))] * 3 + \
             [frame(text_lines=[line("Editor", 10, 10), line("Build failed: 2 errors", 10, 70)], mark=(0, 0, 900, 700))] * 30
    engine = make_engine(config, mock_ollama, frames)
    notes, _ = run_watcher(engine, None)
    assert notes == ["Saved draft notes", "Build failed: 2 errors"], "news only, once each; the baseline screen is not news"


def test_watch_without_ocr_asks_the_vision_model(config, mock_ollama):
    frames = [frame()] * 2 + [frame(mark=(0, 0, 900, 600))] * 30
    engine = make_engine(config, mock_ollama, frames, ocr_ready=False)
    mock_ollama.responder = lambda body: "Your download has finished, sir." if body["messages"][-1].get("images") else "NONE"
    notes, _ = run_watcher(engine, None)
    assert notes and notes[0] == "Your download has finished, sir."


def test_assistant_watch_start_status_and_stop(eyes, mock_ollama):
    downloading = frame(text_lines=[line("Downloading 45%", 50, 50)])
    done = frame(text_lines=[line("Download complete", 50, 50)], mark=(0, 0, 600, 400))
    assistant, events, desktop, say = eyes(frames=[downloading] * 3 + [done] * 50)
    spoken = []
    assistant._announce = spoken.append
    mock_ollama.responder = lambda body: "YES" if "complete" in body["messages"][-1]["content"] else "NO"
    said = say("tell me when my download finishes")
    assert "tell you when your download finishes" in said
    assert events.of("vision_status")[-1]["watching"]
    events.wait_for(lambda: bool(spoken), timeout=15)
    assert "your download finishes" in spoken[0]
    events.wait_for(lambda: not events.of("vision_status")[-1]["watching"], timeout=5)
    assert "wasn't watching" in say("stop watching")
    say("watch my screen")
    assert "watching for anything important" in say("are you watching").lower()
    assert "stopped watching" in say("stop watching")


def test_the_reader_image_helper(tmp_path):
    url = data_url(frame(size=(2000, 1200)))
    assert url.startswith("data:image/jpeg;base64,") and len(url) < 200_000
