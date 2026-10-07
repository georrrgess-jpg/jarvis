"""End-to-end turns through the real Assistant with a mock Ollama, fake voice and a synthetic microphone."""

import socket
import threading
import time

import numpy as np
import pytest

from core.assistant import Assistant
from core.stt import SpeechInput
from core.tts import TTSError
from tests.conftest import wav_bytes
from tests.mock_ollama import DEFAULT_REPLY, MockOllama


class Events:
    def __init__(self):
        self.items = []
        self.cv = threading.Condition()

    def __call__(self, kind, payload):
        with self.cv:
            self.items.append((kind, payload))
            self.cv.notify_all()

    def wait_for(self, predicate, timeout=15.0):
        deadline = time.monotonic() + timeout
        with self.cv:
            while not predicate():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(f"timed out; events: {[k for k, _ in self.items]}")
                self.cv.wait(remaining)

    def of(self, kind):
        return [p for k, p in self.items if k == kind]

    def states(self):
        return [p["state"] for p in self.of("state")]

    def finished(self):
        """A turn is over once the last state event is IDLE after an assistant_end."""
        return bool(self.of("assistant_end")) and self.states()[-1:] == ["IDLE"]


class FakeTTS:
    def __init__(self, fail=False, clip=0.12):
        self.fail = fail
        self.clip = clip
        self.spoken = []

    def synthesize(self, text, voice=None, **_):
        if self.fail:
            raise TTSError("Cannot reach Microsoft's voice service.")
        self.spoken.append(text)
        return wav_bytes(self.clip)

    def list_voices(self):
        return []

    def close(self):
        pass


class FakeMic:
    """Plays back a scripted signal, then silence, as fast as it is read."""

    rate = 16000
    chunk = 512

    def __init__(self, signal):
        self.signal = signal.astype(np.int16)
        self.pos = 0

    def read(self, n):
        seg = self.signal[self.pos : self.pos + n]
        self.pos += n
        if seg.size < n:
            seg = np.concatenate([seg, np.zeros(n - seg.size, np.int16)])
        time.sleep(0.001)
        return seg.tobytes()

    def close(self):
        pass


def speech_signal():
    rate = 16000
    rng = np.random.default_rng(1)
    quiet = rng.normal(0, 40, int(0.6 * rate))
    t = np.arange(int(0.9 * rate)) / rate
    voice = 6000 * np.sin(2 * np.pi * 220 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 3 * t))
    return np.concatenate([quiet, voice, rng.normal(0, 40, int(1.5 * rate))])


@pytest.fixture
def make(config, mock_ollama):
    made = []

    def factory(tts=None, mic_signal=None, host=None, **settings):
        config.update({"ollama_host": host or mock_ollama.url, "pause_threshold": 0.5, **settings})
        events = Events()
        stt = SpeechInput(config, source_factory=lambda: FakeMic(mic_signal if mic_signal is not None else speech_signal()))
        stt.engine_chain = lambda: ["google"]
        stt._recognize_google = lambda capture, on_status=None: "what is the status"
        assistant = Assistant(config, events, tts=tts or FakeTTS(), stt=stt)
        assistant.start()
        assistant._mic = {"available": True, "device": "fake", "reason": None}
        made.append(assistant)
        return assistant, events

    yield factory
    for a in made:
        a.shutdown()


def test_text_turn_full_cycle(make):
    assistant, events = make()
    tts = assistant.tts
    assert assistant.submit_text("status report")
    events.wait_for(events.finished)

    assert events.states() == ["THINKING", "SPEAKING", "IDLE"]
    user = events.of("user_message")[0]
    assert user["text"] == "status report" and user["source"] == "text"
    reply = "".join(p["text"] for p in events.of("assistant_token"))
    assert reply == DEFAULT_REPLY
    assert len(tts.spoken) == 3 and tts.spoken[0] == "Certainly, sir."
    clips = events.of("speech_clip")
    assert len(clips) == 3 and clips[0]["fps"] == 40 and len(clips[0]["frames"][0]) == 48
    end = events.of("assistant_end")[0]
    assert end["interrupted"] is False and end["error"] is None
    assert end["stats"]["memory_turns"] == 1


