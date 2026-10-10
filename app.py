"""J.A.R.V.I.S. desktop assistant - entry point and Python <-> JavaScript bridge.

    python app.py              launch the assistant
    python app.py --debug      launch with the web inspector enabled
    python app.py --selftest   verify the install / frozen build without opening a window
    python app.py --smoke-test open the real window, wait for the HUD to boot, report and exit
"""

from __future__ import annotations

import argparse
import faulthandler
import json
import logging
import logging.handlers
import os
import platform
import queue
import sys
import tempfile
import threading
import time
import traceback
import webbrowser
from pathlib import Path
from typing import Any

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

from core import APP_VERSION  # noqa: E402
from core.config import Config, app_data_dir, resource_path  # noqa: E402

log = logging.getLogger("jarvis")

ALLOWED_EXTERNAL_URLS = ("https://ollama.com/", "https://github.com/ollama/", "https://script.google.com/",
                         "https://docs.google.com/", "https://mail.google.com/")
WEBVIEW2_DOWNLOAD_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"

# Windows MessageBox flags
_MB_OK, _MB_OKCANCEL, _MB_ICONERROR, _MB_ICONWARNING, _MB_ICONINFO = 0x0, 0x1, 0x10, 0x30, 0x40
_IDOK = 1
_dialogs_enabled = os.environ.get("JARVIS_NO_DIALOGS") != "1"  # also disabled for --selftest / --smoke-test
_instance_mutex = None
_dotnet_handlers: list = []  # keep .NET delegates alive
_fault_file = None  # faulthandler needs the file object to stay open

try:  # PyInstaller splash screen, shown by the bootloader while the one-file archive unpacks
    import pyi_splash  # type: ignore
except ImportError:
    pyi_splash = None


def close_splash() -> None:
    global pyi_splash
    if pyi_splash is not None:
        try:
            pyi_splash.close()
        except Exception:
            pass
        pyi_splash = None


# ============================================================================ crash visibility
def message_box(text: str, title: str = "J.A.R.V.I.S.", flags: int = _MB_OK | _MB_ICONINFO) -> int:
    """Native dialog on Windows. A windowed exe has no console, so this is the only way to tell the user."""
    if not _dialogs_enabled or sys.platform != "win32":
        return 0
    try:
        import ctypes

        return int(ctypes.windll.user32.MessageBoxW(None, text, title, flags | 0x00010000 | 0x00040000))
    except Exception:
        return 0


def report_fatal(exc: BaseException, context: str = "startup") -> None:
    """Log a fatal error, save it somewhere guaranteed writable and show it to the user."""
    details = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    where: Path | None = None
    try:
        log.critical("Fatal error during %s:\n%s", context, details)
        where = app_data_dir() / "jarvis.log"
    except Exception:
        pass
    try:  # logging may not be configured (or the profile folder may be unwritable)
        crash = Path(tempfile.gettempdir()) / "jarvis-crash.txt"
        crash.write_text(
            f"J.A.R.V.I.S. {APP_VERSION} failed during {context}\n{platform.platform()} | Python {platform.python_version()}"
            f" | frozen={getattr(sys, 'frozen', False)}\n\n{details}",
            encoding="utf-8",
        )
        where = where or crash
    except OSError:
        pass
    message_box(
        f"J.A.R.V.I.S. could not start.\n\n{exc.__class__.__name__}: {exc}\n\n"
        f"Details were saved to:\n{where or 'the log file'}\n\nPlease include that file when reporting the problem.",
        "J.A.R.V.I.S. failed to start",
        _MB_OK | _MB_ICONERROR,
    )


def enable_fault_log() -> None:
    """Native crashes (PortAudio, SDL, WebView2 interop) bypass Python entirely; dump their stacks to a file."""
    global _fault_file
    try:
        _fault_file = open(app_data_dir() / "jarvis-fault.log", "a", encoding="utf-8")
        _fault_file.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} J.A.R.V.I.S. {APP_VERSION} pid {os.getpid()}\n")
        _fault_file.flush()
        faulthandler.enable(_fault_file, all_threads=True)
    except Exception:
        log.debug("faulthandler unavailable", exc_info=True)


