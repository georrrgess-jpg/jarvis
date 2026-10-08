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
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    import numpy as np

    from core.config import Config
    from core.stt import Capture, SpeechInput, STTError
    from core.weather import Weather, WeatherError, WeatherRequest, parse_weather

    report: dict = {"checks": {}}
    failures: list[str] = []

    def check(name, ok, detail=None):
        report["checks"][name] = {"ok": bool(ok), "detail": detail}
        print(("PASS " if ok else "FAIL ") + name + (f": {detail}" if detail is not None else ""), flush=True)
        if not ok:
            failures.append(name)

    config = Config(Path(tempfile.mkdtemp()) / "config.json")
    weather = Weather(config)

    def patient(fn):
        """Shared CI machines sometimes can't reach the free services for a few seconds: wait and ask again
        (only for "couldn't reach it" - a wrong answer still fails)."""
        for wait in (10, 20):
            try:
                return fn()
            except WeatherError as exc:
                if not exc.offline:
                    raise
                print(f"  (service unreachable, retrying in {wait}s: {exc})", flush=True)
                time.sleep(wait)
        return fn()

    def run(name, fn, ok, detail=lambda r: r):
        try:
            result = patient(fn)
            check(name, ok(result), detail(result))
            return result
        except Exception as exc:  # noqa: BLE001
            check(name, False, f"{exc.__class__.__name__}: {exc}")
            return None

    run("temperature in London", lambda: weather.answer(parse_weather("what's the temperature in London"), "sir"),
        lambda r: re.search(r"-?\d+ degrees? in London", r) is not None)
    run("rain in Tokyo tomorrow", lambda: weather.answer(parse_weather("is it going to rain in Tokyo tomorrow"), "sir"),
        lambda r: "Tokyo" in r and "tomorrow" in r)
    run("weather where this computer is (IP location)", lambda: weather.answer(WeatherRequest("now"), "sir"),
        lambda r: re.search(r"-?\d+ degrees?", r) is not None)
    for spoken, town in (("Bothell Washington", "Bothell"), ("Paris Texas", "Paris"), ("Ashford Kent", "Ashford"),
                         ("Little Snoring", "Little Snoring"), ("Tralee", "Tralee")):
        run(f"found the town: {spoken}", lambda spoken=spoken: weather.geocode(spoken), lambda p, town=town: town.lower() in p.name.lower(),
            lambda p: {"name": p.name, "lat": round(p.latitude, 2), "lon": round(p.longitude, 2), "country": p.country})
    run("found the postcode: 90210 (Beverly Hills, whatever the area is called)", lambda: weather.geocode("90210"),
        lambda p: 33.9 < p.latitude < 34.2 and -118.6 < p.longitude < -118.2,
        lambda p: {"name": p.name, "lat": round(p.latitude, 2), "lon": round(p.longitude, 2)})
    run("Paris Texas is in the USA, not France", lambda: weather.geocode("Paris Texas"), lambda p: p.longitude < -90, lambda p: p.longitude)
    run("weather for a small town", lambda: weather.answer(parse_weather("what's the weather in Bothell Washington"), "sir"),
        lambda r: "Bothell" in r and re.search(r"-?\d+ degrees?", r) is not None)
    run("weekend forecast", lambda: weather.answer(parse_weather("what's the forecast for this weekend in Paris"), "sir"),
        lambda r: "Saturday" in r and "Sunday" in r)

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