def test_voice_turn_with_vad(make):
    assistant, events = make()
    assert assistant.start_listening() == "listening"
    events.wait_for(events.finished)

    phases = [p["phase"] for p in events.of("listen_phase")]
    assert phases[:3] == ["calibrating", "waiting", "capturing"] and "transcribing" in phases
    assert events.of("mic_frame"), "mic levels should stream to the HUD"
    assert events.of("user_message")[0] == {"id": events.of("user_message")[0]["id"], "text": "what is the status", "source": "voice"}
    assert events.states() == ["LISTENING", "THINKING", "SPEAKING", "IDLE"]


def test_silence_times_out_without_a_turn(make):
    rng = np.random.default_rng(3)
    assistant, events = make(mic_signal=rng.normal(0, 30, 16000 * 4), listen_timeout=2)
    assistant.start_listening()
    events.wait_for(lambda: events.states()[-1:] == ["IDLE"])
    assert any("No speech" in n["text"] for n in events.of("notice"))
    assert not events.of("user_message")


def test_interrupt_stops_reply_and_voice(make, config):
    slow = MockOllama(token_delay=0.03, reply="First sentence is right here. " * 12).start()
    try:
        assistant, events = make(host=slow.url, tts=FakeTTS(clip=0.6))
        assistant.submit_text("talk for a while")
        events.wait_for(lambda: "SPEAKING" in events.states())
        assert assistant.interrupt() is True
        events.wait_for(lambda: events.of("assistant_end"))
        time.sleep(0.6)
        assert events.states()[-1] == "IDLE"
        assert events.of("assistant_end")[0]["interrupted"] is True
        clips_after = len(events.of("speech_clip"))
        time.sleep(0.5)
        assert len(events.of("speech_clip")) == clips_after, "no speech after interrupt"
        assert not assistant.audio.voice_busy()
    finally:
        slow.stop()


def test_barge_in_while_speaking(make):
    assistant, events = make(tts=FakeTTS(clip=0.8))
    assistant.submit_text("long answer please")
    events.wait_for(lambda: "SPEAKING" in events.states())
    assistant.start_listening()
    events.wait_for(lambda: events.states()[-1:] == ["LISTENING"] or "LISTENING" in events.states())
    events.wait_for(lambda: len(events.of("user_message")) == 2 and events.finished(), timeout=20)
    assert events.of("user_message")[1]["text"] == "what is the status"


def test_voice_failure_falls_back_to_text(make):
    assistant, events = make(tts=FakeTTS(fail=True))
    assistant.submit_text("hello")
    events.wait_for(events.finished)
    assert "SPEAKING" not in events.states()
    assert events.of("voice_status")[0]["ok"] is False
    assert any("Voice offline" in n["text"] for n in events.of("notice"))
    assert "".join(p["text"] for p in events.of("assistant_token")) == DEFAULT_REPLY


def test_offline_ollama_gives_guidance(make):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assistant, events = make(host=f"http://127.0.0.1:{port}", voice_enabled=False)
    assert assistant.boot_payload()["ollama"]["online"] is False
    assistant.submit_text("are you there?")
    events.wait_for(events.finished)
    reply = "".join(p["text"] for p in events.of("assistant_token"))
    assert "neural core is offline" in reply
    assert events.of("ollama_status")[-1]["online"] is False


def test_pull_model_then_online(make):
    empty = MockOllama(models=[]).start()
    try:
        assistant, events = make(host=empty.url)
        assert assistant.boot_payload()["ollama"]["model"] is None
        assert assistant.pull_model("llama3.2")["ok"]
        events.wait_for(lambda: events.of("pull_done"))
        assert events.of("pull_done")[0]["ok"] is True
        events.wait_for(lambda: any(s["model"] for s in events.of("ollama_status")))
        assert events.of("pull_progress")
        assert assistant.config["model"] == "llama3.2"
        assert assistant.pull_model("bad name; rm -rf")["ok"] is False
    finally:
        empty.stop()


def test_greeting_on_boot(make):
    assistant, events = make()
    assistant.boot_complete()
    events.wait_for(events.finished)
    text = "".join(p["text"] for p in events.of("assistant_token"))
    assert "All systems are online" in text
    assert events.states() == ["SPEAKING", "IDLE"]


def test_settings_update_validates(make):
    assistant, events = make()
    result = assistant.update_settings({"speech_rate": 10, "stt_engine": "google"})
    assert result["speech_rate"] == 10
    assert events.of("settings")[-1]["stt_engine"] == "google"
    with pytest.raises(ValueError):
        assistant.update_settings({"stt_engine": "nope"})