def install_crash_handlers() -> None:
    def excepthook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            return sys.__excepthook__(exc_type, exc, tb)
        report_fatal(exc.with_traceback(tb))

    def thread_excepthook(args):
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread else "?"
        log.error("Unhandled exception in thread %s", name, exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook


def hook_dotnet_crashes() -> bool:
    """Report unhandled .NET exceptions (WinForms/WebView2 threads) instead of dying silently.

    Such an exception terminates the process with 0xE0434352 and bypasses every Python handler;
    AppDomain.UnhandledException is the last hook that still runs before termination.
    """
    if sys.platform != "win32":
        return False
    try:
        import clr  # noqa: F401 - loaded by pywebview's WinForms backend
        from System import AppDomain, UnhandledExceptionEventHandler

        def on_unhandled(_sender, event) -> None:
            report_fatal(RuntimeError(f"Unhandled .NET exception: {event.ExceptionObject}"), "native window layer")

        handler = UnhandledExceptionEventHandler(on_unhandled)
        AppDomain.CurrentDomain.UnhandledException += handler
        _dotnet_handlers.append(handler)
        return True
    except Exception:
        log.warning("Could not install the .NET crash handler", exc_info=True)
        return False


def window_icon(platform_name: str = sys.platform) -> str | None:
    """Icon for the window title bar / taskbar.

    pywebview's Windows backend passes this path straight to System.Drawing.Icon, which accepts only
    .ico files: a PNG throws on the .NET GUI thread and kills the process before the window appears.
    """
    path = resource_path("assets", "jarvis.ico" if platform_name == "win32" else "jarvis.png")
    return str(path) if path.is_file() else None


def webview2_version() -> str | None:
    """Edge WebView2 runtime version, using the same registry lookup pywebview uses to pick its renderer.

    If this returns None on Windows, pywebview silently falls back to the legacy IE (MSHTML)
    engine, which cannot run the HUD, so we stop with a helpful message instead.
    """
    if sys.platform != "win32":
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full") as key:
            if winreg.QueryValueEx(key, "Release")[0] < 394802:  # .NET Framework 4.6.2
                return None
    except OSError:
        return None
    clients = ("{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}", "{2CD8A007-E189-409D-A2C8-9AF4EF3C72AA}",
               "{0D50BFEC-CD6A-4F9A-964C-C7416E3ACB10}", "{65C35B14-6C1D-4122-AC46-7148CC9D6497}")
    wow = "" if platform.machine() == "x86" else "WOW6432Node\\"
    for client in clients:
        for hive, path in ((winreg.HKEY_CURRENT_USER, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{client}"),
                           (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\{wow}Microsoft\EdgeUpdate\Clients\{client}")):
            try:
                with winreg.OpenKey(hive, path) as key:
                    version = str(winreg.QueryValueEx(key, "pv")[0])
                if int(version.split(".")[0]) >= 86:
                    return version
            except (OSError, ValueError):
                continue
    return None


def acquire_single_instance() -> bool:
    """Prevent duplicate windows when the user double-clicks again during the slow first unpack."""
    global _instance_mutex
    if sys.platform != "win32":
        return True
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    handle = kernel32.CreateMutexW(None, False, "Local\\JARVIS-HUD-single-instance")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        return False
    _instance_mutex = handle  # keep the handle alive for the life of the process
    return True


def release_single_instance() -> None:
    """Let the freshly updated copy start while this one waits (hidden) to see that it works."""
    global _instance_mutex
    if sys.platform != "win32" or not _instance_mutex:
        return
    import ctypes

    ctypes.windll.kernel32.CloseHandle(_instance_mutex)
    _instance_mutex = None


def acquire_single_instance_patiently(seconds: float) -> bool:
    """After an update the old copy may still be letting go of the lock for a moment."""
    deadline = time.monotonic() + seconds
    while True:
        if acquire_single_instance():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.25)


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
        self._assistant_obj = assistant
        self._assistant_ready = threading.Event()
        if assistant is not None:
            self._assistant_ready.set()
        self._bridge = bridge
        self._frameless = frameless
        self._window = None
        self._maximized = False
        self._ready = threading.Event()
        self._booted = threading.Event()
        self._after_update = ""  # state file path when this copy was started by an update
        self._frame = None  # core.winframe.WinFrame once the window exists (Windows, borderless)

    def _attach(self, window) -> None:
        self._window = window

    def _set_assistant(self, assistant) -> None:
        self._assistant_obj = assistant
        self._assistant_ready.set()

    @property
    def _assistant(self):
        """The assistant loads in the background so the window can appear first; calls wait for it."""
        if not self._assistant_ready.wait(90):
            raise RuntimeError("J.A.R.V.I.S. core failed to initialise")
        return self._assistant_obj

    # -- lifecycle ---------------------------------------------------------
    def ui_ready(self) -> dict:
        payload = self._assistant.boot_payload()
        payload["window"] = {"frameless": self._frameless}
        self._bridge.set_ready()
        self._ready.set()
        log.info("UI bridge connected")
        return payload

    def boot_complete(self) -> None:
        self._booted.set()
        log.info("HUD boot sequence complete")
        if self._after_update:
            self._assistant.updater.mark_healthy(Path(self._after_update))  # the old copy can go now
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

    # -- Google Docs, Slides & Sheets bridge ---------------------------------------
    def google_status(self) -> dict:
        bridge = self._assistant.tools.google
        return {"configured": bridge.configured, "outdated": bridge.outdated,
                "url": self._assistant.config.get("google_script_url") or ""}

    def google_script(self) -> str:
        return self._assistant.tools.google.script_source()

    def google_connect(self, url: str) -> dict:
        from core.google_bridge import BridgeError

        try:
            bridge = self._assistant.tools.google
            info = bridge.connect(str(url or "") or self._assistant.config.get("google_script_url") or "")
            return {"ok": True, "user": info.get("user"), "outdated": bridge.outdated}
        except BridgeError as exc:
            return {"ok": False, "error": str(exc)}

    def email_send(self, draft_id: str, to: str = "", subject: str = "", body: str = "") -> dict:
        return self._assistant.send_email(str(draft_id), str(to or ""), str(subject or ""), str(body or ""))

    def email_open_gmail(self, draft_id: str, to: str = "", subject: str = "", body: str = "") -> dict:
        return self._assistant.send_email(str(draft_id), str(to or ""), str(subject or ""), str(body or ""), via_gmail=True)

    def email_discard(self, draft_id: str) -> None:
        self._assistant.discard_email(str(draft_id))

    def google_disconnect(self) -> None:
        self._assistant.config.update({"google_script_url": ""})

    def list_voices(self) -> list:
        return self._assistant.list_voices()

    def preview_voice(self, voice: str) -> None:
        self._assistant.preview_voice(str(voice or "") or None)

    # -- window & shell ----------------------------------------------------
    def open_url(self, url: str) -> bool:
        url = str(url or "")
        if not url.startswith(ALLOWED_EXTERNAL_URLS):
            return False
        if self._assistant is not None and self._assistant.open_google_link(url):
            return True  # a Google link: opened in the chosen browser, as the linked account
        return webbrowser.open(url)

    # -- personalities -----------------------------------------------------
    def persona_list(self) -> list:
        return self._assistant.persona_list()

    def persona_set(self, pid: str) -> dict:
        try:
            return {"ok": True, "persona": self._assistant.set_persona(str(pid or ""))}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    def persona_preview(self, pid: str) -> None:
        self._assistant.preview_persona(str(pid or ""))

    def persona_save(self, data: dict) -> dict:
        try:
            return {"ok": True, "persona": self._assistant.save_persona(dict(data or {}))}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    def persona_delete(self, pid: str) -> dict:
        try:
            self._assistant.delete_persona(str(pid or ""))
            return {"ok": True}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    def persona_color(self, pid: str, color: str = "") -> dict:
        try:
            return {"ok": True, "persona": self._assistant.set_persona_color(str(pid or ""), str(color or "") or None)}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    # -- wake words ----------------------------------------------------------
    def wake_learn(self, pid: str) -> dict:
        return self._assistant.wake_learn(str(pid or ""))

    def wake_record(self, pid: str) -> dict:
        return self._assistant.wake_record(str(pid or ""))

    def wake_train_voice(self, pid: str) -> dict:
        return self._assistant.wake_train_voice(str(pid or ""))

    def wake_clear_voice(self, pid: str) -> dict:
        return self._assistant.wake_clear_voice(str(pid or ""))

    # -- long-term memory --------------------------------------------------
    def memory_list(self) -> dict:
        return self._assistant.memory_overview()

    def memory_add(self, kind: str, text: str) -> dict:
        try:
            return {"ok": True, **self._assistant.memory_add(str(kind or "fact"), str(text or ""))}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    def memory_update(self, mid: int, fields: dict) -> dict:
        try:
            return {"ok": True, "memory": self._assistant.memory_update(int(mid), dict(fields or {}))}
        except (ValueError, TypeError) as exc:
            return {"ok": False, "error": str(exc)}

    def memory_delete(self, mid: int) -> dict:
        return {"ok": True, "memory": self._assistant.memory_delete(int(mid))}

    def memory_restore(self, data: dict) -> dict:
        return {"ok": True, "memory": self._assistant.memory_restore(dict(data or {}))}

    def memory_delete_episode(self, eid: int) -> None:
        self._assistant.memory_delete_episode(int(eid))

    def memory_clear(self) -> None:
        self._assistant.memory_clear()

    def memory_export(self) -> dict:
        """Save everything JARVIS remembers as a JSON file in Documents (or the home folder)."""
        data = self._assistant.memory.export()
        folder = Path.home() / "Documents"
        if not folder.is_dir():
            folder = Path.home()
        target = folder / f"JARVIS memory {time.strftime('%Y-%m-%d %H%M')}.json"
        try:
            target.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "path": str(target)}

    def memory_install_embeddings(self) -> dict:
        return self._assistant.pull_model("nomic-embed-text", role="embed")

    # -- protocols & media -------------------------------------------------
    def protocol_list(self) -> dict:
        return self._assistant.protocol_list()

    def protocol_save(self, data: dict) -> dict:
        return self._assistant.protocol_save(data if isinstance(data, dict) else {})

    def protocol_delete(self, pid: str) -> dict:
        return self._assistant.protocol_delete(str(pid))

    def protocol_restore(self, data: dict) -> dict:
        return self._assistant.protocol_restore(data if isinstance(data, dict) else {})

    def protocol_run(self, pid: str) -> dict:
        return self._assistant.protocol_run(str(pid))

    def protocol_stop(self) -> dict:
        return self._assistant.protocol_stop()

    def protocol_answer(self, yes: bool) -> dict:
        return self._assistant.protocol_answer(bool(yes))

    def protocol_proposal_answer(self, action: str, data: dict | None = None) -> dict:
        return self._assistant.protocol_proposal_answer(str(action), data if isinstance(data, dict) else None)

    def protocol_from_template(self, name: str) -> dict:
        return self._assistant.protocol_from_template(str(name))

    def media_control(self, action: str, key: str = "") -> dict:
        return self._assistant.media_control(str(action), str(key or ""))

    def media_volume(self, key: str, level: float) -> dict:
        return self._assistant.media_volume(str(key), float(level))

    def activity_list(self) -> dict:
        return self._assistant.activity_list()

    def browser_status(self) -> dict:
        return self._assistant.browser_status()

    def browser_set_profile(self, directory: str) -> dict:
        self._assistant.config.update({"browser_profile": str(directory or "auto")})
        return self._assistant.browser_status()

    # -- vision ------------------------------------------------------------
    def vision_status(self) -> dict:
        return self._assistant.vision_status()

    def vision_install(self, name: str = "") -> dict:
        from core.vision import DEFAULT_VISION_MODEL

        return self._assistant.pull_model(str(name or DEFAULT_VISION_MODEL), role="vision")

    def vision_confirm(self, act_id: str, yes: bool) -> dict:
        return self._assistant.confirm_act(str(act_id), bool(yes))

    def vision_look(self, question: str = "") -> bool:
        return self._assistant.look_now(str(question or ""))

    def vision_stop_watch(self) -> None:
        self._assistant.stop_watching()

    def google_view(self, kind: str, ref: str) -> dict:
        return self._assistant.view_google(str(kind or "doc"), str(ref or ""))

    def installed_browsers(self) -> dict:
        from core.browsers import installed_browsers

        return installed_browsers()

    # -- corrections ("that was wrong") -----------------------------------------
    def corrections_list(self) -> dict:
        return self._assistant.corrections_list()

    def corrections_delete(self, rid: str) -> dict:
        return self._assistant.corrections_delete(str(rid))

    # -- updates -------------------------------------------------------------
    def update_status(self) -> dict:
        return self._assistant.update_status()

    def update_check(self) -> dict:
        return self._assistant.update_check()

    def update_install(self) -> dict:
        return self._assistant.update_install()

    def update_restore_previous(self) -> dict:
        return self._assistant.update_install(restore=True)

    # -- the window ----------------------------------------------------------
    def _frame_ready(self):
        """Windows' own move / size / maximise for the borderless HUD (None: let pywebview do it)."""
        if self._frame is None and self._window is not None and sys.platform == "win32" and self._frameless:
            try:
                from core.winframe import WinFrame

                native = self._window.native
                from System import Func, Type  # pythonnet, loaded by pywebview's WinForms backend

                def run_ui(fn) -> None:
                    def call():
                        fn()
                        return None

                    if native.InvokeRequired:
                        native.Invoke(Func[Type](call))
                    else:
                        call()

                self._frame = WinFrame(lambda: int(native.Handle.ToInt64()), run_ui,
                                       lambda st: self._bridge.emit("window_state", st))
                try:
                    self._frame.min_size = (int(native.MinimumSize.Width), int(native.MinimumSize.Height))
                except Exception:
                    pass
            except Exception:
                log.warning("native window control unavailable; using pywebview's", exc_info=True)
                self._frame = False
        return self._frame or None

    def window_minimize(self) -> None:
        if self._window:
            self._window.minimize()

    def window_toggle_maximize(self) -> dict:
        log.info("window_toggle_maximize")
        frame = self._frame_ready()
        if frame:
            frame.toggle_maximize()
            return frame.state()
        if not self._window:
            return {}
        if self._maximized:
            self._window.restore()
        else:
            self._window.maximize()
        self._maximized = not self._maximized
        return {"maximized": self._maximized, "fullscreen": False}

    def window_toggle_fullscreen(self) -> dict:
        log.info("window_toggle_fullscreen")
        frame = self._frame_ready()
        if frame:
            frame.toggle_fullscreen()
            return frame.state()
        if self._window:
            self._window.toggle_fullscreen()
        return {}

    def window_drag(self) -> None:
        log.info("window_drag")
        frame = self._frame_ready()
        if frame:
            frame.start_drag()

    def window_resize(self, edge: str) -> None:
        log.info("window_resize %s", edge)
        frame = self._frame_ready()
        if frame:
            frame.start_resize(str(edge))

    def window_state(self) -> dict:
        frame = self._frame_ready()
        return frame.state() if frame else {"maximized": self._maximized, "fullscreen": False, "native": False}

    def window_close(self) -> None:
        log.info("window_close")
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


def window_geometry(webview) -> tuple[int, int, tuple[int, int], bool]:
    """Initial size, minimum size and whether to start maximised, fitted to the primary screen."""
    width, height = 1480, 920
    screen_w = screen_h = None
    try:
        screen = webview.screens[0]
        screen_w, screen_h = int(screen.width), int(screen.height)
    except Exception:
        log.warning("Could not read the screen size", exc_info=True)
    if screen_w and screen_h:
        width = int(min(max(screen_w * 0.86, min(1120, screen_w * 0.96)), 1720, screen_w))
        height = int(min(max(screen_h * 0.86, min(720, screen_h * 0.92)), 1040, screen_h))
    min_size = (min(1000, width), min(640, height))
    small = bool(screen_w and screen_h and (screen_w < 1280 or screen_h < 760))
    return width, height, min_size, small


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

    def gui_backend():
        """Load the native GUI layer exactly like webview.start() does (pythonnet/WinForms/WebView2 on Windows)."""
        import importlib

        # not "from webview import guilib": the package defines a module-level guilib = None that shadows it
        lib = importlib.import_module("webview.guilib").initialize()
        info = {"renderer": getattr(lib, "renderer", None)}
        if sys.platform == "win32":
            info["webview2"] = webview2_version()
            if info["renderer"] != "edgechromium":
                raise RuntimeError(f"renderer is {info['renderer']!r}, expected 'edgechromium' (WebView2 runtime missing?)")
        return info

    def wake_word():
        import numpy as np

        from core.wakeword import CHUNK, WakeWordDetector, available

        ok, why = available()
        if not ok:
            raise RuntimeError(why)
        detector = WakeWordDetector()
        scores = [detector.process(np.zeros(CHUNK, np.int16)) for _ in range(8)]
        if max(scores) > 0.5:
            raise RuntimeError(f"silence scored {max(scores):.2f}")
        return {"silence_score": round(max(scores), 3)}

    def assistant_skills():
        """The bundled request understanding: writing, email, language detection and the Google script."""
        from core.compose import parse_write_request
        from core.google_bridge import script_version
        from core.gdrive import parse_google_request
        from core.language import detect
        from core.mail import parse_email_request
        from core.patience import looks_unfinished
        from core.quick import calculate, parse_quick

        bio = parse_write_request("write a bio on Lionel Messi", google_ready=True)
        mail = parse_email_request("email Sarah saying I'm running late")
        lang = detect("¿Qué hora es?").code
        opened = parse_google_request("open my Messi doc")
        if not (bio and bio.topic == "Lionel Messi" and mail and mail.who == "Sarah" and lang == "es"):
            raise RuntimeError(f"unexpected parse: {bio} {mail} {lang}")
        if not (opened and opened.name == "Messi" and looks_unfinished("open the") and not looks_unfinished("open spotify")
                and calculate("what is 12 times 7") == "84" and parse_quick("thanks").kind == "talk"):
            raise RuntimeError("quick skills / patience / google-file parsing misbehaved")
        from core.gdrive import parse_new_file
        from core.monitors import parse_monitor_command
        from core.vision import parse_act
        from core.weather import parse_weather

        from core.docops import parse_doc_command

        from core.media import parse_media
        from core.protocols import parse_protocol_command

        morning = parse_protocol_command("create a protocol called Morning: open Spotify, then what's the weather")
        song = parse_media("play Bohemian Rhapsody on YouTube")
        if not (morning and morning.action == "create" and morning.steps == ["open Spotify", "what's the weather"]
                and song and song.query == "Bohemian Rhapsody" and song.service == "youtube"):
            raise RuntimeError("protocol / music understanding misbehaved")
        pizza = parse_doc_command("rename the Google Doc to Pizza recipe and then type out a pizza recipe")
        if not (pizza and [s.action for s in pizza.steps] == ["rename", "write"] and parse_act("close tap").action == "close_tab"):
            raise RuntimeError("document steps / tab understanding misbehaved")
        tab = parse_act("close the google chrome tab")
        if not (tab and tab.action == "close_tab" and tab.app == "chrome" and parse_weather("what's the temperature")
                and parse_new_file("create a new document") and parse_monitor_command("move this window to my other monitor")):
            raise RuntimeError("tab / weather / new-document / monitor understanding misbehaved")
        return {"script_version": script_version(), "language": lang}

    def vision():
        """The eyes are bundled: request understanding, image encoding and (on Windows) a real screen capture."""
        from core.screen import default_desktop
        from core.vision import encode_image, parse_act, parse_look

        if not (parse_look("what's on my screen") and parse_act("click send")):
            raise RuntimeError("vision request parsing misbehaved")
        detail = {}
        if sys.platform == "win32":
            shot = default_desktop().capture(None)
            detail["screen"] = list(shot.size)
            detail["jpeg_bytes"] = len(encode_image(shot.image))
        return detail

    def memory_and_personalities():
        """Long-term memory (SQLite) and the personalities are bundled and work."""
        import tempfile as _tf

        import numpy as np

        from core.memory import MemoryStore, parse_memory_command, second_person
        from core.personas import PERSONAS, parse_switch

        with _tf.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp) / "memory.db")
            store.add("preference", second_person("I love jazz"))
            found = store.all()
            store.close()
        if not (found and found[0].text == "You love jazz." and parse_switch("switch to Harper") == "harper"
                and parse_memory_command("remember that my dog is called Max").action == "remember"):
            raise RuntimeError("memory or personality parsing misbehaved")
        from core.wakelearn import FeatureExtractor, TinyNet

        feats = FeatureExtractor()(np.random.default_rng(0).normal(0, 300, 16000 * 3).astype(np.float32))
        net = TinyNet(hidden=4)
        net.fit(np.ones((8, 16, 96), np.float32), np.zeros((8, 16, 96), np.float32), epochs=2)
        if feats.shape[1] != 96 or len(feats) < 20:
            raise RuntimeError(f"wake-word features misbehaved: {feats.shape}")
        return {"personalities": list(PERSONAS), "sqlite": __import__("sqlite3").sqlite_version, "wake_features": list(feats.shape)}

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
    check("wake_word", wake_word)
    check("assistant_skills", assistant_skills)
    check("vision", vision)
    check("memory", memory_and_personalities)
    check("ollama_probe", ollama_probe)  # informational: offline is not a failure
    if sys.platform == "win32":  # elsewhere a display server may be absent during the build
        check("gui_backend", gui_backend)

    required = ["web_assets", "imports", "audio_pipeline", "text_pipeline", "config", "telemetry", "wake_word", "memory"]
    if sys.platform == "win32":
        required.append("gui_backend")
    results["ok"] = all(results["checks"][k]["ok"] for k in required)
    text = json.dumps(results, indent=2)
    if report_path:
        with open(report_path, "w", encoding="utf-8") as fh:
            fh.write(text)
    if sys.stdout is not None:
        print(text)
    return 0 if results["ok"] else 1


