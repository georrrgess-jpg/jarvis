"""Persistent user settings and filesystem locations.

Settings live in a small JSON file inside the per-user application data folder
(``%APPDATA%\\JARVIS`` on Windows). Every value coming from the UI is validated
against the type of its default, so a malformed request can never corrupt the file.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

from . import APP_NAME

log = logging.getLogger("jarvis.config")

DEFAULTS: dict[str, Any] = {
    # Language model (Ollama)
    "ollama_host": "http://localhost:11434",
    "model": "llama3.2",
    "temperature": 0.7,
    "max_history_turns": 12,
    "custom_instructions": "",
    # Abilities (tools the model may use)
    "allow_files": True,  # find, open and read files / apps on this computer
    "allow_internet": True,  # web search, read and open web pages
    # Google Docs, Slides & Sheets via the user's own Apps Script bridge
    "google_script_url": "",
    "google_bridge_token": "",
    "google_bridge_version": 0,
    "google_user_email": "",  # the linked Google account ("email it to me")
    "user_name": "",  # for email sign-offs  # version of the script the user deployed (see BRIDGE_VERSION in the .gs)
    # Voice output (edge-tts)
    "voice": "en-GB-RyanNeural",
    "speech_rate": 0,  # percent, -50..50
    "speech_pitch": 0,  # Hz, -20..20
    "voice_enabled": True,
    # Interface sounds
    "sfx_enabled": True,
    "sfx_volume": 0.45,
    # Speech input
    "stt_engine": "auto",  # auto | google | whisper | vosk
    "stt_language": "en-US",
    "link_browser": "default",  # default | chrome | edge | firefox | brave: where Google links and websites open
    "auto_language": True,  # detect the language spoken/typed, reply in it and switch to a matching voice
    "stt_extra_languages": "",  # other languages to listen for, e.g. "es-ES, fr-FR" (empty = the PC's language)
    "whisper_model": "base.en",
    "vosk_model_path": "",
    "pause_threshold": 1.0,  # seconds of silence after which JARVIS checks whether you've finished
    "patience": 3.0,  # extra seconds it keeps waiting when your sentence sounds unfinished ("open the...")
    "listen_timeout": 10,  # seconds to wait for speech to begin
    "max_phrase_seconds": 45,
    "auto_listen": False,  # keep the conversation going hands-free
    "wake_word": True,  # say "Hey Jarvis" to start listening
    "wake_sensitivity": 0.5,  # 0 = strict, 1 = very sensitive
    # Personality / window
    "user_title": "sir",
    "theme": "arc",  # arc | mark3 | stealth | violet
    # Vision: seeing the screen (local vision model via Ollama + Windows OCR) and acting on it
    "vision_model": "",  # empty = the best installed vision model (qwen2.5vl preferred)
    "allow_control": True,  # let JARVIS click, type and press keys when asked
    "act_confirm": "auto",  # auto: confirm only unsure or risky actions | always: confirm every action
    "watch_interval": 2.0,  # seconds between looks while watching the screen
    "vision_exclusions": "password, 1password, bitwarden, lastpass, keepass, dashlane, bank, banking, paypal",
    "frameless": True,
}

_CHOICES: dict[str, tuple[str, ...]] = {
    "stt_engine": ("auto", "google", "whisper", "vosk"),
    "link_browser": ("default", "chrome", "edge", "firefox", "brave"),
    "theme": ("arc", "mark3", "stealth", "violet"),
    "act_confirm": ("auto", "always"),
}

_RANGES: dict[str, tuple[float, float]] = {
    "temperature": (0.0, 1.5),
    "max_history_turns": (1, 50),
    "speech_rate": (-50, 50),
    "speech_pitch": (-20, 20),
    "sfx_volume": (0.0, 1.0),
    "pause_threshold": (0.4, 3.0),
    "listen_timeout": (2, 30),
    "max_phrase_seconds": (5, 120),
    "patience": (0.0, 8.0),
    "watch_interval": (1.0, 10.0),
    "wake_sensitivity": (0.0, 1.0),
}

_MAX_TEXT = {"vision_exclusions": 1000, "custom_instructions": 2000, "user_title": 40}


def app_data_dir() -> Path:
    """Per-user writable folder for settings, logs and caches."""
    override = os.environ.get("JARVIS_HOME")
    if override:
        base = Path(override)
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / APP_NAME
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / APP_NAME
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "jarvis"
    base.mkdir(parents=True, exist_ok=True)
    return base


def resource_path(*parts: str) -> Path:
    """Locate bundled read-only resources both from source and inside a PyInstaller build."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base.joinpath(*parts)


def _coerce(key: str, value: Any) -> Any:
    default = DEFAULTS[key]
    if isinstance(default, bool):
        if isinstance(value, str):
            value = value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if isinstance(default, (int, float)):
        if isinstance(value, bool) or value is None:
            raise ValueError(f"{key} must be a number")
        number = float(value)
        if number != number:  # NaN
            raise ValueError(f"{key} must be a number")
        lo, hi = _RANGES.get(key, (float("-inf"), float("inf")))
        number = min(max(number, lo), hi)
        return int(round(number)) if isinstance(default, int) else round(number, 3)
    text = "" if value is None else str(value).strip()
    if key in _CHOICES and text not in _CHOICES[key]:
        raise ValueError(f"{key} must be one of {', '.join(_CHOICES[key])}")
    return text[: _MAX_TEXT.get(key, 400)]


class Config:
    """Thread-safe settings store backed by a JSON file."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = Path(path) if path else app_data_dir() / "config.json"
        self._lock = threading.RLock()
        self._data: dict[str, Any] = dict(DEFAULTS)
        self.load()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("Ignoring unreadable config %s: %s", self._path, exc)
            return
        if not isinstance(raw, dict):
            return
        with self._lock:
            for key, value in raw.items():
                if key in DEFAULTS:
                    try:
                        self._data[key] = _coerce(key, value)
                    except (TypeError, ValueError):
                        log.warning("Ignoring invalid setting %s=%r", key, value)

    def save(self) -> None:
        with self._lock:
            payload = json.dumps(self._data, indent=2, sort_keys=True)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=".config-", suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, self._path)
        except OSError as exc:
            log.error("Could not save settings to %s: %s", self._path, exc)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def __getitem__(self, key: str) -> Any:
        with self._lock:
            return self._data[key]

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data)

    def update(self, changes: dict[str, Any], persist: bool = True) -> dict[str, Any]:
        """Validate and apply ``changes``; returns only the keys whose value actually changed."""
        if not isinstance(changes, dict):
            raise ValueError("settings must be an object")
        applied: dict[str, Any] = {}
        with self._lock:
            staged = {k: _coerce(k, v) for k, v in changes.items() if k in DEFAULTS}
            for key, value in staged.items():
                if self._data.get(key) != value:
                    self._data[key] = value
                    applied[key] = value
        if applied and persist:
            self.save()
        return applied
