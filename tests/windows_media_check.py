"""Real-Windows check of media control and the browser manager, through the real Assistant (run by the Windows CI job).

* Chrome (temporary profile) is started by JARVIS itself ("open chrome") and must be reported ready.
* A public site ("open wikipedia") must be confirmed open from the browser's own window / address bar.
* "open gmail" on a browser that isn't signed in must be recognised as needing a sign-in, not reported open.
* Two local pages play tones with Media Session titles; "what's playing", "pause the music", "resume",
  "skip forward 20 seconds" and "pause everything" must change what Windows itself reports.
* "play <song>" must find the real top YouTube result, open it, and confirm it is playing.

    python tests/windows_media_check.py report.json
"""

from __future__ import annotations

import http.server
import json
import math
import os
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PAGE = """<!doctype html><title>{title} - Media Page</title><h1>{title}</h1>
<audio id="a" src="tone{n}.wav" loop autoplay></audio>
<script>
navigator.mediaSession.metadata = new MediaMetadata({{title: '{title}', artist: '{artist}', album: 'CI'}});
document.getElementById('a').play().catch(e => document.title = 'blocked ' + e);
</script>"""


def tone(path: Path, freq: int, seconds: int = 90) -> None:
    rate = 16000
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(2000 * math.sin(2 * math.pi * freq * i / rate))) for i in range(rate * seconds)))


def main() -> int:
    report_path = Path(sys.argv[1] if len(sys.argv) > 1 else "media-check.json")
    import ctypes

    ctypes.windll.user32.SetProcessDPIAware()
    from core.assistant import Assistant
    from core.config import Config
    from core.screen import WindowsDesktop
    from tests.mock_ollama import MockOllama
    from tests.test_assistant import Events, FakeTTS

    report: dict = {"checks": {}}
    failures: list[str] = []

    def check(name, ok, detail=None):
        report["checks"][name] = {"ok": bool(ok), "detail": detail}
        print(("PASS " if ok else "FAIL ") + name + (f": {detail}" if detail is not None else ""), flush=True)
        if not ok:
            failures.append(name)

    work = Path(tempfile.mkdtemp(prefix="jarvis-media-"))
    site = work / "site"
    site.mkdir()
    for n, (title, artist, freq) in enumerate((("Jarvis Test Tune", "CI Orchestra", 330), ("Second Song", "CI Band", 440)), start=1):
        (site / f"p{n}.html").write_text(PAGE.format(title=title, artist=artist, n=n), encoding="utf-8")
        tone(site / f"tone{n}.wav", freq)
    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=str(site), **k)  # noqa: E731
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    os.environ["JARVIS_BROWSER_ARGS"] = (f"--user-data-dir={work / 'chrome'} --no-first-run --no-default-browser-check "
                                         "--autoplay-policy=no-user-gesture-required --disable-features=Translate")
    mock = MockOllama().start()
    config = Config(work / "config.json")
    config.update({"ollama_host": mock.url, "voice_enabled": False, "wake_word": False, "memory_auto_learn": False, "link_browser": "chrome"})
    events = Events()
    assistant = Assistant(config, events, tts=FakeTTS(), desktop=WindowsDesktop())

    def say(text, timeout=90):
        n = len(events.of("assistant_end"))
        assistant.submit_text(text)
        events.wait_for(lambda: len(events.of("assistant_end")) == n + 1 and events.states()[-1] == "IDLE", timeout)
        end = events.of("assistant_end")[-1]
        reply = "".join(t["text"] for t in events.of("assistant_token") if t["id"] == end["id"])
        print(f"  > {text!r} -> {reply!r}", flush=True)
        return reply

    def windows_status(title):
        found = [s for s in assistant.media.sessions() if s.title == title]
        return found[0].status if found else None

    try:
        assistant.start()
        caps = assistant.winhelper.status()
        check("Windows helper: media sessions and address bar available", caps["caps"].get("media") and caps["caps"].get("uia"), caps)

        reply = say("open chrome")
        check("'open chrome' starts Chrome and reports it ready", "is open and ready" in reply or "is up" in reply, reply)
        reply = say("open chrome")
        check("'open chrome' again reuses it (no second launch)", "is up" in reply, reply)

        reply = say("open wikipedia")
        check("a public site is confirmed open from the browser itself", reply == "Wikipedia is open, sir.", reply)
        look = assistant.browser.look()
        check("the address bar is read", "wikipedia.org" in look.url, {"url": look.url, "title": look.title})

        reply = say("open gmail")
        check("Gmail on a signed-out browser: sign-in recognised, not reported open",
              "sign in" in reply and "is open" not in reply, {"reply": reply, "state": assistant.browser.state})
        if assistant._sign_in_wait is not None:
            assistant._sign_in_wait.set()

        nav = assistant.browser.open(f"{base}/p1.html", expect_title="Jarvis Test Tune")
        check("local media page opened (verified)", nav.ok and nav.verified, nav.to_dict())
        for _ in range(40):
            if windows_status("Jarvis Test Tune") == "playing":
                break
            time.sleep(0.25)
        reply = say("what's playing")
        check("'what's playing' reads Windows' media session", "Jarvis Test Tune by CI Orchestra" in reply, reply)
        reply = say("pause the music")
        check("'pause the music' pauses it (Windows reports paused)", reply.startswith("Paused") and windows_status("Jarvis Test Tune") == "paused",
              {"reply": reply, "status": windows_status("Jarvis Test Tune")})
        reply = say("resume")
        check("'resume' plays it again (Windows reports playing)", reply.startswith("Resuming") and windows_status("Jarvis Test Tune") == "playing",
              {"reply": reply, "status": windows_status("Jarvis Test Tune")})
        before = next((s.position for s in assistant.media.sessions() if s.title == "Jarvis Test Tune"), None)
        reply = say("skip forward 20 seconds")
        after = next((s.position for s in assistant.media.sessions() if s.title == "Jarvis Test Tune"), None)
        check("'skip forward 20 seconds' moves the position", before is not None and after is not None and after >= before + 15,
              {"reply": reply, "before": before, "after": after})

        nav = assistant.browser.open(f"{base}/p2.html", expect_title="Second Song")
        for _ in range(40):
            if windows_status("Second Song") == "playing":
                break
            time.sleep(0.25)
        both = {t: windows_status(t) for t in ("Jarvis Test Tune", "Second Song")}
        reply = say("pause everything")
        after_all = {t: windows_status(t) for t in ("Jarvis Test Tune", "Second Song")}
        check("'pause everything' pauses every player", set(after_all.values()) == {"paused"}, {"before": both, "after": after_all, "reply": reply})

        reply = say("play Rick Astley Never Gonna Give You Up")
        playing = [s.to_dict() for s in assistant.media.sessions() if s.playing]
        check("'play <song>' opens the top YouTube video and confirms it's playing", reply.startswith("Playing") and "YouTube" in reply,
              {"reply": reply, "playing": playing})
        reply = say("pause it")
        check("'pause it' pauses the video just started", reply.startswith("Paused"), reply)
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        check("no unexpected errors", False, f"{exc.__class__.__name__}: {exc}")
    finally:
        report["activity"] = list(assistant._activities)
        report["browser"] = assistant.browser.status()
        assistant.shutdown()
        mock.stop()
        subprocess.run(["taskkill", "/F", "/IM", "chrome.exe"], capture_output=True)
        report["ok"] = not failures
        report_path.write_text(json.dumps(report, indent=2, default=str))
    print("ALL MEDIA AND BROWSER CHECKS PASSED" if not failures else f"FAILED: {failures}", flush=True)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
