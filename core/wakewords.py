"""Wake words for every personality: "Hey Jarvis" is built in; other names are learned on this PC.

``WakeWords`` keeps the learned models (``%APPDATA%\\JARVIS\\wakewords``), trains new ones in the
background with the Edge voices (see ``wakelearn``), records a few samples of the user's own voice
to make a name more reliable, and reports progress to the HUD.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable

import numpy as np

from . import wakelearn
from .wakelearn import Cancelled, Trainer, WakeModel, english_voices, load_model, model_path, resample, slug, speech_bounds

log = logging.getLogger("jarvis.wakewords")

MIN_VOICES = 8
RECORD_SECONDS = 2.5
MAX_RECORDINGS = 6


class WakeWords:
    def __init__(self, directory: Path, tts, audio, emit: Callable[..., None]) -> None:
        self.directory = Path(directory)
        self.tts = tts
        self.audio = audio
        self._emit = emit
        self._models: dict[str, WakeModel | None] = {}
        self._lock = threading.Lock()
        self._job: dict | None = None  # the name being learned now: {"name", "label", "progress", "cancel"}
        self._running = False  # a worker thread is alive
        self._queue: list[tuple[str, bool]] = []
        self._errors: dict[str, str] = {}
        self._recordings: dict[str, list[np.ndarray]] = {}
        self.on_ready: Callable[[str], None] = lambda name: None
        self.trainer_factory: Callable[..., Trainer] = Trainer
        self.voice_list: Callable[[], list[str]] = self._voices

    # ------------------------------------------------------------------ models
    def model(self, name: str) -> WakeModel | None:
        key = slug(name)
        with self._lock:
            if key not in self._models:
                self._models[key] = load_model(self.directory, name)
            return self._models[key]

    def status(self, name: str) -> dict:
        model = self.model(name)
        with self._lock:
            job = self._job if self._job and slug(self._job["name"]) == slug(name) else None
            queued = any(slug(n) == slug(name) for n, _ in self._queue)
            error = self._errors.get(slug(name))
        state = "learning" if job else "queued" if queued else "ready" if model else "error" if error else "missing"
        return {
            "name": name, "phrase": f"Hey {name}", "state": state,
            "progress": round(job["progress"], 3) if job else (1.0 if model else 0.0),
            "label": job["label"] if job else "", "error": error,
            "metrics": model.metrics if model else {}, "user_samples": model.user_samples if model else 0,
            "recordings": len(self._recordings.get(slug(name), [])) or self._saved_recordings_count(name),
        }

    def delete(self, name: str) -> None:
        for path in (model_path(self.directory, name), self.directory / f"{slug(name)}-samples.npz",
                     self.directory / f"{slug(name)}-mine.npz"):
            path.unlink(missing_ok=True)
        with self._lock:
            self._models.pop(slug(name), None)

    # ------------------------------------------------------------------ learning
    def learn(self, name: str, with_my_voice: bool = False) -> bool:
        """Queue training for ``name``; returns False if it's already queued or running."""
        name = name.strip()
        if not name:
            return False
        with self._lock:
            if (self._job and slug(self._job["name"]) == slug(name)) or any(slug(n) == slug(name) for n, _ in self._queue):
                return False
            self._queue.append((name, with_my_voice))
            self._errors.pop(slug(name), None)
            start = not self._running
            self._running = True
        self._send(name)
        if start:
            threading.Thread(target=self._worker, name="wake-learn", daemon=True).start()
        return True

    def cancel(self, name: str | None = None) -> None:
        with self._lock:
            self._queue = [(n, v) for n, v in self._queue if name and slug(n) != slug(name)]
            if self._job and (name is None or slug(self._job["name"]) == slug(name)):
                self._job["cancel"].set()

    def busy(self) -> bool:
        with self._lock:
            return self._running

    def _worker(self) -> None:
        while True:
            with self._lock:
                if not self._queue:
                    self._job = None
                    self._running = False
                    return
                name, mine = self._queue.pop(0)
                self._job = {"name": name, "label": "Getting ready", "progress": 0.0, "cancel": threading.Event()}
                job = self._job
            self._send(name)
            try:
                voices = self.voice_list()
                if len(voices) < MIN_VOICES:
                    raise RuntimeError("I need an internet connection to learn a new name (the practice voices come from Microsoft's voice service).")

                def progress(label: str, frac: float) -> None:
                    job["label"], job["progress"] = label, frac
                    self._send(name, throttle=True)

                trainer = self.trainer_factory(synth=self._synth, voices=voices, cache_dir=self.directory,
                                               progress=progress, cancel=job["cancel"])
                samples = self._load_recordings(name) if mine or self._saved_recordings_count(name) else []
                model = trainer.train(name, user_samples=samples)
                model.save(model_path(self.directory, name))
                with self._lock:
                    self._models[slug(name)] = model
                log.info("Learned wake word %r: %s", name, model.metrics)
                self.on_ready(name)
            except Cancelled:
                log.info("Learning %r cancelled", name)
            except Exception as exc:
                log.exception("Learning wake word %r failed", name)
                with self._lock:
                    self._errors[slug(name)] = str(exc) or exc.__class__.__name__
            finally:
                with self._lock:
                    self._job = None
                self._send(name)

    _last_send = 0.0

    def _send(self, name: str, throttle: bool = False) -> None:
        now = time.monotonic()
        if throttle and now - self._last_send < 0.4:
            return
        self._last_send = now
        try:
            self._emit("wake_learn", **self.status(name))
        except Exception:
            log.debug("wake_learn event failed", exc_info=True)

    # ------------------------------------------------------------------ practice voices
    def _voices(self) -> list[str]:
        try:
            listed = self.tts.list_voices()
        except Exception:
            listed = []
        return english_voices({"ShortName": v.get("id"), "Locale": v.get("locale")} for v in listed)

    def _synth(self, items: list[tuple[str, str, int, int]]) -> list[np.ndarray | None]:
        clips = self.tts.synthesize_many(items)
        out: list[np.ndarray | None] = []
        for data in clips:
            if not data:
                out.append(None)
                continue
            try:
                out.append(self._decode(data))
            except Exception as exc:
                log.debug("couldn't decode a practice clip: %s", exc)
                out.append(None)
        return out

    def _decode(self, data: bytes) -> np.ndarray:
        """MP3 -> 16 kHz mono in int16 range (what the microphone gives)."""
        if not self.audio.available:
            self.audio.init()
        sound = self.audio.decode(data)
        mono = self.audio.mono_samples(sound)  # -1..1 at the mixer rate
        return resample(mono * 32767.0, int(self.audio.frequency))

    # ------------------------------------------------------------------ the user's own voice
    def record(self, name: str, source) -> dict:
        """Record one ~2.5 s sample of the user saying the name from ``source`` (16 kHz int16 reader)."""
        frames = int(RECORD_SECONDS * wakelearn.RATE)
        chunk = 1280
        data = b""
        while len(data) < frames * 2:
            data += source.read(chunk)
        audio = np.frombuffer(data[: frames * 2], dtype=np.int16).astype(np.float32)
        start, end = speech_bounds(audio)
        loud = float(np.sqrt(np.mean(audio[start:end] ** 2))) if end > start else 0.0
        if loud < 150 or end - start < 0.2 * wakelearn.RATE:
            return {"ok": False, "error": "I didn't hear anything. Check the microphone and say it a little louder.", "count": self.recording_count(name)}
        if end - start > 2.2 * wakelearn.RATE:
            return {"ok": False, "error": "That was a bit long. Just say the name, like “Hey " + name + "”.", "count": self.recording_count(name)}
        with self._lock:
            takes = self._recordings.setdefault(slug(name), self._load_recordings_unlocked(name))
            takes.append(audio[max(0, start - 1600): end + 1600])
            del takes[:-MAX_RECORDINGS]
            self._save_recordings_unlocked(name, takes)
            count = len(takes)
        return {"ok": True, "count": count, "level": round(min(1.0, loud / 6000), 2)}

    def clear_recordings(self, name: str) -> None:
        with self._lock:
            self._recordings.pop(slug(name), None)
        (self.directory / f"{slug(name)}-mine.npz").unlink(missing_ok=True)

    def recording_count(self, name: str) -> int:
        return len(self._recordings.get(slug(name)) or []) or self._saved_recordings_count(name)

    def _saved_recordings_count(self, name: str) -> int:
        path = self.directory / f"{slug(name)}-mine.npz"
        if not path.is_file():
            return 0
        try:
            with np.load(path) as data:
                return int(len(data["lengths"]))
        except Exception:
            return 0

    def _load_recordings(self, name: str) -> list[np.ndarray]:
        with self._lock:
            return list(self._load_recordings_unlocked(name))

    def _load_recordings_unlocked(self, name: str) -> list[np.ndarray]:
        if slug(name) in self._recordings:
            return self._recordings[slug(name)]
        path = self.directory / f"{slug(name)}-mine.npz"
        try:
            with np.load(path) as data:
                return [a.astype(np.float32) for a in np.split(data["audio"], np.cumsum(data["lengths"])[:-1])]
        except Exception:
            return []

    def _save_recordings_unlocked(self, name: str, takes: list[np.ndarray]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(self.directory / f"{slug(name)}-mine.npz", lengths=np.array([t.size for t in takes]),
                            audio=np.concatenate(takes).astype(np.int16))
