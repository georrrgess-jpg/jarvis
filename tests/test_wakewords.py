"""Learned wake words ("Hey Harper"): the training pipeline, the live detector and the background manager.

The real thing learns from Microsoft's neural voices (checked on Windows CI by tests/windows_wake_check.py).
Here a stand-in 'voice' turns every word into its own little melody, which is enough to prove the
whole pipeline end to end: synthesise -> features -> train -> calibrate -> save -> load -> wake up.
"""

import hashlib
import io
import threading
import time
import wave

import numpy as np
import pytest

from core import wakelearn
from core.wakelearn import (RATE, TinyNet, Trainer, WakeModel, english_voices, end_frame, load_model, model_path, phrases_for,
                            positive_windows, resample, sound_alikes, speech_bounds, streaming_hits)
from core.wakeword import CHUNK, WakeListener, WakeWordDetector, available, learned_threshold
from core.wakewords import WakeWords
from tests.test_wakeword import ScriptedStream, quiet

VOICES = [f"en-XX-Voice{i}Neural" for i in range(24)]
NEGATIVES = ["what's the weather like", "open spotify please", "hey jarvis what time is it", "set a timer for ten minutes",
             "the meeting moved to thursday", "hey there how are you", "I bought some bread and milk", "harbour lights at night",
             "play some music", "good morning everyone", "remind me to call mum", "hello hello", "happy birthday",
             "turn the volume up", "hey sarah wait for me", "paper and pens"] * 2


def melody(text: str, voice: str = "en-XX-Voice0Neural", rate: int = 0, pitch: int = 0) -> np.ndarray:
    """A deterministic stand-in for speech: each word is three tones; each 'voice' shifts them a little."""
    v = (int(hashlib.md5(voice.encode()).hexdigest(), 16) % 1000) / 1000.0
    shift = (0.92 + 0.16 * v) * (1 + pitch / 200)
    word_s = 0.24 / (1 + rate / 100)
    out = [np.zeros(int(0.05 * RATE), np.float32)]
    t = np.arange(int(word_s * RATE)) / RATE
    env = np.sin(np.pi * t / word_s) ** 0.6
    for word in "".join(c if c.isalnum() or c == " " else " " for c in text.lower()).split():
        h = int(hashlib.md5(word.encode()).hexdigest(), 16)
        freqs = [(250 + (h >> (8 * k)) % 2600) * shift for k in range(3)]
        tone = sum(np.sin(2 * np.pi * f * t + k) for k, f in enumerate(freqs)) / 3
        out += [(tone * env * 9000).astype(np.float32), np.zeros(int(0.04 * RATE), np.float32)]
    return np.concatenate(out)


def fake_synth(items):
    return [melody(*item) for item in items]


@pytest.fixture(scope="module")
def models_ok():
    ok, why = available()
    if not ok:
        pytest.skip(why)


@pytest.fixture(scope="module")
def harper(models_ok, tmp_path_factory):
    trainer = Trainer(synth=fake_synth, voices=VOICES, cache_dir=tmp_path_factory.mktemp("wake"), takes_per_voice=2,
                      augment_copies=1, negative_texts=NEGATIVES)
    return trainer.train("Harper"), trainer


# ----------------------------------------------------------------------------- pieces
def test_phrases_and_sound_alikes():
    assert phrases_for("Harper")[:2] == ["Hey Harper", "Harper"]
    alikes = sound_alikes("Harper")
    assert alikes and all("harper" != a for a in alikes) and any(a.startswith("hey ") for a in alikes)


def test_resample_keeps_duration_and_pitch():
    t = np.arange(24000) / 24000
    tone = np.sin(2 * np.pi * 440 * t).astype(np.float32)
    out = resample(tone, 24000, 16000)
    assert out.size == 16000
    peak = np.argmax(np.abs(np.fft.rfft(out))) * 16000 / out.size
    assert abs(peak - 440) < 2


def test_speech_bounds_finds_the_spoken_part():
    clip = np.concatenate([np.zeros(8000), melody("hey harper"), np.zeros(8000)]).astype(np.float32)
    start, end = speech_bounds(clip)
    assert 7000 < start < 9500 and abs(end - (8000 + melody("hey harper").size)) < 1500


def test_positive_windows_line_up_with_the_end_of_the_phrase():
    feats = np.arange(40 * 96, dtype=np.float32).reshape(40, 96)
    end = (8 * 25 + 76) * 160  # the audio that embedding 25 finishes on
    assert end_frame(end) == 25
    pos, neg = positive_windows(feats, end)
    assert len(pos) == 4 and np.array_equal(pos[0][-1], feats[25])
    assert len(neg) == 1 and np.array_equal(neg[-1][-1], feats[15])  # windows well before the name are negatives


