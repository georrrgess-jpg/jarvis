"""Audio output (pygame.mixer) plus the spectrum analysis that drives the HUD visualizers.

Voice clips are decoded once, analysed into per-frame frequency bands, and the
band data is shipped to the UI in one message, so the visualizer stays in sync
with playback without streaming audio levels over the bridge at 60 Hz.
"""

from __future__ import annotations

import io
import logging
import os
import threading

import numpy as np

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

try:  # pygame is optional at import time so the rest of the app can still start
    import pygame
except Exception:  # pragma: no cover - depends on the host install
    pygame = None

log = logging.getLogger("jarvis.audio")

VIS_BANDS = 48
VIS_FPS = 40


# --------------------------------------------------------------------------- analysis


def _band_bins(rate: int, n_fft: int, bands: int, fmin: float, fmax: float) -> np.ndarray:
    """Log-spaced FFT bin edges, strictly increasing so every band owns at least one bin."""
    fmax = min(fmax, rate * 0.48)
    freqs = np.geomspace(fmin, fmax, bands + 1)
    bins = np.round(freqs / rate * n_fft).astype(int)
    bins[0] = max(bins[0], 1)
    for i in range(1, len(bins)):
        if bins[i] <= bins[i - 1]:
            bins[i] = bins[i - 1] + 1
    return np.minimum(bins, n_fft // 2)


def _bands_from_mag(mag: np.ndarray, bins: np.ndarray) -> np.ndarray:
    csum = np.concatenate([np.zeros((*mag.shape[:-1], 1)), np.cumsum(mag, axis=-1)], axis=-1)
    width = np.maximum(bins[1:] - bins[:-1], 1)
    return (csum[..., bins[1:]] - csum[..., bins[:-1]]) / width


def spectrum_frames(
    samples: np.ndarray,
    rate: int,
    fps: int = VIS_FPS,
    bands: int = VIS_BANDS,
    fmin: float = 80.0,
    fmax: float = 8000.0,
) -> tuple[list[list[int]], list[int]]:
    """Analyse a mono clip into ``fps`` frames of ``bands`` values (0-100) plus a loudness envelope."""
    samples = np.asarray(samples, dtype=np.float32).ravel()
    if samples.size == 0 or rate <= 0:
        return [], []
    hop = max(1, int(round(rate / fps)))
    n_fft = 2048 if rate >= 32000 else 1024
    n_frames = int(np.ceil(samples.size / hop))

    padded = np.concatenate([np.zeros(n_fft // 2, np.float32), samples, np.zeros(n_fft + hop, np.float32)])
    idx = np.arange(n_frames)[:, None] * hop + np.arange(n_fft)[None, :]
    window = np.hanning(n_fft).astype(np.float32)
    mag = np.abs(np.fft.rfft(padded[idx] * window, axis=1))

    band = _bands_from_mag(mag, _band_bins(rate, n_fft, bands, fmin, fmax))
    db = 20.0 * np.log10(band + 1e-9)
    db += np.linspace(0.0, 14.0, bands)  # tilt: speech energy falls with frequency
    top = np.percentile(db, 99.5)
    vals = np.clip((db - (top - 46.0)) / 46.0, 0.0, 1.0) ** 1.35

    # loudness envelope per frame, also used to gate the bands so pauses look silent
    seg = np.concatenate([samples, np.zeros(n_frames * hop - samples.size, np.float32)]).reshape(n_frames, hop)
    rms = np.sqrt(np.mean(seg * seg, axis=1))
    peak = float(rms.max()) if rms.size else 0.0
    level = rms / peak if peak > 1e-5 else np.zeros_like(rms)
    vals *= np.clip(level * 3.0, 0.0, 1.0)[:, None]

    frames = np.rint(vals * 100).astype(int).tolist()
    levels = np.rint(np.power(level, 0.6) * 100).astype(int).tolist()
    return frames, levels


class LiveSpectrum:
    """Streaming band analysis for microphone chunks with a slowly decaying peak (auto gain)."""

    def __init__(self, rate: int, bands: int = VIS_BANDS) -> None:
        self.rate = rate
        self.bands = bands
        self._peak_db = -30.0
        self._cache: dict[int, tuple[np.ndarray, np.ndarray]] = {}

    def process(self, chunk: np.ndarray) -> tuple[list[int], int]:
        x = np.asarray(chunk, dtype=np.float32).ravel() / 32768.0
        n = x.size
        if n < 32:
            return [0] * self.bands, 0
        if n not in self._cache:
            self._cache[n] = (np.hanning(n).astype(np.float32), _band_bins(self.rate, n, self.bands, 80.0, 7600.0))
        window, bins = self._cache[n]
        mag = np.abs(np.fft.rfft(x * window))
        db = 20.0 * np.log10(_bands_from_mag(mag, bins) + 1e-9) + np.linspace(0.0, 12.0, self.bands)

        frame_peak = float(db.max())
        self._peak_db = max(frame_peak, self._peak_db - 0.35)
        vals = np.clip((db - (self._peak_db - 42.0)) / 42.0, 0.0, 1.0) ** 1.4

        rms = float(np.sqrt(np.mean(x * x)))
        dbfs = 20.0 * np.log10(rms + 1e-9)
        level = float(np.clip((dbfs + 58.0) / 46.0, 0.0, 1.0))
        vals *= min(1.0, level * 2.2)
        return np.rint(vals * 100).astype(int).tolist(), int(round(level * 100))


# --------------------------------------------------------------------------- playback


class AudioEngine:
    """Owns pygame.mixer. Channel 0 is reserved for JARVIS's voice; the rest play interface sounds."""

    VOICE_CHANNEL = 0

    def __init__(self) -> None:
        self.available = False  # mixer initialised (possibly on the silent dummy driver)
        self.has_output = False  # a real output device is attached
        self.frequency = 44100
        self.channels = 2
        self.error: str | None = None
        self._lock = threading.RLock()
        self._voice: "pygame.mixer.Channel | None" = None

    def init(self) -> bool:
        if pygame is None:
            self.error = "pygame is not installed"
            log.error(self.error)
            return False
        try:
            pygame.mixer.pre_init(44100, -16, 2, 1024)
            pygame.mixer.init()
            self.has_output = True
        except pygame.error as exc:
            # No speaker attached (or a locked device): keep decoding and timing working silently.
            log.warning("No audio output device (%s); continuing with a silent driver", exc)
            self.error = f"No audio output device: {exc}"
            try:
                os.environ["SDL_AUDIODRIVER"] = "dummy"
                pygame.mixer.init(44100, -16, 2, 1024)
            except pygame.error as exc2:
                self.error = f"Audio unavailable: {exc2}"
                log.error(self.error)
                return False
        init = pygame.mixer.get_init()
        if not init:
            return False
        self.frequency, _size, self.channels = init
        pygame.mixer.set_num_channels(16)
        pygame.mixer.set_reserved(1)
        self._voice = pygame.mixer.Channel(self.VOICE_CHANNEL)
        self.available = True
        log.info("Audio ready: %s Hz, %s ch, output=%s", self.frequency, self.channels, self.has_output)
        return True

    # decoding -------------------------------------------------------------
    def decode(self, data: bytes) -> "pygame.mixer.Sound":
        """Decode MP3/WAV/OGG bytes into a mixer Sound."""
        if not self.available:
            raise RuntimeError("audio engine not initialised")
        with self._lock:
            return pygame.mixer.Sound(file=io.BytesIO(data))

    def mono_samples(self, sound: "pygame.mixer.Sound") -> np.ndarray:
        arr = pygame.sndarray.array(sound).astype(np.float32) / 32768.0
        return arr.mean(axis=1) if arr.ndim == 2 else arr

    def make_sound(self, mono: np.ndarray) -> "pygame.mixer.Sound":
        """Turn a float mono buffer (-1..1, at ``self.frequency``) into a playable Sound."""
        pcm = np.clip(np.asarray(mono, dtype=np.float32), -1.0, 1.0)
        pcm = (pcm * 32767).astype(np.int16)
        if self.channels > 1:
            pcm = np.repeat(pcm[:, None], self.channels, axis=1)
        with self._lock:
            return pygame.sndarray.make_sound(np.ascontiguousarray(pcm))

    # playback -------------------------------------------------------------
    def play_voice(self, sound: "pygame.mixer.Sound") -> None:
        if self._voice is not None:
            self._voice.play(sound)

    def voice_busy(self) -> bool:
        return bool(self._voice is not None and self._voice.get_busy())

    def stop_voice(self, fade_ms: int = 140) -> None:
        if self._voice is not None and self._voice.get_busy():
            self._voice.fadeout(fade_ms) if fade_ms else self._voice.stop()

    def play_sfx(self, sound: "pygame.mixer.Sound", volume: float = 0.5) -> None:
        if not self.available:
            return
        # Sound.play() only picks unreserved channels, so effects never cut off the voice.
        sound.set_volume(max(0.0, min(1.0, volume)))
        sound.play()

    def shutdown(self) -> None:
        if self.available and pygame is not None:
            try:
                pygame.mixer.quit()
            except Exception:
                pass
        self.available = False
