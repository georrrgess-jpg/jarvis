"""EdgeTTS wrapper: async loop bridging, caching and error mapping (edge_tts.Communicate is stubbed)."""

import threading

import edge_tts
import pytest

from core.tts import FALLBACK_VOICES, EdgeTTS, TTSError


class FakeCommunicate:
    calls = []
    fail_with = None

    def __init__(self, text, voice, *, rate, pitch, proxy=None, **_):
        FakeCommunicate.calls.append((text, voice, rate, pitch, threading.current_thread().name))

    async def stream(self):
        if FakeCommunicate.fail_with:
            raise FakeCommunicate.fail_with
        yield {"type": "SentenceBoundary", "text": "x"}
        yield {"type": "audio", "data": b"ID3"}
        yield {"type": "audio", "data": b"\xff\xf3mp3-bytes"}


@pytest.fixture
def tts(config, tmp_path, monkeypatch):
    FakeCommunicate.calls = []
    FakeCommunicate.fail_with = None
    monkeypatch.setattr(edge_tts, "Communicate", FakeCommunicate)
    engine = EdgeTTS(config, cache_dir=tmp_path / "cache")
    yield engine
    engine.close()


def test_synthesize_collects_audio_on_background_loop(tts, config):
    config.update({"speech_rate": 10, "speech_pitch": -3})
    assert tts.synthesize("Good evening, sir.") == b"ID3\xff\xf3mp3-bytes"
    text, voice, rate, pitch, thread = FakeCommunicate.calls[0]
    assert (voice, rate, pitch) == ("en-GB-RyanNeural", "+10%", "-3Hz")
    assert thread == "tts-loop"


def test_short_phrases_are_cached(tts):
    tts.synthesize("All systems online.")
    tts.synthesize("All systems online.")
    assert len(FakeCommunicate.calls) == 1
    tts.synthesize("All systems online.", voice="en-GB-ThomasNeural")
    assert len(FakeCommunicate.calls) == 2


def test_long_text_is_not_cached(tts):
    long = "word " * 60
    tts.synthesize(long)
    tts.synthesize(long)
    assert len(FakeCommunicate.calls) == 2


def test_network_errors_become_friendly_tts_errors(tts):
    FakeCommunicate.fail_with = ConnectionResetError("boom")
    with pytest.raises(TTSError, match="internet connection"):
        tts.synthesize("Hello there, sir.")


def test_empty_text_rejected(tts):
    with pytest.raises(TTSError):
        tts.synthesize("   ")


def test_voice_list_falls_back_offline(tts, monkeypatch):
    async def offline(**_):
        raise OSError("no network")

    monkeypatch.setattr(edge_tts, "list_voices", offline)
    voices = tts.list_voices()
    assert [v["id"] for v in voices][:2] == ["en-GB-RyanNeural", "en-GB-ThomasNeural"]
    assert len(voices) == len(FALLBACK_VOICES)


def test_voice_list_from_service_is_english_and_ranked(tts, monkeypatch):
    async def online(**_):
        return [
            {"ShortName": "de-DE-ConradNeural", "Locale": "de-DE", "Gender": "Male"},
            {"ShortName": "en-US-GuyNeural", "Locale": "en-US", "Gender": "Male"},
            {"ShortName": "en-GB-ThomasNeural", "Locale": "en-GB", "Gender": "Male"},
            {"ShortName": "en-GB-RyanNeural", "Locale": "en-GB", "Gender": "Male"},
        ]

    monkeypatch.setattr(edge_tts, "list_voices", online)
    ids = [v["id"] for v in tts.list_voices()]
    assert ids == ["en-GB-RyanNeural", "en-GB-ThomasNeural", "en-US-GuyNeural"]
