"""Speech recognition failures must never crash a turn ("Internal error") and should let the user try again."""

import socket

import numpy as np
import pytest
import speech_recognition as sr

from core.stt import Capture, SpeechInput, STTError, normalise_level


def speech_capture(seconds=1.0, level=3000):
    t = np.arange(int(16000 * seconds)) / 16000
    return Capture((np.sin(2 * np.pi * 220 * t) * level).astype(np.int16).tobytes(), 16000)


@pytest.fixture
def stt(config):
    config.update({"stt_engine": "google", "auto_language": False})
    return SpeechInput(config)


def test_nothing_understood_is_an_empty_transcript_not_a_crash(stt, monkeypatch):
    # SpeechRecognition 3.11+ raises UnknownValueError for show_all=True when it heard no words
    def unknown(self, audio, language="en-US", show_all=False, **kw):
        raise sr.UnknownValueError()

    monkeypatch.setattr(sr.Recognizer, "recognize_google", unknown)
    assert stt.transcribe(speech_capture()) == ""


@pytest.mark.parametrize("error", [socket.timeout("timed out"), ConnectionResetError(10054, "reset"), ValueError("bad json")])
def test_network_hiccups_become_a_friendly_error(stt, monkeypatch, error):
    def broken(self, audio, language="en-US", show_all=False, **kw):
        raise error

    monkeypatch.setattr(sr.Recognizer, "recognize_google", broken)
    with pytest.raises(STTError, match="unreachable|internet"):
        stt.transcribe(speech_capture())


def test_recognition_has_a_timeout(stt, monkeypatch):
    seen = {}

    def spy(self, audio, language="en-US", show_all=False, **kw):
        seen["timeout"] = self.operation_timeout
        return {"alternative": [{"transcript": "hello there", "confidence": 0.9}]}

    monkeypatch.setattr(sr.Recognizer, "recognize_google", spy)
    assert stt.transcribe(speech_capture()) == "hello there"
    assert seen["timeout"] and seen["timeout"] <= 15


def test_quiet_speech_is_brought_up_before_recognition(stt, monkeypatch):
    levels = []

    def spy(self, audio, language="en-US", show_all=False, **kw):
        levels.append(np.abs(np.frombuffer(audio.get_raw_data(), np.int16)).max())
        return []

    monkeypatch.setattr(sr.Recognizer, "recognize_google", spy)
    stt.transcribe(speech_capture(level=1500))
    assert levels[0] > 10000
    loud = speech_capture(level=25000).pcm
    assert normalise_level(loud) == loud  # already loud: untouched
    assert normalise_level(b"") == b""


def test_a_broken_microphone_is_reported_not_crashed(config):
    def no_mic():
        raise OSError(-9996, "Invalid input device (no default output device)")

    stt = SpeechInput(config, source_factory=no_mic)
    import threading

    with pytest.raises(STTError, match="microphone"):
        stt.listen(threading.Event(), threading.Event())


def test_a_microphone_that_dies_mid_sentence_is_reported(config):
    class Dying:
        rate, chunk = 16000, 512
        reads = 0

        def read(self, n):
            self.reads += 1
            if self.reads > 5:
                raise OSError(-9988, "Stream closed")
            return np.zeros(n, np.int16).tobytes()

        def close(self):
            pass

    import threading

    stt = SpeechInput(config, source_factory=Dying)
    with pytest.raises(STTError, match="stopped responding"):
        stt.listen(threading.Event(), threading.Event())
