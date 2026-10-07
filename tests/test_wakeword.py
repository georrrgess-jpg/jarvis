"""Wake word: real openWakeWord models on synthetic speech, the shared-microphone listener and assistant flows."""

import threading
import time
import wave
from pathlib import Path

import numpy as np
import pytest

from core.wakeword import CHUNK, RATE, WakeListener, WakeWordDetector, available

DATA = Path(__file__).parent / "data"


def load(name: str) -> np.ndarray:
    with wave.open(str(DATA / name)) as w:
        assert w.getframerate() == RATE
        return np.frombuffer(w.readframes(w.getnframes()), np.int16)


def quiet(seconds: float, seed: int = 1) -> np.ndarray:
    return np.random.default_rng(seed).normal(0, 30, int(RATE * seconds)).astype(np.int16)


def peak_score(detector: WakeWordDetector, audio: np.ndarray) -> float:
    detector.reset()
    audio = np.concatenate([quiet(1.0), audio, quiet(1.0)])
    audio = audio[: len(audio) // CHUNK * CHUNK]
    return max(detector.process(audio[i : i + CHUNK]) for i in range(0, len(audio), CHUNK))


@pytest.fixture(scope="module")
def detector():
    ok, why = available()
    if not ok:
        pytest.skip(why)
    return WakeWordDetector()


def test_models_are_bundled():
    assert available() == (True, "")


def test_detects_hey_jarvis(detector):
    assert peak_score(detector, load("hey_jarvis_what_time.wav")) > 0.8


def test_ignores_similar_phrase(detector):
    assert peak_score(detector, load("hey_travis.wav")) < 0.2


def test_ignores_silence_and_noise(detector):
    detector.reset()
    noise = np.random.default_rng(5).normal(0, 2000, RATE * 3).astype(np.int16)
    assert max(detector.process(noise[i : i + CHUNK]) for i in range(0, len(noise) - CHUNK, CHUNK)) < 0.3


class ScriptedStream:
    """Plays audio in real-ish time, then silence, like a microphone."""

    def __init__(self, audio: np.ndarray, speed: float = 8.0):
        self.audio = audio
        self.pos = 0
        self.delay = CHUNK / RATE / speed

    def read(self, n):
        seg = self.audio[self.pos : self.pos + n]
        self.pos += n
        if seg.size < n:
            seg = np.concatenate([seg, quiet(n / RATE, seed=self.pos)[: n - seg.size]])
        time.sleep(self.delay)
        return seg.astype(np.int16).tobytes()

    def close(self):
        pass


def test_listener_fires_once_and_lends_the_stream(detector):
    audio = np.concatenate([quiet(1.0), load("hey_jarvis_what_time.wav"), quiet(4.0)])
    wakes = []
    fired = threading.Event()

    def on_wake(score):
        wakes.append(score)
        fired.set()

    listener = WakeListener(on_wake, lambda: 0.5, stream_factory=lambda: ScriptedStream(audio),
                            detector_factory=lambda: detector)
    assert listener.start()
    try:
        assert fired.wait(15), "wake word not detected"
        src = listener.borrow()
        data = src.read(2048)  # the command recogniser reads the same live stream
        assert len(data) == 4096
        src.close()
        time.sleep(1.0)
        assert len(wakes) == 1, "cooldown/reset must prevent double triggers"
    finally:
        listener.stop()
    assert not listener.running


def test_listener_reports_microphone_failure():
    def broken():
        raise OSError("no input device")

    listener = WakeListener(lambda s: None, lambda: 0.5, stream_factory=broken, detector_factory=lambda: object())
    assert listener.start() is False
    assert "no input device" in listener.error


# ------------------------------------------------------------------ assistant flows


class FakeWake:
    """Stands in for WakeListener: lends a scripted command stream when 'fired'."""

    def __init__(self, command: np.ndarray):
        self.command = command
        self.running = True
        self.error = None

    def borrow(self):
        from tests.test_assistant import FakeMic

        return FakeMic(self.command)

    def stop(self):
        self.running = False


def speech(seconds=0.9):
    t = np.arange(int(seconds * RATE)) / RATE
    return np.concatenate([quiet(0.5), 6000 * np.sin(2 * np.pi * 220 * t), quiet(1.5)])


@pytest.fixture
def wired(config, mock_ollama):
    from core.assistant import Assistant
    from core.stt import SpeechInput
    from tests.test_assistant import Events, FakeTTS

    config.update({"ollama_host": mock_ollama.url, "pause_threshold": 0.5, "voice_enabled": True})
    events = Events()
    stt = SpeechInput(config)
    stt.engine_chain = lambda: ["google"]
    heard = {"text": "what time is it"}
    stt._recognize_google = lambda capture, on_status=None: heard["text"]
    assistant = Assistant(config, events, tts=FakeTTS(clip=0.6), stt=stt)
    assistant.start()
    assistant._mic = {"available": True, "device": "fake", "reason": None}
    assistant.wake = FakeWake(speech())
    stt.set_source_provider(assistant.wake.borrow)
    yield assistant, events, heard, mock_ollama
    assistant.shutdown()


def test_wake_then_command(wired):
    assistant, events, heard, mock = wired
    heard["text"] = "Hey Jarvis, what time is it?"
    assistant._on_wake(0.97)
    events.wait_for(events.finished, timeout=20)
    assert events.of("wake")[0]["score"] == 0.97
    assert events.of("user_message")[0]["text"] == "what time is it?", "the wake phrase is stripped"
    assert events.states()[:3] == ["LISTENING", "THINKING", "SPEAKING"]


def test_wake_interrupts_speech_and_stop_phrase_dismisses(wired):
    assistant, events, heard, mock = wired
    assistant.submit_text("tell me a long story")
    events.wait_for(lambda: "SPEAKING" in events.states())
    heard["text"] = "stop"
    chats_before = len([r for r in mock.requests if r[0] == "/api/chat"])
    assistant._on_wake(0.9)
    events.wait_for(lambda: any(n["text"] == "Standing by." for n in events.of("notice")), timeout=20)
    time.sleep(0.4)
    assert events.states()[-1] == "IDLE"
    assert "LISTENING" in events.states()
    assert len([r for r in mock.requests if r[0] == "/api/chat"]) == chats_before, "'stop' never reaches the model"
    assert not assistant.audio.voice_busy()


def test_wake_status_reported_in_boot_payload(wired):
    assistant, *_ = wired
    status = assistant.boot_payload()["wake"]
    assert status["phrase"] == "Hey Jarvis" and status["enabled"] is True