def test_tiny_net_learns_a_simple_rule():
    rng = np.random.default_rng(0)
    pos = rng.normal(0.8, 1, (300, 16, 96)).astype(np.float32)
    neg = rng.normal(-0.8, 1, (900, 16, 96)).astype(np.float32)
    net = TinyNet(hidden=16)
    net.fit(pos, neg, epochs=5)
    assert net.predict(pos).mean() > 0.9 and net.predict(neg).mean() < 0.1


def test_learned_threshold_follows_sensitivity():
    assert learned_threshold(0.9, 0.5) == pytest.approx(0.9)
    assert learned_threshold(0.9, 1.0) < 0.5 and learned_threshold(0.9, 0.0) > 0.99
    assert learned_threshold(0.6, 0.75) < 0.6


def test_english_voices():
    voices = english_voices([{"ShortName": "en-GB-RyanNeural", "Locale": "en-GB"}, {"ShortName": "fr-FR-DeniseNeural", "Locale": "fr-FR"},
                             {"ShortName": "fr-FR-VivienneMultilingualNeural", "Locale": "fr-FR"}])
    assert voices == ["en-GB-RyanNeural", "fr-FR-VivienneMultilingualNeural"]


# ----------------------------------------------------------------------------- training end to end
def test_training_learns_the_name(harper):
    model, _ = harper
    m = model.metrics
    assert model.name == "Harper" and model.phrase == "Hey Harper"
    assert 0.3 <= model.threshold <= 0.99
    assert m["false_wakes"] == 0 and m["held_out_recall"] >= 0.8, m
    assert m["voices"] == len(VOICES) and m["held_out_voices"] >= 2


def test_it_wakes_for_the_name_and_not_for_other_speech(harper):
    model, trainer = harper
    rng = np.random.default_rng(5)
    features = trainer.features

    def fires(text, voice):
        clip, _ = wakelearn.place(melody(text, voice), rng)
        return streaming_hits(model.net, features(clip), model.threshold)

    new_voices = [f"en-YY-Other{i}Neural" for i in range(6)]  # never heard during training
    caught = sum(1 for v in new_voices for p in ("Hey Harper", "Harper") if fires(p, v))
    assert caught >= 10, f"caught only {caught} of 12"
    false = sum(fires(s, v) for v in new_voices for s in ("hey jarvis", "open the window", "hey peter", "good night"))
    assert false <= 1


def test_model_save_and_load(harper, tmp_path):
    model, _ = harper
    model.save(model_path(tmp_path, "Harper"))
    back = load_model(tmp_path, "harper")
    assert back is not None and back.threshold == model.threshold and back.metrics == model.metrics
    w = np.random.default_rng(1).normal(size=(16, 96)).astype(np.float32)
    assert back.score(w) == pytest.approx(model.score(w), abs=1e-6)
    assert load_model(tmp_path, "Nobody") is None


def test_training_reuses_cached_samples(models_ok, tmp_path):
    calls = []

    def counting(items):
        calls.append(len(items))
        return fake_synth(items)

    t = Trainer(synth=counting, voices=VOICES[:10], cache_dir=tmp_path, takes_per_voice=1, augment_copies=0, negative_texts=NEGATIVES[:12])
    t.train("Nova")
    first = sum(calls)
    t.train("Nova", user_samples=[melody("hey nova", "en-ME-MyVoiceNeural")] * 3)
    assert sum(calls) == first  # second run: negatives and samples both came from the cache
    assert (tmp_path / "negatives.npz").is_file() and (tmp_path / "nova-samples.npz").is_file()


# ----------------------------------------------------------------------------- the live detector
def test_listener_wakes_on_a_learned_name(harper):
    model, _ = harper
    detector = WakeWordDetector()
    audio = np.concatenate([quiet(2.0), melody("hey harper", "en-ZZ-FreshNeural").astype(np.int16), quiet(3.0)])
    fired = threading.Event()
    listener = WakeListener(lambda score: fired.set(), lambda: 0.5, stream_factory=lambda: ScriptedStream(audio),
                            detector_factory=lambda: detector, words=lambda: (False, [model]))
    assert listener.start()
    try:
        assert fired.wait(20), "learned wake word not detected"
        assert listener.last_word == "Harper"
    finally:
        listener.stop()


def test_listener_ignores_other_speech_with_a_learned_name(harper):
    model, _ = harper
    detector = WakeWordDetector()
    speech = np.concatenate([melody(s, "en-ZZ-FreshNeural") for s in ("open spotify please", "hey there", "good morning everyone")])
    audio = np.concatenate([quiet(2.0), speech.astype(np.int16), quiet(2.0)])
    fired = threading.Event()
    listener = WakeListener(lambda score: fired.set(), lambda: 0.5, stream_factory=lambda: ScriptedStream(audio),
                            detector_factory=lambda: detector, words=lambda: (False, [model]))
    assert listener.start()
    try:
        assert not fired.wait(audio.size / RATE / 8 + 2)
    finally:
        listener.stop()


