"""Teach the wake-word listener a new name ("Harper", "Hey Harper") without any pretrained model.

openWakeWord's speech-embedding model turns audio into a general-purpose 96-number description of
every 80 ms of sound. Its "Hey Jarvis" model is just a small classifier on top of 16 of those in a
row. We train our own small classifier the same way, for any name:

1. **Positive samples**: the phrases ("Harper", "Hey Harper", "Okay Harper"...) spoken by dozens of
   different neural voices at different speeds and pitches, each also mixed with noise, echo and
   level changes. Optionally a few recordings of the user's own voice, which help the most.
2. **Negative samples**: everyday sentences, the other assistants' names, sound-alike pieces of the
   name ("Harp", "per") and plain noise, so it learns what *not* to wake up for. These are cached
   and reused for every name.
3. A tiny neural network (1536 -> 48 -> 1, plain numpy) is trained in a few seconds, then its
   threshold is set on voices it never saw so it rarely wakes by mistake.

The result is a few hundred kilobytes saved in the app data folder and runs on the same audio stream
as "Hey Jarvis" at no extra cost (the expensive embedding step is shared).
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

import numpy as np

log = logging.getLogger("jarvis.wakelearn")

RATE = 16000
PERSISTENCE = 4  # consecutive confident 80 ms frames before a learned name counts ("Hey Jarvis" uses 2)
WINDOW = 16  # embeddings per classifier input (16 x 80 ms = 1.28 s), as openWakeWord
FORMAT_VERSION = 2
NEGATIVES_VERSION = 5
MAX_NEGATIVE_WINDOWS = 22000  # ~135 MB of training data at most

Synth = Callable[[list[tuple[str, str, int, int]]], list["np.ndarray | None"]]  # [(text, voice, rate%, pitchHz)] -> 16 kHz audio
Progress = Callable[[str, float], None]

NEGATIVE_SENTENCES = (
    "What's the weather like today?", "Open Spotify and play some music.", "Set a timer for ten minutes.",
    "I think we should leave at six.", "Can you pass me the salt, please?", "The meeting has been moved to Thursday.",
    "Hey, how are you doing?", "Hello there, nice to meet you.", "Okay, sounds good to me.", "Hi everyone, welcome back.",
    "Turn the volume up a little.", "What time is it in Tokyo?", "Remind me to call my mum tomorrow.",
    "I'm going to the shop, do you need anything?", "That was the best film I've seen all year.",
    "The quick brown fox jumps over the lazy dog.", "Let's order pizza tonight.", "Did you finish your homework?",
    "My phone is almost out of battery.", "We need to buy milk, eggs and bread.", "Where did I put my keys?",
    "Please close the window, it's cold.", "The train was twenty minutes late again.", "I love this song, turn it up.",
    "Could you send me that file by email?", "Happy birthday, I hope you have a great day.", "The harbour was full of boats.",
    "She opened the paper and read the headlines.", "Harvest season starts in September.", "He played the harp beautifully.",
    "Hurry up, we're going to be late.", "Have a look at this picture.", "Hey Jarvis, what's the time?", "Hey Siri, call home.",
    "Alexa, turn off the lights.", "Okay Google, set an alarm.", "It's Friday, finally the weekend!", "Add sage and thyme to the soup.",
    "Thank you so much for your help.", "No, I don't think that's right.", "Yes, absolutely, let's do it.", "Hmm, let me think about it.",
    "I'll be there in five minutes.", "The price of petrol went up again.", "Can you read me the news headlines?",
    "My favourite colour is blue.", "Who won the football match last night?", "I need to finish this report by Monday.",
    "Let me know when you're ready.", "That's a really good question.", "Write a short story about a dragon.",
    "Hello, hello, can you hear me?", "Hey, wait for me!", "Oh, I almost forgot about that.", "Good morning, everyone.",
    "Good night, sleep well.", "Can we talk about this later?", "Hand me the hammer, would you?", "The hopper was full of grain.",
    "Harry and Peter went camping.", "Happy hour starts at five.", "He's a big fan of hip hop.", "Are you happy with it?",
    "Pepper, salt and a little butter.", "I'm reading a paperback on the bus.", "There's a hippo at the zoo.",
)
HELPER_WORDS = ("hey", "hi", "okay", "hello", "hey there", "ok so")
_WHO = ("I", "We", "My brother", "The kids", "Our neighbour", "She", "He", "They", "My friend", "The teacher", "Everyone", "You")
_DO = ("really want to", "never", "usually", "might", "will probably", "can't wait to", "forgot to", "decided to", "have to", "tried to")
_WHAT = ("visit the museum on Sunday", "cook pasta for dinner", "watch the football tonight", "paint the kitchen yellow",
         "learn to play the piano", "fix the broken bicycle", "book a holiday in Spain", "call the doctor in the morning",
         "clean the garage this weekend", "buy a new laptop", "walk the dog after lunch", "finish the crossword puzzle",
         "plant tomatoes in the garden", "read that new mystery novel", "go swimming before work", "bake a chocolate cake")
_ASK = ("Can you tell me", "Do you know", "I wonder", "Could you check", "Please find out", "Show me")
_TOPIC = ("how far away the moon is", "when the shop closes", "what the capital of Australia is", "who wrote this song",
          "how to make pancakes", "where the nearest station is", "why the sky is blue", "what my next meeting is",
          "how much a flight to Rome costs", "which team is top of the league")


HEY_OTHERS = ("there", "you", "guys", "Jarvis", "Siri", "Google", "Alexa", "Cortana", "mum", "dad", "buddy", "Harry", "Peter",
              "Sarah", "Tom", "Emma", "Jack", "Lucy", "Sam", "Max", "Anna", "Mike", "Kate", "Ben", "Chloe", "Oliver", "Grace",
              "Leo", "Mia", "Noah", "Ella", "James", "Sophie", "Henry", "Ruby", "Arthur", "Isla", "Freddie", "Poppy", "Archie",
              "Friday", "Sage", "Harper", "Ava", "Andrew", "Emily", "what's up", "wait", "look", "listen", "hello", "everyone")


def negative_texts() -> list[str]:
    """A few hundred varied everyday sentences (fixed list plus combinations), so it learns what normal talk sounds like."""
    rng = random.Random(1234)
    made = {f"{rng.choice(_WHO)} {rng.choice(_DO)} {rng.choice(_WHAT)}." for _ in range(170)}
    made |= {f"{rng.choice(_ASK)} {rng.choice(_TOPIC)}?" for _ in range(50)}
    made |= {f"Hey {w}, {rng.choice(_ASK).lower()} {rng.choice(_TOPIC)}." for w in HEY_OTHERS}
    made |= {f"Hey {w}!" for w in HEY_OTHERS} | {f"Okay {w}." for w in HEY_OTHERS[:20]}
    return list(NEGATIVE_SENTENCES) + sorted(made)


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-") or "name"


def phrases_for(name: str) -> list[str]:
    n = name.strip()
    return [f"Hey {n}", f"{n}", f"Hey {n}!", f"Okay {n}", f"Hi {n}", f"{n}?", f"Hey, {n}."]


def sound_alikes(name: str) -> list[str]:
    """Pieces and near-misses of the name: things it must NOT wake up for."""
    n = re.sub(r"[^A-Za-z ]", "", name).strip() or name
    low = n.lower()
    out = [f"hey {low[: max(2, len(low) // 2)]}", low[: max(2, (len(low) + 1) // 2)], low[len(low) // 2:]]
    vowels = "aeiou"
    first = next((i for i, c in enumerate(low) if c in vowels), 0)
    for repl in ("b", "p", "t", "m"):
        if first > 0 and low[0] != repl:
            out.append(repl + low[first:])
    out.append(f"hey {low}s and")  # embedded in a longer word stream
    return [o for o in dict.fromkeys(out) if o and o != low][:8]  # ordered, so training is the same every run


# ----------------------------------------------------------------------------- audio helpers
def resample(x: np.ndarray, rate_in: int, rate_out: int = RATE) -> np.ndarray:
    """Band-limited resampling by FFT (clips are short, so this is fast and alias-free)."""
    x = np.asarray(x, dtype=np.float32)
    if rate_in == rate_out or x.size == 0:
        return x
    n_out = int(round(x.size * rate_out / rate_in))
    spec = np.fft.rfft(x)
    keep = n_out // 2 + 1
    if keep <= spec.size:
        spec = spec[:keep]
    else:
        spec = np.concatenate([spec, np.zeros(keep - spec.size, spec.dtype)])
    return (np.fft.irfft(spec, n_out) * (n_out / x.size)).astype(np.float32)


def speech_bounds(x: np.ndarray, frame: int = 160) -> tuple[int, int]:
    """Start and end sample of the spoken part (energy above a fraction of the loudest frame)."""
    if x.size < frame:
        return 0, x.size
    frames = x[: x.size // frame * frame].reshape(-1, frame)
    energy = np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1))
    level = max(energy.max() * 0.08, 30.0)
    loud = np.nonzero(energy > level)[0]
    if loud.size == 0:
        return 0, x.size
    return int(loud[0] * frame), int((loud[-1] + 1) * frame)


def normalise(x: np.ndarray, peak: float = 12000.0) -> np.ndarray:
    m = float(np.max(np.abs(x))) if x.size else 0.0
    return (x * (peak / m)).astype(np.float32) if m > 1e-6 else x.astype(np.float32)


def _noise(n: int, rng: np.random.Generator, kind: str) -> np.ndarray:
    white = rng.standard_normal(n).astype(np.float32)
    if kind == "white":
        return white
    spec = np.fft.rfft(white)
    f = np.arange(spec.size) + 1.0
    spec = spec / (np.sqrt(f) if kind == "pink" else f)
    out = np.fft.irfft(spec, n).astype(np.float32)
    return out / (np.std(out) + 1e-9)


def augment(x: np.ndarray, rng: np.random.Generator, babble: Sequence[np.ndarray] = ()) -> np.ndarray:
    """One random 'room': level, tone, echo and background noise."""
    y = x.astype(np.float32).copy()
    if rng.random() < 0.5:  # tone: simple first-order tilt (thin laptop mic vs boomy headset)
        a = rng.uniform(-0.9, 0.9)
        y[1:] = y[1:] - a * y[:-1]
    if rng.random() < 0.5:  # echo: a short decaying noise impulse response
        length = int(rng.uniform(0.05, 0.35) * RATE)
        ir = rng.standard_normal(length).astype(np.float32) * np.exp(-np.linspace(0, rng.uniform(4, 9), length)).astype(np.float32)
        ir[0] = 1.0
        y = np.convolve(y, ir * rng.uniform(0.15, 0.6), mode="full")[: y.size].astype(np.float32) + y * 0.5
    y = normalise(y, peak=rng.uniform(3000, 22000))
    if rng.random() < 0.8:  # background noise or chatter at 3..30 dB SNR
        if babble and rng.random() < 0.35:
            src = babble[int(rng.integers(len(babble)))]
            reps = int(np.ceil(y.size / max(1, src.size)))
            noise = np.tile(src, reps)[: y.size].astype(np.float32)
            noise = noise / (np.std(noise) + 1e-9)
        else:
            noise = _noise(y.size, rng, str(rng.choice(["white", "pink", "brown"])))
        snr = rng.uniform(3, 30)
        speech_rms = np.sqrt(np.mean(y ** 2)) + 1e-9
        y = y + noise * speech_rms / (10 ** (snr / 20))
    return np.clip(y, -32000, 32000).astype(np.float32)


def place(x: np.ndarray, rng: np.random.Generator, before: tuple[float, float] = (2.0, 2.8),
          after: tuple[float, float] = (0.45, 0.9)) -> tuple[np.ndarray, int]:
    """Pad a phrase with quiet before/after; returns the clip and where the phrase ends.

    The lead-in must be long: one classifier window needs ~2 s of audio (16 embeddings, each
    looking back 0.76 s), which the live detector always has.
    """
    start, end = speech_bounds(x)
    core = x[start:end]
    pre = np.zeros(int(rng.uniform(*before) * RATE), np.float32)
    post = np.zeros(int(rng.uniform(*after) * RATE), np.float32)
    floor = rng.uniform(5, 60)
    clip = np.concatenate([pre, core, post])
    clip = clip + rng.standard_normal(clip.size).astype(np.float32) * floor
    return clip.astype(np.float32), pre.size + core.size


# ----------------------------------------------------------------------------- features
class FeatureExtractor:
    """Audio -> (T, 96) speech embeddings, one per 80 ms, exactly like the live detector."""

    def __init__(self, directory: Path | None = None) -> None:
        import onnxruntime as ort

        from .wakeword import model_dir

        directory = directory or model_dir()
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2
        opts.log_severity_level = 3
        self._mel = ort.InferenceSession(str(directory / "melspectrogram.onnx"), sess_options=opts, providers=["CPUExecutionProvider"])
        self._emb = ort.InferenceSession(str(directory / "embedding_model.onnx"), sess_options=opts, providers=["CPUExecutionProvider"])

    def __call__(self, audio: np.ndarray) -> np.ndarray:
        audio = np.concatenate([np.zeros(480, np.float32), np.asarray(audio, dtype=np.float32)])
        mel = np.squeeze(self._mel.run(None, {"input": audio[None, :]})[0]) / 10.0 + 2.0
        if mel.ndim != 2 or mel.shape[0] < 76:
            return np.zeros((0, 96), np.float32)
        windows = np.stack([mel[i: i + 76] for i in range(0, mel.shape[0] - 76 + 1, 8)])
        out = []
        for i in range(0, len(windows), 64):
            out.append(self._emb.run(None, {"input_1": windows[i: i + 64, :, :, None].astype(np.float32)})[0].reshape(-1, 96))
        return np.concatenate(out).astype(np.float32)


def end_frame(end_sample: int) -> int:
    """Index of the embedding whose window ends just after ``end_sample``."""
    # embedding t covers mel frames [8t, 8t + 76) -> audio up to roughly (8t + 76) * 160 samples
    return max(0, int(np.ceil((end_sample / 160.0 - 76) / 8.0)))


def windows_of(feats: np.ndarray) -> np.ndarray:
    if len(feats) < WINDOW:
        return np.zeros((0, WINDOW, 96), np.float32)
    return np.stack([feats[t - WINDOW + 1: t + 1] for t in range(WINDOW - 1, len(feats))])


def positive_windows(feats: np.ndarray, end_sample: int) -> tuple[np.ndarray, np.ndarray]:
    """Windows ending right after the phrase (should fire) and windows before it started (should not)."""
    e = end_frame(end_sample)
    pos = [feats[t - WINDOW + 1: t + 1] for t in range(e, e + 4) if WINDOW - 1 <= t < len(feats)]
    neg = [feats[t - WINDOW + 1: t + 1] for t in range(WINDOW - 1, max(WINDOW - 1, e - 9))]
    z = np.zeros((0, WINDOW, 96), np.float32)
    return (np.stack(pos) if pos else z), (np.stack(neg) if neg else z)


# ----------------------------------------------------------------------------- the classifier
class TinyNet:
    """1536 -> hidden (ReLU) -> 1 (sigmoid), trained with Adam on weighted cross-entropy."""

    def __init__(self, hidden: int = 48, seed: int = 0) -> None:
        self.hidden = hidden
        self.rng = np.random.default_rng(seed)
        self.params: dict[str, np.ndarray] = {}
        self.mean = np.zeros(WINDOW * 96, np.float32)
        self.std = np.ones(WINDOW * 96, np.float32)

    def _init(self, dim: int) -> None:
        self.params = {
            "w1": (self.rng.standard_normal((dim, self.hidden)) * np.sqrt(2.0 / dim)).astype(np.float32),
            "b1": np.zeros(self.hidden, np.float32),
            "w2": (self.rng.standard_normal((self.hidden, 1)) * np.sqrt(1.0 / self.hidden)).astype(np.float32),
            "b2": np.zeros(1, np.float32),
        }

    def _forward(self, x: np.ndarray, drop: float = 0.0):
        h = np.maximum(0.0, x @ self.params["w1"] + self.params["b1"])
        mask = None
        if drop:
            mask = (self.rng.random(h.shape) >= drop).astype(np.float32) / (1 - drop)
            h = h * mask
        z = (h @ self.params["w2"] + self.params["b2"]).ravel()
        return h, mask, z

    def predict(self, windows: np.ndarray) -> np.ndarray:
        x = (windows.reshape(len(windows), -1) - self.mean) / self.std
        _, _, z = self._forward(x.astype(np.float32))
        return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))

    def fit(self, x_pos: np.ndarray, x_neg: np.ndarray, neg_weight: np.ndarray | None = None, epochs: int = 30,
            lr: float = 1e-3, batch: int = 256, decay: float = 2e-4, cancel=None) -> None:
        x = np.concatenate([x_pos, x_neg]).reshape(len(x_pos) + len(x_neg), -1).astype(np.float32)
        y = np.concatenate([np.ones(len(x_pos)), np.zeros(len(x_neg))]).astype(np.float32)
        w = np.concatenate([np.full(len(x_pos), len(x_neg) / max(1, len(x_pos))),
                            neg_weight if neg_weight is not None else np.ones(len(x_neg))]).astype(np.float32)
        w = w / w.mean()
        self.mean = x.mean(axis=0)
        self.std = x.std(axis=0) + 1e-3
        x = (x - self.mean) / self.std
        if not self.params:
            self._init(x.shape[1])
        m = {k: np.zeros_like(v) for k, v in self.params.items()}
        v = {k: np.zeros_like(p) for k, p in self.params.items()}
        step = 0
        for _epoch in range(epochs):
            order = self.rng.permutation(len(x))
            for i in range(0, len(x), batch):
                if cancel is not None and cancel.is_set():
                    return
                idx = order[i: i + batch]
                xb, yb, wb = x[idx], y[idx], w[idx]
                h, mask, z = self._forward(xb, drop=0.15)
                p = 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))
                dz = ((p - yb) * wb / len(idx))[:, None].astype(np.float32)
                grads = {"w2": h.T @ dz, "b2": dz.sum(axis=0)}
                dh = (dz @ self.params["w2"].T) * (h > 0)
                if mask is not None:
                    dh = dh * mask
                grads["w1"] = xb.T @ dh
                grads["b1"] = dh.sum(axis=0)
                step += 1
                for k in self.params:
                    g = grads[k] + decay * self.params[k] * (k.startswith("w"))
                    m[k] = 0.9 * m[k] + 0.1 * g
                    v[k] = 0.999 * v[k] + 0.001 * g * g
                    mh = m[k] / (1 - 0.9 ** step)
                    vh = v[k] / (1 - 0.999 ** step)
                    self.params[k] = (self.params[k] - lr * mh / (np.sqrt(vh) + 1e-8)).astype(np.float32)


@dataclass
class WakeModel:
    """A trained wake word, ready for the live detector."""

    name: str
    phrase: str
    net: TinyNet
    threshold: float = 0.6
    metrics: dict = field(default_factory=dict)
    created: float = field(default_factory=time.time)
    user_samples: int = 0

    def score(self, window: np.ndarray) -> float:
        return float(self.net.predict(window[None])[0])

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        info = {"format": FORMAT_VERSION, "name": self.name, "phrase": self.phrase, "threshold": self.threshold,
                "metrics": self.metrics, "created": self.created, "user_samples": self.user_samples, "hidden": self.net.hidden}
        tmp = path.with_suffix(".tmp.npz")
        np.savez_compressed(tmp, info=np.frombuffer(json.dumps(info).encode(), dtype=np.uint8), mean=self.net.mean,
                            std=self.net.std, **{k: v for k, v in self.net.params.items()})
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> "WakeModel":
        with np.load(path) as data:
            info = json.loads(bytes(data["info"]).decode())
            if info.get("format") != FORMAT_VERSION:
                raise ValueError("wake-word model from an older version")
            net = TinyNet(hidden=int(info.get("hidden", 48)))
            net.mean, net.std = data["mean"], data["std"]
            net.params = {k: data[k] for k in ("w1", "b1", "w2", "b2")}
        return cls(name=info["name"], phrase=info["phrase"], net=net, threshold=float(info["threshold"]),
                   metrics=info.get("metrics") or {}, created=float(info.get("created") or 0), user_samples=int(info.get("user_samples") or 0))


# ----------------------------------------------------------------------------- training
class Cancelled(Exception):
    pass


@dataclass
class Trainer:
    """Builds the training data and trains a WakeModel for one name."""

    synth: Synth
    voices: Sequence[str]
    cache_dir: Path
    extractor: Callable[[np.ndarray], np.ndarray] | None = None
    progress: Progress = lambda label, frac: None
    cancel: object = None
    seed: int = 7
    takes_per_voice: int = 3
    augment_copies: int = 3
    negative_texts: Sequence[str] | None = None  # defaults to negative_texts(); smaller in tests

    def _check(self) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise Cancelled()

    @property
    def features(self) -> Callable[[np.ndarray], np.ndarray]:
        if self.extractor is None:
            self.extractor = FeatureExtractor()
        return self.extractor

    def _synth_all(self, items: list[tuple[str, str, int, int]], label: str, lo: float, hi: float) -> list:
        out: list = []
        step = 24
        for i in range(0, len(items), step):
            self._check()
            out += self.synth(items[i: i + step])
            self.progress(label, lo + (hi - lo) * min(1.0, (i + step) / max(1, len(items))))
        return out

    # -- negatives (cached, shared by every name) --------------------------
    def negatives(self) -> tuple[list[np.ndarray], list[np.ndarray], list[str]]:
        """Embedding sequences of non-wake speech/noise, and a little raw speech for 'babble' noise."""
        path = self.cache_dir / "negatives.npz"
        try:
            with np.load(path, allow_pickle=False) as data:
                if int(data["version"]) == NEGATIVES_VERSION:
                    lengths = data["lengths"]
                    flat = data["feats"].astype(np.float32)
                    seqs = np.split(flat, np.cumsum(lengths)[:-1])
                    babble = [data["babble"].astype(np.float32)]
                    texts = [str(t) for t in data["texts"]]
                    return list(seqs), babble, texts
        except (OSError, KeyError, ValueError):
            pass
        rng = random.Random(self.seed)
        voices = list(self.voices)
        texts = list(self.negative_texts) if self.negative_texts is not None else negative_texts()
        items = [(s, voices[(i * 7 + k * 3) % len(voices)], rng.choice([-10, 0, 10]), rng.choice([-4, 0, 4]))
                 for i, s in enumerate(texts) for k in range(2 if i < len(NEGATIVE_SENTENCES) else 1)]
        items += [(w, rng.choice(voices), 0, 0) for w in HELPER_WORDS for _ in range(2)]
        audio = self._synth_all(items, "Learning what everyday speech sounds like", 0.0, 0.3)
        nrng = np.random.default_rng(self.seed)
        seqs, babble, texts = [], [], []
        for (text, *_), a in zip(items, audio):
            if a is None or a.size < RATE // 4:
                continue
            babble.append(a)
            for clean in (True, False):
                clip, _ = place(a, nrng, before=(1.8, 2.4), after=(0.3, 0.8))
                seqs.append(self.features(clip if clean else augment(clip, nrng)))
                texts.append(text)
        for kind in ("white", "pink", "brown"):  # noise and near-silence
            for level in (20, 300, 3000):
                seqs.append(self.features(_noise(RATE * 3, nrng, kind) * level))
                texts.append("")
        if not seqs:
            raise RuntimeError("Couldn't generate any speech samples (is the internet connection working?)")
        lengths = np.array([len(s) for s in seqs])
        mix = np.concatenate(babble)[: RATE * 60] if babble else np.zeros(RATE, np.float32)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp.npz")
        np.savez_compressed(tmp, version=NEGATIVES_VERSION, lengths=lengths, feats=np.concatenate(seqs).astype(np.float16),
                            babble=mix.astype(np.float16), texts=np.array(texts))
        tmp.replace(path)
        return seqs, [mix], texts

    def _samples(self, name: str, items: list[tuple[str, str, int, int]], label: str, lo: float, hi: float) -> list:
        """Synthesised clips for this name, cached so retraining (e.g. with the user's voice) is quick and offline."""
        path = self.cache_dir / f"{slug(name)}-samples.npz"
        keys = ["|".join(map(str, it)) for it in items]
        try:
            with np.load(path, allow_pickle=False) as data:
                cached = dict(zip((str(k) for k in data["keys"]), np.split(data["audio"], np.cumsum(data["lengths"])[:-1])))
            if all(k in cached for k in keys):
                self.progress(label, hi)
                return [cached[k].astype(np.float32) if cached[k].size else None for k in keys]
        except (OSError, KeyError, ValueError):
            pass
        audio = self._synth_all(items, label, lo, hi)
        clips = [(a if a is not None else np.zeros(0, np.float32)).astype(np.int16) for a in audio]
        if sum(1 for c in clips if c.size) >= len(clips) // 2:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp.npz")
            np.savez_compressed(tmp, keys=np.array(keys), lengths=np.array([c.size for c in clips]),
                                audio=np.concatenate(clips) if clips else np.zeros(0, np.int16))
            tmp.replace(path)
        return audio

    # -- one name ----------------------------------------------------------
    def train(self, name: str, user_samples: Sequence[np.ndarray] = ()) -> WakeModel:
        started = time.time()
        rng = random.Random(f"{self.seed}-{name}")
        nrng = np.random.default_rng(int(hashlib.md5(f"{self.seed}-{name}".encode()).hexdigest()[:8], 16))  # same every run
        neg_seqs, babble, neg_texts = self.negatives()
        word = re.compile(rf"\b{re.escape(name.lower())}\b")
        neg_seqs = [q for q, t in zip(neg_seqs, neg_texts) if not word.search(t.lower())]  # "Hey Harper!" is not a negative for Harper
        self._check()

        voices = list(self.voices)
        rng.shuffle(voices)
        held = voices[: max(2, len(voices) // 5)]  # never trained on: used to set the threshold honestly
        train_voices = voices[len(held):] or voices
        phrases = phrases_for(name)
        items, meta = [], []
        for v in voices:
            for k in range(self.takes_per_voice if v in train_voices else 2):
                items.append((rng.choice(phrases if k else phrases[:2]), v, rng.choice([-15, -5, 0, 5, 15]), rng.choice([-6, -2, 0, 2, 6])))
                meta.append(v in held)
        alikes = [(a, rng.choice(voices), 0, 0) for a in sound_alikes(name) for _ in range(3)]
        audio = self._samples(name, items + alikes, f"Learning to hear “{phrases[0]}”", 0.3, 0.75)
        pos_audio, alike_audio = audio[: len(items)], audio[len(items):]

        x_pos, x_neg, val_pos = [], [], []
        for a, is_held in zip(pos_audio, meta):
            self._check()
            if a is None or a.size < RATE // 8:
                continue
            copies = 1 if is_held else 1 + self.augment_copies
            for c in range(copies):
                clip, end = place(a, nrng)
                if c or is_held:
                    clip = augment(clip, nrng, babble)
                p, n = positive_windows(self.features(clip), end)
                if is_held:
                    val_pos.append(p)
                else:
                    x_pos.append(p)
                    x_neg.append(n)
        for sample in user_samples:  # the user's own voice counts a lot
            for c in range(16):
                clip, end = place(normalise(sample), nrng)
                p, n = positive_windows(self.features(augment(clip, nrng, babble) if c else clip), end)
                x_pos.append(p)
                x_neg.append(n)
        for a in alike_audio:
            if a is None:
                continue
            clip, _ = place(a, nrng)
            x_neg.append(windows_of(self.features(augment(clip, nrng, babble))))
        self.progress("Training the listener", 0.8)

        neg_windows = [windows_of(s) for s in neg_seqs]
        split = max(1, len(neg_windows) // 5)
        order = list(range(len(neg_windows)))
        random.Random(self.seed).shuffle(order)
        val_neg_seqs = [neg_seqs[i] for i in order[:split]]
        train_neg = [neg_windows[i] for i in order[split:]] + x_neg
        pos = np.concatenate([p for p in x_pos if len(p)])
        neg = np.concatenate([n for n in train_neg if len(n)])
        if len(neg) > MAX_NEGATIVE_WINDOWS:  # keep memory modest; the name-specific near-misses always stay
            own = np.concatenate([n for n in x_neg if len(n)]) if any(len(n) for n in x_neg) else neg[:0]
            general = np.concatenate([n for n in train_neg[: len(train_neg) - len(x_neg)] if len(n)])
            keep = np.random.default_rng(self.seed).choice(len(general), max(0, MAX_NEGATIVE_WINDOWS - len(own)), replace=False)
            neg = np.concatenate([own, general[np.sort(keep)]])
        if len(pos) < 20:
            raise RuntimeError("Not enough speech samples to learn the name")

        net = TinyNet(seed=self.seed)
        net.fit(pos, neg, epochs=18, cancel=self.cancel)
        self._check()
        # hard-negative mining: whatever still looks like the name gets extra weight in a second round
        scores = net.predict(neg)
        weight = np.where(scores > 0.2, 6.0, 1.0)
        net.fit(pos, neg, neg_weight=weight, epochs=10, lr=5e-4, cancel=self.cancel)
        self._check()
        scores = net.predict(neg)
        weight = np.where(scores > 0.5, 12.0, np.where(scores > 0.1, 3.0, 1.0))
        net.fit(pos, neg, neg_weight=weight, epochs=6, lr=3e-4, cancel=self.cancel)
        self._check()
        self.progress("Checking it on voices it hasn't heard", 0.93)

        threshold, metrics = calibrate(net, val_pos, val_neg_seqs)
        metrics.update({"train_windows_pos": int(len(pos)), "train_windows_neg": int(len(neg)), "voices": len(voices),
                        "held_out_voices": len(held), "seconds": round(time.time() - started, 1)})
        model = WakeModel(name=name, phrase=phrases[0], net=net, threshold=threshold, metrics=metrics,
                          user_samples=len(user_samples))
        self.progress("Ready", 1.0)
        log.info("Wake word %r trained: %s", name, metrics)
        return model


def streaming_hits(net: TinyNet, seq: np.ndarray, threshold: float, persistence: int = PERSISTENCE, cooldown: int = 19) -> int:
    """How many times the live detector would have fired on this stretch of audio."""
    w = windows_of(seq)
    if not len(w):
        return 0
    scores = net.predict(w)
    hits = run = 0
    last = -10 ** 9
    for i, s in enumerate(scores):
        run = run + 1 if s >= threshold else 0
        if run >= persistence and i - last > cooldown:
            hits += 1
            last = i
            run = 0
    return hits


def _run(flags: np.ndarray) -> int:
    best = cur = 0
    for f in flags:
        cur = cur + 1 if f else 0
        best = max(best, cur)
    return best


def calibrate(net: TinyNet, val_pos: list[np.ndarray], val_neg_seqs: list[np.ndarray]) -> tuple[float, dict]:
    """Pick the strictest threshold that still catches (nearly) every held-out voice without false wakes.

    Strict is safer in a real room, so among thresholds with no false wakes on held-out everyday
    speech we take the highest one whose recall is within 3 points of the best.
    """
    neg_minutes = sum(len(s) for s in val_neg_seqs) * 0.08 / 60
    pos_scores = [net.predict(p) for p in val_pos if len(p)]

    def recall_at(t: float) -> float:
        return sum(1 for s in pos_scores if _run(s >= t) >= PERSISTENCE) / max(1, len(pos_scores))

    clean, steps = [], (0.99, 0.98, 0.97, *np.arange(0.95, 0.49, -0.05))
    for t in steps:
        if sum(streaming_hits(net, s, t) for s in val_neg_seqs):
            break
        clean.append((float(round(t, 2)), recall_at(t)))
    if 2 < len(clean) < len(steps):  # it started waking for everyday speech just below: keep a notch of margin
        clean = clean[:-1]
    if clean:
        top = max(r for _, r in clean)
        threshold = max(t for t, r in clean if r >= top - 0.03)
    else:
        threshold = 0.99
    false = sum(streaming_hits(net, s, threshold) for s in val_neg_seqs)
    return threshold, {"held_out_recall": round(recall_at(threshold), 3), "false_wakes": int(false),
                       "negative_minutes": round(neg_minutes, 1), "held_out_clips": len(pos_scores), "threshold": threshold}


# ----------------------------------------------------------------------------- storage
def model_path(directory: Path, name: str) -> Path:
    return directory / f"{slug(name)}.npz"


def load_model(directory: Path, name: str) -> WakeModel | None:
    path = model_path(directory, name)
    if not path.is_file():
        return None
    try:
        return WakeModel.load(path)
    except Exception as exc:
        log.info("Ignoring wake-word model %s: %s", path.name, exc)
        return None


def english_voices(all_voices: Iterable[dict]) -> list[str]:
    """Every English (and multilingual) neural voice Edge offers, for variety."""
    names = []
    for v in all_voices or []:
        short = v.get("ShortName") or v.get("id") or v.get("Name") or ""
        locale = v.get("Locale") or short[:5]
        if locale.startswith("en-") or "Multilingual" in short and short.startswith(("en-", "fr-", "de-", "es-", "it-", "pt-")):
            names.append(short)
    return sorted(set(names))
