"""J.A.R.V.I.S. desktop assistant - entry point and Python <-> JavaScript bridge.

    python app.py              launch the assistant
    python app.py --debug      launch with the web inspector enabled
    python app.py --selftest   verify the install / frozen build without opening a window
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import queue
import sys
import threading
import time
import webbrowser
from typing import Any

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

from core import APP_VERSION  # noqa: E402
from core.config import Config, app_data_dir, resource_path  # noqa: E402

log = logging.getLogger("jarvis")

ALLOWED_EXTERNAL_URLS = ("https://ollama.com/", "https://github.com/ollama/")


# ============================================================================ events → UI
class EventBridge:
    """Batches backend events and pushes them into the page with a single JS call per frame.

    Token streams and microphone levels can produce hundreds of events per second;
    sending each one through ``evaluate_js`` would stall the UI thread, so events are
    coalesced (consecutive tokens merged, only the newest mic frame kept) and
    flushed roughly 50 times a second.
    """

    FLUSH_INTERVAL = 0.02
    MAX_PENDING = 2000

    def __init__(self) -> None:
        self._queue: "queue.Queue[dict]" = queue.Queue()
        self._window = None
        self._ready = threading.Event()
        self._closed = threading.Event()
        self._thread = threading.Thread(target=self._run, name="ui-events", daemon=True)
        self._thread.start()

    def attach(self, window) -> None:
        self._window = window

    def set_ready(self) -> None:
        self._ready.set()

    def close(self) -> None:
        self._closed.set()

    def emit(self, kind: str, payload: dict | None = None) -> None:
        if self._closed.is_set():
            return
        if not self._ready.is_set() and kind == "mic_frame":
            return
        self._queue.put({"type": kind, **(payload or {})})

    @staticmethod
    def coalesce(events: list[dict]) -> list[dict]:
        out: list[dict] = []
        last_mic = None
        for ev in events:
            kind = ev["type"]
            if kind == "mic_frame":
                last_mic = ev
                continue
            if kind == "assistant_token" and out and out[-1]["type"] == "assistant_token" and out[-1]["id"] == ev["id"]:
                out[-1] = {**out[-1], "text": out[-1]["text"] + ev["text"]}
                continue
            out.append(ev)
        if last_mic is not None:
            out.append(last_mic)
        return out

    def _run(self) -> None:
        pending: list[dict] = []
        while not self._closed.is_set():
            try:
                pending.append(self._queue.get(timeout=0.25))
            except queue.Empty:
                if not pending:
                    continue
            while True:
                try:
                    pending.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            if not self._ready.is_set() or self._window is None:
                pending = [e for e in pending if e["type"] != "mic_frame"][-self.MAX_PENDING :]
                time.sleep(0.05)
                continue
            batch, pending = self.coalesce(pending), []
            script = "window.JARVIS&&window.JARVIS.receive(" + json.dumps(batch, separators=(",", ":")) + ")"
            try:
                self._window.run_js(script)
            except Exception as exc:  # window closing, renderer reloading, ...
                log.debug("UI push failed: %s", exc)
            time.sleep(self.FLUSH_INTERVAL)


# ============================================================================ JS API
class JarvisAPI:
    """Methods callable from JavaScript as ``window.pywebview.api.<name>()``.

    pywebview exposes every public attribute recursively, so all state is private.
    Each call runs on its own thread, which keeps slow work off the UI thread.
    """

    def __init__(self, assistant, bridge: EventBridge, frameless: bool = True) -> None:
        self._assistant = assistant
        self._bridge = bridge
        self._frameless = frameless
        self._window = None
        self._maximized = False

    def _attach(self, window) -> None:
        self._window = window

    # -- lifecycle ---------------------------------------------------------
    def ui_ready(self) -> dict:
        payload = self._assistant.boot_payload()
        payload["window"] = {"frameless": self._frameless}
        self._bridge.set_ready()
        return payload

    def boot_complete(self) -> None:
        self._assistant.boot_complete()

    def play_sfx(self, name: str) -> None:
        self._assistant.play_sfx(str(name))

    # -- conversation ------------------------------------------------------
    def send_text(self, text: str) -> bool:
        return self._assistant.submit_text(str(text or ""))

    def start_listening(self) -> str:
        return self._assistant.start_listening()

    def hold_listening(self) -> None:
        self._assistant.hold_listening()

    def stop_listening(self) -> None:
        self._assistant.stop_listening()

    def interrupt(self) -> bool:
        return self._assistant.interrupt()

    def clear_memory(self) -> None:
        self._assistant.clear_memory()

    # -- telemetry ---------------------------------------------------------
    def get_system_stats(self) -> dict:
        return self._assistant.monitor.snapshot()

    def get_core_stats(self) -> dict:
        return self._assistant.core_stats()

    # -- ollama ------------------------------------------------------------
    def check_ollama(self) -> dict:
        return self._assistant.check_ollama()

    def start_ollama(self) -> dict:
        return self._assistant.start_ollama()

    def pull_model(self, name: str) -> dict:
        return self._assistant.pull_model(str(name or ""))

    def cancel_pull(self) -> None:
        self._assistant.cancel_pull()

    # -- settings ----------------------------------------------------------
    def get_settings(self) -> dict:
        return self._assistant.config.as_dict()

    def save_settings(self, changes: dict) -> dict:
        try:
            return {"ok": True, "settings": self._assistant.update_settings(changes or {})}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    def list_voices(self) -> list:
        return self._assistant.list_voices()

    def preview_voice(self, voice: str) -> None:
        self._assistant.preview_voice(str(voice or "") or None)

    # -- window & shell ----------------------------------------------------
    def open_url(self, url: str) -> bool:
        url = str(url or "")
        if not url.startswith(ALLOWED_EXTERNAL_URLS):
            return False
        return webbrowser.open(url)

    def window_minimize(self) -> None:
        if self._window:
            self._window.minimize()

    def window_toggle_maximize(self) -> None:
        if not self._window:
            return
        if self._maximized:
            self._window.restore()
        else:
            self._window.maximize()
        self._maximized = not self._maximized

    def window_toggle_fullscreen(self) -> None:
        if self._window:
            self._window.toggle_fullscreen()

    def window_close(self) -> None:
        if self._window:
            self._window.destroy()


# ============================================================================ helpers
def configure_logging(debug: bool) -> None:
    log_file = app_data_dir() / "jarvis.log"
    handlers: list[logging.Handler] = [
        logging.handlers.RotatingFileHandler(log_file, maxBytes=1_500_000, backupCount=2, encoding="utf-8")
    ]
    if sys.stderr is not None and getattr(sys.stderr, "isatty", lambda: False)():
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    for noisy in ("httpx", "httpcore", "asyncio", "urllib3", "pywebview"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def ensure_std_streams() -> None:
    """Windowed (--noconsole) builds have no stdout/stderr; give libraries something to write to."""
    if sys.stdout is None or sys.stderr is None:
        sink = open(os.devnull, "w", encoding="utf-8")
        sys.stdout = sys.stdout or sink
        sys.stderr = sys.stderr or sink


def window_geometry(webview) -> tuple[int, int]:
    width, height = 1480, 920
    try:
        screen = webview.screens[0]
        width = int(min(max(screen.width * 0.86, 1120), 1720))
        height = int(min(max(screen.height * 0.86, 720), 1040))
    except Exception:
        pass
    return width, height


def run_selftest(report_path: str | None) -> int:
    """Exercise every subsystem that doesn't need a window, microphone or network."""
    results: dict[str, Any] = {"version": APP_VERSION, "frozen": bool(getattr(sys, "frozen", False)), "checks": {}}

    def check(name: str, fn) -> None:
        try:
            detail = fn()
            results["checks"][name] = {"ok": True, "detail": detail}
        except Exception as exc:  # noqa: BLE001 - report everything
            results["checks"][name] = {"ok": False, "detail": f"{exc.__class__.__name__}: {exc}"}

    def web_assets():
        missing = [f for f in ("index.html", "styles.css", "app.js", "fonts/fonts.css") if not resource_path("web", f).is_file()]
        if missing:
            raise FileNotFoundError(", ".join(missing))
        return str(resource_path("web"))

    def imports():
        import edge_tts  # noqa: F401
        import numpy  # noqa: F401
        import ollama  # noqa: F401
        import psutil  # noqa: F401
        import pygame  # noqa: F401
        import speech_recognition  # noqa: F401
        import webview  # noqa: F401

        try:
            from webview._version import version as webview_version
        except ImportError:
            webview_version = getattr(webview, "__version__", "unknown")
        versions = {"pywebview": webview_version, "pygame": pygame.version.ver}
        try:
            import pyaudio

            versions["pyaudio"] = pyaudio.__version__
        except ImportError:
            versions["pyaudio"] = None
        return versions

    def audio_pipeline():
        os.environ["SDL_AUDIODRIVER"] = "dummy"
        from core.audio import AudioEngine, spectrum_frames
        from core.sfx import build_library

        engine = AudioEngine()
        if not engine.init():
            raise RuntimeError(engine.error)
        lib = build_library(engine.frequency)
        sound = engine.make_sound(lib["activate"])
        frames, levels = spectrum_frames(engine.mono_samples(sound), engine.frequency)
        engine.shutdown()
        return {"sfx": sorted(lib), "frames": len(frames), "peak_level": max(levels) if levels else 0}

    def text_pipeline():
        from core.tts import SentenceSplitter, clean_for_speech

        splitter = SentenceSplitter()
        out = splitter.feed("Good evening, sir. **All** systems are online. ")
        out += splitter.flush()
        return [clean_for_speech(s) for s in out]

    def config_roundtrip():
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(Path(tmp) / "c.json")
            cfg.update({"speech_rate": 12})
            return Config(Path(tmp) / "c.json")["speech_rate"] == 12

    def telemetry():
        from core.system import SystemMonitor

        mon = SystemMonitor()
        snap = mon.snapshot()
        return {"cpu": snap["cpu"], "ram": snap["ram"], "host": mon.static_info()["hostname"]}

    def ollama_probe():
        from core.llm import LLMEngine

        with __import__("tempfile").TemporaryDirectory() as tmp:
            status = LLMEngine(Config(__import__("pathlib").Path(tmp) / "c.json")).check()
        return {"online": status.online, "models": status.models[:5]}

    check("web_assets", web_assets)
    check("imports", imports)
    check("audio_pipeline", audio_pipeline)
    check("text_pipeline", text_pipeline)
    check("config", config_roundtrip)
    check("telemetry", telemetry)
    check("ollama_probe", ollama_probe)  # informational: offline is not a failure

    required = ("web_assets", "imports", "audio_pipeline", "text_pipeline", "config", "telemetry")
    results["ok"] = all(results["checks"][k]["ok"] for k in required)
    text = json.dumps(results, indent=2)
    if report_path:
        with open(report_path, "w", encoding="utf-8") as fh:
            fh.write(text)
    if sys.stdout is not None:
        print(text)
    return 0 if results["ok"] else 1


