import io
import os
import sys
import wave
from pathlib import Path

import numpy as np
import pytest

os.environ["SDL_AUDIODRIVER"] = "dummy"  # tests never need real speakers
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Keep config, logs and caches out of the real user profile."""
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "jarvis-home"))
    return tmp_path


@pytest.fixture(autouse=True)
def no_real_browser(monkeypatch):
    """Nothing in the tests ever starts a real browser: launches are recorded in ``no_real_browser`` instead."""
    from core.assistant import Assistant
    from core.browser import BrowserManager

    launched: list[str] = []
    # the same behaviour on every OS: tests that watch windows say so (watch=True), and nothing waits for real app windows
    monkeypatch.setattr(BrowserManager, "WATCH", False)
    monkeypatch.setattr(Assistant, "LAUNCH_WAIT", 0.0)
    monkeypatch.setattr(BrowserManager, "real_launch", BrowserManager._launch, raising=False)  # for tests of the launch itself
    monkeypatch.setattr(BrowserManager, "_launch", lambda self, url, key: launched.append(url or ""))
    return launched


@pytest.fixture
def config(tmp_path):
    from core.config import Config

    return Config(tmp_path / "config.json")


def wav_bytes(seconds: float = 0.25, freq: float = 330.0, rate: int = 24000) -> bytes:
    t = np.arange(int(seconds * rate)) / rate
    pcm = (0.4 * np.sin(2 * np.pi * freq * t) * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


@pytest.fixture
def mock_ollama():
    from tests.mock_ollama import MockOllama

    server = MockOllama().start()
    yield server
    server.stop()
