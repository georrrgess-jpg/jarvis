"""Several monitors: which is which, looking at one, moving windows between them, opening apps on one."""

import pytest

from core.monitors import Monitor, describe, parse_monitor_command, place_on, resolve, split_screen_phrase
from core.screen import Shot, Window
from core.vision import parse_look

MAIN = Monitor((0, 0, 2560, 1440), (0, 0, 2560, 1400), True, 1, r"\\.\DISPLAY1")
SIDE = Monitor((-1920, 200, 0, 1280), (-1920, 200, 0, 1240), False, 2, r"\\.\DISPLAY2")
THIRD = Monitor((2560, 0, 4480, 1080), (2560, 0, 4480, 1040), False, 3, r"\\.\DISPLAY3")


@pytest.mark.parametrize("which, expected", [("main", MAIN), ("primary", MAIN), ("other", SIDE), ("second", SIDE), ("secondary", SIDE),
                                             ("left", SIDE), ("right", MAIN), ("2", SIDE), ("1", MAIN), ("first", MAIN), ("third", None)])
def test_two_monitors(which, expected):
    assert resolve(which, [MAIN, SIDE]) is expected


def test_three_monitors_use_windows_numbers_and_positions():
    mons = [MAIN, SIDE, THIRD]
    assert resolve("3", mons) is THIRD and resolve("third", mons) is THIRD and resolve("second", mons) is SIDE
    assert resolve("left", mons) is SIDE and resolve("right", mons) is THIRD and resolve("middle", mons) is MAIN
    assert describe(THIRD, mons) == "your right monitor" and describe(MAIN, mons) == "your main monitor"


def test_one_monitor():
    assert resolve("other", [MAIN]) is None and resolve("main", [MAIN]) is MAIN


def test_descriptions():
    assert describe(SIDE, [MAIN, SIDE]) == "your other monitor (on the left)"


def test_placement_keeps_size_and_relative_spot():
    x, y, w, h = place_on((100, 100, 1100, 800), MAIN, SIDE)
    assert (w, h) == (1000, 700) and SIDE.work[0] <= x and x + w <= SIDE.work[2] and SIDE.work[1] <= y and y + h <= SIDE.work[3]
    x, y, w, h = place_on((0, 0, 2560, 1400), MAIN, SIDE)  # too big: shrunk to fit
    assert (w, h) == (1920, 1040) and (x, y) == (-1920, 200)


@pytest.mark.parametrize("text, app, which", [
    ("move this window to my other monitor", "", "other"), ("move chrome to my main monitor", "chrome", "main"),
    ("put spotify on the second screen", "spotify", "second"), ("move it to monitor 2", "", "2"),
    ("send the window to my left monitor", "", "left"), ("move yourself to my other screen", "yourself", "other"),
])
def test_parse_move(text, app, which):
    cmd = parse_monitor_command(text)
    assert cmd is not None and (cmd.action, cmd.app, cmd.which) == ("move", app, which)


@pytest.mark.parametrize("text", ["how many monitors do I have", "which one is my main monitor", "what screens do I have"])
def test_parse_info(text):
    assert parse_monitor_command(text).action == "info"


@pytest.mark.parametrize("text", ["move the meeting to monday", "put the kettle on", "move on", "what's on my screen"])
def test_not_monitor_commands(text):
    assert parse_monitor_command(text) is None


def test_look_requests_name_the_monitor():
    assert parse_look("what's on my second monitor").monitor == "second"
    assert parse_look("what's on my main screen").monitor == "main"
    assert parse_look("summarise what's on my main monitor").monitor == "main"
    assert parse_look("what's on monitor 2").monitor == "2"
    plain = parse_look("what's on my screen")
    assert plain.monitor == "" and not plain.full_screen


def test_split_screen_phrase():
    assert split_screen_phrase("open spotify on my second monitor") == ("open spotify", "second")
    assert split_screen_phrase("open chrome on monitor 2") == ("open chrome", "2")
    assert split_screen_phrase("open notepad") == ("open notepad", None)


# ----------------------------------------------------------------------------- through the assistant
class TwoScreens:
    def __init__(self):
        self.mons = [MAIN, SIDE]
        self.wins = [Window(10, "Inbox - Gmail - Google Chrome", "chrome.exe", (100, 100, 1100, 800)),
                     Window(11, "Spotify Premium", "Spotify.exe", (200, 200, 1000, 900))]
        self.moved = []
        self.captured = []

    def monitors(self):
        return self.mons

    def windows(self, include_own=False):
        return list(self.wins)

    def foreground(self):
        return self.wins[0]

    def alive(self, w):
        return True

    def move_to_monitor(self, window, monitor):
        self.moved.append((window.hwnd, monitor.number))

    def capture_monitor(self, monitor):
        from PIL import Image

        self.captured.append(monitor.number)
        return Shot(Image.new("RGB", (monitor.width // 4, monitor.height // 4), "white"), monitor.rect[0], monitor.rect[1], 0.25)

    def capture(self, window=None):
        from PIL import Image

        return Shot(Image.new("RGB", (100, 100), "white"), 0, 0, 1.0, window)


@pytest.fixture
def jarvis(config):
    from core.assistant import Assistant
    from tests.test_assistant import Events, FakeTTS

    desk = TwoScreens()
    assistant = Assistant(config, Events(), tts=FakeTTS(), desktop=desk)
    yield assistant, desk
    assistant.shutdown()


def test_move_the_current_window(jarvis):
    assistant, desk = jarvis
    reply = assistant._monitor_command(parse_monitor_command("move this window to my other monitor"))
    assert desk.moved == [(10, 2)] and reply.startswith("Moved Inbox - Gmail - Google Chrome to your other monitor (on the left)")


def test_move_a_named_app(jarvis):
    assistant, desk = jarvis
    assert assistant._monitor_command(parse_monitor_command("put spotify on my main monitor")).startswith("Moved spotify to your main monitor")
    assert desk.moved == [(11, 1)]
    assert "can't see photoshop" in assistant._monitor_command(parse_monitor_command("move photoshop to my other monitor"))


def test_monitor_info(jarvis):
    assistant, desk = jarvis
    reply = assistant._monitor_command(parse_monitor_command("how many monitors do I have"))
    assert reply.startswith("You have 2 monitors") and "2560 by 1440" in reply


def test_one_monitor_is_explained(jarvis):
    assistant, desk = jarvis
    desk.mons = [MAIN]
    assert assistant._monitor_command(parse_monitor_command("move this window to my other monitor")).startswith("You only have one monitor")


def test_look_at_a_particular_monitor(jarvis):
    assistant, desk = jarvis
    from core.assistant import Turn

    turn = assistant._turn = Turn("text")
    reply = "".join(assistant._look(turn, parse_look("what's on my second monitor"), None))
    assert desk.captured == [2], reply
    assert [e for k, e in assistant._emit_raw.items if k == "tool_activity"][-1]["label"] == "Looking at your other monitor (on the left)"


def test_open_on_another_monitor_moves_the_new_window(jarvis):
    assistant, desk = jarvis
    before = {w.hwnd for w in desk.wins}
    desk.wins.append(Window(12, "Untitled - Notepad", "notepad.exe", (300, 300, 900, 700)))
    assistant._open_on_monitor("second", "notepad", before)
    assert desk.moved == [(12, 2)]