# ============================================================================ GUI smoke test
_SMOKE_PROBE = r"""JSON.stringify({
  body: document.body.className,
  title: (document.querySelector('#state-title span') || {}).textContent,
  bootOverlay: !!document.querySelector('#boot:not(.done)'),
  reactor: [document.querySelector('#reactor').clientWidth, document.querySelector('#reactor').clientHeight],
  chips: [...document.querySelectorAll('.chip')].map(c => c.innerText.replace(/\s+/g, ' ').trim()),
  log: document.querySelector('#log').innerText.slice(0, 300),
  ua: navigator.userAgent
})"""


class SmokeTest:
    """Drives one real launch: window shown -> JS bridge -> HUD boot -> DOM probe -> close."""

    def __init__(self, window, api: JarvisAPI, report: str | None, hold: float) -> None:
        self._window, self._api, self._report, self._hold = window, api, report, hold
        self.passed = False

    def start(self) -> None:
        threading.Thread(target=self._run, name="smoke-test", daemon=True).start()

    def _run(self) -> None:
        result: dict[str, Any] = {"ok": False, "version": APP_VERSION, "platform": platform.platform(),
                                  "webview2": webview2_version(), "stage": "window"}
        try:
            for stage, event, timeout in (("window", self._window.events.shown, 90),
                                          ("bridge", self._api._ready, 60),
                                          ("boot", self._api._booted, 45)):
                result["stage"] = stage
                if not event.wait(timeout):
                    raise TimeoutError(f"no '{stage}' signal within {timeout}s")
            time.sleep(2.0)
            result["stage"] = "probe"
            ui = json.loads(self._window.evaluate_js(_SMOKE_PROBE))
            result["ui"] = ui
            result["renderer"] = getattr(sys.modules.get("webview"), "renderer", None)
            problems = []
            if ui.get("bootOverlay"):
                problems.append("boot overlay still visible")
            if "state-" not in (ui.get("body") or "") or "booting" in (ui.get("body") or ""):
                problems.append(f"unexpected body classes: {ui.get('body')}")
            if min(ui.get("reactor") or [0]) < 100:
                problems.append(f"reactor canvas too small: {ui.get('reactor')}")
            result["problems"] = problems
            result["ok"] = not problems
            result["stage"] = "done"
        except Exception as exc:  # report every failure mode
            result["error"] = f"{exc.__class__.__name__}: {exc}"
            log.exception("Smoke test failed at stage %s", result["stage"])
        self.passed = bool(result["ok"])
        log.info("Smoke test %s: %s", "PASSED" if self.passed else "FAILED", json.dumps(result))
        if self._report:
            Path(self._report).write_text(json.dumps(result, indent=2), encoding="utf-8")
        time.sleep(self._hold if self.passed else 1.0)  # keep the window up for a CI screenshot
        try:
            self._window.destroy()
        except Exception:
            pass
        # Never let a wedged GUI loop hang CI.
        threading.Timer(20, lambda: os._exit(0 if self.passed else 1)).start()


