"""Instant skills: answered locally in milliseconds, with no language model involved."""

import time

import pytest

from core import osctl
from core.quick import calculate, describe_duration, parse_duration, parse_quick


@pytest.mark.parametrize("text, kind", [
    ("hello", "greeting"), ("good evening", "greeting"), ("Hey Jarvis", "greeting"), ("thanks", "talk"), ("thank you so much", "talk"),
    ("how are you doing today jarvis", "talk"), ("who are you", "talk"), ("what can you do", "talk"), ("tell me a joke", "talk"),
    ("what is 15 percent of 240", "math"), ("what's twelve times seven", "math"), ("battery", "status"), ("how's my computer doing", "status"),
    ("how much disk space is left", "status"), ("volume up", "volume"), ("set the volume to 40", "volume"), ("unmute", "volume"),
    ("set a timer for 5 minutes", "timer"), ("remind me in 10 minutes to call mom", "timer"), ("cancel the timer", "timer_cancel"),
    ("how long is left on my timer", "timer_status"), ("take a screenshot", "screenshot"), ("show desktop", "desktop"),
    ("lock the computer", "lock"), ("flip a coin", "coin"), ("roll a d20", "dice"), ("pick a number between 1 and 10", "random"),
])
def test_recognised(text, kind):
    assert parse_quick(text).kind == kind


@pytest.mark.parametrize("text", ["open spotify", "what is the capital of France", "who won the world cup", "write a bio on Lionel Messi",
                                  "what is 5", "1999", "play GTA 5", "tell me about the sun", "set volume to 150", "remind me to buy milk",
                                  "hello how do I bake a cake", "what is the weather in London", "email Sarah saying hi"])
def test_everything_else_goes_to_the_model(text):
    assert parse_quick(text) is None


@pytest.mark.parametrize("text, answer", [
    ("what is 15 percent of 240", "36"), ("what's 12 times 7", "84"), ("calculate 2 plus 2", "4"), ("what is twenty five times four", "100"),
    ("how much is 100 divided by 8", "12.5"), ("what is the square root of 144", "12"), ("10% of 50", "5"), ("what is 7 times 8 plus 2", "58"),
    ("2 to the power of 10", "1024"), ("what is 10 minus 3.5", "6.5"), ("what is two hundred and fifty plus fifty", "300"),
    ("what is 9 squared", "81"), ("what is 20 percent off 80", "64"), ("what is half of 90", "45"),
])
def test_arithmetic(text, answer):
    assert calculate(text) == answer


@pytest.mark.parametrize("text", ["what is 1 divided by 0", "what is 2 to the power of 99999", "import os", "__import__('os')", "what is 3 3"])
def test_arithmetic_never_executes_or_crashes(text):
    assert calculate(text) is None


def test_durations():
    assert parse_duration("5 minutes") == 300 and parse_duration("half an hour") == 1800 and parse_duration("an hour and a half") == 5400
    assert parse_duration("2 hours 30 minutes") == 9000 and parse_duration("ninety") is None and parse_duration("thirty seconds") == 30
    assert describe_duration(90) == "1 minute and 30 seconds" and describe_duration(3600) == "1 hour" and describe_duration(45) == "45 seconds"


# ------------------------------------------------------------------ OS controls (nothing touches the real machine)


class Keys:
    def __init__(self):
        self.events = []

    def keybd_event(self, key, scan, flags, extra):
        self.events.append((key, flags))

    def LockWorkStation(self):
        self.events.append("lock")


def test_windows_volume_uses_the_media_keys():
    keys = Keys()
    osctl.change_volume(10, platform="win32", user32=keys)
    assert [k for k, f in keys.events if f == 0] == [osctl.VK_VOLUME_UP] * 5
    keys.events.clear()
    osctl.set_volume(40, platform="win32", user32=keys)
    downs = [1 for k, f in keys.events if f == 0 and k == osctl.VK_VOLUME_DOWN]
    ups = [1 for k, f in keys.events if f == 0 and k == osctl.VK_VOLUME_UP]
    assert (len(downs), len(ups)) == (50, 20), "all the way down, then up to 40%"
    keys.events.clear()
    osctl.toggle_mute(platform="win32", user32=keys)
    osctl.show_desktop(platform="win32", user32=keys)
    osctl.lock_screen(platform="win32", user32=keys)
    assert keys.events[0] == (osctl.VK_VOLUME_MUTE, 0) and (0x5B, 0) in keys.events and keys.events[-1] == "lock"


def test_linux_volume_uses_pulseaudio():
    calls = []
    osctl.change_volume(-10, platform="linux", runner=lambda cmd, **kw: calls.append(cmd))
    osctl.set_volume(35, platform="linux", runner=lambda cmd, **kw: calls.append(cmd))
    assert calls == [["pactl", "set-sink-volume", "@DEFAULT_SINK@", "-10%"], ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "35%"]]


def test_screenshot_is_saved_as_a_png(tmp_path):
    class Image:
        def save(self, path):
            path.write_bytes(b"png")

    path = osctl.take_screenshot(tmp_path, grabber=Image)
    assert path.parent == tmp_path and path.suffix == ".png" and path.read_bytes() == b"png"
    with pytest.raises(osctl.OsControlError):
        osctl.take_screenshot(tmp_path, grabber=lambda: 1 / 0)


# ------------------------------------------------------------------ in the assistant


