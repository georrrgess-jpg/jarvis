"""Microphone capture with voice-activity detection, plus pluggable speech recognisers.

Capture runs its own energy-based VAD (calibrated to the room on every activation)
so we can stream live mic levels to the HUD and stop automatically on silence.
Recognition engines, all free with no API key:

* ``google``  - Google Web Speech via SpeechRecognition (online, nothing to download)
* ``whisper`` - faster-whisper, fully offline (optional install, model downloads on first use)
* ``vosk``    - Vosk, fully offline (optional install + a model folder)
"""

from __future__ import annotations

import importlib.util
import json
import logging
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

import numpy as np

from .audio import LiveSpectrum
from .config import app_data_dir, resource_path

log = logging.getLogger("jarvis.stt")

ENGINE_LABELS = {
    "google": "Google Web Speech",
    "whisper": "Whisper (offline)",
    "vosk": "Vosk (offline)",
}
_WHISPER_PHANTOMS = {"thank you.", "thanks for watching!", "you", "bye.", "thank you for watching."}


class STTError(Exception):
    """A recognition failure worth telling the user about."""


class _EngineUnavailable(Exception):
    pass


@dataclass
class Capture:
    pcm: bytes
    rate: int
    width: int = 2

    @property
    def duration(self) -> float:
        return len(self.pcm) / float(self.rate * self.width) if self.rate else 0.0


class AudioSource(Protocol):
    rate: int

    def read(self, frames: int) -> bytes: ...

    def close(self) -> None: ...


class _PyAudioSource:
    def __init__(self, preferred_rate: int = 16000) -> None:
        import pyaudio

        self._pa = pyaudio.PyAudio()
        last: Exception | None = None
        for rate in (preferred_rate, None):
            try:
                if rate is None:
                    rate = int(self._pa.get_default_input_device_info()["defaultSampleRate"])
                self.chunk = max(256, int(rate * 0.032))
                self._stream = self._pa.open(
                    format=pyaudio.paInt16, channels=1, rate=rate, input=True, frames_per_buffer=self.chunk
                )
                self.rate = rate
                return
            except Exception as exc:  # unsupported rate or no device
                last = exc
        self._pa.terminate()
        raise STTError(f"Could not open the microphone ({last}).")

    def read(self, frames: int) -> bytes:
        return self._stream.read(frames, exception_on_overflow=False)

    def close(self) -> None:
        try:
            self._stream.stop_stream()
            self._stream.close()
        finally:
            self._pa.terminate()


def _rms(chunk: np.ndarray) -> float:
    return float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2))) if chunk.size else 0.0