def _watch_ui(window, api: JarvisAPI) -> None:
    """Never leave the user staring at nothing: report a window that never appears or a page that never connects."""
    if not window.events.shown.wait(90):
        log.error("The window did not appear within 90 s; thread stacks follow in jarvis-fault.log")
        if _fault_file is not None:
            faulthandler.dump_traceback(_fault_file, all_threads=True)
        message_box(
            "J.A.R.V.I.S. is taking unusually long to open its window.\n\n"
            f"If nothing appears, close it from Task Manager and send this log file:\n{app_data_dir() / 'jarvis.log'}",
            "J.A.R.V.I.S. - still starting",
            _MB_OK | _MB_ICONWARNING,
        )
        return
    if api._ready.wait(60):
        return
    log.error("The interface did not connect to the backend within 60 s of the window appearing")
    message_box(
        "J.A.R.V.I.S. opened its window, but the interface did not load.\n\n"
        "This usually means the Microsoft Edge WebView2 Runtime is damaged or blocked. "
        f"Try reinstalling it from Microsoft, then start J.A.R.V.I.S. again.\n\nLog file:\n{app_data_dir() / 'jarvis.log'}",
        "J.A.R.V.I.S. - interface failed to load",
        _MB_OK | _MB_ICONERROR,
    )
    try:
        window.destroy()
    except Exception:
        pass