# ----------------------------------------------------------------------------- the manager (background learning, recording)
class FakeVoiceService:
    """Edge stand-in: lists voices and 'speaks' melodies as 24 kHz WAV."""

    def __init__(self, voices=VOICES):
        self.voices = voices

    def list_voices(self):
        return [{"id": v, "label": v, "locale": "en-GB", "gender": "Female"} for v in self.voices]

    def synthesize_many(self, items, concurrency=8, timeout=120.0):
        out = []
        for text, voice, rate, pitch in items:
            pcm = resample(melody(text, voice, rate, pitch), RATE, 24000)
            buf = io.BytesIO()
            with wave.open(buf, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(24000)
                w.writeframes(np.clip(pcm, -32000, 32000).astype(np.int16).tobytes())
            out.append(buf.getvalue())
        return out


@pytest.fixture
def audio_engine():
    from core.audio import AudioEngine

    engine = AudioEngine()
    if not engine.init():
        pytest.skip("no audio mixer")
    yield engine
    engine.shutdown()


def small_trainer(**kw):
    return Trainer(**{**kw, "takes_per_voice": 1, "augment_copies": 0, "negative_texts": NEGATIVES[:12]})


def test_manager_learns_in_the_background(models_ok, tmp_path, audio_engine):
    events = []
    ww = WakeWords(tmp_path, FakeVoiceService(), audio_engine, lambda kind, **p: events.append((kind, p)))
    ww.trainer_factory = small_trainer
    ready = threading.Event()
    ww.on_ready = lambda name: ready.set()
    assert ww.status("Harper")["state"] == "missing"
    assert ww.learn("Harper") and not ww.learn("harper")  # already queued
    assert ready.wait(120), [p.get("error") for _, p in events]
    status = ww.status("Harper")
    assert status["state"] == "ready" and status["phrase"] == "Hey Harper" and status["metrics"]["voices"] == len(VOICES)
    states = [p["state"] for k, p in events if k == "wake_learn"]
    assert "learning" in states or "queued" in states
    assert ww.model("Harper") is not None and model_path(tmp_path, "Harper").is_file()
    deadline = time.time() + 5
    while ww.busy() and time.time() < deadline:
        time.sleep(0.05)
    assert not ww.busy()
    ww.delete("Harper")
    assert ww.model("Harper") is None and ww.status("Harper")["state"] == "missing"


def test_manager_needs_the_voice_service(models_ok, tmp_path, audio_engine):
    ww = WakeWords(tmp_path, FakeVoiceService(voices=[]), audio_engine, lambda kind, **p: None)
    done = threading.Event()
    ww._send = lambda name, throttle=False: done.set() if ww.status(name)["state"] == "error" else None
    ww.learn("Harper")
    assert done.wait(10)
    assert "internet" in ww.status("Harper")["error"]


class FakeMic:
    def __init__(self, audio):
        self.audio = np.concatenate([audio, np.zeros(RATE * 3)]).astype(np.int16)
        self.pos = 0

    def read(self, n):
        seg = self.audio[self.pos: self.pos + n]
        self.pos += n
        return np.pad(seg, (0, n - seg.size)).tobytes()


def test_recording_my_voice(models_ok, tmp_path, audio_engine):
    ww = WakeWords(tmp_path, FakeVoiceService(), audio_engine, lambda kind, **p: None)
    said = np.concatenate([np.zeros(RATE // 2), melody("hey harper") * 0.6])
    for i in range(3):
        r = ww.record("Harper", FakeMic(said))
        assert r["ok"] and r["count"] == i + 1
    assert not ww.record("Harper", FakeMic(np.zeros(RATE)))["ok"]  # silence
    again = WakeWords(tmp_path, FakeVoiceService(), audio_engine, lambda kind, **p: None)
    assert again.recording_count("Harper") == 3  # kept on disk
    again.clear_recordings("Harper")
    assert again.recording_count("Harper") == 0


def test_saved_model_works_in_the_real_detector(harper, tmp_path):
    model, _ = harper
    model.save(model_path(tmp_path, "Harper"))
    detector = WakeWordDetector()
    detector.learned = {"Harper": WakeModel.load(model_path(tmp_path, "Harper"))}
    audio = np.concatenate([quiet(2.0), melody("hey harper", "en-QQ-NewNeural").astype(np.int16), quiet(1.0)])
    audio = audio[: audio.size // CHUNK * CHUNK]
    best = 0.0
    for i in range(0, audio.size, CHUNK):
        scores = detector.process_all(audio[i: i + CHUNK], jarvis=False)
        assert set(scores) == {"Harper"}
        best = max(best, scores["Harper"])
    assert best >= model.threshold
