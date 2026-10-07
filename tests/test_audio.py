import numpy as np
import pytest

from core.audio import VIS_BANDS, AudioEngine, LiveSpectrum, spectrum_frames
from core.sfx import SoundFX, build_library
from tests.conftest import wav_bytes


def test_spectrum_frames_shape_and_range():
    rate = 24000
    t = np.arange(rate) / rate
    tone = 0.5 * np.sin(2 * np.pi * 440 * t).astype(np.float32)
    frames, levels = spectrum_frames(tone, rate, fps=40)
    assert len(frames) == len(levels) == 40
    assert all(len(f) == VIS_BANDS for f in frames)
    flat = np.array(frames)
    assert flat.min() >= 0 and flat.max() <= 100
    # energy should concentrate around the 440 Hz band, not at the top of the spectrum
    mid = np.array(frames[20])
    assert mid.argmax() < VIS_BANDS // 2


def test_spectrum_frames_silence_is_zero():
    frames, levels = spectrum_frames(np.zeros(4800, np.float32), 24000)
    assert max(max(f) for f in frames) == 0 and max(levels) == 0


def test_spectrum_frames_empty():
    assert spectrum_frames(np.array([]), 24000) == ([], [])


def test_live_spectrum_tracks_loudness():
    live = LiveSpectrum(16000)
    quiet_bands, quiet = live.process(np.zeros(512, np.int16))
    t = np.arange(512) / 16000
    loud = (8000 * np.sin(2 * np.pi * 300 * t)).astype(np.int16)
    loud_bands, level = live.process(loud)
    assert quiet == 0 and max(quiet_bands) == 0
    assert level > 40 and max(loud_bands) > 50


def test_sfx_library_is_normalised():
    lib = build_library(22050)
    assert {"activate", "boot", "listen", "listen_end", "process", "interrupt", "error", "click", "notify"} <= set(lib)
    for wave in lib.values():
        assert wave.dtype == np.float32
        assert 0.1 < float(np.max(np.abs(wave))) <= 0.65


@pytest.fixture
def engine():
    eng = AudioEngine()
    assert eng.init(), eng.error
    yield eng
    eng.shutdown()


def test_engine_decodes_and_plays_voice(engine):
    sound = engine.decode(wav_bytes(0.2))
    assert 0.18 < sound.get_length() < 0.25
    samples = engine.mono_samples(sound)
    assert samples.ndim == 1 and np.abs(samples).max() > 0.2
    engine.play_voice(sound)
    engine.stop_voice(0)


def test_sfx_load_and_play_respects_toggle(engine, config):
    fx = SoundFX(engine, config)
    fx.load()
    assert "activate" in fx.names
    fx.play("activate")
    config.update({"sfx_enabled": False})
    fx.play("activate")  # silently ignored
    fx.play("does-not-exist")