_PROBE_JS = r"""JSON.stringify((() => {
  const box = (sel) => { const e = document.querySelector(sel); if (!e) return null; const r = e.getBoundingClientRect();
    return r.width ? [r.left, r.top, r.width, r.height] : null; };
  return { t: Date.now(), dpr: window.devicePixelRatio, booted: !document.body.classList.contains('booting'), w: innerWidth, h: innerHeight,
    body: document.body.className, drag: box('#titlebar .tb-fill'), min: box('#btn-min'), max: box('#btn-max'), close: box('#btn-close'),
    grip: box('.rz-bottom'), debug: window.__winDebug || null,
    at: (() => { const r = document.querySelector('#titlebar .tb-fill'); if (!r) return null; const b = r.getBoundingClientRect();
      const e = document.elementFromPoint(b.left + b.width / 2, b.top + b.height / 2); return e ? (e.id || e.className || e.tagName) : null; })() };
})())"""


def _window_probe(window, api: JarvisAPI, path: Path) -> None:
    """CI only: keep writing where the title bar and buttons are (screen pixels) and the window's state."""
    if not window.events.shown.wait(120):
        return
    import ctypes

    while True:
        try:
            frame = api._frame_ready()
            if frame and ctypes.windll.user32.IsIconic(frame._hwnd()):
                time.sleep(0.4)  # minimised: don't ask the page anything until it's back
                continue
            ui = json.loads(window.evaluate_js(_PROBE_JS))
            frame = api._frame_ready()
            ui["state"] = frame.state() if frame else {}
            ui["rect"] = frame.rect() if frame else None
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(ui), encoding="utf-8")
            os.replace(tmp, path)
        except Exception:
            log.debug("window probe hiccup", exc_info=True)
        time.sleep(0.4)


