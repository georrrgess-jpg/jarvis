import inspect

from app import EventBridge, JarvisAPI, run_selftest


def test_coalesce_merges_tokens_and_keeps_latest_mic_frame():
    events = [
        {"type": "assistant_token", "id": "a1", "text": "Hel"},
        {"type": "mic_frame", "level": 1},
        {"type": "assistant_token", "id": "a1", "text": "lo"},
        {"type": "state", "state": "SPEAKING"},
        {"type": "assistant_token", "id": "a1", "text": "!"},
        {"type": "mic_frame", "level": 9},
    ]
    out = EventBridge.coalesce(events)
    assert out == [
        {"type": "assistant_token", "id": "a1", "text": "Hello"},
        {"type": "state", "state": "SPEAKING"},
        {"type": "assistant_token", "id": "a1", "text": "!"},
        {"type": "mic_frame", "level": 9},
    ]


def test_bridge_holds_events_until_ui_ready():
    class Window:
        def __init__(self):
            self.scripts = []

        def run_js(self, script):
            self.scripts.append(script)

    import time

    bridge = EventBridge()
    window = Window()
    bridge.attach(window)
    bridge.emit("state", {"state": "IDLE"})
    time.sleep(0.15)
    assert window.scripts == []
    bridge.set_ready()
    deadline = time.time() + 2
    while not window.scripts and time.time() < deadline:
        time.sleep(0.02)
    bridge.close()
    assert window.scripts and '"state":"IDLE"' in window.scripts[0]
    assert window.scripts[0].startswith("window.JARVIS&&window.JARVIS.receive([")


def test_js_api_exposes_only_methods():
    """pywebview walks public attributes recursively; anything non-callable would leak internals."""
    api = JarvisAPI(assistant=object(), bridge=EventBridge())
    public = {name: getattr(api, name) for name in dir(api) if not name.startswith("_")}
    assert public and all(inspect.ismethod(v) for v in public.values())
    assert {"send_text", "start_listening", "stop_listening", "interrupt", "get_system_stats", "check_ollama"} <= set(public)


def test_open_url_is_restricted():
    api = JarvisAPI(assistant=object(), bridge=EventBridge())
    assert api.open_url("file:///etc/passwd") is False
    assert api.open_url("https://evil.example/ollama.com/") is False


def test_selftest_passes(tmp_path):
    report = tmp_path / "report.json"
    assert run_selftest(str(report)) == 0
    assert '"ok": true' in report.read_text()


# --- Windows start-up regressions -------------------------------------------------------------


def test_windows_window_icon_is_a_real_ico():
    """WinForms feeds the icon to System.Drawing.Icon, which crashes the whole process on a PNG."""
    from pathlib import Path

    from app import window_icon

    ico = window_icon("win32")
    assert ico and ico.endswith(".ico")
    header = Path(ico).read_bytes()[:4]
    assert header == b"\x00\x00\x01\x00", "not an ICO file"
    assert window_icon("linux").endswith(".png")


def test_build_bundles_the_ico():
    import argparse

    import build

    args = argparse.Namespace(onedir=False, console=False, no_splash=True)
    cli = build.pyinstaller_args(args, None, "1.0.0")
    datas = [cli[i + 1] for i, a in enumerate(cli) if a == "--add-data"]
    assert any("jarvis.ico" in d for d in datas)


class _Screen:
    def __init__(self, w, h):
        self.width, self.height = w, h


class _FakeWebview:
    def __init__(self, screens):
        self.screens = screens


def test_window_fits_small_screens():
    from app import window_geometry

    w, h, min_size, maximized = window_geometry(_FakeWebview([_Screen(1024, 768)]))
    assert w <= 1024 and h <= 768
    assert min_size[0] <= w and min_size[1] <= h
    assert maximized is True


def test_window_on_large_and_typical_screens():
    from app import window_geometry

    assert window_geometry(_FakeWebview([_Screen(2560, 1440)])) == (1720, 1040, (1000, 640), False)
    w, h, _, maximized = window_geometry(_FakeWebview([_Screen(1920, 1080)]))
    assert (w, h, maximized) == (1651, 928, False)


def test_window_geometry_survives_missing_screen_info():
    from app import window_geometry

    class Broken:
        @property
        def screens(self):
            raise RuntimeError("no backend")

    assert window_geometry(Broken()) == (1480, 920, (1000, 640), False)


def test_js_api_waits_for_background_core():
    import threading

    api = JarvisAPI(assistant=None, bridge=EventBridge())

    class Core:
        def core_stats(self):
            return {"ok": True}

    threading.Timer(0.1, lambda: api._set_assistant(Core())).start()
    assert api.get_core_stats() == {"ok": True}
