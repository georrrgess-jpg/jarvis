"""Quick real-Windows probe of the Windows helper (media sessions, app volumes, browser address bar).

Starts Chrome (temporary profile) on a local page that plays a tone and sets Media Session metadata, then
asks the helper what Windows reports and pauses / resumes it. Prints everything; exits 1 if a core part fails.

    python tests/windows_helper_probe.py
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PAGE = """<!doctype html><title>Jarvis Test Tune - Media Page</title>
<audio id="a" src="tone.wav" loop autoplay></audio>
<script>
navigator.mediaSession.metadata = new MediaMetadata({title: 'Jarvis Test Tune', artist: 'CI Orchestra', album: 'Probe'});
document.getElementById('a').play().catch(e => document.title = 'blocked ' + e);
</script>"""


def make_tone(path: Path, seconds: int = 40) -> None:
    rate = 22050
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(2500 * math.sin(2 * math.pi * 330 * i / rate))) for i in range(rate * seconds)))


def serve(folder: Path) -> int:
    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=str(folder), **k)  # noqa: E731
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server.server_address[1]


def find_window(fragment: str) -> int | None:
    found = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        n = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, n + 1)
        if fragment in buf.value and ctypes.windll.user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    ctypes.windll.user32.EnumWindows(cb, 0)
    return found[0] if found else None


def main() -> int:
    from core.winhelper import WinHelper

    failures = []
    helper = WinHelper()
    started = time.monotonic()
    ok = helper.available()
    print("helper available:", ok, round(time.monotonic() - started, 1), "s", helper.status(), flush=True)
    if not ok:
        return 1
    print("ping:", helper.call("ping"), flush=True)
    print("sessions before:", helper.call("media.sessions"), flush=True)
    print("audio before:", helper.call("audio.sessions"), flush=True)

    folder = Path(tempfile.mkdtemp(prefix="jarvis-probe-"))
    (folder / "index.html").write_text(PAGE, encoding="utf-8")
    make_tone(folder / "tone.wav")
    port = serve(folder)
    chrome = next((p for p in (Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
                               Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe") if p.is_file()), None)
    print("chrome:", chrome, flush=True)
    proc = subprocess.Popen([str(chrome), f"--user-data-dir={tempfile.mkdtemp(prefix='jarvis-chrome-')}", "--no-first-run",
                             "--no-default-browser-check", "--autoplay-policy=no-user-gesture-required", "--new-window",
                             f"http://127.0.0.1:{port}/index.html"])
    try:
        session = None
        for _ in range(40):
            time.sleep(0.5)
            r = helper.call("media.sessions")
            chromes = [s for s in r.get("sessions") or [] if "chrome" in str(s.get("app")).lower()]
            if chromes:
                session = chromes[0]
                if session.get("title"):
                    break
        print("chrome window:", find_window("Jarvis Test Tune"), flush=True)
        print("session:", json.dumps(session), flush=True)
        if not session:
            failures.append("no Chrome media session")
        else:
            for action, want in (("pause", "Paused"), ("play", "Playing"), ("toggle", "Paused"), ("toggle", "Playing")):
                r = helper.call("media.control", app=session["app"], title=session.get("title"), index=session.get("index"), action=action)
                status = None
                for _ in range(20):
                    time.sleep(0.2)
                    now = [s for s in helper.call("media.sessions").get("sessions") or [] if s.get("app") == session["app"]]
                    status = now[0]["status"] if now else None
                    if status == want:
                        break
                print(f"{action}: reply={r} status={status}", flush=True)
                if status != want:
                    failures.append(f"{action} -> {status}")
        audio = helper.call("audio.sessions")
        print("audio during:", audio, flush=True)
        chrome_pids = [s["pid"] for s in audio.get("sessions") or [] if str(s.get("name")).lower() == "chrome"]
        if chrome_pids:
            print("set:", helper.call("audio.set", pid=chrome_pids[0], volume=0.4), flush=True)
            print("after set:", [s for s in helper.call("audio.sessions").get("sessions") or [] if s["pid"] == chrome_pids[0]], flush=True)
            helper.call("audio.set", pid=chrome_pids[0], volume=1.0)
        hwnd = find_window("Jarvis Test Tune")
        if hwnd:
            started = time.monotonic()
            print("url:", helper.call("browser.url", hwnd=hwnd), round(time.monotonic() - started, 2), "s", flush=True)
    finally:
        proc.kill()
        subprocess.run(["taskkill", "/F", "/IM", "chrome.exe"], capture_output=True)
        helper.stop()
    print("FAILURES:", failures, flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
