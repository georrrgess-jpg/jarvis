"""Real-voice check of learned wake words (run by the Windows CI job, which can reach Microsoft's voice service).

Trains "Harper" and "Sage" exactly as the app does, then tests them on voices and sentences they never
saw: does "Hey Harper" wake it, and does ordinary speech leave it alone? Also runs the real live
listener over a stream of chatter with the name mixed in.

    python tests/windows_wake_check.py report.json
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

UNSEEN_SENTENCES = [
    "Could you turn on the kitchen lights?", "I think it's going to rain this afternoon.", "Hey, what are you doing this weekend?",
    "My sister is coming over for dinner.", "Let's watch a film tonight.", "Where is the nearest petrol station?",
    "Hey Jarvis, set an alarm for seven.", "I'd like a cup of tea, please.", "The bus leaves at half past eight.",
    "Happy anniversary, darling.", "Can you help me with my homework?", "Okay, I'll call you back later.",
    "That restaurant was absolutely amazing.", "Hey Peter, over here!", "Please read me the latest news.",
    "We're out of milk again.", "He parked the car by the harbour.", "Hand me that hammer, would you?",
    "She is reading a paperback on the train.", "It's Friday afternoon, nearly the weekend.", "Add a pinch of sage and pepper.",
    "Page twelve has the answer.", "Hi everyone, thanks for coming.", "Open the calculator app.",
]


def main() -> int:
    report_path = Path(sys.argv[1] if len(sys.argv) > 1 else "wake-check.json")
    from core.audio import AudioEngine
    from core.config import Config
    from core.tts import EdgeTTS
    from core.wakelearn import Trainer, augment, english_voices, place, streaming_hits
    from core.wakeword import RATE, WakeListener, WakeWordDetector
    from core.wakewords import WakeWords

    work = Path(tempfile.mkdtemp(prefix="jarvis-wake-"))
    tts = EdgeTTS(Config(work / "config.json"), cache_dir=None)
    audio = AudioEngine()
    audio.init()
    ww = WakeWords(work / "wakewords", tts, audio, lambda kind, **p: None)
    voices = english_voices({"ShortName": v["id"], "Locale": v.get("locale")} for v in tts.list_voices())
    report: dict = {"voices": len(voices), "names": {}}
    failures: list[str] = []
    print(f"{len(voices)} English voices available", flush=True)
    if len(voices) < 20:
        report["ok"] = False
        report["error"] = "voice list unavailable"
        report_path.write_text(json.dumps(report, indent=2))
        print("FAILED: voice list unavailable", flush=True)
        return 1
    test_voices = voices[3::7]  # never used for training
    train_voices = [v for v in voices if v not in test_voices]
    rng = np.random.default_rng(42)

    for name in ("Harper", "Sage"):
        started = time.time()
        trainer = Trainer(synth=ww._synth, voices=train_voices, cache_dir=work / "wakewords",
                          progress=lambda label, frac: None)
        model = trainer.train(name)
        seconds = round(time.time() - started, 1)
        feats = trainer.features

        positives = [(p, v, r) for v in test_voices for p in (f"Hey {name}", name, f"Okay {name}") for r in (0, 12)]
        pos_audio = ww._synth([(p, v, r, 0) for p, v, r in positives])
        caught = total = 0
        for a in pos_audio:
            if a is None:
                continue
            total += 1
            clip, _ = place(a, rng, after=(0.6, 1.0))
            caught += streaming_hits(model.net, feats(augment(clip, rng)), model.threshold) > 0
        neg_items = [(s, v, 0, 0) for s in UNSEEN_SENTENCES for v in test_voices[:4]]
        neg_audio = ww._synth(neg_items)
        false = []
        for (text, voice, *_), a in zip(neg_items, neg_audio):
            if a is None:
                continue
            clip, _ = place(a, rng, after=(0.4, 0.8))
            if streaming_hits(model.net, feats(augment(clip, rng)), model.threshold):
                false.append(f"{text} ({voice})")
        recall = caught / max(1, total)
        negatives = sum(1 for a in neg_audio if a is not None)

        # the real live listener on a stream of chatter with the name said three times
        chatter = [a for a in neg_audio[:12] if a is not None]
        calls = [a for a in pos_audio[:: max(1, len(pos_audio) // 3)][:3] if a is not None]
        pieces = []
        for i, c in enumerate(chatter):
            pieces += [c, np.zeros(int(0.6 * RATE), np.float32)]
            if i % 4 == 3 and calls:
                pieces += [calls.pop(0), np.zeros(int(2.5 * RATE), np.float32)]
        stream_audio = np.concatenate([np.zeros(2 * RATE, np.float32), *pieces, np.zeros(2 * RATE, np.float32)])
        stream_audio = np.clip(stream_audio + rng.normal(0, 40, stream_audio.size), -32000, 32000).astype(np.int16)
        fired = []

        class Stream:
            pos = 0

            def read(self, n):
                seg = stream_audio[self.pos: self.pos + n]
                self.pos += n
                time.sleep(0.002)
                return np.pad(seg, (0, n - seg.size)).astype(np.int16).tobytes()

            def close(self):
                pass

        listener = WakeListener(lambda score: fired.append(round(score, 2)), lambda: 0.5, stream_factory=Stream,
                                detector_factory=WakeWordDetector, words=lambda: (False, [model]))
        listener.COOLDOWN_S = 0.3
        listener.start()
        deadline = time.time() + 600
        while listener.running and Stream.pos < stream_audio.size and time.time() < deadline:
            time.sleep(0.2)
        listener.stop()

        result = {"threshold": model.threshold, "training_metrics": model.metrics, "train_seconds": seconds,
                  "unseen_voices": len(test_voices), "unseen_recall": round(recall, 3), "unseen_clips": total,
                  "unseen_sentences": negatives, "false_wakes": len(false), "false_examples": false[:8],
                  "live_stream_wakes": len(fired), "live_stream_calls": 3}
        report["names"][name] = result
        print(json.dumps({name: result}, indent=2), flush=True)
        if recall < 0.8:
            failures.append(f"{name}: recall {recall:.2f} on unseen voices")
        if len(false) > max(2, negatives // 30):
            failures.append(f"{name}: {len(false)} false wakes in {negatives} unseen sentences")

    report["ok"] = not failures
    report["failures"] = failures
    report_path.write_text(json.dumps(report, indent=2))
    tts.close()
    print("ALL WAKE-WORD CHECKS PASSED" if not failures else f"FAILED: {failures}", flush=True)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