def _diag_dotnet_crash() -> int:
    """CI check: throw on a .NET thread and let the AppDomain hook report it (process ends with 0xE0434352)."""
    from System.Threading import Thread as NetThread
    from System.Threading import ThreadStart

    def boom() -> None:
        raise RuntimeError("diagnostic .NET crash test")

    NetThread(ThreadStart(boom)).Start()
    time.sleep(15)  # releases the GIL so the crash handler can run; the process should terminate first
    return 3


# ============================================================================ updates
def _update_status(state_path: str) -> str:
    try:
        return str(json.loads(Path(state_path).read_text(encoding="utf-8")).get("status") or "")
    except Exception:
        return ""


def wire_updates(assistant, window, bridge: EventBridge, args) -> None:
    """Give the updater what it needs to restart into a new version, and tell a freshly updated copy what happened."""
    def quit_now() -> None:
        try:
            bridge.close()
            assistant.shutdown()
        except Exception:
            log.debug("shutdown before update hiccup", exc_info=True)
        threading.Timer(8, lambda: os._exit(0)).start()  # never linger
        try:
            window.destroy()
        except Exception:
            os._exit(0)

    updater = assistant.updater
    updater.hooks = {"release_lock": release_single_instance, "quit": quit_now}
    if args.after_update:
        assistant.just_updated = updater.after_update(Path(args.after_update))
    if args.install_update_when_ready:
        updater.auto_install = True


