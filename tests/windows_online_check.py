"""Checks against the real free services (run by the Windows CI job, which has internet access).

* Weather: Open-Meteo forecast + geocoding + IP location give a sensible spoken answer.
* Speech: Google's recogniser given audio with no words in it must come back empty, not crash
  (SpeechRecognition 3.11+ raises UnknownValueError there, which used to surface as "Internal error").

    python tests/windows_online_check.py report.json
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    import numpy as np

    from core.config import Config
    from core.stt import Capture, SpeechInput, STTError
    from core.weather import Weather, WeatherRequest, parse_weather

    report: dict = {"checks": {}}
    failures: list[str] = []

    def check(name, ok, detail=None):
        report["checks"][name] = {"ok": bool(ok), "detail": detail}
        print(("PASS " if ok else "FAIL ") + name + (f": {detail}" if detail is not None else ""), flush=True)
        if not ok:
            failures.append(name)

    config = Config(Path(tempfile.mkdtemp()) / "config.json")
    weather = Weather(config)
    try:
        london = weather.answer(parse_weather("what's the temperature in London"), "sir")
        check("temperature in London", re.search(r"-?\d+ degrees? in London", london) is not None, london)
        tomorrow = weather.answer(parse_weather("is it going to rain in Tokyo tomorrow"), "sir")
        check("rain in Tokyo tomorrow", "Tokyo" in tomorrow and "tomorrow" in tomorrow, tomorrow)
        here = weather.answer(WeatherRequest("now"), "sir")
        check("weather where this computer is (IP location)", re.search(r"-?\d+ degrees?", here) is not None, here)
        weekend = weather.answer(parse_weather("what's the forecast for this weekend in Paris"), "sir")
        check("weekend forecast", "Saturday" in weekend and "Sunday" in weekend, weekend)
    except Exception as exc:  # noqa: BLE001
        check("weather service", False, f"{exc.__class__.__name__}: {exc}")

    config.update({"stt_engine": "google", "auto_language": False})
    stt = SpeechInput(config)
    noise = np.random.default_rng(0).normal(0, 300, 16000 * 2).astype(np.int16).tobytes()
    try:
        text = stt.transcribe(Capture(noise, 16000))
        check("Google speech: no words -> empty transcript, no crash", text == "", repr(text))
    except STTError as exc:
        check("Google speech: no words -> empty transcript, no crash", "unreachable" in str(exc), f"service unreachable: {exc}")
    except Exception as exc:  # noqa: BLE001
        check("Google speech: no words -> empty transcript, no crash", False, f"{exc.__class__.__name__}: {exc}")

    report["ok"] = not failures
    Path(sys.argv[1] if len(sys.argv) > 1 else "online-check.json").write_text(json.dumps(report, indent=2))
    print("ALL ONLINE CHECKS PASSED" if not failures else f"FAILED: {failures}", flush=True)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
