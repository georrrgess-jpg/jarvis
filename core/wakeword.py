"""Offline "Hey Jarvis" wake word: openWakeWord's pretrained models run directly on onnxruntime.

Pipeline (per 80 ms chunk of 16 kHz audio): mel spectrogram -> 96-dim speech embedding over the
last 76 mel frames -> wake-word classifier over the last 16 embeddings -> score 0..1.
Running the three small ONNX models ourselves avoids the openwakeword package's heavy
scipy/scikit-learn dependencies. Models: https://github.com/dscripka/openWakeWord (v0.5.1).

``WakeListener`` owns the microphone while idle and hands the very same stream to the command
recogniser when the wake word fires, so there is never a second device open and no audio gap.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable

import numpy as np

from .config import resource_path

log = logging.getLogger("jarvis.wakeword")

RATE = 16000
CHUNK = 1280  # 80 ms, the frame size the models were trained on
MODEL_FILES = ("melspectrogram.onnx", "embedding_model.onnx", "hey_jarvis_v0.1.onnx")


def model_dir() -> Path:
    return resource_path("assets", "wakeword")


def available() -> tuple[bool, str]:
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        return False, "onnxruntime is not installed"
    missing = [f for f in MODEL_FILES if not (model_dir() / f).is_file()]
    if missing:
        return False, f"wake-word model files missing: {', '.join(missing)}"
    return True, ""


class WakeWordDetector:
    def __init__(self, directory: Path | None = None) -> None:
        import onnxruntime as ort

        directory = directory or model_dir()
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        opts.log_severity_level = 3

        def load(name: str):
            return ort.InferenceSession(str(directory / name), sess_options=opts, providers=["CPUExecutionProvider"])

        self._mel_model, self._emb_model, self._wake_model = (load(f) for f in MODEL_FILES)
        self._wake_input = self._wake_model.get_inputs()[0].name
        self.learned: dict[str, object] = {}  # name -> wakelearn.WakeModel, sharing the same embeddings
        self.reset()

    # -- model steps -----------------------------------------------------
    def _melspec(self, audio: np.ndarray) -> np.ndarray:
        out = self._mel_model.run(None, {"input": audio[None, :].astype(np.float32)})[0]
        return np.squeeze(out) / 10.0 + 2.0  # openWakeWord's fixed transform

    def _embed(self, mel_windows: np.ndarray) -> np.ndarray:
        out = self._emb_model.run(None, {"input_1": mel_windows[..., None].astype(np.float32)})[0]
        return out.reshape(-1, 96)

    def reset(self) -> None:
        """Forget all audio context (e.g. after JARVIS has taken over the microphone)."""
        self._tail = np.zeros(480, np.float32)  # 3 hops of context for the next spectrogram
        self._mel = np.ones((76, 32), np.float32)
        noise = np.random.default_rng(0).integers(-1000, 1000, RATE * 4).astype(np.float32)
        mel = self._melspec(noise)
        windows = np.stack([mel[i : i + 76] for i in range(0, mel.shape[0] - 76 + 1, 8)])
        self._feats = self._embed(windows)
        self._frames = 0

    def process(self, chunk: np.ndarray) -> float:
        """Feed exactly one 1280-sample int16 chunk; returns the "Hey Jarvis" score (0..1)."""
        return self.process_all(chunk, jarvis=True)["jarvis"]

    def process_all(self, chunk: np.ndarray, jarvis: bool = True, learned: tuple[str, ...] | None = None) -> dict[str, float]:
        """Feed one chunk; returns a score per wake word: "jarvis" and each learned name (e.g. "Harper")."""
        audio = np.asarray(chunk, dtype=np.float32).ravel()
        mel = self._melspec(np.concatenate([self._tail, audio]))
        self._tail = audio[-480:]
        self._mel = np.vstack([self._mel, mel])[-970:]
        self._feats = np.vstack([self._feats, self._embed(self._mel[-76:][None])])[-120:]
        self._frames += 1
        ready = self._frames >= 5  # the first few frames are unreliable
        window = self._feats[-16:]
        scores: dict[str, float] = {}
        if jarvis:
            score = float(self._wake_model.run(None, {self._wake_input: window[None].astype(np.float32)})[0].ravel()[0])
            scores["jarvis"] = score if ready else 0.0
        for name, model in self.learned.items():
            if learned is None or name in learned:
                scores[name] = model.score(window) if ready else 0.0
        return scores


LEARNED_PERSISTENCE = 4  # learned names need two more confident frames than the professionally trained "Hey Jarvis"


def learned_threshold(calibrated: float, sensitivity: float) -> float:
    """Sensitivity 0.5 uses the threshold set during training; higher wakes more easily, lower is stricter."""
    if sensitivity >= 0.5:
        return max(0.3, calibrated - (sensitivity - 0.5) * 2 * max(0.0, calibrated - 0.35))
    return min(0.995, calibrated + (0.5 - sensitivity) * 2 * (0.995 - calibrated))


class _BorrowedSource:
    """An AudioSource fed from the listener's live stream (what SpeechInput reads during a command)."""

    def __init__(self, listener: "WakeListener") -> None:
        self.rate = RATE
        self.chunk = 512
        self._listener = listener
        self._queue: "queue.Queue[bytes]" = queue.Queue()
        self._buffer = b""
        self._closed = False
        # Room noise measured while idle, so the command capture needn't calibrate (the user is
        # usually already talking by then).
        self.noise_floor: float | None = None

    def feed(self, data: bytes) -> None:
        self._queue.put(data)

    def read(self, frames: int) -> bytes:
        need = frames * 2
        while len(self._buffer) < need:
            try:
                self._buffer += self._queue.get(timeout=2.0)
            except queue.Empty:
                if not self._listener.running:
                    raise OSError("microphone stopped")
        data, self._buffer = self._buffer[:need], self._buffer[need:]
        return data

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._listener._release(self)


