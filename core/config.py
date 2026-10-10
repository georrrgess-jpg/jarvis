"""Persistent user settings and filesystem locations.

Settings live in a small JSON file inside the per-user application data folder
(``%APPDATA%\\JARVIS`` on Windows). Every value coming from the UI is validated
against the type of its default, so a malformed request can never corrupt the file.
"""

from __future__ import annotations

import json
import logging
import os
import re
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
    "google_user_email": "",
    "last_document": {},  # the Google file made or opened last, so "open it" works after a restart  # the linked Google account ("email it to me")
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
    "persona": "jarvis",  # jarvis | harper | friday | sage (see core/personas.py)
    "persona_theme": True,  # switching personality also switches the HUD colours
    "music_service": "youtube",
    "duck_media": True,  # lower other apps' sound while JARVIS speaks (needs Windows' per-app volume)
    "duck_level": 0.3,  # ... to this fraction of their volume
    "media_history": [],  # what was played lately, for "resume what I was listening to"
    "browser_profile": "auto",  # which Chrome / Edge / Brave profile links open in ("auto" = the linked Google account's)
    "favorite_website": "",  # "open my favourite website"
    "corrections": [],  # "that was wrong": rewrites learnt from the user's corrections (see core/feedback.py)
    "feedback_log": [],  # the last corrections: what was heard, what JARVIS did, what was meant
    "auto_update": True,  # look for new versions and get them ready in the background (installing always waits for you)
    "activity_history": [],  # the last actions and whether they were verified (Automation Center)  # youtube | spotify: where "play <song>" goes (see core/media.py)
    "protocols": [],  # named lists of commands: [{id, name, steps, schedule, created, last_run}] (see core/protocols.py)
    "custom_personas": [],  # personalities the user made: [{id, name, description, voice, gender, color, address}]
    "persona_colors": {},  # the user's HUD colour for any personality: {persona id: "#RRGGBB"}
    "wake_jarvis_always": True,  # "Hey Jarvis" works whichever personality is active
    "wake_learn_auto": True,  # learn a personality's name as a wake word the first time it's used
    "theme": "arc",  # arc | mark3 | stealth | violet | rose
    "weather_location": "",  # town for weather ("London"); empty = what you've told me, else your connection's location
    "temperature_unit": "auto",  # auto (from your language) | celsius | fahrenheit
    # Long-term memory (memory.db next to the settings file; never leaves this computer)
    "memory_enabled": True,  # remember facts, preferences, routines and projects across sessions
    "memory_auto_learn": True,  # pick things up from conversation by itself (otherwise only "remember that ...")
    "memory_resume": True,  # carry on the last conversation if it ended less than 3 hours ago
    "routine_reminders": True,  # mention today's routines in the greeting
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
    "music_service": ("youtube", "spotify"),
    "theme": ("arc", "mark3", "stealth", "violet", "rose"),
    "persona": ("jarvis", "harper", "friday", "sage"),
    "temperature_unit": ("auto", "celsius", "fahrenheit"),
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
    "duck_level": (0.05, 1.0),
}

_MAX_TEXT = {"weather_location": 80, "vision_exclusions": 1000, "custom_instructions": 2000, "user_title": 40}


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


_SIZE_LIMITS = {"protocols": 600_000, "activity_history": 120_000, "media_history": 40_000, "custom_personas": 60_000, "corrections": 80_000, "feedback_log": 60_000}


def _coerce(key: str, value: Any) -> Any:
    default = DEFAULTS[key]
    if isinstance(default, (list, dict)):
        if isinstance(value, str):
            value = json.loads(value or ("[]" if isinstance(default, list) else "{}"))
        if not isinstance(value, type(default)):
            raise ValueError(f"{key} must be a {'list' if isinstance(default, list) else 'mapping'}")
        if len(json.dumps(value)) > _SIZE_LIMITS.get(key, 20000):
            raise ValueError(f"{key} is too large")
        if key == "persona_colors":
            return {str(k)[:40]: (str(v).upper() if str(v).startswith("#") else str(v)) for k, v in value.items()
                    if re.match(r"^#[0-9a-fA-F]{6}$", str(v)) or str(v) in _CHOICES["theme"]}
        return json.loads(json.dumps(value))  # a private, plain-JSON copy
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
    if key == "theme" and re.match(r"^#[0-9a-fA-F]{6}$", text):
        return text.upper()  # a custom colour
    if key == "persona" and text.startswith("my-") and re.match(r"^my-[a-z0-9-]{1,40}$", text):
        return text  # one of the user's own personalities
    if key in _CHOICES and text not in _CHOICES[key]:
        raise ValueError(f"{key} must be one of {', '.join(_CHOICES[key])}")
    return text[: _MAX_TEXT.get(key, 400)]


class Config:
    """Thread-safe settings store backed by a JSON file."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = Path(path) if path else app_data_dir() / "config.json"
        self._lock = threading.RLock()
        self._data: dict[str, Any] = json.loads(json.dumps(DEFAULTS))  # private copies of the list/dict defaults
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