# ============================================================================ main
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="J.A.R.V.I.S. desktop voice assistant")
    parser.add_argument("--debug", action="store_true", help="enable the web inspector and verbose logs")
    parser.add_argument("--selftest", action="store_true", help="run headless diagnostics and exit")
    parser.add_argument("--selftest-report", metavar="PATH", help="write the self-test JSON report to PATH")
    parser.add_argument("--gui", help="force a pywebview backend (edgechromium, qt, gtk, cef)")
    parser.add_argument("--framed", action="store_true", help="use a normal OS window frame")
    args, _unknown = parser.parse_known_args(argv)

    ensure_std_streams()
    configure_logging(args.debug)
    log.info("J.A.R.V.I.S. %s starting (frozen=%s)", APP_VERSION, getattr(sys, "frozen", False))

    if args.selftest:
        return run_selftest(args.selftest_report)

    import webview

    from core.assistant import Assistant

    config = Config()
    bridge = EventBridge()
    assistant = Assistant(config, bridge.emit)
    frameless = bool(config.get("frameless", True)) and not args.framed
    api = JarvisAPI(assistant, bridge, frameless)

    width, height = window_geometry(webview)
    window = webview.create_window(
        "J.A.R.V.I.S.",
        url=str(resource_path("web", "index.html")),
        js_api=api,
        width=width,
        height=height,
        min_size=(1100, 700),
        frameless=frameless,
        easy_drag=False,
        background_color="#02070d",
        text_select=True,
    )
    api._attach(window)
    bridge.attach(window)

    def on_closed() -> None:
        bridge.close()
        assistant.shutdown()

    window.events.closed += on_closed

    icon = resource_path("assets", "jarvis.png")
    webview.start(
        assistant.start,
        debug=args.debug,
        http_server=True,
        private_mode=True,
        gui=args.gui,
        icon=str(icon) if icon.is_file() else None,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