@pytest.fixture
def jarvis(config, mock_ollama, monkeypatch):
    from core.assistant import Assistant
    from tests.test_assistant import Events, FakeTTS

    config.update({"ollama_host": mock_ollama.url, "voice_enabled": False})
    events, calls = Events(), []
    monkeypatch.setattr(osctl, "change_volume", lambda delta: calls.append(("volume", delta)))
    monkeypatch.setattr(osctl, "set_volume", lambda n: calls.append(("set", n)))
    monkeypatch.setattr(osctl, "toggle_mute", lambda: calls.append(("mute",)))
    monkeypatch.setattr(osctl, "take_screenshot", lambda: "C:/Pictures/Screenshots/JARVIS.png")
    assistant = Assistant(config, events, tts=FakeTTS())
    assistant.start()

    def say(text):
        n = len(events.of("assistant_end"))
        started = time.monotonic()
        assistant.submit_text(text)
        events.wait_for(lambda: len(events.of("assistant_end")) == n + 1 and events.states()[-1] == "IDLE", timeout=10)
        return "".join(t["text"] for t in events.of("assistant_token")[-40:]), time.monotonic() - started

    yield assistant, events, say, mock_ollama, calls
    assistant.shutdown()


def model_calls(mock):
    return [1 for p, _ in mock.requests if p == "/api/chat"]


def test_small_talk_and_sums_never_reach_the_model(jarvis):
    assistant, events, say, mock, calls = jarvis
    for text in ("hello", "thanks", "how are you", "what is 15 percent of 240", "flip a coin", "tell me a joke", "who are you"):
        said, took = say(text)
        assert said.strip() and took < 2.0, (text, said, took)
    assert not model_calls(mock)
    assert "36" in say("what is 15 percent of 240")[0]


def test_it_works_even_when_ollama_is_off(config):
    from core.assistant import Assistant
    from tests.test_assistant import Events, FakeTTS

    config.update({"ollama_host": "http://127.0.0.1:9", "voice_enabled": False})
    events = Events()
    assistant = Assistant(config, events, tts=FakeTTS())
    assistant.start()
    try:
        assistant.submit_text("what is 6 times 7")
        events.wait_for(events.finished)
        assert "42" in "".join(t["text"] for t in events.of("assistant_token"))
    finally:
        assistant.shutdown()


def test_system_status_comes_from_the_monitor(jarvis):
    assistant, events, say, mock, calls = jarvis
    assistant.monitor.snapshot = lambda: {"cpu": 14.2, "ram": 54.1, "ram_used_gb": 17.4, "ram_total_gb": 32.0, "disk": 61.5,
                                          "battery": {"percent": 87, "plugged": True}, "uptime_s": 273600}
    assert "87 percent and charging" in say("what's my battery")[0]
    assert "14 percent" in say("cpu usage")[0]
    assert "17.4 of 32.0 gigabytes" in say("how much ram")[0]
    assert "62 percent full" in say("how much disk space is left")[0]
    said = say("how's my computer doing")[0]
    assert "processor at 14 percent" in said and "battery at 87 percent" in said
    assert "3 days and 4 hours" in say("how long has my computer been on")[0]
    assert not model_calls(mock)


def test_volume_commands(jarvis):
    assistant, events, say, mock, calls = jarvis
    say("volume up")
    say("turn it down")
    say("set the volume to 40")
    say("mute")
    assert calls == [("volume", 10), ("volume", -10), ("set", 40), ("mute",)]
    assert not model_calls(mock)


def test_timers_fire_speak_and_can_be_cancelled(jarvis, monkeypatch):
    assistant, events, say, mock, calls = jarvis
    import core.assistant as module

    monkeypatch.setattr(module, "describe_duration", lambda s: "a moment")
    spoken = []
    monkeypatch.setattr(assistant, "speak", lambda text, voice=None, delay=0.0: spoken.append(text))
    assert "Timer set for a moment" in say("set a timer for 5 minutes")[0]
    assert len(assistant._timers) == 1 and "left" in say("how long is left on my timer")[0]
    assert "Cancelled your timer" in say("cancel the timer")[0] and not assistant._timers
    # a short one that really fires
    said, _ = say("remind me in 5 seconds to stretch")
    assert "remind you to stretch" in said
    entry = next(iter(assistant._timers.values()))
    entry["timer"].cancel()
    assistant._timer_fired(next(iter(assistant._timers)))
    assert spoken and "stretch" in spoken[0]
    assert events.of("timers"), "the UI is told about running timers"


def test_screenshot_and_dice(jarvis):
    assistant, events, say, mock, calls = jarvis
    assert "Screenshots folder" in say("take a screenshot")[0]
    assert "rolled a" in say("roll a die")[0] and any("Screenshot saved" in n["text"] for n in events.of("notice"))


def test_the_clock_and_open_commands_still_win(jarvis):
    assistant, events, say, mock, calls = jarvis
    assert say("what time is it")[0].startswith("It's ")
    assert not model_calls(mock)


def test_common_phrases_are_prewarmed_in_the_voice_cache(config, mock_ollama):
    from core.assistant import Assistant
    from tests.test_assistant import Events, FakeTTS

    config.update({"ollama_host": mock_ollama.url, "voice_enabled": True, "user_title": "sir"})
    tts = FakeTTS()
    assistant = Assistant(config, Events(), tts=tts)
    phrases = assistant._prewarm_phrases()
    assert "Done, sir." in phrases and any("You're most welcome" in p for p in phrases) and len(set(phrases)) == len(phrases)
