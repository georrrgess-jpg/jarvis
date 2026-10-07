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