class WakeListener:
    """Background microphone loop: detects the wake word, and lends its stream to command capture."""

    COOLDOWN_S = 1.5
    PERSISTENCE = 2  # consecutive 80 ms frames above threshold (filters single-frame blips)
    LOOKBACK_CHUNKS = 2  # audio kept from just before the detection: people run "Hey Jarvis" into the command
    BACKLOG_S = 4.0  # audio buffered between the detection and the command capture starting

    def __init__(self, on_wake: Callable[[float], None], sensitivity: Callable[[], float],
                 stream_factory: Callable[[], object] | None = None,
                 detector_factory: Callable[[], WakeWordDetector] = WakeWordDetector,
                 words: Callable[[], tuple[bool, list]] | None = None) -> None:
        """``words`` says what to listen for: (listen for "Hey Jarvis"?, [learned WakeModels])."""
        self._on_wake = on_wake
        self._sensitivity = sensitivity
        self._stream_factory = stream_factory or self._open_pyaudio
        self._detector_factory = detector_factory
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._borrower: _BorrowedSource | None = None
        self._lock = threading.Lock()
        self._last_wake = 0.0
        self._recent: deque[bytes] = deque(maxlen=4)
        self._energy: deque[float] = deque(maxlen=int(5 * RATE / CHUNK))
        self._backlog: list[bytes] | None = None
        self._words = words or (lambda: (True, []))
        self._words_dirty = True
        self.last_word = "jarvis"  # which wake word fired last ("jarvis" or a learned name)
        self.running = False
        self.error: str | None = None

    def refresh_words(self) -> None:
        """Pick up a new active wake word (personality switched, or a name was just learned)."""
        self._words_dirty = True

    # -- control ---------------------------------------------------------
    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return True
        self._stop.clear()
        self.error = None
        ready = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(ready,), name="wake-word", daemon=True)
        self._thread.start()
        ready.wait(10)
        return self.running

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(3)
        self._thread = None

    def borrow(self) -> _BorrowedSource:
        """Hand the live microphone to a command capture; wake detection pauses until it's closed."""
        with self._lock:
            src = _BorrowedSource(self)
            if len(self._energy) >= 10:
                src.noise_floor = float(np.percentile(np.asarray(self._energy), 20))
            for data in self._backlog or []:
                src.feed(data)  # everything said since the wake word, so the command's first word isn't lost
            self._backlog = None
            self._borrower = src
            return src

    def _release(self, src: _BorrowedSource) -> None:
        with self._lock:
            if self._borrower is src:
                self._borrower = None
                self._last_wake = time.monotonic()  # cooldown: ignore our own echo / the tail of the command
                self._reset_pending = True

    # -- loop ------------------------------------------------------------
    @staticmethod
    def _open_pyaudio():
        import pyaudio

        pa = pyaudio.PyAudio()
        stream = pa.open(format=pyaudio.paInt16, channels=1, rate=RATE, input=True, frames_per_buffer=CHUNK)

        class Stream:
            def read(self, n):
                return stream.read(n, exception_on_overflow=False)

            def close(self):
                try:
                    stream.stop_stream()
                    stream.close()
                finally:
                    pa.terminate()

        return Stream()

    def _run(self, ready: threading.Event) -> None:
        try:
            detector = self._detector_factory()
            stream = self._stream_factory()
        except Exception as exc:
            self.error = f"{exc.__class__.__name__}: {exc}"
            log.warning("Wake word unavailable: %s", self.error)
            ready.set()
            return
        self.running = True
        self._reset_pending = False
        ready.set()
        log.info("Listening for the wake word")
        hits: dict[str, int] = {}
        use_jarvis, learned_thresholds = True, {}
        try:
            while not self._stop.is_set():
                data = stream.read(CHUNK)
                with self._lock:
                    borrower = self._borrower
                    if borrower is None and self._backlog is not None:
                        self._backlog.append(data)
                        if len(self._backlog) * CHUNK / RATE > self.BACKLOG_S:
                            self._backlog = None  # nobody borrowed the stream: stop buffering
                if borrower is not None:
                    borrower.feed(data)
                    hits.clear()
                    continue
                if self._reset_pending:
                    detector.reset()
                    self._reset_pending = False
                if self._words_dirty:
                    self._words_dirty = False
                    try:
                        use_jarvis, models = self._words()
                    except Exception:
                        log.exception("Couldn't read the active wake words")
                        use_jarvis, models = True, []
                    if hasattr(detector, "learned"):
                        detector.learned = {m.name: m for m in models}
                    learned_thresholds = {m.name: m.threshold for m in models}
                    hits.clear()
                samples = np.frombuffer(data, dtype=np.int16)
                self._energy.append(float(np.sqrt(np.mean(samples.astype(np.float32) ** 2))))
                if hasattr(detector, "process_all"):
                    scores = detector.process_all(samples, jarvis=use_jarvis)
                else:  # a bare detector (tests): "Hey Jarvis" only
                    scores = {"jarvis": detector.process(samples)}
                self._recent.append(data)
                sens = float(self._sensitivity())
                fired = None
                for word, score in scores.items():
                    if word == "jarvis":
                        threshold, need = 0.85 - 0.6 * sens, self.PERSISTENCE  # sensitivity 0..1 -> 0.85..0.25
                    else:
                        threshold, need = learned_threshold(learned_thresholds.get(word, 0.9), sens), LEARNED_PERSISTENCE
                    hits[word] = hits.get(word, 0) + 1 if score >= threshold else 0
                    if hits[word] >= need and fired is None:
                        fired = (word, score)
                if fired and time.monotonic() - self._last_wake > self.COOLDOWN_S:
                    word, score = fired
                    hits.clear()
                    self._last_wake = time.monotonic()
                    self.last_word = word
                    log.info("Wake word %r detected (score %.2f)", word, score)
                    with self._lock:
                        self._backlog = list(self._recent)[len(self._recent) - self.LOOKBACK_CHUNKS:] if self.LOOKBACK_CHUNKS else []
                    try:
                        self._on_wake(score)
                    except Exception:
                        log.exception("Wake handler failed")
        except Exception as exc:
            self.error = f"{exc.__class__.__name__}: {exc}"
            log.warning("Wake-word microphone loop stopped: %s", self.error)
        finally:
            self.running = False
            try:
                stream.close()
            except Exception:
                pass
