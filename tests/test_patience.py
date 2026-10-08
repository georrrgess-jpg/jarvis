"""JARVIS is patient: it waits through hesitations and unfinished sentences instead of answering over you."""

import threading
import time

import numpy as np
import pytest

from core.patience import looks_unfinished
from core.stt import Capture, SpeechInput

RATE = 16000


@pytest.mark.parametrize("text", ["open the", "write an email to John and", "um", "uh", "open", "write a", "hey jarvis", "because",
                                  "send it to", "what is", "tell me about", "email Sarah about", "search for", "I want to",
                                  "open my,", "play some", "Jarvis", "ouvre le", "escribe un correo para"])
def test_trailing_off_is_unfinished(text):
    assert looks_unfinished(text)


@pytest.mark.parametrize("text", ["open spotify", "what time is it", "yes", "okay", "tell me", "email it to me", "turn it on",
                                  "yes I can", "who am I", "what is the weather in London", "write a bio on Lionel Messi", "stop",
                                  "", "thank you", "where is it", "hello jarvis what is up", "play GTA 5", "ouvre spotify"])
def test_complete_requests_are_finished(text):
    assert not looks_unfinished(text)


# ------------------------------------------------------------------ the microphone loop


def tone(seconds, seed=0):
    t = np.arange(int(seconds * RATE)) / RATE
    return 6000 * np.sin(2 * np.pi * 220 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 3 * t))


def quiet(seconds, seed=1):
    return np.random.default_rng(seed).normal(0, 40, int(seconds * RATE))


class Mic:
    rate, chunk = RATE, 512

    def __init__(self, signal, speed=10.0):
        self.signal = np.asarray(signal).astype(np.int16)
        self.pos = 0
        self.delay = self.chunk / RATE / speed

    def read(self, n):
        seg = self.signal[self.pos:self.pos + n]
        self.pos += n
        if seg.size < n:
            seg = np.concatenate([seg, np.zeros(n - seg.size, np.int16)])
        time.sleep(self.delay)
        return seg.tobytes()

    def close(self):
        pass


def listen(config, signal, probe=None, **settings):
    config.update({"pause_threshold": 1.0, "patience": 3.0, "listen_timeout": 5, **settings})
    mic = Mic(signal)
    stt = SpeechInput(config, source_factory=lambda: mic)
    capture = stt.listen(threading.Event(), threading.Event(), probe=probe)
    return capture, mic


def test_a_pause_in_the_middle_of_a_request_does_not_end_it(config):
    """'open the ...' (thinking for a second) '... pitch deck': one capture holding both halves."""
    signal = np.concatenate([quiet(0.6), tone(0.7), quiet(1.4), tone(0.8), quiet(4.5)])

    def probe(pcm, rate):
        seconds = len(pcm) / 2 / rate
        return ("open the", False) if seconds < 2.6 else ("open the pitch deck", True)

    capture, _ = listen(config, signal, probe)
    assert capture.duration > 3.2, "both halves were captured"
    assert capture.text == "open the pitch deck"


def test_without_patience_the_same_pause_splits_the_request(config):
    signal = np.concatenate([quiet(0.6), tone(0.7), quiet(1.4), tone(0.8), quiet(4.5)])
    capture, _ = listen(config, signal, probe=None)
    assert capture.duration < 2.4, "the old behaviour: cut off at the first pause"


def test_a_finished_sentence_is_answered_promptly(config):
    signal = np.concatenate([quiet(0.6), tone(0.9), quiet(6.0)])
    capture, mic = listen(config, signal, lambda pcm, rate: ("what time is it", True))
    assert capture.text == "what time is it"
    assert mic.pos / RATE < 0.6 + 0.9 + 1.0 + 0.9, "it stopped listening about a pause after the sentence ended"


def test_hesitation_is_given_a_limited_amount_of_patience(config):
    signal = np.concatenate([quiet(0.6), tone(0.4), quiet(9.0)])
    capture, mic = listen(config, signal, lambda pcm, rate: ("um", False), patience=2.0)
    waited = mic.pos / RATE - (0.6 + 0.4)
    assert 2.8 < waited < 4.2, f"waited {waited:.1f}s: pause (1s) + patience (2s), then it gives up"
    assert capture.text == "um"


def test_patience_can_be_switched_off_with_zero(config):
    signal = np.concatenate([quiet(0.6), tone(0.4), quiet(6.0)])
    capture, mic = listen(config, signal, lambda pcm, rate: ("open the", False), patience=0.0)
    assert mic.pos / RATE < 0.6 + 0.4 + 1.8, "with patience 0 even an unfinished sentence ends at the pause"


def test_a_failing_probe_never_breaks_listening(config):
    def broken(pcm, rate):
        raise RuntimeError("offline")

    signal = np.concatenate([quiet(0.6), tone(0.8), quiet(5.0)])
    capture, _ = listen(config, signal, broken)
    assert capture is not None and capture.text is None, "falls back to normal recognition"


def test_long_dictation_is_not_cut_at_the_old_25_second_limit(config):
    assert config["max_phrase_seconds"] >= 45 and config["listen_timeout"] >= 10 and config["pause_threshold"] >= 1.0


# ------------------------------------------------------------------ the assistant


def test_assistant_waits_for_the_rest_of_the_request(config, mock_ollama):
    from core.assistant import Assistant
    from tests.test_assistant import Events, FakeTTS

    config.update({"ollama_host": mock_ollama.url, "pause_threshold": 1.0, "patience": 3.0, "voice_enabled": False})
    signal = np.concatenate([quiet(0.6), tone(0.7), quiet(1.4), tone(0.8), quiet(5.0)])
    events = Events()
    stt = SpeechInput(config, source_factory=lambda: Mic(signal))
    stt.engine_chain = lambda: ["google"]
    heard = []

    def recognise(capture: Capture, on_status=None):
        heard.append(capture.duration)
        return "what is" if capture.duration < 2.6 else "what is the status"

    stt._recognize_google = recognise
    assistant = Assistant(config, events, tts=FakeTTS(), stt=stt)
    assistant.start()
    assistant._mic = {"available": True, "device": "fake", "reason": None}
    try:
        assert assistant.start_listening() == "listening"
        events.wait_for(events.finished, timeout=30)
        messages = events.of("user_message")
        assert [m["text"] for m in messages] == ["what is the status"], "one request, heard whole"
        assert any(p.get("phase") == "capturing" for p in events.of("listen_phase"))
    finally:
        assistant.shutdown()


def test_the_ui_is_told_when_jarvis_is_waiting_politely(config):
    phases = []
    config.update({"pause_threshold": 1.0, "patience": 3.0, "listen_timeout": 5})
    signal = np.concatenate([quiet(0.6), tone(0.7), quiet(1.4), tone(0.8), quiet(4.5)])
    stt = SpeechInput(config, source_factory=lambda: Mic(signal))
    stt.listen(threading.Event(), threading.Event(), on_phase=phases.append,
               probe=lambda pcm, rate: ("open the", False) if len(pcm) / 2 / rate < 2.6 else ("open the pitch deck", True))
    assert phases == ["calibrating", "waiting", "capturing", "patient", "capturing"]
