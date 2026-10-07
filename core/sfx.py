"""Interface sound effects, synthesised at startup so no audio assets need to ship with the exe."""

from __future__ import annotations

import logging

import numpy as np

log = logging.getLogger("jarvis.sfx")


def _t(duration: float, rate: int) -> np.ndarray:
    return np.arange(int(duration * rate), dtype=np.float32) / rate


def _env(n: int, rate: int, attack: float = 0.004, decay: float = 0.25) -> np.ndarray:
    t = np.arange(n, dtype=np.float32) / rate
    a = np.clip(t / max(attack, 1e-4), 0.0, 1.0)
    return a * np.exp(-t / max(decay, 1e-4))


def _sweep(f0: float, f1: float, duration: float, rate: int, harmonics=((1, 1.0),), curve: float = 1.0) -> np.ndarray:
    t = _t(duration, rate)
    if t.size == 0:
        return t
    p = (t / t[-1]) ** curve if t[-1] > 0 else t
    freq = f0 * (f1 / f0) ** p  # exponential glide sounds natural
    phase = 2 * np.pi * np.cumsum(freq) / rate
    return sum(amp * np.sin(phase * k) for k, amp in harmonics).astype(np.float32)


def _ping(freq: float, duration: float, rate: int, decay: float, shimmer: float = 1.5) -> np.ndarray:
    t = _t(duration, rate)
    tone = (
        np.sin(2 * np.pi * freq * t)
        + 0.32 * np.sin(2 * np.pi * (freq * 2 + shimmer) * t)
        + 0.12 * np.sin(2 * np.pi * (freq * 3.01) * t)
    )
    return (tone * _env(t.size, rate, 0.003, decay)).astype(np.float32)


def _place(buffer: np.ndarray, clip: np.ndarray, at: float, rate: int, gain: float = 1.0) -> None:
    start = int(at * rate)
    end = min(buffer.size, start + clip.size)
    if end > start:
        buffer[start:end] += clip[: end - start] * gain


def _noise(duration: float, rate: int, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    white = rng.standard_normal(int(duration * rate)).astype(np.float32)
    # one-pole low-pass for an airy whoosh instead of harsh hiss
    out = np.empty_like(white)
    acc = 0.0
    for i, v in enumerate(white):
        acc += 0.08 * (v - acc)
        out[i] = acc
    return out


def _normalise(x: np.ndarray, peak: float) -> np.ndarray:
    m = float(np.max(np.abs(x))) if x.size else 0.0
    if m > 0:
        x = x * (peak / m)
    fade = min(x.size, 256)
    if fade:
        x[-fade:] *= np.linspace(1.0, 0.0, fade, dtype=np.float32)
    return x.astype(np.float32)


def build_library(rate: int) -> dict[str, np.ndarray]:
    """Return every effect as a mono float buffer at ``rate`` Hz."""
    lib: dict[str, np.ndarray] = {}

    # Activation chime: a bright rising triad over a soft sub "power" thump.
    buf = np.zeros(int(1.0 * rate), np.float32)
    _place(buf, _sweep(140, 60, 0.28, rate) * _env(int(0.28 * rate), rate, 0.01, 0.12), 0.0, rate, 0.55)
    for i, f in enumerate((880.0, 1318.5, 1760.0)):
        _place(buf, _ping(f, 0.75, rate, 0.30 - i * 0.04), 0.03 + i * 0.075, rate, 0.55 - i * 0.08)
    lib["activate"] = _normalise(buf, 0.62)

    # Boot: filtered-noise whoosh and a long rising sweep that lands on the chime.
    boot = np.zeros(int(2.1 * rate), np.float32)
    whoosh = _noise(1.25, rate) * np.sin(np.linspace(0, np.pi, int(1.25 * rate))) ** 2
    _place(boot, whoosh, 0.0, rate, 2.2)
    rise = _sweep(110, 1320, 1.15, rate, ((1, 1.0), (2, 0.25)), curve=1.6)
    rise *= np.linspace(0.05, 1.0, rise.size) ** 2
    _place(boot, rise, 0.05, rate, 0.30)
    _place(boot, lib["activate"], 1.12, rate, 1.0)
    lib["boot"] = _normalise(boot, 0.6)

    # Listening start / end: two quick blips, rising then falling.
    def blips(freqs, gap=0.075):
        out = np.zeros(int((gap * len(freqs) + 0.12) * rate), np.float32)
        for i, f in enumerate(freqs):
            _place(out, _ping(f, 0.11, rate, 0.035, 0.0), i * gap, rate)
        return out

    lib["listen"] = _normalise(blips((987.8, 1480.0)), 0.5)
    lib["listen_end"] = _normalise(blips((1480.0, 987.8)), 0.4)

    # Processing: three tiny data ticks.
    ticks = np.zeros(int(0.3 * rate), np.float32)
    for i, f in enumerate((2200.0, 2600.0, 3100.0)):
        _place(ticks, _ping(f, 0.04, rate, 0.009, 0.0), i * 0.06, rate, 0.8 - i * 0.15)
    lib["process"] = _normalise(ticks, 0.32)

    # Interrupt: quick downward glide.
    stop = _sweep(1100, 280, 0.2, rate, ((1, 1.0), (2, 0.2)), curve=0.7)
    lib["interrupt"] = _normalise(stop * _env(stop.size, rate, 0.004, 0.09), 0.42)

    # Error: two soft, low, slightly buzzy pulses.
    err = np.zeros(int(0.42 * rate), np.float32)
    pulse = _sweep(233, 220, 0.14, rate, ((1, 1.0), (3, 0.3), (5, 0.12))) * _env(int(0.14 * rate), rate, 0.006, 0.07)
    _place(err, pulse, 0.0, rate)
    _place(err, pulse, 0.18, rate, 0.8)
    lib["error"] = _normalise(err, 0.45)

    lib["click"] = _normalise(_ping(3800.0, 0.03, rate, 0.005, 0.0), 0.16)
    lib["notify"] = _normalise(_ping(1046.5, 0.5, rate, 0.16), 0.38)
    return lib


class SoundFX:
    def __init__(self, audio, config) -> None:
        self._audio = audio
        self._config = config
        self._sounds: dict[str, object] = {}

    @property
    def names(self) -> list[str]:
        return sorted(self._sounds)

    def load(self) -> None:
        if not self._audio.available:
            return
        try:
            for name, wave in build_library(self._audio.frequency).items():
                self._sounds[name] = self._audio.make_sound(wave)
        except Exception:
            log.exception("Could not synthesise interface sounds")

    def play(self, name: str) -> None:
        if not self._config.get("sfx_enabled", True):
            return
        sound = self._sounds.get(name)
        if sound is not None:
            try:
                self._audio.play_sfx(sound, float(self._config.get("sfx_volume", 0.45)))
            except Exception:
                log.debug("sfx %s failed", name, exc_info=True)