class SpeechInput:
    CALIBRATE_S = 0.3
    PRE_ROLL_S = 0.45
    MIN_THRESHOLD = 260.0
    MAX_THRESHOLD = 3500.0

    def __init__(self, config, source_factory: Callable[[], AudioSource] | None = None) -> None:
        self._config = config
        self._source_factory = source_factory or _PyAudioSource
        self._lock = threading.Lock()
        self._whisper = None
        self._whisper_name: str | None = None
        self._vosk = None
        self._vosk_path: str | None = None

    def set_source_provider(self, provider: Callable[[], AudioSource] | None) -> None:
        """Use a shared live stream (the wake-word listener's) instead of opening the microphone."""
        self._source_factory = provider or _PyAudioSource

    # ------------------------------------------------------------------ status
    def availability(self) -> dict:
        info = {"available": False, "device": None, "reason": None, "engine": self.engine_label()}
        if importlib.util.find_spec("pyaudio") is None:
            info["reason"] = "Microphone support (PyAudio) is not installed."
            return info
        try:
            import pyaudio

            pa = pyaudio.PyAudio()
            try:
                info["device"] = pa.get_default_input_device_info().get("name")
                info["available"] = True
            finally:
                pa.terminate()
        except Exception:
            info["reason"] = "No microphone detected."
        return info

    def engine_chain(self) -> list[str]:
        choice = self._config.get("stt_engine", "auto")
        has_whisper = importlib.util.find_spec("faster_whisper") is not None
        has_vosk = importlib.util.find_spec("vosk") is not None and self._vosk_model_dir() is not None
        if choice == "whisper":
            return ["whisper", "google"]
        if choice == "vosk":
            return ["vosk", "google"]
        if choice == "google":
            return ["google"]
        chain = (["whisper"] if has_whisper else []) + (["vosk"] if has_vosk else [])
        return chain + ["google"]

    def engine_label(self) -> str:
        return ENGINE_LABELS[self.engine_chain()[0]]

    # ------------------------------------------------------------------ capture
    def listen(
        self,
        cancel: threading.Event,
        stop: threading.Event,
        hold: threading.Event | None = None,
        on_frame: Callable[[list[int], int], None] | None = None,
        on_phase: Callable[[str], None] | None = None,
    ) -> Capture | None:
        """Record one utterance.

        Ends on trailing silence, when ``stop`` is set (button released / clicked again)
        or at the phrase limit. While ``hold`` is set (push-to-talk held down) silence
        never ends the capture. Returns None on cancel or if nobody spoke.
        """
        hold = hold or threading.Event()
        on_frame = on_frame or (lambda bands, level: None)
        on_phase = on_phase or (lambda phase: None)
        pause_s = float(self._config.get("pause_threshold", 0.9))
        timeout_s = float(self._config.get("listen_timeout", 8))
        max_s = float(self._config.get("max_phrase_seconds", 25))

        source = self._source_factory()
        rate = source.rate
        chunk = getattr(source, "chunk", max(256, int(rate * 0.032)))
        chunk_s = chunk / rate
        spectrum = LiveSpectrum(rate)
        pre_roll: deque[bytes] = deque(maxlen=max(1, int(self.PRE_ROLL_S / chunk_s)))
        waiting_audio: list[bytes] = []
        frames: list[bytes] = []

        def read() -> tuple[bytes, float]:
            data = source.read(chunk)
            samples = np.frombuffer(data, dtype=np.int16)
            bands, level = spectrum.process(samples)
            on_frame(bands, level)
            return data, _rms(samples)

        try:
            on_phase("calibrating")
            energies = []
            for _ in range(max(1, int(self.CALIBRATE_S / chunk_s))):
                if cancel.is_set():
                    return None
                data, energy = read()
                pre_roll.append(data)
                waiting_audio.append(data)
                energies.append(energy)
            ambient = float(np.median(energies))
            threshold = float(np.clip(ambient * 2.6, self.MIN_THRESHOLD, self.MAX_THRESHOLD))
            peak_waiting = 0.0

            on_phase("waiting")
            speaking, voiced_run, waited = False, 0, 0.0
            silence, spoken = 0.0, 0.0
            while True:
                if cancel.is_set():
                    return None
                data, energy = read()
                if not speaking:
                    pre_roll.append(data)
                    if len(waiting_audio) * chunk_s < max_s:
                        waiting_audio.append(data)
                    peak_waiting = max(peak_waiting, energy)
                    waited += chunk_s
                    if energy > threshold:
                        voiced_run += 1
                    else:
                        voiced_run = 0
                        ambient = 0.95 * ambient + 0.05 * energy  # track a changing room
                        threshold = float(np.clip(ambient * 2.6, self.MIN_THRESHOLD, self.MAX_THRESHOLD))
                    if voiced_run >= 3:
                        speaking = True
                        frames = list(pre_roll)
                        on_phase("capturing")
                    elif stop.is_set():
                        # Released before the VAD triggered: keep a quiet utterance if there was one.
                        if peak_waiting > max(ambient * 1.8, self.MIN_THRESHOLD * 0.6):
                            frames = waiting_audio
                            break
                        return None
                    elif waited >= timeout_s and not hold.is_set():
                        return None
                else:
                    frames.append(data)
                    spoken += chunk_s
                    silence = 0.0 if energy > threshold * 0.75 else silence + chunk_s
                    if stop.is_set() or spoken >= max_s:
                        break
                    if silence >= pause_s and not hold.is_set():
                        break
        finally:
            try:
                source.close()
            except Exception:
                log.debug("closing microphone failed", exc_info=True)

        # Trim the trailing silence but keep a natural tail.
        keep_tail = int(0.25 / chunk_s)
        tail = int(silence / chunk_s) if speaking else 0
        if tail > keep_tail:
            frames = frames[: len(frames) - (tail - keep_tail)]
        return Capture(b"".join(frames), rate) if frames else None

    # ------------------------------------------------------------------ recognition
    def transcribe(self, capture: Capture, on_status: Callable[[str], None] | None = None) -> str:
        last_error: STTError | None = None
        for engine in self.engine_chain():
            try:
                text = getattr(self, f"_recognize_{engine}")(capture, on_status)
                return (text or "").strip()
            except _EngineUnavailable as exc:
                log.warning("STT engine %s unavailable: %s", engine, exc)
                continue
            except STTError as exc:
                last_error = exc
                continue
        raise last_error or STTError("No speech recognition engine is available.")

    def _recognize_google(self, capture: Capture, on_status=None) -> str:
        try:
            import speech_recognition as sr
        except ImportError as exc:
            raise _EngineUnavailable("SpeechRecognition is not installed") from exc
        recognizer = sr.Recognizer()
        audio = sr.AudioData(capture.pcm, capture.rate, capture.width)
        try:
            return recognizer.recognize_google(audio, language=self._config.get("stt_language", "en-US"))
        except sr.UnknownValueError:
            return ""
        except sr.RequestError as exc:
            raise STTError(
                "Google speech service is unreachable. Check your internet connection, "
                "or install faster-whisper for offline recognition."
            ) from exc

    def _recognize_whisper(self, capture: Capture, on_status=None) -> str:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise _EngineUnavailable("faster-whisper is not installed") from exc
        name = self._config.get("whisper_model") or "base.en"
        with self._lock:
            if self._whisper is None or self._whisper_name != name:
                if on_status:
                    on_status(f"Loading Whisper model '{name}' (first use downloads it)")
                try:
                    self._whisper = WhisperModel(name, device="cpu", compute_type="int8")
                    self._whisper_name = name
                except Exception as exc:
                    raise _EngineUnavailable(f"could not load Whisper model {name}: {exc}") from exc
            model = self._whisper
        audio = np.frombuffer(capture.pcm, dtype=np.int16).astype(np.float32) / 32768.0
        if capture.rate != 16000 and audio.size:
            n = int(audio.size * 16000 / capture.rate)
            audio = np.interp(np.linspace(0, audio.size - 1, n), np.arange(audio.size), audio).astype(np.float32)
        language = None if name.endswith(".en") else (self._config.get("stt_language") or "en").split("-")[0]
        segments, _info = model.transcribe(audio, language=language, beam_size=1, condition_on_previous_text=False)
        text = " ".join(seg.text.strip() for seg in segments).strip()
        if capture.duration < 1.2 and text.lower() in _WHISPER_PHANTOMS:
            return ""
        return text

    def _vosk_model_dir(self) -> Path | None:
        configured = (self._config.get("vosk_model_path") or "").strip()
        for candidate in filter(None, [configured, resource_path("models", "vosk"), app_data_dir() / "vosk-model"]):
            path = Path(candidate)
            if path.is_dir() and any(path.iterdir()):
                return path
        return None

    def _recognize_vosk(self, capture: Capture, on_status=None) -> str:
        try:
            from vosk import KaldiRecognizer, Model, SetLogLevel
        except ImportError as exc:
            raise _EngineUnavailable("vosk is not installed") from exc
        model_dir = self._vosk_model_dir()
        if model_dir is None:
            raise _EngineUnavailable("no Vosk model folder found")
        with self._lock:
            if self._vosk is None or self._vosk_path != str(model_dir):
                SetLogLevel(-1)
                self._vosk = Model(str(model_dir))
                self._vosk_path = str(model_dir)
            model = self._vosk
        recognizer = KaldiRecognizer(model, capture.rate)
        recognizer.AcceptWaveform(capture.pcm)
        return json.loads(recognizer.FinalResult()).get("text", "")