# ============================================================================ main
def log_environment() -> None:
    log.info("Platform: %s | Python %s | frozen=%s | exe=%s", platform.platform(), platform.python_version(),
             getattr(sys, "frozen", False), sys.executable)
    if sys.platform == "win32":
        log.info("Edge WebView2 runtime: %s", webview2_version() or "NOT FOUND")


def main(argv: list[str] | None = None) -> int:
    global _dialogs_enabled
    parser = argparse.ArgumentParser(description="J.A.R.V.I.S. desktop voice assistant")
    parser.add_argument("--debug", action="store_true", help="enable the web inspector and verbose logs")
    parser.add_argument("--selftest", action="store_true", help="run headless diagnostics and exit")
    parser.add_argument("--selftest-report", metavar="PATH", help="write the self-test JSON report to PATH")
    parser.add_argument("--smoke-test", action="store_true", help="open the real window, verify the HUD boots, then exit")
    parser.add_argument("--smoke-report", metavar="PATH", help="write the smoke-test JSON report to PATH")
    parser.add_argument("--smoke-hold", type=float, default=0.0, metavar="SECONDS", help="keep the window open after passing")
    parser.add_argument("--diag-dotnet-crash", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--gui", help="force a pywebview backend (edgechromium, qt, gtk, cef)")
    parser.add_argument("--framed", action="store_true", help="use a normal OS window frame")
    parser.add_argument("--after-update", metavar="STATE", help=argparse.SUPPRESS)  # started by the updater
    parser.add_argument("--finish-update", metavar="STATE", help=argparse.SUPPRESS)  # the new exe swapping itself in
    parser.add_argument("--install-update-when-ready", action="store_true", help=argparse.SUPPRESS)  # the update CI check
    parser.add_argument("--window-probe", metavar="PATH", help=argparse.SUPPRESS)  # the window CI check: where things are
    args, _unknown = parser.parse_known_args(argv)

    ensure_std_streams()
    configure_logging(args.debug)
    enable_fault_log()
    if args.selftest or args.smoke_test:
        _dialogs_enabled = False
    log.info("J.A.R.V.I.S. %s starting", APP_VERSION)
    log_environment()

    if args.selftest:
        return run_selftest(args.selftest_report)
    if args.finish_update:  # no window: swap the files, start the new Jarvis.exe, roll back if it doesn't come up
        from core.updater import Updater

        state = Path(args.finish_update)
        return Updater(APP_VERSION, state_dir=state.parent, supported=True).finish(state)

    if sys.platform == "win32" and not args.gui:
        if webview2_version() is None:
            log.error("Microsoft Edge WebView2 runtime not found; cannot render the HUD")
            choice = message_box(
                "J.A.R.V.I.S. needs the Microsoft Edge WebView2 Runtime to draw its interface, "
                "and it isn't installed on this PC.\n\nClick OK to open Microsoft's download page "
                "(choose \"Evergreen Bootstrapper\"), install it, then start J.A.R.V.I.S. again.",
                "J.A.R.V.I.S. - component missing",
                _MB_OKCANCEL | _MB_ICONWARNING,
            )
            if choice == _IDOK:
                webbrowser.open(WEBVIEW2_DOWNLOAD_URL)
            return 2
        if args.after_update and os.environ.get("JARVIS_TEST_FAIL_AFTER_UPDATE") == "1" and _update_status(args.after_update) == "starting":
            log.error("JARVIS_TEST_FAIL_AFTER_UPDATE: pretending this update is broken")
            return 3  # lets the CI check prove that a broken update is rolled back
        if args.after_update:
            locked = acquire_single_instance_patiently(20)
        else:
            locked = args.smoke_test or acquire_single_instance()
        if not locked:
            log.info("Another instance is already running; exiting")
            message_box("J.A.R.V.I.S. is already running.\n\nIt can take a few seconds to appear after you open it.",
                        "J.A.R.V.I.S.", _MB_OK | _MB_ICONINFO)
            return 0

    started = time.monotonic()
    import webview

    config = Config()
    bridge = EventBridge()
    frameless = bool(config.get("frameless", True)) and not args.framed
    api = JarvisAPI(None, bridge, frameless)
    api._after_update = args.after_update or ""
    assistants: list = []

    width, height, min_size, maximized = window_geometry(webview)  # also loads the native GUI backend
    hook_dotnet_crashes()
    if args.diag_dotnet_crash:
        return _diag_dotnet_crash()
    log.info("Creating window %dx%d min=%s maximized=%s frameless=%s", width, height, min_size, maximized, frameless)
    native_frame = frameless and sys.platform == "win32" and not args.gui
    if native_frame:
        # The page asks Windows to move / size the window itself (see core/winframe.py), so pywebview's
        # JavaScript dragging is switched off; and a borderless window "maximised" by WinForms would cover the
        # taskbar, so start normal and maximise to the work area once it's shown.
        webview.settings["DRAG_REGION_SELECTOR"] = ".pywebview-drag-off"
    window = webview.create_window(
        "J.A.R.V.I.S.",
        url=str(resource_path("web", "index.html")),
        js_api=api,
        width=width,
        height=height,
        min_size=min_size,
        maximized=maximized and not native_frame,
        frameless=frameless,
        easy_drag=False,
        background_color="#02070d",
        text_select=True,
    )
    api._attach(window)
    bridge.attach(window)
    def on_shown() -> None:
        close_splash()
        log.info("Window shown after %.1fs (renderer: %s)", time.monotonic() - started, getattr(webview, "renderer", "?"))
        if native_frame and maximized:
            threading.Timer(0.3, lambda: (api._frame_ready() and api._frame.maximize())).start()

    window.events.shown += on_shown

    def load_assistant() -> None:
        """Runs on pywebview's worker thread once the GUI loop is up: heavy imports happen behind the boot screen."""
        try:
            t0 = time.monotonic()
            from core.assistant import Assistant

            assistant = Assistant(config, bridge.emit)
            assistants.append(assistant)
            wire_updates(assistant, window, bridge, args)
            api._set_assistant(assistant)
            log.info("Core loaded in %.1fs", time.monotonic() - t0)
            assistant.start()
            log.info("Subsystems ready %.1fs after launch", time.monotonic() - started)
        except Exception as exc:
            report_fatal(exc, "core initialisation")
            try:
                window.destroy()
            except Exception:
                pass

    def on_closed() -> None:
        bridge.close()
        for assistant in assistants:
            assistant.shutdown()

    window.events.closed += on_closed

    if args.window_probe:
        threading.Thread(target=_window_probe, args=(window, api, Path(args.window_probe)), name="window-probe", daemon=True).start()
    smoke = SmokeTest(window, api, args.smoke_report, args.smoke_hold) if args.smoke_test else None
    if smoke:
        smoke.start()
    else:
        threading.Thread(target=_watch_ui, args=(window, api), name="ui-watchdog", daemon=True).start()

    webview.start(
        load_assistant,
        debug=args.debug,
        http_server=True,
        private_mode=True,
        gui=args.gui,
        icon=window_icon(),
    )
    log.info("GUI loop ended")
    code = (0 if smoke.passed else 1) if smoke else 0
    # The window is gone: don't let a background thread (a download, a helper, a timer) keep JARVIS
    # running invisibly. Give shutdown a moment to finish, then leave.
    for assistant in assistants:
        try:
            assistant.shutdown()
        except Exception:
            log.debug("shutdown hiccup", exc_info=True)
    logging.shutdown()
    threading.Timer(3.0, lambda: os._exit(code)).start()
    return code


if __name__ == "__main__":
    install_crash_handlers()
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 - last line of defence: never die silently
        report_fatal(exc)
        sys.exit(1)
