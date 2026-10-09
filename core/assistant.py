"""The JARVIS brain: wires speech input, the language model and the voice together.

Every interaction runs as a *turn* on its own worker thread. Starting a new turn
(or pressing Escape) cancels the previous one, so the user can always barge in.
A cancelled turn keeps running until it notices, but it is no longer allowed to
touch the state machine or the UI.
"""

from __future__ import annotations

import itertools
import json
import logging
import random
import sys
import queue
from collections import deque
from urllib.parse import quote_plus
import re
import threading
import time
from datetime import datetime
from typing import Any, Callable, Iterable, Iterator

from . import APP_VERSION
from .audio import VIS_FPS, AudioEngine, spectrum_frames
from .compose import (EditRequest, WriteRequest, clean_document, document_prompt, extra_slide_prompt, parse_deck,
                      parse_edit_request, parse_write_request, revise_prompt, section_prompt, slides_prompt)
from .config import Config, app_data_dir
from .gdrive import GoogleRequest, NewFileRequest, parse_google_request, parse_new_file
from . import docs_keys
from .docops import DocCommand, docs_tab_title, parse_doc_command, to_plain, writing_prompt
from .google_bridge import BridgeError
from .mail import (Draft, EmailRequest, email_prompt, first_name, gmail_compose_url, is_cancellation, is_confirmation,
                   parse_email, parse_email_request)
from .llm import LLMConnectionError, LLMEngine, LLMError, LLMModelError, OllamaStatus
from .sfx import SoundFX
from .state import State, StateMachine
from .stt import ENGINE_LABELS, Capture, SpeechInput, STTError
from .system import SystemMonitor
from .tools import Toolbox, ToolError, describe_call
from . import osctl, tabs, wakeword
from .quick import Quick, describe_duration, parse_quick, pick
from .ocr import default_ocr
from .screen import ForegroundTracker, ScreenError, default_desktop, is_private
from .vision import (DEFAULT_VISION_MODEL, ActRequest, LookRequest, VisionEngine, VisionUnavailable, Watcher, WatchRequest,
                     data_url, is_risky, parse_act, parse_look, parse_watch, wants_vision_install)
from .compose import SHORT_FORM
from .language import LANGUAGES, Detection, base_language, detect, voice_for
from .patience import looks_unfinished
from .media import MediaCommand, find_youtube_video, parse_media, spotify_targets, youtube_search_url
from .mediahub import _HINTS, MediaHub, MediaSession, describe_position, hint_from
from .winhelper import WinHelper
from .browser import (AUTHENTICATION_REQUIRED, CONSENT, ERROR as BROWSER_ERROR, FIRST_RUN_SETUP, OFFLINE, PROFILE_SELECTION,
                      BrowserManager, NavResult, needs_account, site_name, tab_title as tab_title_of)
from .protocols import (CATEGORIES as PROTOCOL_CATEGORIES, ICONS as PROTOCOL_ICONS, TEMPLATES as PROTOCOL_TEMPLATES, Protocol, ProtocolCommand,
                        ProtocolError, ProtocolStore, clean_name as clean_protocol_name, condition_met as protocol_condition,
                        describe_condition, describe_schedule, due as protocol_due, join_names, parse_protocol_command, recording_reply,
                        risk as protocol_risk, split_steps, spoken_steps, step_kind, structured_step)
from .hotkeys import HotkeyManager
from .weather import Weather, WeatherError, asks_pc_temperature, parse_weather
from .monitors import MonitorCommand, describe as describe_monitor, parse_monitor_command, resolve as resolve_monitor, split_screen_phrase
from .memory import Memory, MemoryCommand, MemoryEngine, first_person_echo, parse_memory_command, spoken, today_routines
from .personas import (PERSONAS, Persona, address_for, asks_who, get_persona, is_custom, load_custom, names_pattern, parse_switch,
                       theme_for, validate_custom)
from .wakewords import WakeWords

PRESET_THEMES = ("arc", "mark3", "stealth", "violet", "rose")
from .tts import CODE_MARK, EdgeTTS, SentenceSplitter, TTSError, clean_for_speech

log = logging.getLogger("jarvis.assistant")

Emit = Callable[[str, dict], None]
_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/:]{0,120}$")
def _bare(text: str) -> str:
    """'stop listening, Jarvis!' -> 'stop listening' (a personality's name or 'please' at the end)."""
    return re.sub(r"(?:[,\s]+(?:" + names_pattern() + r"|please|thanks|thank you))+\W*$", "", text.strip(), flags=re.I)


def _media_hint(name: str):
    """The test for 'is this the player the user named' ("youtube", "spotify", "video"...)."""
    return _HINTS.get(hint_from(name) or name, lambda s: True)


def _strip_wake(text: str) -> str:
    """'Hey Harper, what time is it' -> 'what time is it' (any personality's name)."""
    return re.sub(r"^\s*(?:(?:hey|hi|okay|ok)\s+)?(?:" + names_pattern() + r")\b[\s,.!?]*", "", text, count=1, flags=re.I)
_PAUSE_LISTENING = re.compile(
    r"^\W*(?:(?:please|can you|could you|would you|just|ok(?:ay)?|now)\s+)*(?:stop\s+listening(?:\s+(?:to\s+me|for\s+(?:now|a\s+(?:while|bit))|please))*|"
    r"(?:go\s+to\s+sleep|take\s+a\s+break)(?:\s+(?:for\s+now|please))?|(?:mute|turn\s+off|switch\s+off|disable)\s+(?:the\s+|your\s+|my\s+)?(?:mic|microphone|listening)|"
    r"mute\s+yourself|don'?t\s+listen(?:\s+to\s+me)?(?:\s+(?:for\s+now|anymore|any\s+more))?|stop\s+the\s+(?:mic|microphone))\W*$", re.I)
_RESUME_LISTENING = re.compile(
    r"^\W*(?:(?:please|can you|could you|ok(?:ay)?|now|you\s+can)\s+)*(?:start\s+listening(?:\s+(?:again|to\s+me))*|listen\s+(?:again|to\s+me(?:\s+again)?)|"
    r"resume\s+listening|wake\s+up|unmute(?:\s+(?:yourself|the\s+(?:mic|microphone)))?|(?:turn|switch)\s+(?:the\s+|your\s+)?(?:mic|microphone)\s+(?:back\s+)?on|"
    r"(?:turn|switch)\s+on\s+(?:the\s+|your\s+)?(?:mic|microphone)|you\s+can\s+listen(?:\s+again)?)\W*$", re.I)
_STOP_PHRASES = {
    "stop", "stop it", "stop talking", "stop listening", "cancel", "never mind", "nevermind", "forget it",
    "that's all", "thats all", "that is all", "nothing", "be quiet", "quiet", "shut up", "hush", "enough",
    "thank you that's all", "thanks that's all", "no thanks", "dismiss", "go to sleep", "standby", "stand by",
}
_LEAD = r"^(?:(?:hey |ok |okay )?jarvis[, ]+)?(?:please |can you |could you |would you |will you |let's |lets |i want to |i wanna )*"
_PLAY_COMMAND = re.compile(
    _LEAD + r"(?:play|fire up|boot up|game on)\s+(?:the\s+game\s+|a\s+game\s+of\s+|some\s+)?(?P<target>.+?)"
    r"(?:\s+(?:please|for me|now))?[\s.!?]*$", re.IGNORECASE)
_SEARCH_COMMAND = re.compile(
    _LEAD + r"(?:search(?:\s+(?:the\s+web|the\s+internet|online|google))?(?:\s+for)?|look\s+up|google(?!\s+(?:docs?|sheets?|slides?|drive|calendar|maps)\b)|find\s+out(?:\s+about)?)"
    r"\s+(?P<query>.+?)[\s.!?]*$", re.IGNORECASE)
# Questions whose answer changes over time: always ground them in a fresh search.
_LIVE_INFO = re.compile(
    r"\b(weather|forecast|temperature (?:in|outside)|news|headlines?|latest|scores?|who won|stock|share price|"
    r"price of|exchange rate|bitcoin|crypto|right now|today'?s|tonight|this week|traffic|release date|"
    r"when is the next|when does .* come out)\b", re.IGNORECASE)
_CLOCK = re.compile(
    r"^(?:(?:hey |ok )?jarvis[, ]+)?(?:what(?:'s| is)?\s+(?:the\s+)?(?P<what>time|date|day)(?:\s+is\s+it)?(?:\s+(?:today|now|right now))?"
    r"|what\s+day\s+is\s+(?:it|today)|what(?:'s| is)\s+today'?s\s+date)[\s?.!]*$", re.IGNORECASE)
# Only offer tools when the request plausibly needs one: small models otherwise call them for small talk.
_TOOL_CUES = re.compile(
    r"\b(open|launch|start|run|play|file|files|folder|document|doc|docs|docx|pdf|read|find|desktop|downloads|"
    r"spreadsheet|sheet|sheets|slides?|presentation|deck|app|game|search|look up|google|internet|online|web|website|"
    r"news|weather|latest|current|price|summari[sz]e|"
    r"write|create|make|add|edit|update|replace|list|delete|remove|move|reorder|rearrange|rename|table|row|rows|column|cell)\b", re.IGNORECASE)
_CLOSING = re.compile(r"^\W*(?:please\s+)?(?:close|quit|exit|shut|kill|end)\b", re.I)  # never answer these by opening things
_FAILED_REPLY = re.compile(r"^(?:I couldn't|I can't|I'm afraid|I could not|I cannot|Sorry|I don't have|I didn't|There's nothing|Nothing's playing|"
                           r"I don't know|That works on|Internet access is switched off|.{0,80}\b(?:didn't (?:load|open|respond)|isn't open|doesn't let me|"
                           r"is switched off)\b)", re.I)
_PLACEHOLDER = re.compile(r"\bmy\s+(?P<thing>game|project(?:\s+folder)?|playlist|favou?rite\s+(?:song|playlist|game|album|artist|show)|editor|ide|music\s+app|"
                          r"browser\s+game|work\s+app|chat\s+app|code\s+editor)\b", re.I)


def _verb_of(text: str) -> str:
    first = (text.split() or ["open"])[0].lower()
    return first if first in ("open", "launch", "play", "start", "run", "load") else "open"


_ICON_WORDS = [("game", r"game|gaming|steam|xbox|discord|fortnite|minecraft"), ("code", r"code|dev|vs\s*code|visual studio|github|project|program"),
               ("music", r"music|spotify|song|playlist|lo-?fi|jazz"), ("film", r"movie|film|netflix|youtube|entertain|watch|tv"),
               ("focus", r"focus|study|pomodoro|concentrat|deep work"), ("sun", r"morning|wake|sunrise|breakfast"),
               ("moon", r"night|bed|sleep|wind down|evening"), ("work", r"work|office|meeting|email|slack|teams|outlook"),
               ("party", r"party|celebrat|dance"), ("book", r"read|book|learn|research"), ("coffee", r"coffee|break|lunch")]
_CATEGORY_OF = {"game": "Gaming", "code": "Development", "music": "Entertainment", "film": "Entertainment", "focus": "Focus",
                "sun": "Morning", "moon": "Evening", "work": "Work", "party": "Entertainment", "book": "Focus", "coffee": "General"}


def _guess_icon(name: str, steps) -> str:
    text = " ".join([name or ""] + [getattr(s, "text", s) if not isinstance(s, dict) else s.get("text", "") for s in steps or []]).lower()
    return next((icon for icon, pattern in _ICON_WORDS if re.search(pattern, text)), "bolt")


def _guess_category(name: str, steps) -> str:
    return _CATEGORY_OF.get(_guess_icon(name, steps), "General")


_GOOGLE_SEARCH = re.compile(r"^(?:(?:please|can you|could you)\s+)*(?:(?:search|look)\s+(?:on\s+)?google\s+for\s+(?P<q>.+)|google\s+(?:search\s+)?for\s+(?P<q2>.+)|"
                            r"(?:search\s+for|look\s+up|search)\s+(?P<q3>.+?)\s+on\s+google)$", re.I)
_BROWSER_NAMES = {"chrome": "chrome", "google chrome": "chrome", "browser": "", "web browser": "", "internet": "", "edge": "edge",
                  "microsoft edge": "edge", "firefox": "firefox", "brave": "brave"}
_WHAT_PAGE = re.compile(r"^(?:(?:hey\s+)?\w+,\s*)?(?:what|which)\s+(?:page|website|site|tab|web\s*page)\s+(?:am\s+i\s+on|is\s+(?:this|open|showing)|is\s+chrome\s+on)|"
                        r"^what(?:'s|\s+is)\s+(?:this|the)\s+(?:page|website|site|tab)(?:\s+called)?|^where\s+am\s+i\s+in\s+(?:the\s+)?(?:browser|chrome)", re.I)
_FAVORITE_SITE = re.compile(r"^(?:my\s+)?favou?rite\s+(?:web\s*)?site\s+is\s+(?P<site>\S+(?:\s+\S+){0,3})[\s.!]*$", re.I)
_CHAIN_SPLIT = re.compile(r"\s*(?:,\s*(?:and\s+)?(?:then\s+)?|\s+and\s+(?:then\s+)?|\s+then\s+)(?=(?:open|launch|start|play|fire up|boot up)\b)", re.IGNORECASE)
_OPEN_COMMAND = re.compile(
    r"^(?:(?:hey |ok |okay )?jarvis[, ]+)?(?:please |can you |could you |would you )*(?:open|launch|start|run|load|pull up|bring up|show me)"
    r"\s+(?:up\s+)?(?:the\s+|my\s+)?(?P<target>.+?)(?:\s+(?:please|for me|now))?[\s.!?]*$",
    re.IGNORECASE,
)


def _you(text: str) -> str:
    """'my download finishes' -> 'your download finishes' (for speaking back to the user)."""
    swaps = {"my": "your", "mine": "yours", "i": "you", "i'm": "you're", "me": "you", "myself": "yourself", "i've": "you've"}
    return re.sub(r"\b(my|mine|i|i'm|me|myself|i've)\b", lambda m: swaps[m.group(1).lower()], text, flags=re.I)


def _tidy_memory(text: str) -> str:
    """Text typed into the Memory Core: 'I love jazz' -> 'You love jazz.'; anything else tidied up as it is."""
    from .memory import second_person

    t = re.sub(r"\s+", " ", str(text or "")).strip()
    if not t:
        raise ValueError("A memory needs some text.")
    return second_person(t) if re.search(r"\b(i|i'm|my|me|mine|myself)\b", t, re.I) else t[0].upper() + t[1:] + ("" if t[-1] in ".!?" else ".")


def _about_you(summary: str) -> str:
    """Episode summaries say "the user"; spoken back, that's "you"."""
    s = re.sub(r"\bthe user's\b", "your", summary, flags=re.I)
    s = re.sub(r"\bthe user\b", "you", s, flags=re.I)
    s = re.sub(r"\byou was\b", "you were", s)
    s = re.sub(r"\byou (asks|wants|needs|says|likes|has|is)\b", lambda m: "you " + {"asks": "ask", "wants": "want", "needs": "need", "says": "say", "likes": "like", "has": "have", "is": "are"}[m.group(1)], s)
    return s[0].lower() + s[1:] if s[:1].isupper() and not s.startswith("I ") else s


def _quick_small_talk():
    from .quick import _SMALL_TALK

    return _SMALL_TALK


def _seconds_until(clock: str, now: datetime | None = None) -> int | None:
    """'6 pm' / '6:30pm' / '17:30' / 'noon' / '5' -> seconds until the clock next shows that ('5' means whichever 5 o'clock comes first)."""
    from datetime import timedelta

    c = (clock or "").strip().lower()
    now = now or datetime.now()
    if c in ("noon", "midday"):
        options = [(12, 0)]
    elif c == "midnight":
        options = [(0, 0)]
    else:
        m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(?:([ap])\.?m\.?)?$", c)
        if not m:
            return None
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
        if hour > 23 or minute > 59:
            return None
        if m.group(3):
            if hour > 12:
                return None
            options = [(hour % 12 + (12 if m.group(3) == "p" else 0), minute)]
        elif hour > 12 or hour == 0 or m.group(2) and hour >= 13:
            options = [(hour, minute)]
        else:
            options = [(hour % 12, minute), (hour % 12 + 12, minute)]
    best = None
    for hour, minute in options:
        due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if due <= now:
            due += timedelta(days=1)
        best = due if best is None or due < best else best
    return int((best - now).total_seconds())


def _view_event(data: dict, file_id: str | None) -> dict:
    """Payload for the reader: ``kind`` is taken by emit(), so the file type travels as ``doc_kind``."""
    out = {k: v for k, v in data.items() if k != "kind"}
    return {**out, "doc_kind": data.get("kind"), "id": file_id}


class Turn:
    _ids = itertools.count(1)

    def __init__(self, kind: str) -> None:
        self.id = next(Turn._ids)
        self.kind = kind  # "listen" | "text" | "announce"
        self.cancel = threading.Event()
        self.stop_listening = threading.Event()
        self.hold = threading.Event()

    def abort(self) -> None:
        self.cancel.set()
        self.stop_listening.set()


class Speaker:
    """Synthesises sentences in the background and plays them back in order."""

    def __init__(self, assistant: "Assistant", turn: Turn, voice: str | None = None, language: str | None = None,
                 working: bool = False) -> None:
        self._a = assistant
        self._turn = turn
        self._working = working  # long task: go back to THINKING between spoken updates
        self._closed = False
        self._voice = voice
        self._auto = voice is None and bool(assistant.config.get("auto_language", True))
        self._lang_voice = self._voice_for_language(language) if self._auto and language else None
        self._sentences: "queue.Queue[str | None]" = queue.Queue()
        self._clips: "queue.Queue[tuple | None]" = queue.Queue(maxsize=3)
        self.spoke = False
        self._ducked = False
        self.failed: str | None = None
        self._threads = [
            threading.Thread(target=self._synth_loop, name=f"tts-{turn.id}", daemon=True),
            threading.Thread(target=self._play_loop, name=f"play-{turn.id}", daemon=True),
        ]
        for t in self._threads:
            t.start()

    def say(self, sentence: str) -> None:
        self._sentences.put(sentence)

    def _voice_for_language(self, code: str) -> str | None:
        """None means the user's chosen voice; otherwise a voice that speaks ``code`` (matching the personality)."""
        chosen = self._a.config.get("voice") or ""
        if code == base_language(chosen or "en"):
            return None
        return voice_for(code, chosen, self._a.persona.gender)

    def _pick_voice(self, text: str) -> str | None:
        if not self._auto:
            return self._voice
        found = detect(text)
        sure = found.confidence >= 0.9 or (found.confidence >= 0.5 and len(text.split()) >= 3)
        if sure:  # short or ambiguous sentences keep the reply's current voice
            self._lang_voice = self._voice_for_language(found.code)
        return self._lang_voice

    def close(self) -> None:
        self._closed = True
        self._sentences.put(None)

    def wait(self, timeout: float | None = None) -> None:
        deadline = None if timeout is None else time.monotonic() + timeout
        for t in self._threads:
            t.join(None if deadline is None else max(0.0, deadline - time.monotonic()))

    def _synth_loop(self) -> None:
        a, turn = self._a, self._turn
        try:
            while True:
                sentence = self._sentences.get()
                if sentence is None or turn.cancel.is_set():
                    break
                if sentence == CODE_MARK:
                    sentence = f"I've put the code on screen, {a.title}."
                text = clean_for_speech(sentence)
                if not re.search(r"\w", text) or self.failed:
                    continue
                voice = self._pick_voice(text)
                try:
                    try:
                        mp3 = a.tts.synthesize(text, voice=voice)
                    except TTSError:
                        if voice is None or voice == self._voice:
                            raise
                        log.warning("Voice %s failed; using the default voice", voice)
                        self._auto, self._lang_voice = False, None
                        mp3 = a.tts.synthesize(text, voice=self._voice)
                except TTSError as exc:
                    self.failed = str(exc)
                    a._voice_status(False, str(exc))
                    continue
                if turn.cancel.is_set():
                    break
                try:
                    sound = a.audio.decode(mp3)
                    frames, levels = spectrum_frames(a.audio.mono_samples(sound), a.audio.frequency)
                except Exception:
                    log.exception("Could not decode synthesised speech")
                    continue
                a._voice_status(True)
                self._clips.put((sound, frames, levels, float(sound.get_length())))
        finally:
            self._clips.put(None)

    def _play_loop(self) -> None:
        a = self._a
        try:
            self._play_clips()
        finally:
            if self._ducked:
                a._duck(False)  # always give the music its volume back, even after an error or an interruption

    def _play_clips(self) -> bool:
        a, turn = self._a, self._turn
        while True:
            item = self._clips.get()
            if item is None:
                break
            if turn.cancel.is_set():
                continue  # drain
            sound, frames, levels, duration = item
            if not a._set_state(State.SPEAKING, turn, "speech"):
                continue
            if not self._ducked and a.config.get("duck_media", True):
                self._ducked = True
                a._duck(True)
            self.spoke = True
            a._emit_turn(turn, "speech_clip", fps=VIS_FPS, frames=frames, levels=levels, duration=round(duration, 3))
            a.audio.play_voice(sound)
            deadline = time.monotonic() + duration + 0.6
            time.sleep(0.03)
            while a.audio.voice_busy() and time.monotonic() < deadline:
                if turn.cancel.is_set():
                    a.audio.stop_voice()
                    break
                time.sleep(0.02)
            if self._working and not self._closed and self._clips.empty():
                a._set_state(State.THINKING, turn, "working")
        return self._ducked


class Assistant:
    def __init__(
        self,
        config: Config,
        emit: Emit,
        *,
        llm: LLMEngine | None = None,
        tts: EdgeTTS | None = None,
        stt: SpeechInput | None = None,
        audio: AudioEngine | None = None,
        monitor: SystemMonitor | None = None,
        tools: Toolbox | None = None,
        desktop=None,
        ocr=None,
    ) -> None:
        self.config = config
        self._emit_raw = emit
        self.audio = audio or AudioEngine()
        self.sfx = SoundFX(self.audio, config)
        self.llm = llm or LLMEngine(config)
        self.tts = tts or EdgeTTS(config, cache_dir=app_data_dir() / "tts_cache")
        self.stt = stt or SpeechInput(config)
        self.monitor = monitor or SystemMonitor()
        self.tools = tools or Toolbox(config)
        self.desktop = desktop or default_desktop()
        self.vision = VisionEngine(config, self.llm, self.desktop, ocr or default_ocr())
        self.tracker = ForegroundTracker(self.desktop)
        self.watcher: Watcher | None = None
        self._pending_act: dict | None = None  # an action waiting for "yes" (uncertain or risky clicks)
        self._act_ids = itertools.count(1)
        self.state = StateMachine(self._on_state)

        self._turn: Turn | None = None
        self._turn_lock = threading.RLock()
        self._ids = itertools.count(1)
        self._started = threading.Event()
        self._ollama: OllamaStatus | None = None
        self._mic: dict = {"available": False, "reason": "Starting up", "device": None}
        self._voice_ok: bool | None = None
        self._last_voice_notice = float("-inf")  # monotonic() starts near 0 right after boot
        self._pull_cancel: threading.Event | None = None
        self._pull_thread: threading.Thread | None = None
        self.wake: wakeword.WakeListener | None = None
        self._wake_error: str | None = None
        self.__dict__["_last_doc_value"] = None  # see the _last_doc property (loaded from settings when first needed)
        self._timers: dict[int, dict] = {}
        self._timer_ids = itertools.count(1)
        self._prewarm_stop = threading.Event()
        self._open_hints = 0  # how often we've explained the "sign in / show it here" fallback
        self._drafts: dict[str, Draft] = {}  # emails shown on screen, by id
        self._awaiting: Draft | None = None  # the draft JARVIS just asked "shall I send it?" about
        self._pending_forget = 0.0  # when JARVIS asked "forget everything?"
        self._paused = False  # "stop listening": the microphone stays closed until asked again
        self.protocols = ProtocolStore(config, backup_dir=app_data_dir() / "backups")
        try:
            migrated = self.protocols.migrate()
            if migrated:
                log.info("Updated %d protocol(s) to the new format (originals kept in backups/)", migrated)
        except ProtocolError as exc:
            log.warning("Protocols left as they were: %s", exc)
        self._protocol_lock = threading.Lock()
        self._protocol_run: dict | None = None  # the protocol running now: {id, name, steps, index, stop, turn, ...}
        self._protocol_last: dict | None = None  # the one that ran last (so "stop the protocol" right after still makes sense)
        self._recording: dict | None = None  # a protocol being dictated step by step: {name, steps, at, id}
        self._proposal: dict | None = None  # a protocol made from a sentence, waiting for details / a yes
        self._protocol_confirm: dict | None = None  # a running step waiting for "shall I go ahead?"
        self._pending_skip: list[str] = []
        self._scheduler_tick = 5.0
        self.hotkeys = HotkeyManager(self._hotkey_pressed) if sys.platform == "win32" else None
        self._scheduler_stop = threading.Event()
        self._now_playing: MediaSession | None = None
        self._media_snapshot = ""
        self._pending_media: dict | None = None  # "Spotify and YouTube are both playing. Which one?"
        self.winhelper = WinHelper()
        self.media = MediaHub(self.winhelper, windows=self._safe_windows, media_key=lambda a: osctl.media_key(a), youtube_keys=self._youtube_keys,
                              state_dir=app_data_dir())
        self.browser = BrowserManager(config, windows=self._safe_windows, helper=self.winhelper, front=self._foreground,
                                      bring_to_front=lambda w: self.desktop.bring_to_front(w),
                                      on_state=lambda st: self.emit("browser", **st))
        if getattr(self.tools, "custom_url_launcher", False):
            self.browser.url_launcher = self.tools._launch_url
        else:
            self.tools._launch_url = lambda url: self.browser._launch(url, self.browser.key())  # every link: right browser, right profile
        self._activities: deque = deque((a for a in (config.get("activity_history") or []) if isinstance(a, dict)), maxlen=120)
        self._activity_ids = itertools.count(int(time.time() * 1000))
        self._duck_jobs: "queue.Queue[str]" = queue.Queue()
        self._duck_worker: threading.Thread | None = None
        self._sign_in_wait: threading.Event | None = None
        self._announcements = 0  # timers / reminders waiting for a quiet moment to speak
        self._restored: list[dict] = []  # the end of the last conversation, carried on after a restart
        self.memory = MemoryEngine(config, self.llm, on_event=lambda kind, payload: self.emit(kind, **payload))
        self.weather = Weather(config, self.memory)
        load_custom(config.get("custom_personas"))
        self.wakewords = WakeWords(app_data_dir() / "wakewords", self.tts, self.audio, self.emit)
        self.wakewords.on_ready = self._wake_learned
        self.llm.persona_provider = lambda: (self.persona, self.title)

    # ================================================================== plumbing
    @property
    def _last_doc(self) -> dict | None:
        """The Google file JARVIS made or opened last ("open it", "add a section to it"); survives restarts."""
        value = self.__dict__.get("_last_doc_value")
        if value is None and not self.__dict__.get("_last_doc_loaded"):
            self.__dict__["_last_doc_loaded"] = True
            saved = self.config.get("last_document") or {}
            value = saved if saved.get("kind") in ("doc", "slides", "sheet") and (saved.get("id") or saved.get("url")) else None
            self.__dict__["_last_doc_value"] = value
        return value

    @_last_doc.setter
    def _last_doc(self, value: dict | None) -> None:
        self.__dict__["_last_doc_value"] = value
        self.__dict__["_last_doc_loaded"] = True
        if value:
            try:
                self.config.update({"last_document": {k: value.get(k) or "" for k in ("kind", "id", "title", "url")}})
            except Exception:
                log.debug("couldn't remember the last document", exc_info=True)

    @property
    def persona(self) -> Persona:
        return get_persona(self.config.get("persona"))

    @property
    def title(self) -> str:
        """How the active personality addresses the user ("sir", their name, "boss")."""
        return address_for(self.persona, self.config)

    def emit(self, kind: str, **payload: Any) -> None:
        try:
            self._emit_raw(kind, payload)
        except Exception:
            log.exception("emit %s failed", kind)

    def _emit_turn(self, turn: Turn, kind: str, **payload: Any) -> None:
        if self._is_current(turn):
            self.emit(kind, **payload)

    def _is_current(self, turn: Turn) -> bool:
        return self._turn is turn and not turn.cancel.is_set()

    def _set_state(self, state: State, turn: Turn | None = None, reason: str = "") -> bool:
        if turn is not None and not self._is_current(turn):
            return False
        return self.state.transition(state, reason)

    def _on_state(self, old: State, new: State, reason: str) -> None:
        self.emit("state", state=new.value, prev=old.value, reason=reason)

    def _new_turn(self, kind: str) -> Turn:
        with self._turn_lock:
            old = self._turn
            run = self._protocol_run
            if old is not None and run is not None and run.get("turn") is old and not old.cancel.is_set():
                run["interrupted_by"] = kind  # a timer going off pauses a protocol; the user speaking stops it
            if old is not None:
                old.abort()
            self.audio.stop_voice()
            if old is not None:
                self.emit("speech_stop")
            turn = Turn(kind)
            self._turn = turn
            if kind == "announce" and self.state.state != State.IDLE:
                self.state.transition(State.IDLE, "announce")
            return turn

    def _finish_turn(self, turn: Turn) -> bool:
        with self._turn_lock:
            if self._turn is not turn:
                return False
            self._turn = None
        if not turn.cancel.is_set():
            self.state.transition(State.IDLE, "turn complete")
        return True

    def _spawn(self, target: Callable, *args: Any, name: str = "turn") -> None:
        def run() -> None:
            try:
                target(*args)
            except Exception:
                log.exception("%s crashed", name)
                self.emit("notice", level="error", text="Internal error. Details are in the log file.")
                self.state.transition(State.IDLE, "crash")

        threading.Thread(target=run, name=name, daemon=True).start()

    def _voice_status(self, ok: bool, error: str | None = None) -> None:
        if ok == self._voice_ok:
            return
        self._voice_ok = ok
        self.emit("voice_status", ok=ok, error=error)
        if not ok and time.monotonic() - self._last_voice_notice > 60:
            self._last_voice_notice = time.monotonic()
            self.emit("notice", level="warn", text=f"Voice offline: {error} Replies will be shown as text.")

    # ================================================================== lifecycle
    def start(self) -> None:
        """Initialise subsystems. Runs on a background thread while the window loads."""
        steps = (
            ("audio output", self.audio.init),
            ("interface sounds", self.sfx.load),
            ("microphone", lambda: setattr(self, "_mic", self.stt.availability())),
            ("ollama", self.check_ollama),
            ("file index", self._warm_file_index),
            ("wake word", self._sync_wake_word),
            ("vision", self._start_vision),
            ("memory", self._start_memory),
            ("wake words", self._wake_changed),
            ("protocol scheduler", lambda: self._spawn(self._protocol_scheduler, name="protocol-scheduler")),
            ("protocol hotkeys", self._sync_hotkeys),
            ("media watcher", lambda: self._spawn(self._media_watch, name="media-watch")),
        )
        try:
            for name, step in steps:  # logged one by one so jarvis.log pinpoints a native crash
                log.info("Starting %s", name)
                try:
                    step()
                except Exception:
                    log.exception("%s failed to start", name)
            self.emit("mic_status", **{**self._mic, "engine": self.stt.engine_label()})
        finally:
            self._started.set()

    def boot_payload(self, wait: float = 25.0) -> dict:
        self._started.wait(wait)
        return {
            "version": APP_VERSION,
            "settings": self.config.as_dict(),
            "state": self.state.state.value,
            "ollama": self._ollama.to_dict() if self._ollama else None,
            "mic": self._mic,
            "stt_engine": self.stt.engine_label(),
            "stt_engines": ENGINE_LABELS,
            "audio": {"available": self.audio.available, "output": self.audio.has_output, "error": self.audio.error},
            "system": self.monitor.static_info(),
            "core": self.core_stats(),
            "wake": self.wake_status(),
            "vision": self.vision_status(),
            "persona": self.persona_info(),
            "personas": self.persona_list(),
            "memory": self.memory.stats(),
            "restored": self._restored,
            "paused": self._paused,
            "protocols": self.protocol_list(),
            "media": self._now_playing.to_dict() if self._now_playing else None,
            "activity": list(self._activities)[-40:],
        }

    def boot_complete(self) -> None:
        """Called by the UI once the boot animation has finished."""
        status = self._ollama
        hour = datetime.now().hour
        part = "morning" if 5 <= hour < 12 else "afternoon" if 12 <= hour < 18 else "evening"
        if status and status.online and status.model:
            text = random.choice(self.persona.greeting).format(part=part, title=self.title)
            if self._restored:
                text = re.split(r"(?<=[.!?])\s+", text)[0] + " " + self._resume_line()
            text += self._routine_line()
            self._spawn(self.llm.warmup, name="warmup")
            if self.config.get("voice_enabled", True):
                self._spawn(self._prewarm_voice, name="voice-prewarm")
        elif status and status.online:
            text = f"Good {part}, {self.title}. Ollama is running, but no language model is installed yet. I've put the details on screen."
        else:
            text = f"Good {part}, {self.title}. I'm afraid my neural core is offline. I've put instructions on screen to bring it online."
        self.sfx.play("activate")
        self.speak(text, delay=0.45)
        self._spawn(self._startup_protocols, name="startup-protocols")

    def shutdown(self) -> None:
        self._prewarm_stop.set()
        self._scheduler_stop.set()
        if self._sign_in_wait is not None:
            self._sign_in_wait.set()
        try:
            self.media._duck_depth = 0
            self.media.restore()
        except Exception:
            log.debug("couldn't restore volumes", exc_info=True)
        try:
            self.config.update({"activity_history": list(self._activities)[-60:]})
        except Exception:
            log.debug("couldn't save the activity log", exc_info=True)
        run = self._protocol_run
        if run is not None:
            run["stop"].set()
        self.memory.stop()
        self.wakewords.cancel()
        self.tracker.stop()
        if self.watcher is not None:
            self.watcher.stop()
        stop_ocr = getattr(self.vision.ocr, "stop", None)
        if stop_ocr:
            stop_ocr()
        for entry in list(self._timers.values()):
            entry["timer"].cancel()
        if self.wake is not None:
            self.wake.stop()
        try:
            self.winhelper.stop()
            if self.hotkeys is not None:
                self.hotkeys.stop()
        except Exception:
            pass
        with self._turn_lock:
            if self._turn is not None:
                self._turn.abort()
            self._turn = None
        if self._pull_cancel is not None:
            self._pull_cancel.set()
        self.audio.stop_voice(0)
        self.tts.close()
        self.audio.shutdown()

    # ================================================================== wake word
    def wake_status(self) -> dict:
        ok, why = wakeword.available()
        active = bool(self.wake and self.wake.running)
        reason = None
        if not self.config.get("wake_word", True):
            reason = "turned off in Settings"
        elif not ok:
            reason = why
        elif not self._mic.get("available"):
            reason = "no microphone"
        elif not active:
            reason = self._wake_error or (self.wake.error if self.wake else None) or "not running"
        use_jarvis, models = self._wake_words()
        phrases = [m.phrase for m in models] + (["Hey Jarvis"] if use_jarvis else [])
        learning = None
        if self.persona.id != "jarvis" and not models:
            state = self.wakewords.status(self.persona.name)
            if state["state"] in ("learning", "queued"):
                learning = {"name": self.persona.name, "progress": state["progress"]}
        if self._paused:
            reason, active = "paused (you said \"stop listening\")", False
        return {"enabled": bool(self.config.get("wake_word", True)), "active": active, "phrase": phrases[0] if phrases else "Hey Jarvis",
                "phrases": phrases, "reason": reason, "learning": learning, "paused": self._paused}

    def _sync_wake_word(self) -> None:
        """Start or stop the background "Hey Jarvis" listener to match settings and hardware."""
        ok, why = wakeword.available()
        want = bool(self.config.get("wake_word", True)) and ok and self._mic.get("available") and not self._paused
        if want and not (self.wake and self.wake.running):
            self.wake = wakeword.WakeListener(self._on_wake, lambda: float(self.config.get("wake_sensitivity", 0.5)),
                                              words=self._wake_words)
            if self.wake.start():
                self.stt.set_source_provider(self.wake.borrow)
                self._wake_error = None
            else:
                self._wake_error = self.wake.error
                self.stt.set_source_provider(None)
        elif not want and self.wake is not None:
            self.wake.stop()
            self.wake = None
            self.stt.set_source_provider(None)
        if not ok and self.config.get("wake_word", True):
            log.info("Wake word unavailable: %s", why)
        self.emit("wake_status", **self.wake_status())

    def _on_wake(self, score: float) -> None:
        """Called from the wake-word thread: barge in if busy, then listen for the command."""
        if self._paused:
            return
        self.emit("wake", score=round(score, 2))
        if self.state.state in (State.SPEAKING, State.THINKING):
            with self._turn_lock:
                turn, self._turn = self._turn, None
                if turn is not None:
                    turn.abort()
            self.audio.stop_voice(80)
            self.emit("speech_stop")
            self.state.transition(State.IDLE, "wake word")
        if self.state.state != State.LISTENING:
            self.start_listening()

    # ================================================================== public actions
    def submit_text(self, text: str, source: str = "text") -> bool:
        text = (text or "").strip()[:4000]
        if not text:
            return False
        turn = self._new_turn("text")
        self._spawn(self._converse, turn, text, source, name=f"turn-{turn.id}")
        return True

    def speak(self, text: str, voice: str | None = None, delay: float = 0.0) -> None:
        turn = self._new_turn("announce")

        def run() -> None:
            if delay:
                time.sleep(delay)
            if self._is_current(turn):
                self._deliver(turn, [text], voice=voice)

        self._spawn(run, name=f"announce-{turn.id}")

    # ================================================================== protocols
    def protocol_list(self) -> dict:
        items = []
        for p in self.protocols.all():
            d = p.to_dict()
            d["when"] = p.describe_schedule()
            d["steps"] = [{**st.to_dict(), **self._step_info(st)} for st in p.steps]
            items.append(d)
        return {"protocols": items, "running": self._protocol_status(), "recording": self._recording_status(),
                "proposal": self._proposal_status(), "templates": PROTOCOL_TEMPLATES, "icons": list(PROTOCOL_ICONS),
                "categories": list(PROTOCOL_CATEGORIES), "suggestions": self._protocol_suggestions()}

    def _step_info(self, step) -> dict:
        """How JARVIS will understand a step (for the editor): a label, whether it needs a go-ahead, and its condition."""
        text = getattr(step, "text", step)
        label, kind = self._understand(text)
        info = {"label": label, "kind": kind, "risk": protocol_risk(text)}
        when = getattr(step, "when", None)
        if when:
            info["condition"] = describe_condition(when)
        return info

    def _understand(self, text: str) -> tuple[str, str]:
        t = _bare(text).strip()
        k = step_kind(t)
        if k.kind == "wait":
            return f"Wait {describe_duration(int(k.seconds))}", "wait"
        if k.kind == "say":
            return "Say", "say"
        cmd = parse_protocol_command(t, find=self.protocols.find)
        if cmd is not None and cmd.action == "run":
            return f"Run protocol {cmd.name}", "protocol"
        m = parse_media(t)
        if m is not None:
            return {"play": "Play music / video", "pause": "Pause media", "resume": "Resume media", "next": "Next track",
                    "previous": "Previous track", "volume": "Music volume", "now_playing": "What's playing",
                    "resume_history": "Resume earlier media", "search": "Search YouTube"}.get(m.action, "Media control"), "media"
        if parse_switch(t):
            return "Switch personality", "persona"
        q = parse_quick(t)
        if q is not None and q.kind not in ("talk", "greeting"):
            return {"timer": "Timer / reminder", "volume": "System volume", "status": "System status", "screenshot": "Screenshot",
                    "desktop": "Show desktop", "lock": "Lock the PC", "math": "Calculate"}.get(q.kind, q.kind.replace("_", " ").title()), "quick"
        if parse_weather(t) is not None:
            return "Weather", "info"
        if _GOOGLE_SEARCH.match(t):
            return "Google search", "browser"
        o = _OPEN_COMMAND.match(t)
        if o:
            target = o.group("target")
            return ("Open website" if self.tools.website_for(target) else "Open app / file"), "open"
        if parse_doc_command(t) is not None:
            return "Google Doc", "doc"
        if parse_email_request(t):
            return "Email (asks before sending)", "email"
        if parse_act(t) is not None:
            return "Screen / tab action", "act"
        return "Ask JARVIS", "ai"

    def _protocol_status(self) -> dict | None:
        run = self._protocol_run
        if run is None:
            return None
        return {"id": run["id"], "name": run["name"], "index": run["index"], "total": len(run["steps"]),
                "step": run["steps"][run["index"]].text if 0 <= run["index"] < len(run["steps"]) else "",
                "results": list(run["results"]), "steps": [s.text for s in run["steps"]], "ephemeral": run.get("ephemeral", False),
                "confirm": ({k: v for k, v in self._protocol_confirm.items() if k != "event"} if self._protocol_confirm else None)}

    def _recording_status(self) -> dict | None:
        rec = self._recording
        return {"name": rec["name"], "steps": list(rec["steps"])} if rec else None

    def _proposal_status(self) -> dict | None:
        prop = self._proposal
        if not prop:
            return None
        data = prop["protocol"]
        return {**data, "steps": [{**st, **self._step_info(st["text"])} for st in data["steps"]], "question": prop.get("question", ""),
                "stage": prop["stage"]}

    def _emit_protocols(self) -> None:
        self.emit("protocols", **self.protocol_list())

    def protocol_save(self, data: dict) -> dict:
        data = data if isinstance(data, dict) else {}
        steps = data.get("steps") or []
        if isinstance(steps, str):
            steps = [structured_step(line).to_dict() for line in steps.splitlines() if line.strip()]
        try:
            schedule = data.get("schedule") if "schedule" in data else None
            meta = {k: data[k] for k in ("description", "icon", "category", "phrases", "enabled", "on_failure", "triggers") if k in data}
            p = self.protocols.save(str(data.get("name") or ""), steps, pid=data.get("id") or None, schedule=schedule, **meta)
        except ProtocolError as exc:
            return {"ok": False, "error": str(exc)}
        self._activity("protocol", f"Saved protocol {p.name}", "ok")
        self._sync_hotkeys()
        self._emit_protocols()
        return {"ok": True, "protocol": p.to_dict()}

    def protocol_delete(self, pid: str) -> dict:
        gone = self.protocols.delete(str(pid))
        self._sync_hotkeys()
        self._emit_protocols()
        return {"ok": gone is not None, "protocol": gone.to_dict() if gone else None}

    def protocol_restore(self, data: dict) -> dict:
        p = self.protocols.restore(data if isinstance(data, dict) else {})
        self._sync_hotkeys()
        self._emit_protocols()
        return {"ok": p is not None}

    def protocol_from_template(self, name: str) -> dict:
        tpl = next((t for t in PROTOCOL_TEMPLATES if t["name"] == name), None)
        if tpl is None:
            return {"ok": False, "error": "No such template."}
        base, n = tpl["name"], 2
        new_name = base
        taken = {x.lower() for x in self.protocols.names()}
        while new_name.lower() in taken and n < 50:
            new_name, n = f"{base} {n}", n + 1
        return self.protocol_save({**tpl, "name": new_name, "steps": [{"text": t} for t in tpl["steps"]],
                                   "phrases": [] if new_name != base else tpl.get("phrases", [])})

    def protocol_run(self, pid: str) -> dict:
        p = self.protocols.get(str(pid))
        if p is None:
            return {"ok": False, "error": "That protocol doesn't exist any more."}
        if self._protocol_run is not None:
            return {"ok": False, "error": f"The {self._protocol_run['name']} protocol is still running."}
        turn = self._new_turn("announce")

        def run() -> None:
            self._deliver(turn, [self._protocol_intro(p)])
            if not turn.cancel.is_set():
                self._start_protocol(p, trigger="button")

        self._spawn(run, name=f"protocol-start-{p.id}")
        return {"ok": True}

    def _protocol_intro(self, p) -> str:
        line = self.persona.line("protocol_start", (), self.title) if "protocol_start" in getattr(self.persona, "lines", {}) else ""
        return line.replace("{name}", p.name) if line else f"Initiating the {p.name} protocol, {self.title}."

    def protocol_stop(self) -> dict:
        run = self._protocol_run
        if run is None:
            return {"ok": False}
        run["stop"].set()
        confirm = self._protocol_confirm
        if confirm is not None:
            confirm["answer"] = False
            confirm["event"].set()
        step_turn = run.get("turn")
        if step_turn is not None:
            run["interrupted_by"] = "stop"
            step_turn.abort()
            self.audio.stop_voice(80)
        return {"ok": True}

    def protocol_answer(self, yes: bool) -> dict:
        """The HUD's Go ahead / Skip buttons for a step that needs a yes."""
        confirm = self._protocol_confirm
        if confirm is None:
            return {"ok": False}
        confirm["answer"] = bool(yes)
        confirm["event"].set()
        return {"ok": True}

    def _expand_steps(self, p, depth: int = 0, seen: tuple = ()) -> list:
        """A step "run the Lights protocol" runs that protocol's steps in place (up to 3 deep, no loops)."""
        out = []
        for st in p.steps:
            if not st.enabled:
                continue
            cmd = parse_protocol_command(st.text, find=self.protocols.find)
            inner = self.protocols.find(cmd.name) if cmd is not None and cmd.action == "run" else None
            if inner is not None and inner.id not in seen + (p.id,) and depth < 3:
                out.extend(self._expand_steps(inner, depth + 1, seen + (p.id,)))
            elif inner is None:
                out.append(st)
        return out

    def _start_protocol(self, p, trigger: str = "voice", skip: list[str] | None = None, ephemeral: bool = False) -> bool:
        with self._protocol_lock:
            if self._protocol_run is not None:
                return False
            steps = self._expand_steps(p)
            skipped = []
            if skip:
                words = [re.sub(r"[^a-z0-9 ]", "", x.lower()).strip() for x in skip]
                keep = []
                for st in steps:
                    low = st.text.lower()
                    if any(w and (w in low or all(part in low for part in w.split() if part not in ("open", "the", "my", "launch", "start"))) for w in words):
                        skipped.append(st.text)
                    else:
                        keep.append(st)
                steps = keep
            run = {"id": p.id, "name": p.name, "steps": steps, "index": -1, "stop": threading.Event(), "turn": None,
                   "interrupted_by": None, "trigger": trigger, "started": time.time(), "results": [],
                   "policy": getattr(p, "on_failure", "stop"), "ephemeral": ephemeral, "excluded": skipped,
                   "unattended": trigger in ("schedule", "startup", "app")}
            self._protocol_run = run
        if not ephemeral:
            try:
                self.protocols.update(p.id, last_run=time.time())
            except ProtocolError:
                pass
        threading.Thread(target=self._protocol_runner, args=(run,), name=f"protocol-{p.id}", daemon=True).start()
        return True

    def _running_apps(self) -> set[str]:
        out = set()
        for w in self._safe_windows():
            app = re.sub(r"\.exe$", "", (w.app or "").lower())
            if app:
                out.add(re.sub(r"[^a-z0-9]", "", app))
            title = re.sub(r"[^a-z0-9 ]", " ", (w.title or "").lower())
            out.update(x for x in title.split() if len(x) > 3)
        return out

    def _ask_protocol_confirm(self, run: dict, i: int, step, why: str) -> bool | None:
        """Ask "shall I go ahead?" for one step and wait (up to a minute) for yes / no by voice or on screen."""
        event = threading.Event()
        self._protocol_confirm = {"event": event, "answer": None, "run": run["name"], "index": i, "text": step.text, "why": why}
        self.emit("protocol", status="confirm", **self._protocol_status())
        question = (f"Step {i + 1} of {run['name']}: “{step.text}”. " + (f"It {why}. " if why else "") + f"Shall I go ahead, {self.title}?")
        turn = self._new_turn("announce")
        self._deliver(turn, [question])
        if not self._paused and self._mic.get("available") and self.config.get("voice_enabled", True):
            self.start_listening()
        event.wait(60)
        answer = self._protocol_confirm["answer"] if self._protocol_confirm else None
        self._protocol_confirm = None
        time.sleep(0.05)
        self._wait_for_quiet(run["stop"], 15)  # let "Going ahead" / "Skipping that step" be heard before carrying on
        return answer

    def _run_command_step(self, run: dict, text: str, timeout: float) -> tuple[str, str]:
        """Run one command exactly as if the user had said it; work out from what happened whether it worked."""
        with self._turn_lock:
            speaking = self._turn is not None and self._turn.kind == "announce"
        if speaking:
            self._wait_for_quiet(run["stop"])
        turn = self._new_turn("protocol")
        turn.learn = False
        run["turn"], run["interrupted_by"] = turn, None
        started = time.time()

        def too_slow() -> None:
            if run.get("turn") is turn and not turn.cancel.is_set():
                run["interrupted_by"] = "timeout"
                turn.abort()

        watchdog = threading.Timer(timeout, too_slow)
        watchdog.daemon = True
        watchdog.start()
        try:
            self._converse(turn, text, "protocol")
        except Exception:
            log.exception("Protocol step %r failed", text)
            self._finish_turn(turn)
            return "failed", "an internal error (details in the log)"
        finally:
            watchdog.cancel()
            run["turn"] = None
        if run.get("interrupted_by") == "timeout":
            return "failed", f"took longer than {int(timeout)} seconds"
        reply = getattr(turn, "reply", "") or ""
        acts = [a for a in list(self._activities) if a.get("at", 0) >= started]
        statuses = {a["status"] for a in acts}
        if "failed" in statuses:
            return "failed", next(a["text"] for a in acts if a["status"] == "failed")
        if _FAILED_REPLY.search(reply):
            return "failed", reply[:160]
        if "waiting" in statuses:
            return "unverified", next(a["text"] for a in acts if a["status"] == "waiting")
        if "unverified" in statuses:
            return "unverified", reply[:160]
        return "ok", reply[:160]

    def _protocol_runner(self, run: dict) -> None:
        stop, steps, results = run["stop"], run["steps"], run["results"]
        outcome = "done"
        failed_step = None
        previous_ok: bool | None = None
        results.extend({"text": st.text, "status": "pending", "detail": ""} for st in steps)
        self.emit("protocol", status="running", **self._protocol_status())
        try:
            for i, st in enumerate(steps):
                if stop.is_set():
                    outcome = "stopped"
                    break
                run["index"] = i
                results[i]["status"] = "running"
                began = time.time()
                self.emit("protocol", status="step", **self._protocol_status())
                status, detail = self._run_step(run, i, st, previous_ok)
                results[i].update(status=status, detail=detail, seconds=round(time.time() - began, 1))
                self.emit("protocol", status="step_done", **self._protocol_status())
                if status == "stopped" or stop.is_set():
                    outcome = "stopped"
                    break
                if status == "interrupted":
                    outcome = "interrupted"  # the user spoke, typed or pressed stop: they've taken over
                    break
                if status in ("ok", "unverified"):
                    previous_ok = True
                elif status == "failed":
                    previous_ok = False
                    keep_going = st.continue_on_error if st.continue_on_error is not None else run["policy"] == "continue"
                    if not keep_going:
                        outcome, failed_step = "failed", st.text
                        break
        finally:
            for r in results:
                if r["status"] in ("pending", "running"):
                    r["status"] = "not_run" if r["status"] == "pending" else "stopped"
            with self._protocol_lock:
                self._protocol_run = None
                run["outcome"], run["ended"] = outcome, time.time()
                self._protocol_last = run
            self._protocol_confirm = None
            if not run.get("ephemeral"):
                try:
                    self.protocols.record_run(run["id"], {"at": run["started"], "status": outcome, "trigger": run["trigger"],
                                                         "seconds": round(time.time() - run["started"], 1),
                                                         "steps": [{k: r.get(k) for k in ("text", "status", "detail")} for r in results],
                                                         "excluded": run.get("excluded", [])})
                except Exception:
                    log.debug("couldn't save the protocol run", exc_info=True)
            self.emit("protocol", status=outcome, id=run["id"], name=run["name"], index=run["index"], total=len(steps),
                      results=list(results), ephemeral=run.get("ephemeral", False))
            self._emit_protocols()
        problems = [r for r in results if r["status"] == "failed"]
        verdict = "ok" if outcome == "done" and not problems else ("failed" if outcome == "failed" or problems else "unverified")
        if not run.get("ephemeral"):
            self._activity("protocol", f"Protocol {run['name']}: {outcome}" + (f" ({len(problems)} step(s) failed)" if problems else ""), verdict)
        if self._prewarm_stop.is_set():
            return
        if outcome == "done" and not run.get("ephemeral"):
            if problems:
                self.speak(f"The {run['name']} protocol has finished, {self.title}, but {len(problems)} step"
                           f"{' did' if len(problems) == 1 else 's did'}n't work: {join_names([p['text'] for p in problems][:3])}.")
            else:
                self.speak(f"The {run['name']} protocol is complete, {self.title}.")
        elif outcome == "failed":
            self.speak(f"I've stopped the {'request' if run.get('ephemeral') else run['name'] + ' protocol'}, {self.title}: "
                       f"“{failed_step}” didn't work, so I haven't done the steps after it.")

    def _run_step(self, run: dict, i: int, st, previous_ok: bool | None) -> tuple[str, str]:
        if not protocol_condition(st.when, datetime.now(), self._running_apps(), previous_ok):
            return "skipped", describe_condition(st.when)
        why = protocol_risk(st.text)
        if st.confirm or (why and not st.approved):
            if run["unattended"] and why and not st.approved:
                return "skipped", f"needs your OK first ({why}); it never runs unattended"
            answer = self._ask_protocol_confirm(run, i, st, why)
            if run["stop"].is_set():
                return "stopped", ""
            if not answer:
                return "skipped", "you said no" if answer is False else "no answer"
        kind = step_kind(st.text)
        if kind.kind == "wait":
            return ("stopped", "") if run["stop"].wait(kind.seconds) else ("ok", f"waited {describe_duration(int(kind.seconds))}")
        if kind.kind == "say":
            turn = self._new_turn("protocol")
            turn.learn = False
            turn.text = ""
            run["turn"] = turn
            if self._set_state(State.THINKING, turn, "protocol"):
                self._deliver(turn, [kind.text])
            run["turn"] = None
            if turn.cancel.is_set() and run.get("interrupted_by") not in (None, "announce"):
                return "interrupted", ""
            return "ok", kind.text
        status, detail = "failed", ""
        for attempt in range(1 + max(0, int(st.retries))):
            status, detail = self._run_command_step(run, st.text, st.timeout)
            if run.get("interrupted_by") not in (None, "announce", "timeout"):
                return "interrupted", ""
            if status != "failed" or run["stop"].is_set():
                break
            if attempt < st.retries:
                run["stop"].wait(1.5)
        if status == "failed" and st.fallback and not run["stop"].is_set():
            alt, alt_detail = self._run_command_step(run, st.fallback, st.timeout)
            if run.get("interrupted_by") not in (None, "announce", "timeout"):
                return "interrupted", ""
            return alt, f"used the fallback “{st.fallback}”: {alt_detail}"
        return status, detail

    def _wait_for_quiet(self, stop: threading.Event, limit: float = 120.0) -> None:
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline and not stop.is_set():
            with self._turn_lock:
                busy = self._turn is not None
            if not busy:
                return
            time.sleep(0.1)

    # -- what the user says about protocols
    def _protocol_command(self, cmd: ProtocolCommand, source: str) -> tuple[str, object | None]:
        """What to say about a protocol request, and the protocol to start once it has been said (if any)."""
        t = self.title
        names = self.protocols.names()

        def unknown(name: str) -> str:
            if not names:
                return (f"You haven't made any protocols yet, {t}. Say “create a protocol called {name or 'Morning'}” "
                        "and tell me the steps.")
            return f"I don't have a protocol called {name}, {t}. Your protocols are {join_names(names)}."

        if cmd.action == "list":
            if not names:
                return (f"You don't have any protocols yet, {t}. Say “create a protocol called Morning: open Spotify, "
                        "then tell me the weather”, or press the protocols button.", None)
            return (f"You have {len(names)} protocol{'s' if len(names) != 1 else ''}, {t}: {join_names(names)}.", None)
        if cmd.action == "stop":
            run = self._protocol_run
            if run is not None:
                self.protocol_stop()
                return (f"I've stopped the {run['name']} protocol, {t}.", None)
            last = self._protocol_last
            if last is not None and time.time() - last.get("ended", 0) < 20 and last.get("outcome") != "done":
                return (f"I've stopped the {last['name']} protocol, {t}.", None)
            return (f"No protocol is running, {t}.", None)
        if cmd.action == "record":
            if source == "protocol":
                return (f"I can't record a protocol from inside another one, {t}.", None)
            self._recording = {"name": cmd.name, "steps": [], "at": time.time()}
            self._emit_protocols()
            if not cmd.name:
                return (f"Of course, {t}. What shall I call the new protocol?", None)
            replacing = " It will replace the one you have now." if self.protocols.find(cmd.name) else ""
            return (f"Recording the {cmd.name} protocol, {t}.{replacing} Tell me the first step, and say “done” when you've finished.", None)
        if cmd.action == "create":
            if source == "protocol":
                return (f"I can't create a protocol from inside another one, {t}.", None)
            return (self._propose(cmd.name, cmd.steps), None)

        p = self.protocols.find(cmd.name) if cmd.name else None
        if p is None:
            return (unknown(cmd.name), None)
        if cmd.action == "run":
            if source == "protocol":
                return (f"I can't start the {p.name} protocol from inside another protocol, {t}.", None)
            if self._protocol_run is not None:
                return (f"The {self._protocol_run['name']} protocol is still running, {t}. Say “stop the protocol” first.", None)
            if not p.enabled:
                return (f"The {p.name} protocol is switched off, {t}. Turn it on in the protocols panel first.", None)
            if not p.steps:
                return (f"The {p.name} protocol has no steps yet, {t}.", None)
            self._pending_skip = list(cmd.skip)
            extra = f" Leaving out {join_names(cmd.skip)}." if cmd.skip else ""
            return (self._protocol_intro(p) + extra, p)
        if cmd.action == "show":
            when = f" It runs {p.describe_schedule()}." if p.schedule else ""
            last = p.history[-1] if p.history else None
            ran = f" Last time it {'finished' if last['status'] == 'done' else last['status']}." if last else ""
            return (f"The {p.name} protocol has {len(p.steps)} step{'s' if len(p.steps) != 1 else ''}, {t}: {spoken_steps(p.steps, 12)}.{when}{ran}", None)
        if cmd.action == "add":
            if not cmd.steps:
                return (f"What should I add to the {p.name} protocol, {t}?", None)
            try:
                p = self.protocols.update(p.id, steps=[st.to_dict() for st in p.steps] + [structured_step(x).to_dict() for x in cmd.steps])
            except ProtocolError as exc:
                return (f"I couldn't change it, {t}: {exc}", None)
            self._emit_protocols()
            return (f"Added to the {p.name} protocol, {t}. It now has {len(p.steps)} steps.", None)
        if cmd.action == "remove_step":
            if len(p.steps) <= 1:
                return (f"That's the only step in the {p.name} protocol, {t}. Say “delete the {p.name} protocol” to remove it altogether.", None)
            index = (cmd.index or len(p.steps)) - 1
            if not 0 <= index < len(p.steps):
                return (f"The {p.name} protocol only has {len(p.steps)} steps, {t}.", None)
            removed = p.steps[index].text
            p = self.protocols.update(p.id, steps=[st.to_dict() for j, st in enumerate(p.steps) if j != index])
            self._emit_protocols()
            return (f"Removed “{removed}” from the {p.name} protocol, {t}.", None)
        if cmd.action == "delete":
            self.protocols.delete(p.id)
            self._sync_hotkeys()
            self._emit_protocols()
            return (f"I've deleted the {p.name} protocol, {t}.", None)
        if cmd.action == "rename":
            try:
                new = self.protocols.update(p.id, name=cmd.new_name)
            except ProtocolError as exc:
                return (f"I couldn't rename it, {t}: {exc}", None)
            self._emit_protocols()
            return (f"The {p.name} protocol is now called {new.name}, {t}.", None)
        if cmd.action == "schedule":
            p = self.protocols.update(p.id, schedule=cmd.schedule or {})
            self._emit_protocols()
            return (f"Done, {t}. I'll run the {p.name} protocol {describe_schedule(p.schedule)}.", None)
        if cmd.action == "unschedule":
            p = self.protocols.update(p.id, schedule={})
            self._emit_protocols()
            return (f"The {p.name} protocol won't run by itself any more, {t}.", None)
        return (unknown(cmd.name), None)

    # -- making one from a sentence: a proposal, details asked for, then a yes
    def _propose(self, name: str, texts: list[str]) -> str:
        t = self.title
        steps = [structured_step(x).to_dict() for x in texts]
        self._proposal = {"protocol": {"name": name, "steps": steps, "icon": _guess_icon(name, texts), "category": _guess_category(name, texts)},
                          "stage": "name" if not name else "detail", "at": time.time(), "question": ""}
        if not name:
            self._proposal["question"] = "What shall I call this protocol?"
            self._emit_protocols()
            return f"Got {len(steps)} step{'s' if len(steps) != 1 else ''}, {t}. What shall I call this protocol?"
        return self._next_proposal_question()

    def _next_proposal_question(self) -> str:
        t = self.title
        prop = self._proposal
        for i, st in enumerate(prop["protocol"]["steps"]):
            m = _PLACEHOLDER.search(st["text"])
            if m:
                prop.update(stage="detail", index=i, placeholder=m.group(0))
                thing = m.group("thing").lower()
                prop["question"] = f"Which {thing} should {prop['protocol']['name']} {_verb_of(st['text'])}?"
                self._emit_protocols()
                return f"{prop['question'][:-1]}, {t}?"
        prop.update(stage="approve", question="Shall I save it?")
        self._emit_protocols()
        steps = [st["text"] for st in prop["protocol"]["steps"]]
        risky = [x for x in steps if protocol_risk(x)]
        warn = f" I'll check with you before “{risky[0]}” each time it runs." if len(risky) == 1 else \
            (f" I'll check with you before the {len(risky)} steps that send, close or change things." if risky else "")
        return (f"Here's the {prop['protocol']['name']} protocol, {t}: {spoken_steps(steps, 12)}.{warn} Shall I save it? "
                "You can also say “edit it” to change it on screen.")

    def _proposal_reply(self, text: str) -> str | None:
        """While a proposed protocol waits: answers to its questions, "yes" / "no" / "edit it"."""
        prop = self._proposal
        if prop is None:
            return None
        if time.time() - prop["at"] > 600:
            self._proposal = None
            return None
        t = self.title
        clean = _bare(text).strip()
        if is_cancellation(clean) or re.match(r"^(?:no|nope|don'?t save it|scrap it|forget it|discard it)\W*$", clean, re.I):
            self._proposal = None
            self._emit_protocols()
            return f"Very well, {t}. I haven't saved it."
        if prop["stage"] == "name":
            name = clean_protocol_name(re.sub(r"^(?:call it|name it|it'?s called|let'?s call it)\s+", "", clean, flags=re.I))
            if not name:
                return f"What shall I call it, {t}?"
            prop["protocol"]["name"] = name
            prop["at"] = time.time()
            return self._next_proposal_question()
        if prop["stage"] == "detail":
            answer = re.sub(r"^(?:it'?s|use|launch|open|the one called|my)\s+", "", clean, flags=re.I).strip(" .")
            if not answer:
                return prop["question"]
            st = prop["protocol"]["steps"][prop["index"]]
            st["text"] = st["text"].replace(prop["placeholder"], answer, 1)
            prop["at"] = time.time()
            return self._next_proposal_question()
        if re.match(r"^(?:edit|change|tweak|adjust)\s+(?:it|that|the\s+protocol)\W*$", clean, re.I):
            self.emit("protocol_edit", protocol=prop["protocol"])
            self._proposal = None
            self._emit_protocols()
            return f"I've opened it in the protocol editor, {t}. Save it there when it's right."
        if is_confirmation(clean):
            return self._save_proposal()
        return None  # something else entirely: the proposal waits on screen

    def _save_proposal(self, data: dict | None = None) -> str:
        t = self.title
        prop = self._proposal
        if prop is None and data is None:
            return f"There's nothing waiting to be saved, {t}."
        payload = dict(data or prop["protocol"])
        self._proposal = None
        existed = self.protocols.find(payload.get("name") or "") is not None
        result = self.protocol_save(payload)
        if not result["ok"]:
            self._emit_protocols()
            return f"I couldn't save it, {t}: {result['error']}"
        name = result["protocol"]["name"]
        return (f"Protocol {name} {'updated' if existed else 'saved'}, {t}. Say “run {name}” whenever you like.")

    def protocol_proposal_answer(self, action: str, data: dict | None = None) -> dict:
        """The proposal card's Save / Edit / Cancel buttons."""
        if action == "save":
            reply = self._save_proposal(data if isinstance(data, dict) else None)
            return {"ok": reply.startswith("Protocol"), "message": reply}
        if action == "edit" and self._proposal:
            self.emit("protocol_edit", protocol=self._proposal["protocol"])
        self._proposal = None
        self._emit_protocols()
        return {"ok": True}

    def _record_step(self, rec: dict, text: str) -> str:
        """The user is dictating a protocol: each thing they say is a step until they say "done"."""
        t = self.title
        rec["at"] = time.time()
        text = _bare(text).strip()
        if not rec["name"]:
            kind = recording_reply(text)
            if kind == "abandon":
                self._recording = None
                self._emit_protocols()
                return f"Very well, {t}. I've scrapped it."
            rec["name"] = clean_protocol_name(re.sub(r"^(?:call it|name it|it'?s called|let'?s call it)\s+", "", text, flags=re.I))
            if not rec["name"]:
                return f"What shall I call it, {t}?"
            self._emit_protocols()
            if rec["steps"]:
                return self._finish_recording(rec)
            return f"The {rec['name']} protocol it is, {t}. What's the first step? Say “done” when you've finished."
        kind = recording_reply(text)
        if kind == "abandon":
            self._recording = None
            self._emit_protocols()
            return f"Very well, {t}. I haven't saved the {rec['name']} protocol."
        if kind == "undo":
            if rec["steps"]:
                gone = rec["steps"].pop()
                self._emit_protocols()
                return f"Removed “{gone}”. What's next, {t}?"
            return f"There aren't any steps yet, {t}. What's the first one?"
        if kind == "done":
            if not rec["steps"]:
                return f"There aren't any steps yet, {t}. Tell me one, or say “cancel”."
            return self._finish_recording(rec)
        new = split_steps(text)
        if not new:
            return f"Sorry, {t}, what's the next step?"
        rec["steps"].extend(new)
        self._emit_protocols()
        n = len(rec["steps"])
        lead = f"Step {n}" if len(new) == 1 else f"Steps {n - len(new) + 1} to {n}"
        return f"{lead}: {'; '.join(new)}. What's next? Or say “done”."

    def _finish_recording(self, rec: dict) -> str:
        t = self.title
        self._recording = None
        try:
            p = self.protocols.save(rec["name"], [structured_step(x).to_dict() for x in rec["steps"]],
                                    icon=_guess_icon(rec["name"], rec["steps"]), category=_guess_category(rec["name"], rec["steps"]))
        except ProtocolError as exc:
            self._emit_protocols()
            return f"I couldn't save it, {t}: {exc}"
        self._emit_protocols()
        return (f"Protocol {p.name} saved with {len(p.steps)} step{'s' if len(p.steps) != 1 else ''}, {t}. "
                f"Say “run {p.name}” whenever you like.")

    # -- triggers: schedules, starting up, an app opening, a hotkey
    def _protocol_scheduler(self) -> None:
        """Runs protocols by themselves: on a schedule ("every weekday at 7:30") or when an app opens."""
        seen_apps: set[str] | None = None
        fired: dict[str, float] = {}
        while not self._scheduler_stop.wait(self._scheduler_tick):
            try:
                now = datetime.now()
                items = [p for p in self.protocols.all() if p.enabled and p.steps]
                for p in items:
                    if protocol_due(p.schedule, now, p.last_run) and self._protocol_run is None:
                        self.protocols.update(p.id, last_run=time.time())
                        self._auto_run(p, "schedule", f"It's {now.strftime('%H:%M')}, {self.title}. Running your {p.name} protocol.")
                        break
                watched = [p for p in items if p.triggers.get("app")]
                if watched:
                    running = self._running_apps()
                    if seen_apps is not None:
                        for p in watched:
                            app = re.sub(r"[^a-z0-9]", "", p.triggers["app"].lower())
                            now_up = any(app and (app == r or (len(app) > 3 and app in r)) for r in running)
                            was_up = any(app and (app == r or (len(app) > 3 and app in r)) for r in seen_apps)
                            if now_up and not was_up and time.time() - fired.get(p.id, 0) > 900 and self._protocol_run is None:
                                fired[p.id] = time.time()
                                self._auto_run(p, "app", f"{p.triggers['app']} just opened, {self.title}. Running your {p.name} protocol.")
                                break
                    seen_apps = running
            except Exception:
                log.exception("Protocol scheduler hiccup")

    def _auto_run(self, p, trigger: str, line: str) -> None:
        self._wait_for_quiet(self._scheduler_stop, 60)
        turn = self._new_turn("announce")
        self._deliver(turn, [line])
        self._start_protocol(p, trigger=trigger)

    def _startup_protocols(self) -> None:
        for p in self.protocols.all():
            if p.enabled and p.steps and p.triggers.get("startup"):
                time.sleep(6)  # after the greeting
                self._auto_run(p, "startup", f"Running your {p.name} protocol, {self.title}, as you asked when I start.")
                return

    def _sync_hotkeys(self) -> None:
        if self.hotkeys is None:
            return
        wanted = {}
        for p in self.protocols.all():
            key = p.triggers.get("hotkey") if p.enabled else None
            if key:
                wanted[key] = p.id
        try:
            problems = self.hotkeys.set(wanted)
            for key, why in problems.items():
                self._activity("protocol", f"Hotkey {key} isn't available", "failed", why)
        except Exception:
            log.debug("hotkeys unavailable", exc_info=True)

    def _hotkey_pressed(self, pid: str) -> None:
        p = self.protocols.get(pid)
        if p is not None and self._protocol_run is None:
            self._spawn(lambda: self._auto_run(p, "hotkey", self._protocol_intro(p)), name="protocol-hotkey")

    # -- "you keep doing these together": protocol ideas from the command history
    def _protocol_suggestions(self) -> list[dict]:
        cmds = [a for a in self._activities if a.get("kind") == "command"]
        pairs: dict[tuple, int] = {}
        for a, b in zip(cmds, cmds[1:]):
            if 0 < b["at"] - a["at"] < 180 and a["text"].lower() != b["text"].lower():
                key = (a["text"].lower(), b["text"].lower())
                pairs[key] = pairs.get(key, 0) + 1
        existing = {tuple(s.text.lower() for s in p.steps[:2]) for p in self.protocols.all()}
        out = [{"steps": list(k), "count": n} for k, n in pairs.items() if n >= 3 and k not in existing]
        return sorted(out, key=lambda x: -x["count"])[:3]

    # ================================================================== activity log (what JARVIS did, and whether it worked)
    def _activity(self, kind: str, text: str, status: str = "ok", detail: str = "") -> dict:
        """Record an action: status is ok (verified), unverified (done, couldn't check), failed, or waiting."""
        item = {"id": next(self._activity_ids), "at": time.time(), "kind": kind, "text": text[:200], "status": status,
                "detail": detail[:300], "persona": self.persona.name}
        self._activities.append(item)
        self.emit("activity_log", item=item)
        return item

    def activity_list(self) -> dict:
        return {"items": list(self._activities), "health": self.health()}

    def health(self) -> dict:
        """What's working (Automation Center ▸ System health)."""
        helper = self.winhelper.status()
        caps = helper.get("caps") or {}
        voice = self._voice_ok
        return {
            "neural_core": bool(self._ollama and self._ollama.online and self._ollama.model),
            "model": (self._ollama.model if self._ollama else None),
            "voice": voice is not False, "microphone": bool(self._mic.get("available")),
            "wake_word": bool(self.wake is not None and getattr(self.wake, "running", False)),
            "media_sessions": bool(caps.get("media")), "app_volumes": bool(caps.get("audio")),
            "address_bar": bool(caps.get("uia")), "helper_errors": helper.get("errors") or {},
            "browser": self.browser.status(), "ocr": bool(getattr(self.vision.ocr, "error", None) is None),
            "google": bool(self.tools.google.configured), "internet": bool(self.tools.web_enabled),
        }

    def _foreground(self):
        try:
            return self.desktop.foreground()
        except Exception:
            return None

    def _duck(self, on: bool) -> None:
        """Lower (or restore) other apps' sound while JARVIS speaks, in order, off the audio thread."""
        self._duck_jobs.put("duck" if on else "unduck")
        if self._duck_worker is None or not self._duck_worker.is_alive():
            self._duck_worker = threading.Thread(target=self._duck_loop, name="ducking", daemon=True)
            self._duck_worker.start()

    def _duck_loop(self) -> None:
        while True:
            try:
                job = self._duck_jobs.get(timeout=30)
            except queue.Empty:
                return
            try:
                if job == "duck":
                    self.media.duck(float(self.config.get("duck_level", 0.3)))
                else:
                    self.media.unduck()
            except Exception:
                log.debug("ducking hiccup", exc_info=True)

    # ================================================================== the browser
    def _open_site(self, turn: Turn, url: str, label: str, host: str = "", title: str = "", done: str = "") -> str:
        """Open a page and only say it's open once the browser shows it."""
        t = self.title
        self._emit_turn(turn, "tool_activity", tool="browser", label=f"Opening {label}")
        nav = self.browser.open(url, expect_host=host, expect_title=title)
        self._tool_used(turn, "open_website", {"url": url})
        if nav.ok and nav.verified:
            self._activity("browser", f"Opened {label}", "ok", nav.url or nav.title)
            return f"{done}, {t}." if done else f"{label[:1].upper() + label[1:]} is open, {t}."
        if nav.ok:
            self._activity("browser", f"Opened {label}", "unverified")
            return f"Opening {label}, {t}."
        if nav.blocked:
            return self._blocked_reply(nav, label, url, host=host, title=title)
        self._activity("browser", f"Opened {label} (still loading)", "unverified", nav.title)
        return f"Opening {label}, {t}." + (" It's taking a while to load." if self.browser.watch else "")

    def _blocked_reply(self, nav: NavResult, label: str, url: str = "", host: str = "", title: str = "") -> str:
        """Explain what stopped the browser, and (where the user can fix it) wait for them and carry on."""
        t = self.title
        browser = nav.browser or self.browser.label()
        host = host or ((url.split("/")[2] if "://" in url else "") if url else "")
        if nav.state == AUTHENTICATION_REQUIRED:
            account = self.config.get("google_user_email") or "your Google account"
            self._wait_for_page(nav, label, url, host, title, "sign-in")
            self._activity("browser", f"{label}: waiting for you to sign in", "waiting", nav.url or nav.title)
            if url and not needs_account(url):
                return (f"{browser} is showing a Google sign-in page instead of {label}, {t}. You don't need to sign in for {label}: "
                        "close that page or sign in, and I'll carry on as soon as it's there.")
            return (f"{label[:1].upper() + label[1:]} needs you to sign in to {account} first, {t}. Please sign in in {browser}; "
                    "I'll carry on as soon as you're in. I never type passwords myself.")
        if nav.state == PROFILE_SELECTION:
            self._wait_for_page(nav, label, url, host, title, "profile", reopen=True)
            self._activity("browser", f"{browser} is asking which profile to use", "waiting")
            return f"{browser} is asking which profile to use, {t}. Pick yours and I'll open {label} straight after."
        if nav.state == FIRST_RUN_SETUP:
            self._wait_for_page(nav, label, url, host, title, "setup", reopen=True)
            self._activity("browser", f"{browser} is showing its first-run setup", "waiting")
            return f"{browser} is showing its first-run setup, {t}. Finish or skip it and I'll open {label} for you."
        if nav.state == CONSENT:
            self._wait_for_page(nav, label, url, host, title, "consent")
            self._activity("browser", f"{label}: cookie consent page", "waiting")
            return f"{label[:1].upper() + label[1:]} wants you to accept or reject cookies first, {t}. Once you've chosen, it'll load and I'll let you know."
        if nav.state == OFFLINE:
            self._activity("browser", f"Couldn't open {label}: offline", "failed", nav.title)
            return f"{browser} says there's no internet connection, {t}, so {label} didn't load."
        self._activity("browser", f"Couldn't open {label}", "failed", nav.title)
        return f"{label[:1].upper() + label[1:]} didn't load, {t}: {browser} shows “{nav.title.split(' - ')[0]}”."

    def _wait_for_page(self, nav: NavResult, label: str, url: str, host: str, title: str, why: str, reopen: bool = False) -> None:
        """Wait (up to 5 minutes, in the background) for the user to clear the blocker, then confirm or carry on."""
        if self._sign_in_wait is not None:
            self._sign_in_wait.set()  # only one wait at a time: the newest request wins
        cancel = threading.Event()
        self._sign_in_wait = cancel
        self.browser.waiting = {"label": label, "why": why, "since": time.time()}
        self.emit("browser", **self.browser.status())

        def run() -> None:
            try:
                if reopen:
                    for _ in range(150):  # wait until the browser is usable, then open the page again (the picker drops it)
                        if cancel.wait(2):
                            return
                        look = self.browser.look()
                        if look.state not in (PROFILE_SELECTION, FIRST_RUN_SETUP, "NOT_RUNNING"):
                            break
                    if url:
                        self.browser._launch(url, self.browser.key())
                result = self.browser.wait_until(host, title, site_name(url) if url else label, timeout=300, cancel=cancel)
                if cancel.is_set():
                    return
                if result.ok:
                    self._activity("browser", f"{label} is open (after {why})", "ok", result.url or result.title)
                    self.speak(f"Thank you, {self.title}. {label[:1].upper() + label[1:]} is open now.")
                else:
                    self._activity("browser", f"Stopped waiting for {label}", "failed", result.title)
            finally:
                if self._sign_in_wait is cancel:
                    self._sign_in_wait = None
                    self.browser.waiting = None
                    self.emit("browser", **self.browser.status())

        self._spawn(run, name="browser-wait")

    def _open_browser(self, turn: Turn, which: str) -> str:
        """'Open Chrome': bring the running one forward or start it, and report what it shows."""
        t = self.title
        key_before = self.browser.key()
        nav = self.browser.launch_browser() if which in ("", key_before) else self._launch_other(which)
        name = nav.browser or which.title()
        if nav.ok:
            self._activity("browser", f"{name} ready", "ok" if nav.verified else "unverified", nav.title)
            return f"{name} is {'up' if 'RUNNING' in nav.transitions else 'open and ready'}, {t}."
        if nav.blocked:
            return self._blocked_reply(nav, name)
        self._activity("browser", f"{name} didn't open", "failed")
        return f"{name} didn't open within 15 seconds, {t}. It may still be starting."

    def _launch_other(self, which: str) -> NavResult:
        saved = self.config.get("link_browser")
        try:
            self.config.update({"link_browser": which}, persist=False)
            return self.browser.launch_browser()
        finally:
            self.config.update({"link_browser": saved}, persist=False)

    def browser_status(self) -> dict:
        self.browser.look()
        return {**self.browser.status(), "profiles": self.browser.profiles()}

    # ================================================================== music and media
    def _youtube_keys(self, keys: list, times: int = 1) -> bool:
        """Press YouTube shortcuts in its tab: switches to the YouTube tab (in any browser) first."""
        vk = {"shift": 0x10, "left": 0x25, "right": 0x27, "up": 0x26, "down": 0x28, ".": 0xBE, ",": 0xBC, "0": 0x30}
        codes = [vk.get(k, ord(k.upper()) if len(k) == 1 else 0) for k in keys]
        if not all(codes):
            return False
        window = None
        for w in self._safe_windows():
            if (w.app or "").lower().removesuffix(".exe") in ("chrome", "msedge", "firefox", "brave", "opera", "vivaldi") and " - YouTube" in (w.title or ""):
                window = w
                break
        if window is None:
            try:
                window = tabs.find_tab(self.desktop, "youtube", "browser")
            except Exception:
                window = None
        if window is None:
            return False
        try:
            self.desktop.bring_to_front(window)
            time.sleep(0.25)
            for _ in range(max(1, min(int(times), 30))):
                self.desktop.press(codes, window=window)
                time.sleep(0.08)
            return True
        except Exception:
            log.debug("YouTube keys failed", exc_info=True)
            return False

    def _media_line(self, s) -> str:
        return f"{s.spoken()} on {s.label}" if s.title else s.label

    def _media(self, turn: Turn, cmd: MediaCommand) -> str:
        t = self.title
        hub = self.media
        if cmd.action == "now_playing":
            s = hub.now_playing()
            if s is None:
                return f"Nothing's playing right now, {t}."
            if not s.title:
                return f"{s.label} is open, but nothing's playing, {t}."
            where = ""
            if s.position is not None and s.duration:
                where = f", {describe_position(s.position)} of {describe_position(s.duration)}"
            state = "" if s.playing else " (paused)"
            self._activity("media", f"Now playing: {s.spoken()}", "ok")
            return f"That's {s.spoken()}, on {s.label}{where}{state}, {t}."
        if cmd.action in ("play",) and not cmd.vague:
            return self._play_media(turn, cmd)
        if cmd.action == "search":
            return self._open_site(turn, youtube_search_url(cmd.query), f"YouTube results for {cmd.query}", host="youtube.com")
        if cmd.action == "resume_history":
            return self._resume_history(turn)
        if cmd.action == "volume":
            return self._media_volume(cmd)
        if cmd.action in ("seek", "seek_to", "restart"):
            return self._media_seek(cmd)
        if cmd.action in ("fullscreen", "captions", "speed"):
            return self._youtube_extra(cmd)
        action = {"pause": "pause", "resume": "play", "play": "play", "next": "next", "previous": "previous", "stop": "stop"}[cmd.action]
        out = hub.control(action, cmd.target)
        if out.detail == "ambiguous":
            self._pending_media = {"action": action, "choices": out.choices, "at": time.time()}
            names = join_names([c.label for c in out.choices])
            return f"{names} are both playing, {t}. Which one?"
        if out.detail == "nothing":
            if action == "play":
                if cmd.target in ("", "music") and not cmd.query:  # "play music" with nothing paused: put something on
                    return self._play_media(turn, MediaCommand("play", query="music", vague=True))
                return f"There's nothing paused to resume, {t}."
            if cmd.target and cmd.target not in ("all", "music"):
                return f"I can't find {cmd.target.title() if cmd.target != 'youtube' else 'YouTube'} playing anything, {t}."
            return f"Nothing's playing right now, {t}."
        if out.detail == "already":
            return f"It's already {'paused' if action == 'pause' else 'playing'}, {t}."
        if out.detail == "no_helper":
            return f"I can't reach the media controls on this system, {t}."
        if out.detail == "unsupported":
            if action in ("next", "previous") and out.sessions and out.sessions[0].site.startswith("YouTube"):
                y = hub.youtube(["shift", "n" if action == "next" else "p"])
                if y.ok:
                    self._activity("media", f"{'Next' if action == 'next' else 'Previous'} on YouTube", "unverified")
                    return f"{'Next' if action == 'next' else 'Previous'} video, {t}."
            label = out.sessions[0].label if out.sessions else "That player"
            return f"{label} doesn't let me {'skip' if action == 'next' else 'go back' if action == 'previous' else action} from outside, {t}."
        if not out.ok:
            label = out.sessions[0].label if out.sessions else "the player"
            self._activity("media", f"{action.title()} {label}", "failed")
            return f"I asked {label} to {action}, {t}, but it didn't respond."
        names = join_names(sorted({s.label for s in out.sessions}))
        status = "ok" if out.verified else "unverified"
        self._activity("media", f"{action.title()}: {names}", status)
        self._spawn(self._media_refresh, 0.2, name="media-refresh")
        sure = "" if out.verified else " I can't confirm it from here, though."
        if action == "pause":
            return f"Paused {names}, {t}.{sure}" if cmd.query != "stop" else f"Stopped {names}, {t}.{sure}"
        if action == "stop":
            return f"Stopped {names}, {t}.{sure}"
        if action == "play":
            first = out.sessions[0]
            return f"Resuming {self._media_line(first)}, {t}.{sure}"
        now = next((hub._find(s, out.after) for s in out.sessions), None) if out.after else None
        what = f" Now playing {now.spoken()}." if now is not None and now.title and now.title != out.sessions[0].title else ""
        return f"{'Skipped' if action == 'next' else 'Back a track'}, {t}.{what}{sure}"

    def _media_choice(self, text: str) -> str | None:
        """The answer to "Spotify and YouTube are both playing. Which one?"."""
        pending, self._pending_media = self._pending_media, None
        if pending is None or time.time() - pending["at"] > 60:
            return None
        low = text.lower()
        choices = pending["choices"]
        if re.search(r"\b(?:both|all(?: of them)?|everything|each)\b", low):
            picked = choices
        else:
            hint = hint_from(low)
            picked = [c for c in choices if hint and (c.label.lower().startswith(hint) or c.process == hint or (hint == "video" and c.browser))]
            if not picked:
                ordinal = {"first": 0, "the first": 0, "second": 1, "the second": 1, "last": -1}
                pos = next((v for k, v in ordinal.items() if re.search(r"\b" + k + r"\b", low)), None)
                picked = [choices[pos]] if pos is not None and choices else []
        if not picked:
            return None
        out = self.media.control(pending["action"], targets=picked)
        names = join_names(sorted({c.label for c in picked}))
        verb = {"pause": "Paused", "play": "Resumed", "next": "Skipped", "previous": "Went back on", "stop": "Stopped"}[pending["action"]]
        self._activity("media", f"{verb} {names}", "ok" if out.verified else ("unverified" if out.ok else "failed"))
        return f"{verb} {names}, {self.title}." if out.ok else f"{names} didn't respond, {self.title}."

    def _media_volume(self, cmd: MediaCommand) -> str:
        t = self.title
        hub = self.media
        target = None
        found = hub.sessions()
        if cmd.target and cmd.target != "music":
            target = next((s for s in found if hint_from(cmd.target) and _media_hint(cmd.target)(s)), None)
        if target is None:
            target = next((s for s in found if s.playing), None) or hub.now_playing()
        if target is not None and hub.can_mix():
            if cmd.muted is not None:
                res = hub.app_volume(target.process, muted=cmd.muted)
            elif cmd.level is not None:
                res = hub.app_volume(target.process, level=cmd.level / 100)
            else:
                res = hub.app_volume(target.process, delta=(cmd.amount or 20) / 100)
            if res is not None:
                self._activity("media", f"{target.label} volume → {round(res['after'] * 100)}%{' (muted)' if res['muted'] else ''}", "ok")
                if cmd.muted is not None:
                    return f"{'Muted' if cmd.muted else 'Unmuted'} {target.label}, {t}."
                return f"{target.label} is at {round(res['after'] * 100)} percent now, {t}. My voice stays where it was."
        # no per-app volume on this PC: the whole system instead, said plainly
        try:
            if cmd.muted is not None:
                osctl.set_mute(cmd.muted)
                return f"{'Muted' if cmd.muted else 'Unmuted'} the sound, {t}. (I can't mute just the music on this PC.)"
            if cmd.level is not None:
                osctl.set_volume(int(cmd.level))
                return f"Volume set to {int(cmd.level)} percent, {t}."
            osctl.change_volume(int(cmd.amount or 20) // 2)
            return f"Volume {'up' if (cmd.amount or 0) > 0 else 'down'}, {t}."
        except osctl.OsControlError as exc:
            return f"I'm afraid {exc}"

    def _media_seek(self, cmd: MediaCommand) -> str:
        t = self.title
        hub = self.media
        found = hub.sessions()
        s = next((x for x in found if x.playing), None) or hub.now_playing()
        if s is None:
            return f"Nothing's playing to {'restart' if cmd.action == 'restart' else 'skip through'}, {t}."
        target = None
        if cmd.action == "restart":
            target = 0.0
        elif cmd.action == "seek_to":
            if cmd.query in ("middle", "end"):
                if not s.duration:
                    return f"I don't know how long it is, {t}."
                target = s.duration / 2 if cmd.query == "middle" else max(0.0, s.duration - 5)
            else:
                target = cmd.amount or 0.0
        elif s.position is not None:
            target = max(0.0, s.position + (cmd.amount or 0))
            if s.duration:
                target = min(target, max(0.0, s.duration - 1))
        if target is not None and "seek" in s.can and s.source == "sessions":
            out = hub.control("seek", position=target, targets=[s])
            if out.ok:
                self._activity("media", f"{s.label}: jumped to {describe_position(target)}", "ok" if out.verified else "unverified")
                if cmd.action == "restart":
                    return f"Back to the start, {t}."
                return f"Jumped to {describe_position(target)}, {t}."
        if s.site.startswith("YouTube") or s.browser:
            if cmd.action == "restart":
                out = hub.youtube(["0"], verify="start", before=s)
                done = "Back to the start"
            elif cmd.action == "seek_to":
                return f"YouTube didn't let me jump straight there, {t}. Try “skip forward 2 minutes”."
            else:
                amount = cmd.amount or 10
                out = hub.youtube(["l" if amount > 0 else "j"], times=max(1, round(abs(amount) / 10)),
                                  verify="forward" if amount > 0 else "back", before=s)
                done = f"{'Forward' if amount > 0 else 'Back'} {describe_duration(int(abs(amount)))}"
            if out.ok:
                self._activity("media", f"YouTube: {done.lower()}", "ok" if out.verified else "unverified")
                return f"{done}, {t}."
            return f"I couldn't find the YouTube tab to do that, {t}."
        return f"{s.label} doesn't let me move through it from outside, {t}."

    def _youtube_extra(self, cmd: MediaCommand) -> str:
        t = self.title
        keys, label = {"fullscreen": (["f"], "Full screen" if cmd.on is not False else "Out of full screen"),
                       "captions": (["c"], "Captions " + ("on" if cmd.on is not False else "off") if cmd.on is not None else "Captions toggled"),
                       "speed": ([("shift"), "." if cmd.query == "up" else ","], "Faster" if cmd.query == "up" else "Slower")}[cmd.action]
        if cmd.action == "speed" and cmd.query == "normal":
            return f"YouTube has no shortcut for normal speed, {t}: say “slow it down” or “speed it up” to step back to it."
        if not any((s.site.startswith("YouTube") for s in self.media.sessions())) and \
                not any(" - YouTube" in (w.title or "") for w in self._safe_windows()):
            return f"That works on a YouTube video, {t}, and I can't see one open."
        out = self.media.youtube(keys)
        if not out.ok:
            return f"I couldn't find the YouTube tab, {t}."
        self._activity("media", f"YouTube: {label.lower()}", "unverified")
        return f"{label}, {t}."

    def _play_media(self, turn: Turn, cmd: MediaCommand) -> str:
        t = self.title
        if not self.tools.web_enabled:
            return f"Internet access is switched off in Settings, {t}, so I can't fetch music."
        hub = self.media
        service = cmd.service or self.config.get("music_service") or "youtube"
        query = cmd.query
        found = hub.sessions()
        if cmd.vague:
            paused = [s for s in found if s.status == "paused" and s.title]
            spotify = next((s for s in found if s.process == "spotify"), None)
            if spotify is not None and spotify.playing:
                return f"Spotify is already playing {spotify.spoken()}, {t}."
            if paused:
                out = hub.control("play", targets=[hub._prefer(paused)])
                if out.ok:
                    return f"Resuming {self._media_line(out.sessions[0])}, {t}."
            query = self._favourite_music() or ("relaxing music mix" if service == "youtube" else "Daily Mix")
        for s in found:  # don't play two things at once
            if s.playing:
                hub.control("pause", targets=[s])
        if service == "spotify":
            uri, web = spotify_targets(query)
            self._emit_turn(turn, "tool_activity", tool="media", label=f"Opening {query} in Spotify")
            try:
                self.tools.open_uri(uri)
                where = "Spotify"
            except Exception:
                self.tools.open_website(web)
                where = "Spotify's web player"
            self._activity("media", f"Spotify search: {query}", "unverified")
            self._remember_media({"title": query, "app": "Spotify", "query": query})
            return (f"I've searched {where} for {query}, {t}. Press play on the top result. "
                    "If you'd like me to start songs by myself, say “play it on YouTube”.")
        self._emit_turn(turn, "tool_activity", tool="media", label=f"Finding {query} on YouTube")
        try:
            found_video = find_youtube_video(self.tools._client(), query)
        except Exception:
            log.info("YouTube lookup for %r failed", query, exc_info=True)
            found_video = None
        if turn.cancel.is_set():
            return ""
        if found_video is None:
            return self._open_site(turn, youtube_search_url(query), f"YouTube's results for {query}", host="youtube.com")
        url, title = found_video
        self._emit_turn(turn, "activity", label=f"Opening {title or query}")
        nav = self.browser.open(url, expect_host="youtube.com", expect_title=title, wait=12.0)
        self._tool_used(turn, "open_website", {"url": url})
        self._remember_media({"title": title or query, "app": "YouTube", "url": url, "query": query})
        if nav.blocked:
            return self._blocked_reply(nav, f"{title or query} on YouTube")
        playing = self._await_playing(title, 10.0)
        if playing is not None and playing.playing:
            hub.remember(playing, "YouTube")
            self._activity("media", f"Playing {title or query} on YouTube", "ok")
            return f"Playing {title or query} on YouTube, {t}."
        if playing is not None:  # opened but not started (autoplay blocked): press play
            hub.remember(playing, "YouTube")
            out = hub.control("play", targets=[playing])
            if out.verified:
                self._activity("media", f"Playing {title or query} on YouTube", "ok")
                return f"Playing {title or query} on YouTube, {t}."
        if hub.can_read():
            self._activity("media", f"Opened {title or query} on YouTube (not confirmed playing)", "unverified")
            return f"I've opened {title or query} on YouTube, {t}, but it hasn't started playing yet. Your browser may want a click first."
        self._activity("media", f"Opened {title or query} on YouTube", "unverified")
        return f"Playing {title or query} on YouTube, {t}."

    def _await_playing(self, title: str, wait: float):
        """The media session for the video just opened, once Windows reports it (None if it never shows up)."""
        if not self.media.can_read():
            return None
        want = re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()
        deadline = time.monotonic() + wait
        best = None
        while time.monotonic() < deadline:
            for s in self.media.sessions():
                have = re.sub(r"[^a-z0-9]+", " ", s.title.lower()).strip()
                if s.browser and have and want and (have in want or want in have):
                    best = s
                    if s.playing:
                        return s
            time.sleep(0.4)
        return best

    def _favourite_music(self) -> str:
        """Something the user said they like ("I love jazz" -> "jazz mix"), from long-term memory."""
        try:
            search = getattr(self.memory, "search", None) or getattr(getattr(self.memory, "engine", None), "search", None)
            for m in (search("music I like favourite songs artists genre", limit=6) if search else []):
                text = m.text if hasattr(m, "text") else str(m)
                hit = re.search(r"\b(?:loves?|likes?|enjoys?|into|fan of|favou?rite (?:band|artist|singer|genre|music) is)\s+(?:listening to\s+)?(?P<what>[\w' &-]{3,40})", text, re.I)
                if hit and re.search(r"music|jazz|rock|pop|rap|hip|classical|band|song|singer|lo-?fi|metal|country|r&b|indie|edm|house|techno|soul", text, re.I):
                    what = hit.group("what").strip()
                    return what if re.search(r"\b(?:mix|music|songs|playlist)\b", what, re.I) else f"{what} mix"
        except Exception:
            log.debug("couldn't look up music taste", exc_info=True)
        return ""

    def _remember_media(self, entry: dict) -> None:
        history = [h for h in (self.config.get("media_history") or []) if isinstance(h, dict)]
        entry = {**entry, "at": time.time()}
        if history and history[-1].get("title") == entry.get("title"):
            history[-1] = {**history[-1], **entry}
        else:
            history.append(entry)
        try:
            self.config.update({"media_history": history[-30:]})
        except Exception:
            log.debug("couldn't save media history", exc_info=True)

    def _resume_history(self, turn: Turn) -> str:
        t = self.title
        hub = self.media
        paused = [s for s in hub.sessions() if s.status == "paused" and s.title]
        if paused:
            out = hub.control("play", targets=[hub._prefer(paused)])
            if out.ok:
                return f"Picking up {self._media_line(out.sessions[0])}, {t}."
        history = [h for h in (self.config.get("media_history") or []) if isinstance(h, dict) and h.get("title")]
        if not history:
            return f"I don't have a record of what you were listening to, {t}. Tell me what to play."
        last = history[-1]
        if last.get("url"):
            url = last["url"]
            if last.get("position") and "youtube.com/watch" in url and "&t=" not in url:
                url += f"&t={int(last['position'])}s"
            nav = self.browser.open(url, expect_host="youtube.com", expect_title=last["title"], wait=12.0)
            if nav.blocked:
                return self._blocked_reply(nav, last["title"])
            self._activity("media", f"Back to {last['title']}", "ok" if nav.ok else "unverified")
            return f"Back to {last['title']}, {t}."
        if last.get("app") == "Spotify":
            return self._play_media(turn, MediaCommand("play", query=last.get("query") or last["title"], service="spotify"))
        return self._play_media(turn, MediaCommand("play", query=last.get("query") or last["title"], service="youtube"))

    def media_control(self, action: str, key: str = "") -> dict:
        """The HUD's ⏮ ⏯ ⏭ buttons (for one player, or whatever is playing)."""
        if action not in ("toggle", "next", "previous", "play", "pause"):
            return {"ok": False}
        found = self.media.sessions()
        targets = [s for s in found if s.key == key] if key else None
        if action == "toggle" and targets:
            action = "pause" if targets[0].playing else "play"
        elif action == "toggle":
            playing = [s for s in found if s.playing]
            action, targets = ("pause", playing[:1]) if playing else ("play", None)
        out = self.media.control(action, targets=targets)
        self._spawn(self._media_refresh, 0.1, name="media-refresh")
        return {"ok": out.ok, "verified": out.verified, "error": None if out.ok else (out.detail or "failed")}

    def media_volume(self, key: str, level: float) -> dict:
        s = next((x for x in self.media.sessions() if x.key == key), None)
        if s is None or not self.media.can_mix():
            return {"ok": False, "error": "App volume isn't available on this PC."}
        res = self.media.app_volume(s.process, level=max(0.0, min(1.0, float(level))))
        return {"ok": res is not None, **(res or {})}

    def _media_refresh(self, delay: float = 0.0) -> None:
        if delay:
            time.sleep(delay)
        found = self.media.sessions()
        mixer = {str(r.get("name") or "").lower(): r for r in self.media.mixer()} if found and self.media.can_mix() else {}
        rows = []
        for s in found:
            d = s.to_dict()
            vol = mixer.get(s.process)
            if vol is not None:
                d["volume"], d["muted"] = round(float(vol.get("volume") or 0), 2), bool(vol.get("muted"))
            rows.append(d)
        playing = next((s for s in found if s.playing), None) or (found[0] if found else None)
        summary = playing.to_dict() if playing else None
        snapshot = json.dumps([[r["key"], r["status"], r.get("volume")] for r in rows])
        if snapshot != self._media_snapshot:
            self._media_snapshot = snapshot
            self._now_playing = playing
            self.emit("media", playing=summary, sessions=rows, status=self.media.status())
            if playing is not None and playing.playing and playing.title:
                self._remember_media({"title": playing.title, "artist": playing.artist, "app": playing.label,
                                      **({"position": playing.position} if playing.position else {})})

    def _media_watch(self) -> None:
        """Keeps the HUD's now-playing strip and the Automation Center current."""
        try:
            restored = self.media.recover()
            if restored:
                log.info("Restored %d app volume(s) left lowered by a crash", restored)
        except Exception:
            log.debug("volume recovery failed", exc_info=True)
        while not self._scheduler_stop.wait(3):
            try:
                self._media_refresh()
            except Exception:
                log.debug("media watch hiccup", exc_info=True)

    # ================================================================== "stop listening"
    def pause_listening(self) -> str:
        """Close the microphone (wake word included) until the user presses the mic button or says/types "start listening"."""
        self._paused = True
        if self.wake is not None:
            self.wake.stop()
            self.wake = None
            self.stt.set_source_provider(None)
        self.emit("listening", paused=True)
        self.emit("wake_status", **self.wake_status())
        return (f"Okay, {self.title}, I've stopped listening. Press the microphone button, or type \"start listening\", "
                "whenever you need me.")

    def resume_listening(self) -> str:
        was = self._paused
        self._paused = False
        self.emit("listening", paused=False)
        self._spawn(self._sync_wake_word, name="wake-sync")
        if not was:
            return f"I'm already listening, {self.title}."
        phrase = self.wake_status().get("phrase") or "Hey Jarvis"
        hands_free = f" Say \"{phrase}\" any time." if self.config.get("wake_word", True) else ""
        return f"I'm listening again, {self.title}.{hands_free}"

    def start_listening(self) -> str:
        if self._paused:  # pressing the mic button means "I want to talk": listen again
            self._paused = False
            self.emit("listening", paused=False)
            self._spawn(self._sync_wake_word, name="wake-sync")
        with self._turn_lock:
            current = self._turn
            if current is not None and current.kind == "listen" and self.state.state == State.LISTENING:
                current.stop_listening.set()  # second click = "I'm done talking"
                return "stopping"
        if not self._mic.get("available"):
            self._mic = self.stt.availability()
            if not self._mic.get("available"):
                self.sfx.play("error")
                self.emit("notice", level="error", text=self._mic.get("reason") or "No microphone detected.")
                self.emit("mic_status", **self._mic)
                return "unavailable"
        turn = self._new_turn("listen")
        self._spawn(self._listen_turn, turn, name=f"listen-{turn.id}")
        return "listening"

    def hold_listening(self) -> None:
        turn = self._turn
        if turn is not None and turn.kind == "listen":
            turn.hold.set()

    def stop_listening(self) -> None:
        turn = self._turn
        if turn is not None and turn.kind == "listen":
            turn.hold.clear()
            turn.stop_listening.set()

    def interrupt(self) -> bool:
        with self._turn_lock:
            turn, self._turn = self._turn, None
            if turn is not None:
                turn.abort()
        self.audio.stop_voice()
        self.emit("speech_stop")
        was_active = self.state.state != State.IDLE
        self.state.transition(State.IDLE, "interrupted")
        if was_active:
            self.sfx.play("interrupt")
        return was_active

    def clear_memory(self) -> None:
        self.llm.clear_history()
        self.emit("core_stats", **self.core_stats())

    def play_sfx(self, name: str) -> None:
        self.sfx.play(name)

    # ================================================================== turns
    def _listen_turn(self, turn: Turn) -> None:
        if not self._set_state(State.LISTENING, turn, "listen"):
            return
        self.sfx.play("listen")
        try:
            capture = self.stt.listen(
                turn.cancel,
                turn.stop_listening,
                turn.hold,
                on_frame=lambda bands, level: self._emit_turn(turn, "mic_frame", bands=bands, level=level),
                on_phase=lambda phase: self._emit_turn(turn, "listen_phase", phase=phase),
                probe=self._probe if float(self.config.get("patience", 3.0)) > 0 else None,
            )
        except STTError as exc:
            self._fail_turn(turn, str(exc))
            return
        except Exception:
            log.exception("Listening failed")
            self._fail_turn(turn, f"Something went wrong with the microphone, {self.title}. Please try again.")
            return
        if not self._is_current(turn):
            return
        self.sfx.play("listen_end")
        if capture is None or capture.duration < 0.3:
            self.emit("notice", level="info", text="No speech detected.")
            self._finish_turn(turn)
            return

        self._set_state(State.THINKING, turn, "transcribing")
        self._emit_turn(turn, "listen_phase", phase="transcribing")
        try:
            text = capture.text if capture.text is not None else self.stt.transcribe(
                capture, on_status=lambda msg: self._emit_turn(turn, "notice", level="info", text=msg)
            )
        except STTError as exc:
            self._fail_turn(turn, str(exc))
            return
        except Exception:
            log.exception("Transcription failed")
            self._fail_turn(turn, f"I couldn't make out what you said, {self.title}. Please try again.")
            return
        if not self._is_current(turn):
            return
        text = _strip_wake(text).strip()
        if not text:
            self._missed(turn)
            return
        if _PAUSE_LISTENING.match(_bare(text)):
            self._converse(turn, text, "voice")  # "stop listening": handled (and answered) like any request
            return
        if re.sub(r"[^\w' ]+", "", text.lower()).strip() in _STOP_PHRASES and self._awaiting is None:
            self.sfx.play("interrupt")
            self.emit("notice", level="info", text="Standing by.")
            self._finish_turn(turn)
            return
        self._converse(turn, text, "voice")

    def _missed(self, turn: Turn) -> None:
        """Nothing intelligible was heard: say so, and (once) listen again so the user can just repeat it."""
        retry = not getattr(turn, "retried", False) and turn.kind == "listen"
        line = f"Sorry, {self.title}, I didn't catch that." + (" Could you say it again?" if retry else "")
        self.emit("notice", level="info", text=line)
        if self.config.get("voice_enabled", True) and self.audio.available:
            try:
                self.audio.play_voice(self.audio.decode(self.tts.synthesize(line)))
                deadline = time.monotonic() + 6
                while self.audio.voice_busy() and time.monotonic() < deadline and not turn.cancel.is_set():
                    time.sleep(0.05)
            except Exception:
                log.debug("couldn't say the 'didn't catch that' line", exc_info=True)
        if not self._finish_turn(turn) or turn.cancel.is_set() or not retry:
            return
        again = self._new_turn("listen")
        again.retried = True
        self._spawn(self._listen_turn, again, name=f"listen-{again.id}")

    def _probe(self, pcm: bytes, rate: int) -> tuple[str, bool]:
        """Recognise the speech so far, in the background, to decide whether the speaker has finished."""
        text = self.stt.transcribe(Capture(pcm, rate))
        return text, not looks_unfinished(_strip_wake(text) or text)

    def _fail_turn(self, turn: Turn, message: str) -> None:
        if self._is_current(turn):
            self.sfx.play("error")
            self.emit("system_message", level="error", text=message)
            self._finish_turn(turn)

    def _warm_file_index(self) -> None:
        if self.tools.files_enabled:
            self._spawn(self.tools.index.entries, name="file-index")

    def _tool_used(self, turn: Turn, name: str, args: dict) -> None:
        self._emit_turn(turn, "tool_activity", tool=name, label=describe_call(name, args))

    def _clock_answer(self, text: str) -> str | None:
        match = _CLOCK.match(text.strip())
        if not match:
            return None
        now = datetime.now()
        if (match.group("what") or "day").lower() == "time":
            hour = now.hour % 12 or 12
            return f"It's {hour}:{now.minute:02d} {'AM' if now.hour < 12 else 'PM'}, {self.title}."
        return f"Today is {now.strftime('%A')}, {now.day} {now.strftime('%B %Y')}, {self.title}."

    def _direct_chain(self, turn: Turn, text: str) -> str | None:
        """'open Spotify and open Notepad' / 'launch chrome, then play GTA 5': run each part in order."""
        parts = [p.strip() for p in _CHAIN_SPLIT.split(text.strip()) if p.strip()]
        if len(parts) < 2 or len(parts) > 5:
            return None
        replies, understood = [], 0
        for part in parts:
            if turn.cancel.is_set():
                break
            reply = self._direct_command(turn, part)
            if reply is None:
                replies.append(f"I couldn't work out '{part}', {self.title}.")
                continue
            understood += 1
            replies.append(reply)
        if not understood:
            return None  # "open the pod bay doors and start the engine": the model can answer that
        suffix = f", {self.title}."
        return " ".join(r[: -len(suffix)] + "." if i < len(replies) - 1 and r.endswith(suffix) else r for i, r in enumerate(replies))

    # ================================================================== vision: look, watch, act
    def _start_vision(self) -> None:
        self.tracker.start()
        warm = getattr(self.vision.ocr, "available", None)
        if warm:
            self._spawn(warm, name="ocr-warmup")  # loads Windows OCR in the background so the first look is quick

    def vision_status(self) -> dict:
        watching = self.watcher is not None and self.watcher.running
        return {**self.vision.status(), "watching": watching, "watch_label": self.watcher.label if watching else "",
                "allow_control": bool(self.config.get("allow_control", True)),
                "pulling": bool(self._pull_thread is not None and self._pull_thread.is_alive())}

    def _vision_request(self, turn: Turn, text: str, lang: Detection | None):
        """Returns what to say (a list or a token stream) when ``text`` is about the screen, else None."""
        if wants_vision_install(text):
            return [self._install_vision()]
        screens = parse_monitor_command(text)
        if screens is not None:
            return [self._monitor_command(screens)]
        watch = parse_watch(text)
        if watch is not None:
            return [self._watch(turn, watch)]
        act = parse_act(text)
        if act is not None:
            return self._act(turn, act)
        look = parse_look(text)
        if look is not None:
            return self._look(turn, look, lang)
        return None

    # ================================================================== monitors
    def _monitors(self) -> list:
        try:
            return list(self.desktop.monitors())
        except Exception:
            log.debug("couldn't list monitors", exc_info=True)
            return []

    def _monitor_command(self, cmd: MonitorCommand) -> str:
        title = self.title
        monitors = self._monitors()
        if cmd.action == "info":
            if len(monitors) <= 1:
                return f"You have one monitor connected, {title}."
            main = next(m for m in monitors if m.primary)
            others = ", ".join(describe_monitor(m, monitors).replace("your ", "") for m in monitors if not m.primary)
            return (f"You have {len(monitors)} monitors, {title}. Your main one is the {main.width} by {main.height} display "
                    f"(Windows calls it display {main.number or 1}); the others: {others}. You can change which is main in Windows' Display settings.")
        if not self.config.get("allow_control", True):
            return f"Screen control is switched off in Settings, {title}."
        target = resolve_monitor(cmd.which, monitors)
        if target is None:
            return (f"You only have one monitor connected, {title}." if len(monitors) <= 1
                    else f"I'm not sure which monitor you mean, {title}. Try \"main monitor\" or \"other monitor\".")
        if cmd.app.lower() in ("yourself", "you", "jarvis", "the hud", "your window", self.persona.name.lower()):
            own = [w for w in self.desktop.windows(include_own=True) if self.desktop.is_own(w.hwnd)] if hasattr(self.desktop, "is_own") else []
            if not own:
                return f"I couldn't find my own window, {title}."
            try:
                self.desktop.move_to_monitor(own[0], target)
            except ScreenError as exc:
                return f"I couldn't move, {title}: {exc}"
            return f"Moving over to {describe_monitor(target, monitors)}, {title}."
        if cmd.app:
            windows = self._app_windows(cmd.app)
            if not windows:
                return f"I can't see {cmd.app} open, {title}."
            window = windows[0]
        else:
            window = self.tracker.current()
            if window is None:
                return f"I'm not sure which window you mean, {title}. Click on it once, then ask me again."
        try:
            self.desktop.move_to_monitor(window, target)
        except ScreenError as exc:
            return f"I couldn't move it, {title}: {exc}"
        return f"Moved {cmd.app or window.label} to {describe_monitor(target, monitors)}, {title}."

    def _open_on_monitor(self, which: str, name: str, before: set[int]) -> None:
        """After launching something, wait for its window and move it to the monitor the user asked for."""
        monitors = self._monitors()
        target = resolve_monitor(which, monitors)
        if target is None:
            return
        want = re.sub(r"[^a-z0-9]", "", name.lower())
        deadline = time.monotonic() + 15
        fallback_at = time.monotonic() + 4
        while time.monotonic() < deadline and not self._prewarm_stop.is_set():
            fresh = [w for w in self.desktop.windows() if w.hwnd not in before]
            match = next((w for w in fresh if want and (want in re.sub(r"[^a-z0-9]", "", (w.app or "").lower())
                                                       or want in re.sub(r"[^a-z0-9]", "", (w.title or "").lower()))), None)
            if match is None and fresh and time.monotonic() > fallback_at:
                match = fresh[0]
            if match is not None:
                time.sleep(0.4)  # let it finish drawing before we move it
                try:
                    self.desktop.move_to_monitor(match, target)
                except Exception as exc:
                    log.info("Couldn't move %s to the monitor: %s", name, exc)
                return
            time.sleep(0.3)

    def _install_vision(self) -> str:
        if self.vision.model:
            return f"My eyes are already online, {self.title}: I'm using {self.vision.model}."
        if not self.llm.online:
            return f"Ollama isn't running, {self.title}, so I can't download the vision model yet."
        name = self.config.get("vision_model") or DEFAULT_VISION_MODEL
        result = self.pull_model(name, role="vision")
        if not result.get("ok"):
            return f"I couldn't start the download, {self.title}: {result.get('error')}"
        self.emit("vision_status", **self.vision_status())
        return (f"Downloading my vision model, {name}, {self.title}. It's about 6 gigabytes, a one-time download. "
                "I'll tell you when my eyes are ready.")

    def _target_window(self):
        window = self.tracker.current()
        if window is None and sys.platform == "win32":
            raise ScreenError("I'm not sure which window you mean. Click on it once, then ask me again.")
        return window

    def _look(self, turn: Turn, req: LookRequest, lang: Detection | None):
        monitor = None
        if req.monitor:
            monitors = self._monitors()
            monitor = resolve_monitor(req.monitor, monitors)
            if monitor is None:
                yield (f"You only have one monitor connected, {self.title}." if len(monitors) <= 1
                       else f"I'm not sure which monitor you mean, {self.title}. Try \"main monitor\" or \"other monitor\".")
                return
        window = None if req.full_screen else self._target_window()
        label = describe_monitor(monitor, self._monitors()) if monitor else window.label if window else "your screen"
        self._emit_turn(turn, "tool_activity", tool="vision", label=f"Looking at {label}")
        try:
            obs = self.vision.observe(window, monitor=monitor)
        except ScreenError as exc:
            yield f"I'm afraid {exc}"
            return
        self._emit_turn(turn, "vision_look", image=data_url(obs.shot.image), title=label, model=self.vision.model or "",
                        ocr=bool(obs.lines))
        answer: list[str] = []
        try:
            for piece in self.vision.describe(obs, req.question, turn.cancel, lang.name if lang else None, self.title,
                                                 name=self.persona.name if self.persona.id != "jarvis" else "J.A.R.V.I.S."):
                answer.append(piece)
                yield piece
        except VisionUnavailable as exc:
            yield f"{exc}"
            return
        if not self.vision.model:
            self._emit_turn(turn, "system_message", level="info",
                            text="I only read the text on screen this time. Say \"install vision\" to give me real sight (one-time 6 GB download).")
        if answer:
            self.llm.remember(req.question, f"[Looking at {label}] " + "".join(answer).strip())

    def _watch(self, turn: Turn, req: WatchRequest) -> str:
        title = self.title
        current = self.watcher if self.watcher is not None and self.watcher.running else None
        if req.action == "stop":
            if current is None:
                return f"I wasn't watching anything, {title}."
            current.stop()
            self.watcher = None
            self.emit("vision_status", **self.vision_status())
            return f"I've stopped watching your screen, {title}."
        if req.action == "status":
            if current is None:
                return f"I'm not watching your screen at the moment, {title}."
            return f"Yes, {title}. I'm watching for {current.label}."
        if not (self.vision.model or self.vision.ocr_ready):
            return (f"I need eyes for that, {title}: say \"install vision\" and I'll download a vision model, "
                    "or make sure Windows OCR is available.")
        try:
            window = self._target_window() if req.condition else None
        except ScreenError as exc:
            return f"I'm afraid {exc}"
        if window is not None and is_private(window, self.config.get("vision_exclusions") or ""):
            return f"{window.label} is on your privacy list, {title}, so I won't watch it."
        if current is not None:
            current.stop()
        self.watcher = Watcher(self.vision, self.tracker.current, notify=self._watch_notify, on_end=self._watch_ended,
                               condition=req.condition, window=window,
                               interval=float(self.config.get("watch_interval", 2.0)))
        self.watcher.start()
        self.emit("vision_status", **self.vision_status())
        if req.condition:
            where = f" on {window.label}" if window else ""
            return f"Very well, {title}. I'll keep an eye{where} and tell you when {_you(req.condition)}."
        return (f"Watching your screen, {title}. I'll speak up if anything important happens. "
                "Say \"stop watching\" when you're done.")

    def _watch_notify(self, what: str) -> None:
        watcher = self.watcher
        if watcher is not None and watcher.condition:
            message = f"Heads up, {self.title}. You asked me to tell you when {_you(watcher.condition)}, and it has."
        else:
            message = f"Heads up, {self.title}: {what}"
        self.emit("system_message", level="ok", text=message)
        self._announce(message)

    def _watch_ended(self, reason: str) -> None:
        watcher = self.watcher
        if watcher is None or watcher.running:
            return  # an older watcher, replaced by the current one
        self.watcher = None
        if reason == "timeout":
            self._announce(f"I've stopped watching for {_you(watcher.label)}, {self.title}; it's been a while.")
        elif reason == "closed":
            self._announce(f"The window I was watching has closed, {self.title}, so I've stopped watching.")
        elif reason.startswith("error"):
            self.emit("notice", level="error", text=f"Screen watch stopped: {reason}")
        self.emit("vision_status", **self.vision_status())

    def _announce(self, message: str) -> None:
        """Say something unprompted, but never over the top of a conversation in progress."""
        def run() -> None:
            if self._prewarm_stop.is_set():
                return  # shutting down
            self.sfx.play("notify")
            deadline = time.monotonic() + 90
            while self.state.state != State.IDLE and time.monotonic() < deadline:
                time.sleep(0.4)
            if not self._prewarm_stop.is_set():
                self.speak(message, delay=0.3)

        self._spawn(run, name="announce")

    def _act(self, turn: Turn, act: ActRequest):
        title = self.title
        if not self.config.get("allow_control", True):
            yield f"Screen control is switched off in Settings, {title}."
            return
        if act.action in ("close_tab", "reopen_tab", "close_browser", "close_app", "new_tab", "next_tab", "previous_tab",
                          "switch_tab", "close_other_tabs"):
            yield self._window_command(act)
            return
        try:
            window = self._target_window()
        except ScreenError as exc:
            yield f"I'm afraid {exc}"
            return
        if is_private(window, self.config.get("vision_exclusions") or ""):
            yield f"{window.label} is on your privacy list, {title}, so I won't touch it."
            return
        if act.action == "type" and re.search(r"pass(?:word|code)|pin\b|card number|cvv|security code", act.target or "", re.I):
            yield f"I'd rather not type into password or payment fields, {title}. Those are yours."
            return
        target = None
        obs = None
        if act.target:
            self._emit_turn(turn, "tool_activity", tool="vision", label=f"Looking for {act.target}")
            try:
                obs = self.vision.observe(window)
                target = self.vision.locate(obs, act.target)
            except (ScreenError, VisionUnavailable) as exc:
                yield f"I'm afraid {exc}"
                return
            if target is None:
                hint = "" if self.vision.model else " Say \"install vision\" and I'll be able to find things that aren't text."
                yield f"I can't see \"{act.target}\" on {window.label if window else 'the screen'}, {title}.{hint}"
                return
        always = self.config.get("act_confirm", "auto") == "always"
        needs_ok = always or is_risky(act) or (target is not None and not target.sure)
        if needs_ok:
            act_id = f"act{next(self._act_ids)}"
            picture = None
            try:
                if obs is None:
                    obs = self.vision.observe(window, read_text=False)
                picture = data_url(self.vision.mark_target(obs, target) if target else obs.shot.image)
            except Exception:
                log.debug("no confirmation picture", exc_info=True)
            self._pending_act = {"id": act_id, "act": act, "window": window, "target": target, "at": time.time()}
            what = act.label or act.action
            self._emit_turn(turn, "act_confirm", id=act_id, label=what, image=picture,
                            where=window.label if window else "", reason="risky" if is_risky(act) else "unsure")
            if target is not None:
                yield f"Is this the right one, {title}? Say yes and I'll {what}."
            else:
                yield f"Shall I {what} on {window.label if window else 'the screen'}, {title}?"
            return
        yield self._run_act(act, window, target)

    def _window_command(self, act: ActRequest) -> str:
        """Tabs and whole apps the user named ("close the YouTube tab", "close Spotify"): no looking needed."""
        title = self.title
        current = self.tracker.current()
        try:
            if act.action == "close_tab" and act.tab:
                closed = tabs.close_named_tab(self.desktop, act.tab, act.app or "browser", current)
                done = f"Closed the {closed or act.tab} tab, {title}."
            elif act.action == "close_tab":
                # the browser you were just in, else the browser window nearest the front: never some other app
                tabs.close_current_tab(self.desktop, act.app or "browser", current)
                done = f"Tab closed, {title}."
            elif act.action == "new_tab":
                address = (self.tools.website_for(act.text) or act.text) if act.text else ""
                if not tabs.browser_windows(self.desktop, act.app or "browser", current):
                    self.tools.open_website(address or "google.com")  # no browser open: start one
                    return f"Opening {act.text or 'a new tab'}, {title}."
                tabs.new_tab(self.desktop, act.app or "browser", current, address)
                return f"Opened {act.text} in a new tab, {title}." if act.text else f"New tab, {title}."
            elif act.action in ("next_tab", "previous_tab"):
                tabs.step_tab(self.desktop, act.action == "next_tab", act.app or "browser", current)
                return f"Done, {title}."
            elif act.action == "switch_tab":
                shown = tabs.switch_to_tab(self.desktop, act.tab, act.app or "browser", current)
                return f"Here's {shown}, {title}."
            elif act.action == "close_other_tabs":
                count = tabs.close_other_tabs(self.desktop, act.app or "browser", current)
                return (f"Closed {count} other tab{'s' if count != 1 else ''}, {title}." if count else f"That was the only tab, {title}.")
            elif act.action == "reopen_tab":
                tabs.reopen_tab(self.desktop, act.app or "browser", current)
                return f"Brought it back, {title}."
            elif act.action == "close_browser":
                count = tabs.close_browser(self.desktop, act.app, current)
                return f"Closing {tabs.BROWSER_NAMES.get(act.app, act.app)}{'' if count == 1 else f' ({count} windows)'}, {title}."
            else:
                windows = self._app_windows(act.target)
                if not windows:
                    return f"I can't see {act.target} open, {title}."
                if any(is_private(w, self.config.get("vision_exclusions") or "") for w in windows):
                    return f"{act.target} is on your privacy list, {title}, so I'll leave it alone."
                for w in windows:
                    self.desktop.close(w)
                return f"Closing {act.target}, {title}."
        except ScreenError as exc:
            return f"I couldn't do that, {title}: {exc}"
        self.emit("act_done", label=act.label, x=None, y=None)
        return done + " Say \"reopen the tab\" if you need it back."

    def _safe_windows(self) -> list:
        try:
            return list(self.desktop.windows())
        except Exception:
            return []

    def _app_windows(self, name: str) -> list:
        """Open windows that belong to the app the user named ("Spotify", "Word", "file explorer")."""
        want = re.sub(r"[^a-z0-9]", "", name.lower())
        if len(want) < 2:
            return []
        found = []
        for w in self.desktop.windows():
            exe = re.sub(r"[^a-z0-9]", "", re.sub(r"\.exe$", "", (w.app or "").lower()))
            title = (w.title or "").lower()
            app_part = re.sub(r"[^a-z0-9]", "", title.rsplit(" - ", 1)[-1]) if " - " in title else ""
            if (exe and (want in exe or (len(exe) >= 4 and exe in want))) or (app_part and want == app_part) \
                    or re.sub(r"[^a-z0-9]", "", title) == want:
                found.append(w)
        return found

    def _run_act(self, act: ActRequest, window, target=None, act_id: str | None = None) -> str:
        title = self.title
        try:
            if act.action in ("click", "double", "right"):
                button = "right" if act.action == "right" else "left"
                self.desktop.click(target.x, target.y, button=button, double=act.action == "double", window=window)
                done = f"Done, {title}."
            elif act.action == "type":
                if target is not None:
                    self.desktop.click(target.x, target.y, window=window)
                    time.sleep(0.15)
                self.desktop.type_text(act.text, window=window)
                done = f"Typed it, {title}."
            elif act.action == "keys":
                self.desktop.press(act.keys or [], window=window)
                done = f"Done, {title}."
            elif act.action == "scroll":
                self.desktop.scroll(act.amount, window=window)
                done = f"Scrolled {'up' if act.amount > 0 else 'down'}, {title}."
            elif act.action == "close":
                self.desktop.close(window)
                done = f"Closing {window.label if window else 'it'}, {title}."
            else:
                return f"I don't know how to do that yet, {title}."
        except ScreenError as exc:
            if act_id:
                self.emit("act_status", id=act_id, status="failed")
            return f"I couldn't do that, {title}: {exc}"
        if act_id:
            self.emit("act_status", id=act_id, status="done")
        self.emit("act_done", label=act.label, x=getattr(target, "x", None), y=getattr(target, "y", None))
        return done

    def confirm_act(self, act_id: str, yes: bool) -> dict:
        """The CLICK / CANCEL buttons on the confirmation card."""
        pending = self._pending_act
        if pending is None or pending["id"] != act_id:
            return {"ok": False, "error": "That action has expired."}
        self._pending_act = None
        if not yes:
            self.emit("act_status", id=act_id, status="cancelled")
            return {"ok": True}
        message = self._run_act(pending["act"], pending["window"], pending.get("target"), act_id)
        self.emit("notice", level="info", text=message)
        return {"ok": True, "message": message}

    def look_now(self, question: str = "") -> bool:
        """The eye button in the HUD."""
        return self.submit_text(question or "What's on my screen?", source="text")

    def stop_watching(self) -> None:
        if self.watcher is not None:
            self.watcher.stop()
            self.watcher = None
        self.emit("vision_status", **self.vision_status())

    # ================================================================== instant skills (no model)
    def _quick(self, turn: Turn, text: str) -> str | None:
        q = parse_quick(text)
        if q is None:
            return None
        title = self.title
        handler = getattr(self, f"_quick_{q.kind}", None)
        return handler(turn, q, title) if handler else None

    def _quick_greeting(self, turn: Turn, q: Quick, title: str) -> str:
        if "greeting" in self.persona.lines:
            return self.persona.line("greeting", (), title)
        hour = datetime.now().hour
        part = "morning" if 5 <= hour < 12 else "afternoon" if 12 <= hour < 18 else "evening"
        return pick([f"Good {part}, {{title}}. How may I help?", "At your service, {title}.", "Hello, {title}. What can I do for you?"], title)

    def _quick_talk(self, turn: Turn, q: Quick, title: str) -> str:
        return self.persona.line(q.args.get("category") or "", q.args["replies"], title)

    def _quick_math(self, turn: Turn, q: Quick, title: str) -> str:
        answer = q.args["answer"]
        return pick([f"That comes to {answer}, {{title}}.", f"The answer is {answer}, {{title}}.", f"{answer}, {{title}}."], title)

    def _quick_coin(self, turn: Turn, q: Quick, title: str) -> str:
        return f"{pick(['Heads', 'Tails'], title)}, {title}."

    def _quick_dice(self, turn: Turn, q: Quick, title: str) -> str:
        sides = max(2, min(1000, int(q.args.get("sides", 6))))
        return f"You rolled a {random.randint(1, sides)}, {title}."

    def _quick_random(self, turn: Turn, q: Quick, title: str) -> str:
        a, b = sorted((int(q.args["a"]), int(q.args["b"])))
        return f"{random.randint(a, b)}, {title}."

    def _quick_status(self, turn: Turn, q: Quick, title: str) -> str:
        snap = self.monitor.snapshot()
        what = q.args["what"]
        battery = snap.get("battery")
        if what == "battery":
            if not battery:
                return f"This computer doesn't appear to have a battery, {title}."
            state = "and charging" if battery["plugged"] else "and running on battery"
            return f"The battery is at {battery['percent']} percent {state}, {title}."
        if what == "cpu":
            return f"The processor is at {round(snap['cpu'])} percent load, {title}."
        if what == "ram":
            return f"Memory is {round(snap['ram'])} percent in use: {snap['ram_used_gb']} of {snap['ram_total_gb']} gigabytes, {title}."
        if what == "disk":
            return (f"The system drive is {round(snap['disk'])} percent full, {title}." if snap.get("disk") is not None
                    else f"I couldn't read the disk usage, {title}.")
        if what == "uptime":
            return f"This computer has been running for {describe_duration(max(60, snap['uptime_s']))}, {title}."
        parts = [f"processor at {round(snap['cpu'])} percent", f"memory at {round(snap['ram'])} percent"]
        if snap.get("disk") is not None:
            parts.append(f"the system drive {round(snap['disk'])} percent full")
        if battery:
            parts.append(f"battery at {battery['percent']} percent")
        return f"All systems nominal, {title}: " + ", ".join(parts) + "."

    def _quick_volume(self, turn: Turn, q: Quick, title: str) -> str:
        try:
            if q.args.get("mute"):
                on = q.args.get("word") != "unmute"
                osctl.set_mute(on)
                return f"{'Muted' if on else 'Sound back on'}, {title}."
            if "set" in q.args:
                osctl.set_volume(q.args["set"])
                return f"Volume set to {q.args['set']} percent, {title}."
            delta = int(q.args["delta"])
            osctl.change_volume(delta)
            return f"Volume {'up' if delta > 0 else 'down'}, {title}."
        except osctl.OsControlError as exc:
            return f"I'm afraid {exc}"

    def _quick_screenshot(self, turn: Turn, q: Quick, title: str) -> str:
        try:
            path = osctl.take_screenshot()
        except osctl.OsControlError as exc:
            return f"I'm afraid {exc}"
        self.emit("notice", level="info", text=f"Screenshot saved: {path}")
        return f"Done, {title}. The screenshot is in your Screenshots folder."

    def _quick_desktop(self, turn: Turn, q: Quick, title: str) -> str:
        try:
            osctl.show_desktop()
        except osctl.OsControlError as exc:
            return f"I'm afraid {exc}"
        return f"Desktop cleared, {title}."

    def _quick_lock(self, turn: Turn, q: Quick, title: str) -> str:
        try:
            osctl.lock_screen()
        except osctl.OsControlError as exc:
            return f"I'm afraid {exc}"
        return f"Locking the computer, {title}."

    # -- timers and reminders
    def _quick_timer(self, turn: Turn, q: Quick, title: str) -> str:
        seconds = q.args.get("seconds")
        clock = q.args.get("clock")
        if clock and not seconds:
            seconds = _seconds_until(clock)
        if not seconds or seconds < 1:
            return f"I didn't catch how long, {title}. Try 'set a timer for five minutes'."
        if seconds > 86400:
            return f"I can only keep timers up to 24 hours, {title}."
        label = q.args.get("label") or ""
        tid = next(self._timer_ids)
        timer = threading.Timer(seconds, self._timer_fired, args=(tid,))
        timer.daemon = True
        self._timers[tid] = {"timer": timer, "due": time.time() + seconds, "label": label, "seconds": seconds}
        timer.start()
        self._emit_timers()
        span = describe_duration(seconds)
        if label:
            when = f"at {clock}" if clock and not q.args.get("seconds") else f"in {span}"
            return f"Very well, {title}. I'll remind you to {_you(label)} {when}."
        if q.args.get("reminder"):
            return f"Reminder set for {span} from now, {title}."
        return f"Timer set for {span}, {title}."

    def _quick_timer_cancel(self, turn: Turn, q: Quick, title: str) -> str:
        count = len(self._timers)
        for entry in self._timers.values():
            entry["timer"].cancel()
        self._timers.clear()
        self._emit_timers()
        if not count:
            return f"You have no timers running, {title}."
        return f"Cancelled {'your timer' if count == 1 else f'all {count} timers'}, {title}."

    def _quick_timer_status(self, turn: Turn, q: Quick, title: str) -> str:
        if not self._timers:
            return f"You have no timers running, {title}."
        now = time.time()
        parts = []
        for entry in sorted(self._timers.values(), key=lambda e: e["due"]):
            left = describe_duration(max(1, entry["due"] - now))
            parts.append(f"{left} left" + (f" to {entry['label']}" if entry["label"] else ""))
        return (f"You have {len(parts)} timer{'s' if len(parts) != 1 else ''}, {title}: " + "; ".join(parts) + ".") if len(parts) > 1 \
            else f"{parts[0].capitalize()}, {title}."

    def _emit_timers(self) -> None:
        now = time.time()
        self.emit("timers", timers=[{"id": tid, "label": e["label"], "left": max(0, int(e["due"] - now)), "total": e["seconds"]}
                                    for tid, e in sorted(self._timers.items(), key=lambda kv: kv[1]["due"])])

    def _timer_fired(self, tid: int) -> None:
        entry = self._timers.pop(tid, None)
        if entry is None:
            return
        self._emit_timers()
        label, title = entry["label"], self.title
        message = (f"A reminder, {title}: {_you(label)}." if label else
                   f"Time's up, {title}. Your timer for {describe_duration(entry['seconds'])} has finished.")
        self.emit("notice", level="info", text=("Reminder: " + _you(label)) if label else "Timer finished.")
        self.sfx.play("notify")
        with self._turn_lock:
            self._announcements += 1  # a running protocol pauses between steps to let this through
        try:
            deadline = time.monotonic() + 1800
            while time.monotonic() < deadline and not self._prewarm_stop.is_set():  # never cut off a conversation or a task
                with self._turn_lock:
                    if self._turn is None:
                        break
                time.sleep(0.2)
            if not self._prewarm_stop.is_set():
                self.speak(message, delay=0.4)
        finally:
            with self._turn_lock:
                self._announcements -= 1

    def _prewarm_phrases(self) -> list[str]:
        title = self.title
        phrases = [f"Good morning, {title}. How may I help?", f"Good afternoon, {title}. How may I help?", f"Good evening, {title}. How may I help?",
                   f"At your service, {title}.", f"Hello, {title}. What can I do for you?", f"Done, {title}.", f"Very well, {title}.",
                   f"Standing by, {title}.", f"Volume up, {title}.", f"Volume down, {title}.", f"Muted, {title}.", f"Sound back on, {title}.",
                   f"Desktop cleared, {title}.", f"Heads, {title}.", f"Tails, {title}.", f"Timer set for 5 minutes, {title}.",
                   f"Timer set for 10 minutes, {title}.", f"You have no timers running, {title}.", f"Shall I send it?",
                   f"I couldn't find that, {title}.", f"Opening Spotify, {title}.", f"Opening Chrome, {title}.", f"Opening Notepad, {title}."]
        persona = self.persona
        for category, _pattern, replies in _quick_small_talk():
            options = persona.lines.get(category) or replies
            phrases += [r.format(title=title) for r in options if len(r) < 160]
        phrases += [r.format(title=title) for r in persona.lines.get("greeting", ()) if len(r) < 160]
        return phrases

    def _prewarm_voice(self) -> None:
        """Synthesise the phrases JARVIS says most so they play instantly (the voice cache does the rest)."""
        time.sleep(12)  # let the greeting and the first request go first
        for phrase in self._prewarm_phrases():
            if self._prewarm_stop.is_set() or not self.config.get("voice_enabled", True):
                return
            while self._turn is not None and not self._prewarm_stop.is_set():
                time.sleep(1.0)  # never compete with a live request
            try:
                self.tts.synthesize(phrase)
            except TTSError:
                return  # offline: try again next launch
            except Exception:
                log.debug("prewarm failed", exc_info=True)
                return
            time.sleep(0.15)

    def _direct_command(self, turn: Turn, text: str) -> str | None:
        """Handle plain "open X" / "play X" requests without the model: instant, and reliable even with small models."""
        rest, which = split_screen_phrase(text)
        if which and (_OPEN_COMMAND.match(rest.strip()) or _PLAY_COMMAND.match(rest.strip())) and len(self._monitors()) > 1:
            before = {w.hwnd for w in self._safe_windows()}
            reply = self._direct_command(turn, rest)
            if reply:
                target = (_OPEN_COMMAND.match(rest.strip()) or _PLAY_COMMAND.match(rest.strip())).group("target")
                self._spawn(self._open_on_monitor, which, target, before, name="open-on-monitor")
                monitors = self._monitors()
                chosen = resolve_monitor(which, monitors)
                where = describe_monitor(chosen, monitors) if chosen else ""
                if where and f", {self.title}." in reply:
                    return reply.replace(f", {self.title}.", f" on {where}, {self.title}.", 1)
                return reply.rstrip(".") + (f" on {where}." if where else ".")
            return None
        play = _PLAY_COMMAND.match(text.strip())
        if play and self.tools.files_enabled:
            target = play.group("target").strip(" \"'")
            try:
                result = self.tools.open_target(target, min_score=70, apps_only=True)
                self._tool_used(turn, "open_file", {"query": target})
                return f"Launching {result['name']}. Enjoy, {self.title}."
            except ToolError:
                media = parse_media(text)  # no game called that: music ("play some jazz")
                return self._media(turn, media) if media is not None and media.action == "play" else None
        new = parse_new_file(text) if self.tools.web_enabled else None
        if new is not None:
            return self._new_file(turn, new)
        google = parse_google_request(text, have_last=self._last_doc is not None) if self.tools.web_enabled else None
        ready = self.tools.google.configured
        if google and (google.action != "open" or google.google or not google.name or google.recent):
            reply = self._google_request(turn, google, ready)  # "open the doc", "show me my budget sheet"...
            if reply:
                return reply
        search = _GOOGLE_SEARCH.match(text.strip())
        if search and self.tools.web_enabled:
            query = (search.group("q") or search.group("q2") or search.group("q3")).strip(" ?.!\"'")
            return self._open_site(turn, "https://www.google.com/search?q=" + quote_plus(query), f"a Google search for {query}",
                                   host="google.com", title=query, done=f"Here are Google's results for {query}")
        match = _OPEN_COMMAND.match(text.strip())
        if not match:
            return None
        target = match.group("target").strip(" \"'")
        which = _BROWSER_NAMES.get(re.sub(r"^(?:the|my)\s+", "", target.lower()).strip())
        if which is not None:
            return self._open_browser(turn, which)
        if re.fullmatch(r"(?:my\s+)?(?:favou?rite|usual)\s+(?:web\s*)?site", target, re.I):
            fav = str(self.config.get("favorite_website") or "")
            if not fav:
                return (f"You haven't told me your favourite website yet, {self.title}. "
                        "Say “my favourite website is …” and I'll remember it.")
            target = fav
        site = self.tools.website_for(target) if self.tools.web_enabled else None
        explicit_url = bool(site) and "." in target
        if self.tools.files_enabled and not explicit_url:
            try:
                result = self.tools.open_target(target, min_score=70)
                self._tool_used(turn, "open_file", {"query": target})
                return f"Opening {result['name']}, {self.title}."
            except ToolError:
                pass
        if google and google.name and not site:  # no such file on this PC: maybe it's in Google Drive
            reply = self._google_request(turn, google, ready)
            if reply:
                return reply
        if site:
            url = site if site.startswith("http") else "https://" + site
            return self._open_site(turn, url, site_name(url) if "." not in target or "://" in target else target)
        return None

    def _weather_answer(self, turn: Turn, req) -> str | None:
        if not self.tools.web_enabled:
            return f"Internet access is switched off in Settings, {self.title}, so I can't check the weather."
        self._emit_turn(turn, "tool_activity", tool="weather", label=f"Checking the weather{' in ' + req.place if req.place else ''}")
        try:
            reply = self.weather.answer(req, self.title)
        except WeatherError as exc:
            if exc.offline and self.llm.online and self.llm.model:
                log.info("Weather service unavailable (%s); falling back to a web search", exc)
                return None
            return f"I'm afraid {exc}"
        except Exception:
            log.exception("Weather lookup failed")
            return f"I couldn't get the weather just now, {self.title}. Please try again in a moment."
        self.llm.remember(f"weather ({req.kind}, {req.place or 'here'})", reply)
        return reply

    def _pc_temperature(self) -> str:
        temp = osctl.cpu_temperature()
        load = round(self.monitor.snapshot().get("cpu") or 0)
        if temp is None:
            return (f"Your PC doesn't report its processor temperature to me, {self.title}; many Windows PCs only share it with "
                    f"admin rights or a monitor like LibreHardwareMonitor running. The processor is at {load} percent load right now.")
        verdict = "which is running hot" if temp >= 85 else "which is warm but fine" if temp >= 70 else "which is perfectly healthy"
        return f"Your processor is at {temp:g} degrees Celsius, {verdict}, {self.title}. It's at {load} percent load."

    def _user_language(self, text: str) -> Detection | None:
        """The language of the user's message when it's clearly not the one JARVIS was set up in."""
        if not self.config.get("auto_language", True):
            return None
        found = detect(text)
        if found.confidence >= 0.9 or (found.confidence >= 0.5 and len(text.split()) >= 2):
            return found
        return None

    def _converse(self, turn: Turn, text: str, source: str) -> None:
        turn.text = text
        lang = self._user_language(text)
        home = base_language(self.config.get("stt_language") or "en-US")
        foreign = lang if lang and lang.code != home else None
        extra = {"lang": foreign.code, "lang_name": foreign.name} if foreign else {}
        self._emit_turn(turn, "user_message", id=f"u{next(self._ids)}", text=text, source=source, **extra)
        if not self._set_state(State.THINKING, turn, "thinking"):
            return
        text = _strip_wake(text).strip(" ,") or text  # "Harper, close the tab" -> "close the tab"
        self.sfx.play("process")

        pending, self._pending_act = self._pending_act, None
        if pending is not None and time.time() - pending["at"] < 120:
            if is_confirmation(text):
                self._deliver(turn, [self._run_act(pending["act"], pending["window"], pending.get("target"), pending["id"])])
                return
            if is_cancellation(text):
                self.emit("act_status", id=pending["id"], status="cancelled")
                self._deliver(turn, [f"Very well, {self.title}. I'll leave it."])
                return

        awaiting, self._awaiting = self._awaiting, None
        if awaiting is not None and awaiting.id in self._drafts:
            if is_confirmation(text):
                self._deliver(turn, [self._send_draft(awaiting)])
                return
            if is_cancellation(text):
                self.discard_email(awaiting.id)
                self._deliver(turn, [f"Very well, {self.title}. I've discarded that email."])
                return

        if _PAUSE_LISTENING.match(_bare(text)):
            turn.learn = False
            self._deliver(turn, [self.pause_listening()])
            return
        if _RESUME_LISTENING.match(_bare(text)) and (self._paused or (source == "text" and not re.match(r"^\W*unmute\b", _bare(text), re.I))):
            turn.learn = False
            self._deliver(turn, [self.resume_listening()])
            return

        if source != "protocol":
            recording = self._recording
            if recording is not None and time.time() - recording["at"] < 600:
                turn.learn = False
                self._deliver(turn, [self._record_step(recording, text)], listen_after=lambda: self._recording is not None)
                return
            self._recording = None
        if source in ("voice", "text"):
            self._activity("command", text, "ok")
        confirm = self._protocol_confirm
        if confirm is not None and source != "protocol":
            bare = _bare(text)
            if is_confirmation(bare) or re.match(r"^(?:go\s+ahead|do\s+it|proceed|carry\s+on|continue)\W*$", bare, re.I):
                confirm["answer"] = True
                confirm["event"].set()
                self._deliver(turn, [f"Going ahead, {self.title}."])
                return
            if is_cancellation(bare) or re.match(r"^(?:no|skip(?:\s+it|\s+that)?|don'?t)\W*$", bare, re.I):
                confirm["answer"] = False
                confirm["event"].set()
                self._deliver(turn, [f"Skipping that step, {self.title}."])
                return
        if self._proposal is not None and source != "protocol":
            answer = self._proposal_reply(text)
            if answer:
                self._deliver(turn, [answer], listen_after=lambda: self._proposal is not None)
                return
        if self._pending_media is not None:
            choice = self._media_choice(text)
            if choice:
                self._deliver(turn, [choice])
                return
        if _WHAT_PAGE.match(text.strip()):
            look = self.browser.look()
            if look.state == "NOT_RUNNING":
                self._deliver(turn, [f"Your browser isn't open, {self.title}."])
            else:
                where = f" ({look.url})" if look.url else ""
                self._deliver(turn, [f"You're on “{tab_title_of(look.title)}”{where}, {self.title}."])
            return
        fav = _FAVORITE_SITE.match(_bare(text))
        if fav:
            site = self.tools.website_for(fav.group("site").strip(" .")) or fav.group("site").strip(" .")
            self.config.update({"favorite_website": site})
            self._deliver(turn, [f"Noted, {self.title}. Say “open my favourite website” any time."])
            return
        phrase = self.protocols.find_phrase(_bare(text)) if source != "protocol" else None
        if phrase is not None:
            protocol_cmd = ProtocolCommand("run", name=phrase.name)
        else:
            protocol_cmd = parse_protocol_command(_bare(text), find=self.protocols.find)
        if protocol_cmd is not None:
            turn.learn = False
            self._pending_skip = []
            reply, start = self._protocol_command(protocol_cmd, source)
            self._deliver(turn, [reply], listen_after=lambda: self._recording is not None or self._proposal is not None)
            if start is not None and not turn.cancel.is_set():
                self._start_protocol(start, trigger="voice" if source == "voice" else "text", skip=self._pending_skip)
            return

        asked_forget, self._pending_forget = self._pending_forget, 0.0
        if asked_forget and time.time() - asked_forget < 120:
            turn.learn = False
            if is_confirmation(text):
                self.memory.clear()
                self.emit("memory_cleared")
                self._deliver(turn, [f"Done, {self.title}. I've forgotten everything I knew about you. We're starting fresh."])
                return
            if is_cancellation(text):
                self._deliver(turn, [f"Phew. Your memories are safe, {self.title}."])
                return

        switch = parse_switch(text)
        if switch:
            turn.learn = False
            self._deliver(turn, [self._switch_persona(switch)], voice=self.config.get("voice") or None)
            return
        if asks_who(text):
            turn.learn = False
            self._deliver(turn, [self._who_line(text)])
            return
        command = parse_memory_command(text)
        if command:
            turn.learn = False
            try:
                answer = self._memory_command(command)
            except Exception:
                log.exception("Memory command failed")
                answer = f"I'm afraid my memory hiccuped there, {self.title}. The details are in the log."
            if answer:
                self._deliver(turn, [answer], listen_after=lambda: bool(self._pending_forget))
                return

        doc_cmd = parse_doc_command(text)
        if doc_cmd is not None and self._doc_command_applies(doc_cmd, text):
            self._deliver(turn, self._doc_command(turn, text, doc_cmd, lang), language=lang.code if lang else None, working=True)
            return

        early = parse_quick(text)
        if early is not None and early.kind in ("desktop", "status", "lock", "screenshot", "timer", "timer_cancel", "timer_status"):
            reply = self._quick(turn, text)  # "show me the desktop" / "run a diagnostic" aren't apps to open
            if reply:
                self._deliver(turn, [reply])
                return
        media = parse_media(text)
        if media is not None and media.action != "play":
            self._deliver(turn, [self._media(turn, media)])
            return
        try:
            direct = self._direct_chain(turn, text) or self._direct_command(turn, text)
        except Exception:
            log.exception("Direct command failed")
            direct = None
        if direct:
            self._deliver(turn, [direct])
            return
        if media is not None:  # "play Bohemian Rhapsody" (no game or app by that name)
            self._deliver(turn, [self._media(turn, media)], working=True)
            return
        clock = self._clock_answer(text)
        if clock:
            self._deliver(turn, [clock])
            return
        forecast = parse_weather(text)
        if forecast is not None or asks_pc_temperature(text):
            answer = self._weather_answer(turn, forecast) if forecast else self._pc_temperature()
            if answer:  # None: the weather service is unreachable, so try a web search with the model instead
                self._deliver(turn, [answer], language=lang.code if lang else None)
                return
        lang_code = lang.code if lang else None
        try:
            seeing = self._vision_request(turn, text, lang)
        except Exception:
            log.exception("Vision request failed")
            seeing = [f"I'm afraid something went wrong with my vision, {self.title}. The details are in the log."]
        if seeing is not None:
            self._deliver(turn, seeing, language=lang_code, working=True,
                          listen_after=lambda: self._pending_act is not None)
            return
        try:
            quick = self._quick(turn, text)  # small talk, sums, volume, timers...: no language model needed
        except Exception:
            log.exception("Quick skill failed")
            quick = None
        if quick:
            self._deliver(turn, [quick])
            return

        if not (self.llm.online and self.llm.model):
            self.check_ollama()  # maybe the user just started it
        if not (self.llm.online and self.llm.model):
            reason = "Ollama is not running" if not self.llm.online else "no language model is installed"
            self._deliver(turn, [f"I'm afraid my neural core is offline, {self.title}: {reason}. "
                                 "The setup steps are on screen."])
            return
        google_ready = self.tools.web_enabled and self.tools.google.configured
        mail = parse_email_request(text) if self.tools.web_enabled else None
        if mail:
            self._deliver(turn, self._email(turn, text, mail, lang), language=lang.code if lang else None,
                          working=True, listen_after=True)
            return
        change = parse_edit_request(text) if google_ready else None
        target = self._edit_target(change) if change else None
        if change and target:
            self._deliver(turn, self._edit(turn, text, change, target, lang), language=lang.code if lang else None,
                          working=True)
            return
        writing = parse_write_request(text, google_ready=google_ready)
        if writing:
            self._deliver(turn, self._write(turn, text, writing, lang), language=lang.code if lang else None,
                          working=True)
            return
        toolbox = self.tools if (self.tools.files_enabled or self.tools.web_enabled) else None
        prefetch = []
        if self.tools.web_enabled:
            search = _SEARCH_COMMAND.match(text.strip())
            query = search.group("query") if search else (text if _LIVE_INFO.search(text) else None)
            if query:
                self._tool_used(turn, "web_search", {"query": query})
                prefetch.append(("web_search", {"query": query}, self.tools.run("web_search", {"query": query})))
        self._deliver(turn, self.llm.stream_reply(
            text, turn.cancel, toolbox=toolbox, on_tool=lambda name, args: self._tool_used(turn, name, args),
            offer_tools=bool(_TOOL_CUES.search(text)) and not _CLOSING.match(text), prefetch=prefetch,
            language=lang.name if lang else None, memory=self._memory_context(text)), language=lang.code if lang else None)

    def _deliver(self, turn: Turn, tokens: Iterable[str], voice: str | None = None, language: str | None = None,
                 working: bool = False, listen_after=False) -> None:
        mid = f"a{next(self._ids)}"
        speaker = None
        if self.config.get("voice_enabled", True) and self.audio.available:
            speaker = Speaker(self, turn, voice, language, working=working)
        splitter = SentenceSplitter()
        error: str | None = None
        self._emit_turn(turn, "assistant_start", id=mid)
        reply: list[str] = []
        try:
            for token in tokens:
                if turn.cancel.is_set():
                    break
                reply.append(token)
                self._emit_turn(turn, "assistant_token", id=mid, text=token)
                if speaker:
                    for sentence in splitter.feed(token):
                        speaker.say(sentence)
            if speaker and not turn.cancel.is_set():
                for sentence in splitter.flush():
                    speaker.say(sentence)
        except (LLMConnectionError, LLMModelError) as exc:
            error = str(exc)
            self._spawn(self.check_ollama, name="recheck")
        except LLMError as exc:
            error = str(exc)
        except Exception:
            log.exception("Reply generation failed")
            error = "Something went wrong while generating the reply. See the log for details."

        turn.reply = "".join(reply).strip()
        self.emit("assistant_end", id=mid, interrupted=turn.cancel.is_set(), error=error, stats=self.core_stats())
        said = getattr(turn, "text", None)
        if said and not error:
            try:
                self.memory.log_exchange(said, "".join(reply).strip(), self.persona.id, learn=getattr(turn, "learn", True))
            except Exception:
                log.exception("Couldn't log the exchange to memory")
        if error and self._is_current(turn):
            self.sfx.play("error")
            self.emit("system_message", level="error", text=error)
            if speaker:
                speaker.say(f"I'm afraid something went wrong, {self.title}. The details are on screen.")
        if speaker:
            speaker.close()
            speaker.wait()
        if self._finish_turn(turn) and not turn.cancel.is_set():
            if callable(listen_after):
                listen_after = listen_after()  # decided after the reply ("shall I click it?" -> listen for yes)
            if turn.kind == "listen" and not error and not self._paused and (self.config.get("auto_listen") or listen_after):
                self.start_listening()

    # ================================================================== long-form writing
    def _write(self, turn: Turn, text: str, req: WriteRequest, lang: Detection | None) -> Iterator[str]:
        """Research, write and file a document or deck, narrating briefly; yields the spoken reply."""
        language = req.language or (lang.name if lang else None)
        lang_code = next((code for code, (name, _, _) in LANGUAGES.items() if name == language), "en")
        place = {"doc": "a Google Doc", "slides": "Google Slides", "chat": ""}[req.target]
        if req.target == "chat":
            if not self.tools.google.configured and req.kind not in SHORT_FORM and self.tools.web_enabled:
                self._emit_turn(turn, "system_message", level="info",
                                text="Tip: link Google in Settings ▸ Google and I'll put pieces like this straight into a Google Doc.")
        else:
            yield f"Right away, {self.title}. I'll write that up in {place}. "
        notes = ""
        if self.tools.web_enabled and req.kind not in ("poem", "story", "short story"):
            self._tool_used(turn, "web_search", {"query": req.topic})
            try:
                notes = self.tools.research(req.topic, language=lang_code)
            except Exception:
                log.exception("Research failed")
        if turn.cancel.is_set():
            return
        label = f"Writing {req.kind}: {req.topic}" if req.kind != "document" else f"Writing about {req.topic}"
        self._emit_turn(turn, "tool_activity", tool="compose", label=label)

        def progress(partial: str) -> None:
            words = len(partial.split())
            self._emit_turn(turn, "activity", label=f"{label} · {words} words" if req.target != "slides"
                            else f"{label} · slide {max(1, partial.count(chr(10) + chr(10)))}")

        maker = slides_prompt if req.target == "slides" else document_prompt
        system, prompt = maker(req, text, notes, language)
        if req.target == "chat":
            system += " Keep it under %d words." % req.words
        draft = self.llm.compose(system, prompt, turn.cancel, on_progress=progress,
                                 max_tokens=int(req.words * 2.2) + 400 if req.target != "slides" else 1800)
        if turn.cancel.is_set():
            return
        if req.target == "chat":
            _title, body = clean_document(draft)
            self.llm.remember(text, body)
            yield body
            return

        try:
            if req.target == "slides":
                title, subtitle, slides = parse_deck(draft)
                if not slides:
                    raise BridgeError("the model didn't produce any slides; please try again")
                title = req.title or title or req.topic.title()
                self._emit_turn(turn, "activity", label=f"Creating Google Slides: {title}")
                result = self.tools.google.create_deck(title, subtitle, slides)
                summary = f"a {len(slides) + 1}-slide presentation on {req.topic}"
                kind = "slides"
            else:
                title, body = clean_document(draft)
                if req.title:
                    title = req.title
                    body = re.sub(r"^#\s+.*", "# " + title, body, count=1) if body.startswith("#") else f"# {title}\n{body}"
                title = title or f"{req.kind.capitalize()} of {req.topic}"
                self._emit_turn(turn, "activity", label=f"Creating Google Doc: {title}")
                result = self.tools.google.doc("create", title=title, text=body)
                words = len(re.sub(r"[#*|>-]", " ", body).split())
                summary = f"a {words}-word {req.kind} of {req.topic}" if req.kind == "biography" else f"a {words}-word {req.kind} on {req.topic}"
                kind = "doc"
        except BridgeError as exc:
            self._emit_turn(turn, "system_message", level="error", text=f"Google: {exc}")
            body = draft if req.target == "slides" else clean_document(draft)[1]
            self.llm.remember(text, body)
            yield f"I couldn't save it to Google, {self.title}, so here it is instead.\n\n{body}"
            return
        url = result.get("url") or ""
        self._last_doc = {"kind": kind, "id": result.get("id") or "", "title": result.get("title") or title, "url": url}
        self._emit_turn(turn, "document", doc_kind=kind, id=result.get("id"), title=result.get("title") or title, url=url)
        if url:
            self.tools.open_link(url)
        self.llm.remember(text, f'I wrote {summary} in the Google {"Slides presentation" if kind == "slides" else "Doc"} '
                                f'"{result.get("title") or title}" ({url}).')
        yield f"Done. I've written {summary} and opened it for you."

    # ================================================================== working on a document step by step
    def _docs_windows(self) -> list:
        """Browser windows showing a Google Doc, the one the user was just using first."""
        current = self.tracker.current()
        found = [w for w in self._safe_windows() if tabs.is_browser(w) and docs_tab_title(w.title)]
        if current is not None and docs_tab_title(current.title or ""):
            found.sort(key=lambda w: w.hwnd != current.hwnd)
        return found

    def _doc_command_applies(self, cmd: DocCommand, text: str = "") -> bool:
        if len(cmd.steps) == 1 and cmd.steps[0].action == "write" and self.tools.google.configured:
            change = parse_edit_request(text)
            if change is not None and self._edit_target(change) is not None:
                return False  # "add a section about X to it": the dedicated section writer does that best
        if cmd.needs_document:
            return True
        # "type out a cover letter" with no document mentioned: into the Google Doc on screen, or whatever app is in front
        if any(s.verb == "type" or s.action == "type" for s in cmd.steps):
            return True
        current = self.tracker.current()
        return bool(current is not None and docs_tab_title(current.title or ""))

    def _resolve_doc(self, cmd: DocCommand) -> dict | None:
        """Which document: one the user named, the Google Doc open on screen, or the one JARVIS used last."""
        google = self.tools.google
        windows = self._docs_windows()
        if cmd.name:
            window = next((w for w in windows if cmd.name.lower() in (docs_tab_title(w.title) or "").lower()), None)
            file = google.find_file(cmd.kind, cmd.name) if google.configured else None
            if file:
                return {"kind": file.get("kind") or cmd.kind, "id": file.get("id") or "", "title": file.get("title") or cmd.name,
                        "url": file.get("url") or "", "window": window}
            if window is not None:
                return {"kind": "doc", "id": "", "title": docs_tab_title(window.title), "url": "", "window": window}
            raise BridgeError(f"I couldn't find a document called {cmd.name}")
        last = self._last_doc
        if windows:
            window = windows[0]
            title = docs_tab_title(window.title)
            if last and last.get("kind") == "doc" and last.get("title", "").lower() == title.lower():
                return {**last, "window": window}
            if google.configured:
                try:
                    file = google.find_file("doc", title)
                except BridgeError:
                    file = None
                if file and (file.get("title") or "").lower() == title.lower():
                    return {"kind": "doc", "id": file.get("id") or "", "title": file.get("title"), "url": file.get("url") or "", "window": window}
            return {"kind": "doc", "id": "", "title": title, "url": "", "window": window}
        if last and last.get("kind") == "doc":
            return {**last, "window": None}
        return None

    def _wait_for_docs_window(self, timeout: float = 20.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            windows = self._docs_windows()
            if windows:
                time.sleep(1.5)  # let the editor finish loading before typing into it
                return self._docs_windows()[0]
            time.sleep(0.4)
        return None

    def _doc_command(self, turn: Turn, text: str, cmd: DocCommand, lang: Detection | None) -> Iterator[str]:
        """Run "rename it to X, then type out Y" one step at a time, saying what happened."""
        title, google = self.title, self.tools.google
        bridge = google.configured
        done: list[str] = []
        current = self.tracker.current()
        needs_doc = cmd.needs_document or bool(current is not None and docs_tab_title(current.title or ""))
        typing_only = not needs_doc  # "type out a poem" into whatever app is in front
        target = None
        try:
            if needs_doc:
                create = next((s for s in cmd.steps if s.action == "create"), None)
                target = None if create else self._resolve_doc(cmd)
                if target is None:
                    # nothing to work on yet: start a new doc (named after the rename, if there is one)
                    name = (create.text if create else "") or next((s.text for s in cmd.steps if s.action == "rename"), "")
                    if bridge:
                        self._emit_turn(turn, "tool_activity", tool="google_doc", label="Creating a new Google Doc")
                        made = google.doc("create", title=name or "Untitled document", text="")
                        target = {"kind": "doc", "id": made.get("id") or "", "title": made.get("title") or name, "url": made.get("url") or "", "window": None}
                        done.append(f"created {target['title']}")
                    else:
                        if not self.config.get("allow_control", True):
                            yield (f"I'd need either the Google link (Settings, Google) or screen control to make a document, {title}.")
                            return
                        self.tools.open_link(self._NEW_URLS["doc"])
                        self._emit_turn(turn, "tool_activity", tool="google_doc", label="Opening a new Google Doc")
                        window = self._wait_for_docs_window()
                        if window is None:
                            yield (f"I opened a new Google Doc, {title}, but it didn't appear in time (is your browser signed in to Google?). "
                                   "Link Google in Settings and I can do all of this directly.")
                            return
                        target = {"kind": "doc", "id": "", "title": docs_tab_title(window.title), "url": "", "window": window}
                        done.append("opened a new document")
                    if name and create is None:
                        cmd.steps = [s for s in cmd.steps if not (s.action == "rename" and s.text == name and target.get("id"))]
            for step in cmd.steps:
                if turn.cancel.is_set():
                    return
                if step.action in ("create",):
                    continue
                if step.action == "open":
                    if target and target.get("window") is not None:
                        self.desktop.bring_to_front(target["window"])
                    elif target and target.get("url"):
                        self.tools.open_link(target["url"])
                    continue
                if step.action == "rename":
                    yield self._doc_rename(turn, target, step.text)
                    done.append(f"renamed it to {step.text}")
                    continue
                if step.action == "clear":
                    self._doc_clear(turn, target)
                    done.append("cleared it")
                    continue
                # write (generated) or type (exact words)
                if step.action == "type":
                    body_md, body_plain, about = step.text, step.text, "that"
                else:
                    if not (self.llm.online and self.llm.model):
                        self.check_ollama(emit_event=False)
                    if not (self.llm.online and self.llm.model):
                        yield f"My neural core is offline, {title}, so I can't write {step.text} right now."
                        return
                    about = step.text
                    current = ""
                    if target and target.get("id") and bridge:
                        try:
                            current = google.doc("read", document=target["id"]).get("text", "")
                        except BridgeError:
                            current = ""
                    plain = typing_only or not (bridge and target and target.get("id"))
                    self._emit_turn(turn, "tool_activity", tool="compose", label=f"Writing {about}")
                    system, prompt = writing_prompt(about, current, lang.name if lang else None, plain=plain)
                    draft = self.llm.compose(system, prompt, turn.cancel, max_tokens=1400,
                                             on_progress=lambda p: self._emit_turn(turn, "activity", label=f"Writing · {len(p.split())} words"))
                    if turn.cancel.is_set():
                        return
                    _t, body_md = clean_document(draft) if not plain else ("", draft.strip())
                    body_md = body_md or draft.strip()
                    body_plain = to_plain(draft)
                    if len(body_plain.split()) < 3:
                        yield f"I couldn't come up with {about} just now, {title}. Please try again."
                        return
                if typing_only:
                    yield self._type_into_app(turn, body_plain, about)
                    return
                self._doc_write(turn, target, body_md, body_plain)
                done.append(f"wrote {about} in it" if step.action == "write" else "typed it in")
        except BridgeError as exc:
            self._emit_turn(turn, "system_message", level="error", text=f"Google: {exc}")
            yield f"I'm afraid I couldn't finish that, {title}: {exc}."
            return
        except ScreenError as exc:
            yield f"I'm afraid I couldn't finish that, {title}: {exc}"
            return
        if target is None:
            return
        if target.get("id"):
            self._last_doc = {k: target.get(k) or "" for k in ("kind", "id", "title", "url")}
            self._emit_turn(turn, "document", doc_kind=target["kind"], id=target["id"], title=target["title"], url=target.get("url") or "")
            if target.get("window") is None and target.get("url"):
                self.tools.open_link(target["url"])  # show it
        summary = ", then ".join(d for d in done if not d.startswith("renamed"))
        self.llm.remember(text, f"I {', then '.join(done) or 'worked on the document'} ({target.get('title')}).")
        if summary:
            yield f"Done, {title}. I {summary}" + (f" ({target.get('title')})." if target.get("title") else ".")

    def _doc_rename(self, turn: Turn, target: dict, name: str) -> str:
        google = self.tools.google
        self._emit_turn(turn, "tool_activity", tool="google_doc", label=f"Renaming to {name}")
        if target.get("id") and google.configured:
            try:
                result = google.rename(target["kind"], target["id"], name)
                target["title"] = result.get("title") or name
                target["url"] = result.get("url") or target.get("url") or ""
                return f"Renamed it to {target['title']}. "
            except BridgeError as exc:
                if target.get("window") is None or "out of date" not in str(exc):
                    raise
                log.info("Bridge can't rename yet (%s); renaming in the browser instead", exc)
        if target.get("window") is None:
            raise BridgeError("I can only rename a document through the Google link, or when it's open in your browser")
        if not self.config.get("allow_control", True):
            raise BridgeError("screen control is switched off in Settings, so I can't rename it in the browser")
        docs_keys.rename(self.desktop, target["window"], name)
        target["title"] = name
        return f"Renamed it to {name}. "

    def _doc_write(self, turn: Turn, target: dict, body_md: str, body_plain: str) -> None:
        google = self.tools.google
        if target.get("id") and google.configured:
            result = google.doc("append", document=target["id"], text=body_md)
            target["url"] = result.get("url") or target.get("url") or ""
            return
        if target.get("window") is None:
            raise BridgeError("I can only write in a document through the Google link, or when it's open in your browser")
        if not self.config.get("allow_control", True):
            raise BridgeError("screen control is switched off in Settings, so I can't type into it")
        self._emit_turn(turn, "tool_activity", tool="vision", label=f"Typing into {target.get('title') or 'the document'}")
        docs_keys.type_at_end(self.desktop, target["window"], body_plain)

    def _doc_clear(self, turn: Turn, target: dict) -> None:
        google = self.tools.google
        if target.get("id") and google.configured:
            google.call("doc_rewrite", document=target["id"], text=" ")
            return
        if target.get("window") is None:
            raise BridgeError("I can only clear a document through the Google link, or when it's open in your browser")
        docs_keys.clear(self.desktop, target["window"])

    def _type_into_app(self, turn: Turn, text: str, about: str) -> str:
        """Type generated text into whatever app the user is working in (Notepad, Word, an email...)."""
        title = self.title
        if not self.config.get("allow_control", True):
            return f"Screen control is switched off in Settings, {title}, so I can't type for you."
        window = self.tracker.current()
        if window is None:
            return f"Click where you'd like me to type it, {title}, then ask me again."
        if is_private(window, self.config.get("vision_exclusions") or ""):
            return f"{window.label} is on your privacy list, {title}, so I won't type into it."
        self._emit_turn(turn, "tool_activity", tool="vision", label=f"Typing into {window.label}")
        try:
            self.desktop.type_text(text, window=window)
        except ScreenError as exc:
            return f"I couldn't type into {window.label}, {title}: {exc}"
        self.llm.remember(f"type out {about}", f"[Typed {about} into {window.label}]")
        return f"Done, {title}. I've typed {about} into {window.label}."

    # ================================================================== existing Google files
    _KIND_NAMES = {"doc": "Google Doc", "slides": "Google Slides presentation", "sheet": "Google Sheet"}

    def _google_request(self, turn: Turn, req: GoogleRequest, ready: bool) -> str | None:
        """Open, show or list the user's Google files. Returns what to say, or None to let other handlers try."""
        google = self.tools.google
        if not ready:
            if req.action == "open" and (req.google or not req.name):
                site = {"doc": "google docs", "slides": "google slides", "sheet": "google sheets"}.get(req.kind or "doc")
                self.tools.open_website(self.tools.website_for(site) or "")
                self._tool_used(turn, "open_website", {"url": site})
                return (f"Opening {site.title()}, {self.title}. Link Google in Settings and I can open your files by name "
                        "and show them right here.")
            return None
        try:
            if req.action == "list":
                self._tool_used(turn, "google_doc", {"action": "list"})
                files = google.recent_files(6)
                if not files:
                    return f"I couldn't find any Google files in your Drive, {self.title}."
                self._emit_turn(turn, "file_list", files=files)
                return f"Here are your {len(files)} most recent files, {self.title}."
            last = self._last_doc
            file = None
            if req.recent and not req.name and last and req.kind in (None, last["kind"]):
                file = dict(last)
            else:
                kind = req.kind or (last["kind"] if last else "doc")
                label = f"Looking for {req.name}" if req.name else "Finding your latest " + kind
                self._emit_turn(turn, "tool_activity", tool="google_doc", label=label)
                file = google.find_file(kind, req.name)
                if file is None and req.name:  # "doc" said, but it's a deck (or the other way round)
                    file = next((f for k in ("doc", "slides", "sheet") if k != kind
                                 for f in [google.find_file(k, req.name)] if f), None)
            if file is None:
                what = {"doc": "document", "slides": "presentation", "sheet": "spreadsheet"}.get(req.kind or "doc")
                return (f"I couldn't find a Google {what} called {req.name}, {self.title}." if req.name
                        else f"You don't seem to have any Google {what}s yet, {self.title}.")
            return self._show_file(turn, file, req)
        except BridgeError as exc:
            self._emit_turn(turn, "system_message", level="error", text=f"Google: {exc}")
            return f"I'm afraid I couldn't reach your Google files, {self.title}: {exc}."

    _NEW_URLS = {"doc": "https://docs.google.com/document/create", "slides": "https://docs.google.com/presentation/create",
                 "sheet": "https://docs.google.com/spreadsheets/create"}  # Google's own "new blank file" links (like docs.new)

    def _new_file(self, turn: Turn, req: NewFileRequest) -> str:
        """A new, blank Google Doc / Slides / Sheet, opened straight away (and remembered as "it")."""
        what = {"doc": "document", "slides": "presentation", "sheet": "spreadsheet"}[req.kind]
        google = self.tools.google
        if not google.configured:
            # Google's own shortcut makes a blank file in whichever account the browser is signed in to
            self.tools.open_link(self._NEW_URLS[req.kind])
            self._tool_used(turn, "open_website", {"url": self._NEW_URLS[req.kind]})
            named = f" You can name it {req.title} at the top left." if req.title else ""
            return (f"Opening a new Google {what}, {self.title}.{named} Link Google in Settings and I'll be able to "
                    "write in it and open it again by name.")
        title = req.title or {"doc": "Untitled document", "slides": "Untitled presentation", "sheet": "Untitled spreadsheet"}[req.kind]
        self._emit_turn(turn, "tool_activity", tool="google_doc", label=f"Creating a new Google {what}")
        try:
            if req.kind == "doc":
                result = google.doc("create", title=title, text="")
            elif req.kind == "slides":
                result = google.create_deck(title, "", [])
            else:
                result = google.sheets("create", title=title, text="")
        except BridgeError as exc:
            self._emit_turn(turn, "system_message", level="error", text=f"Google: {exc}")
            return f"I couldn't create the {what}, {self.title}: {exc}."
        url = result.get("url") or ""
        name = result.get("title") or title
        self._last_doc = {"kind": req.kind, "id": result.get("id") or "", "title": name, "url": url}
        self._emit_turn(turn, "document", doc_kind=req.kind, id=result.get("id"), title=name, url=url)
        if url:
            self.tools.open_link(url)
        self.llm.remember(f"create a new {what}" + (f" called {req.title}" if req.title else ""),
                          f'I created a new blank Google {what} "{name}" ({url}) and opened it.')
        follow = {"doc": "Tell me what to write in it, like \"add a paragraph about our trip\".",
                  "slides": "Tell me what to add, like \"add a slide about our goals\".",
                  "sheet": "Tell me what to put in it, like \"add rent 1200 and food 300\"."}[req.kind]
        return f"Done, {self.title}. I've created {name} and opened it. {follow}"

    def _show_file(self, turn: Turn, file: dict, req: GoogleRequest) -> str:
        title = file.get("title") or "that file"
        self._last_doc = {"kind": file["kind"], "id": file.get("id") or "", "title": title, "url": file.get("url") or ""}
        if req.action == "view":
            data = self.tools.google.view(file["kind"], file.get("id") or title)
            self._emit_turn(turn, "document_view", **_view_event(data, file.get("id")))
            if req.aloud and data.get("markdown"):
                plain = re.sub(r"^#+\s*|^[-*]\s+|\*\*", "", data["markdown"], flags=re.M)
                return f"Here is {title}. " + plain[:1400].strip()
            return f"Here is {title}, {self.title}."
        self._emit_turn(turn, "document", doc_kind=file["kind"], id=file.get("id"), title=title, url=file.get("url") or "")
        self.tools.open_link(file.get("url") or "")
        hint = ""
        if self.config.get("link_browser", "default") == "default" and self._open_hints < 2:
            self._open_hints += 1
            hint = " If your browser asks you to sign in, say 'show it here' and I'll display it myself."
        return f"Opening {title}.{hint}"

    def view_google(self, kind: str, ref: str) -> dict:
        """The 'VIEW' button on a document card: show the file in JARVIS's own reader (no browser, no sign-in)."""
        def run() -> None:
            try:
                data = self.tools.google.view(kind, ref)
                self.emit("document_view", **_view_event(data, ref if len(ref) > 20 else None))
            except BridgeError as exc:
                self.emit("notice", level="error", text=f"Google: {exc}")

        self._spawn(run, name="view-google")
        return {"ok": True}

    def open_google_link(self, url: str) -> bool:
        """Open a Google link from the UI with the chosen browser, signed in as the linked account."""
        return self.tools.open_link(url)

    # ================================================================== email
    def _find_document(self, name: str) -> dict | None:
        for kind, action in (("doc", self.tools.google.doc), ("slides", self.tools.google.slides)):
            try:
                files = action("list", **({"document": name} if kind == "doc" else {"presentation": name})).get("files") or []
            except BridgeError:
                continue
            if files:
                return {"kind": kind, "id": files[0]["id"], "title": files[0]["title"], "url": files[0]["url"]}
        return None

    def _email(self, turn: Turn, text: str, req: EmailRequest, lang: Detection | None) -> Iterator[str]:
        """Write an email and put it on screen for confirmation. Nothing is sent from here."""
        google = self.tools.google
        to, to_name, candidates = req.address or "", "", []
        if not to and re.fullmatch(r"(?:me|myself|my\s+self)", req.who, re.I):
            to, to_name = self.config.get("google_user_email") or "", self.config.get("user_name") or ""
        elif not to and google.can_email:
            self._emit_turn(turn, "tool_activity", tool="email", label=f"Looking up {req.who}'s address")
            try:
                candidates = google.find_contacts(req.who)
            except BridgeError as exc:
                log.info("Contact lookup failed: %s", exc)
            if candidates:
                to, to_name = candidates[0]["email"], candidates[0].get("name") or req.who
        if not to_name and not req.address and req.who.lower() not in ("me", "myself"):
            to_name = req.who
        doc = None
        if req.share:
            doc = self._find_document(req.doc_name) if req.doc_name and google.configured else self._last_doc
            if not doc:
                yield f"Which document should I send, {self.title}? I couldn't tell which one you meant."
                return
        self._emit_turn(turn, "tool_activity", tool="email", label=f"Writing an email to {to_name or to or req.who}")
        if doc:
            req = EmailRequest(who=req.who, address=req.address, how="sharing",
                               what=(f'the Google {"Slides presentation" if doc["kind"] == "slides" else "Doc"} '
                                     f'"{doc["title"]}". Include this link exactly as written: {doc["url"]}'
                                     + (f". Also: {req.what}" if req.what else "")))
        system, prompt = email_prompt(req, first_name(to_name or req.who), self.config.get("user_name") or "",
                                      lang.name if lang else None)
        draft_text = self.llm.compose(system, prompt, turn.cancel, max_tokens=700, temperature=0.5)
        if turn.cancel.is_set():
            return
        fallback = doc["title"] if doc else (req.what[:60].capitalize() if req.what else "Hello")
        subject, body = parse_email(draft_text, fallback)
        if doc and doc["url"] and doc["url"] not in body:
            body += f"\n\n{doc['url']}"
        draft = Draft(id=f"m{next(self._ids)}", to=to, to_name=to_name, subject=subject, body=body, candidates=candidates,
                      share_id=doc["id"] if doc else "")
        self._drafts[draft.id] = draft
        self._emit_turn(turn, "email_draft", **draft.as_event(), can_send=google.can_email)
        who = to_name or to
        if not to:
            button = "Send" if google.can_email else "Open in Gmail"
            yield (f"I've drafted the email, {self.title}, but I couldn't find {req.who}'s address. "
                   f"Type it into the card on screen and press {button}.")
            return
        self._awaiting = draft
        yield f"I've drafted an email to {who}. Shall I send it?"

    def _send_draft(self, draft: Draft) -> str:
        """Send (or hand to Gmail) a draft the user has confirmed."""
        google = self.tools.google
        if google.can_email and draft.to:
            if draft.share_id:
                try:
                    google.share_file(draft.share_id, draft.to)
                except BridgeError as exc:
                    log.info("Sharing before emailing failed: %s", exc)
            try:
                google.send_email(draft.to, draft.subject, draft.body)
            except BridgeError as exc:
                self.emit("email_status", id=draft.id, status="error", error=str(exc))
                return f"I'm afraid the email didn't go through, {self.title}: {exc}."
            self._drafts.pop(draft.id, None)
            self.emit("email_status", id=draft.id, status="sent", to=draft.to)
            return f"Sent to {draft.to_name or draft.to}, {self.title}."
        self.tools.open_link(gmail_compose_url(draft.to, draft.subject, draft.body))
        self._drafts.pop(draft.id, None)
        self.emit("email_status", id=draft.id, status="opened")
        return f"I've opened it in Gmail for you, {self.title}. Just press Send."

    # -- called from the email card on screen
    def send_email(self, draft_id: str, to: str = "", subject: str = "", body: str = "", via_gmail: bool = False) -> dict:
        draft = self._drafts.get(draft_id)
        if draft is None:
            return {"ok": False, "error": "That draft is no longer available."}
        draft.to = (to or draft.to).strip()
        draft.subject = subject if subject else draft.subject
        draft.body = body if body else draft.body
        if self._awaiting is draft:
            self._awaiting = None
        if via_gmail:
            self.tools.open_link(gmail_compose_url(draft.to, draft.subject, draft.body))
            self._drafts.pop(draft_id, None)
            self.emit("email_status", id=draft_id, status="opened")
            return {"ok": True, "status": "opened"}
        if self.tools.google.can_email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", draft.to):
            return {"ok": False, "error": "Please enter a valid email address."}
        message = self._send_draft(draft)
        failed = draft_id in self._drafts
        return {"ok": not failed, "message": message, "error": message if failed else None}

    def discard_email(self, draft_id: str) -> None:
        draft = self._drafts.pop(draft_id, None)
        if self._awaiting is draft:
            self._awaiting = None
        self.emit("email_status", id=draft_id, status="discarded")

    def _edit_target(self, change: EditRequest) -> dict | None:
        """Which document an edit means: a named one, or the one JARVIS worked on last."""
        if change.name:
            return {"kind": change.target or "doc", "id": change.name, "title": change.name, "url": ""}
        last = self._last_doc
        if last and (change.target in (None, last["kind"])):
            if change.action == "revise" and last["kind"] == "slides":
                return None  # rewriting a whole deck goes to the model (it can edit slide by slide)
            return last
        return None

    def _edit(self, turn: Turn, text: str, change: EditRequest, target: dict, lang: Detection | None) -> Iterator[str]:
        """Add to or revise a Google Doc, or add a slide, with the model writing the new content."""
        google = self.tools.google
        language = lang.name if lang else None
        ref = target["id"] or target["title"]
        try:
            if target["kind"] == "slides":
                deck = google.slides("read", presentation=ref)
                deck_text = "\n\n".join(s.get("text", "") for s in deck.get("slides", []))
                self._emit_turn(turn, "tool_activity", tool="compose", label=f"Writing a slide: {change.topic or change.part}")
                system, prompt = extra_slide_prompt(change, deck_text, language)
                draft = self.llm.compose(system, prompt, turn.cancel, max_tokens=300)
                if turn.cancel.is_set():
                    return
                _t, _s, slides = parse_deck(draft)
                slide = slides[0] if slides else {"title": change.topic.capitalize(), "body": []}
                result = google.call("slides_add", presentation=ref, title=slide["title"], body=slide["body"])
                done = f"I've added a slide on {change.topic or slide['title']} to {deck.get('title') or 'the presentation'}."
            else:
                doc = google.doc("read", document=ref)
                current = doc.get("text", "")
                if change.action == "add":
                    self._emit_turn(turn, "tool_activity", tool="compose",
                                    label=f"Writing a {change.part}" + (f": {change.topic}" if change.topic else ""))
                    system, prompt = section_prompt(change, current, language)
                    draft = self.llm.compose(system, prompt, turn.cancel, max_tokens=900,
                                             on_progress=lambda p: self._emit_turn(turn, "activity", label=f"Writing · {len(p.split())} words"))
                    if turn.cancel.is_set():
                        return
                    _title, body = clean_document(draft)
                    result = google.doc("append", document=ref, text=body)
                    what = f"a {change.part} on {change.topic}" if change.topic else f"a {change.part}"
                    done = f"I've added {what} to {doc.get('title') or 'the document'}."
                else:
                    self._emit_turn(turn, "tool_activity", tool="compose", label=f"Revising {doc.get('title') or 'the document'}")
                    system, prompt = revise_prompt(change, current, language)
                    draft = self.llm.compose(system, prompt, turn.cancel, max_tokens=int(len(current.split()) * 2.5) + 600,
                                             on_progress=lambda p: self._emit_turn(turn, "activity", label=f"Revising · {len(p.split())} words"))
                    if turn.cancel.is_set():
                        return
                    _title, body = clean_document(draft)
                    if len(body.split()) < 20:
                        raise BridgeError("the rewrite came out empty, so I left the document unchanged")
                    result = google.doc("rewrite", document=ref, text=body)
                    done = f"I've revised {doc.get('title') or 'the document'} as you asked."
        except BridgeError as exc:
            self._emit_turn(turn, "system_message", level="error", text=f"Google: {exc}")
            yield f"I'm afraid I couldn't change it, {self.title}: {exc}."
            return
        url = result.get("url") or target.get("url") or ""
        self._last_doc = {"kind": target["kind"], "id": result.get("id") or target["id"],
                          "title": result.get("title") or target["title"], "url": url}
        self._emit_turn(turn, "document", doc_kind=target["kind"], id=self._last_doc["id"], title=self._last_doc["title"], url=url)
        self.llm.remember(text, done)
        yield f"Done. {done}"

    # ================================================================== ollama
    def check_ollama(self, emit_event: bool = True) -> dict:
        status = self.llm.check()
        self._ollama = status
        self.vision.refresh(status.models)
        if self.memory.embed_model not in (status.models or []):
            self.memory.pick_embed_model(status.models)
        if emit_event:
            self.emit("vision_status", **self.vision_status())
        if emit_event:
            self.emit("ollama_status", **status.to_dict())
            self.emit("core_stats", **self.core_stats())
        return status.to_dict()

    def start_ollama(self) -> dict:
        if not self.llm.start_server():
            return {"started": False, "reason": "Ollama is not installed on this computer."}

        def wait_online() -> None:
            for _ in range(20):
                time.sleep(1.0)
                if self.check_ollama(emit_event=False)["online"]:
                    break
            self.check_ollama()

        self._spawn(wait_online, name="ollama-start")
        return {"started": True}

    def pull_model(self, name: str, role: str = "chat") -> dict:
        name = (name or "").strip()
        if not _MODEL_NAME.match(name):
            return {"ok": False, "error": "Invalid model name."}
        if self._pull_thread is not None and self._pull_thread.is_alive():
            return {"ok": False, "error": "A download is already in progress."}
        cancel = threading.Event()
        self._pull_cancel = cancel

        def run() -> None:
            last = [0.0, ""]

            def progress(status: str, completed: int, total: int) -> None:
                now = time.monotonic()
                if now - last[0] < 0.12 and status == last[1]:
                    return
                last[0], last[1] = now, status
                pct = round(completed / total * 100, 1) if total else None
                self.emit("pull_progress", model=name, status=status, completed=completed, total=total, percent=pct, role=role)

            try:
                self.llm.pull(name, progress, cancel)
                if cancel.is_set():
                    self.emit("pull_done", model=name, ok=False, error="Download cancelled.", role=role)
                    return
                if role != "embed":
                    self.config.update({"vision_model" if role == "vision" else "model": name})
                self.emit("pull_done", model=name, ok=True, error=None, role=role)
                self.check_ollama()
                if role == "embed":
                    self.memory.poke()  # index the existing memories for meaning-based recall
                    self.emit("memory_changed", stats=self.memory.stats())
                elif role == "vision":
                    self._announce(f"My eyes are online, {self.title}. Ask me what's on your screen any time.")
                else:
                    self.llm.warmup()
            except LLMError as exc:
                self.emit("pull_done", model=name, ok=False, error=str(exc), role=role)

        self._pull_thread = threading.Thread(target=run, name="ollama-pull", daemon=True)
        self._pull_thread.start()
        return {"ok": True}

    def cancel_pull(self) -> None:
        if self._pull_cancel is not None:
            self._pull_cancel.set()

    def core_stats(self) -> dict:
        return {
            "online": self.llm.online,
            "model": self.llm.model,
            "host": self.llm.host,
            "first_token_ms": self.llm.last_first_token_ms,
            "tokens_per_sec": self.llm.last_tokens_per_sec,
            "memory_turns": self.llm.history_turns,
        }

    # ================================================================== settings
    def update_settings(self, changes: dict) -> dict:
        changes = dict(changes or {})
        persona = changes.pop("persona", None)
        if persona and persona != self.config.get("persona"):
            self.set_persona(persona, announce=False)
        applied = self.config.update(changes)
        if {"model", "ollama_host"} & applied.keys():
            self._spawn(self.check_ollama, name="recheck")
        if {"stt_engine", "vosk_model_path", "whisper_model"} & applied.keys():
            self.emit("mic_status", **{**self._mic, "engine": self.stt.engine_label()})
        if "wake_word" in applied:
            self._spawn(self._sync_wake_word, name="wake-sync")
        if {"wake_jarvis_always", "wake_word"} & applied.keys():
            self._wake_changed()
        self.emit("settings", **self.config.as_dict())
        return self.config.as_dict()

    # ================================================================== personalities
    def persona_info(self, persona: Persona | None = None) -> dict:
        p = persona or self.persona
        wake = {"name": "Jarvis", "phrase": "Hey Jarvis", "state": "ready", "progress": 1.0, "builtin": True} if p.id == "jarvis" \
            else self.wakewords.status(p.name)
        return {**p.to_dict(), "address": address_for(p, self.config), "active": p.id == self.persona.id,
                "color": theme_for(p, self.config), "custom_color": p.id in (self.config.get("persona_colors") or {}),
                "wake": wake, "saved": next((c for c in self.config.get("custom_personas") or [] if c.get("id") == p.id), None)}

    def persona_list(self) -> list[dict]:
        return [self.persona_info(p) for p in PERSONAS.values()]

    def set_persona(self, pid: str, announce: bool = True) -> dict:
        """Become another personality: its voice, colours and character. Memory is shared."""
        new = PERSONAS.get((pid or "").lower())
        if new is None:
            raise ValueError(f"Unknown personality: {pid}")
        changes = {"persona": new.id, "voice": new.voice, "speech_rate": new.rate, "speech_pitch": new.pitch}
        if self.config.get("persona_theme", True):
            changes["theme"] = theme_for(new, self.config)
        self.config.update(changes)
        info = self.persona_info()
        self.emit("persona", **info)
        self.emit("settings", **self.config.as_dict())
        self._wake_changed()
        if announce:
            self.speak(new.intro.format(title=self.title))
        return info

    def save_persona(self, data: dict) -> dict:
        """Create or edit one of the user's own personalities."""
        saved = list(self.config.get("custom_personas") or [])
        data = dict(data or {})
        old = next((c for c in saved if c.get("id") == data.get("id")), None) if is_custom(str(data.get("id") or "")) else None
        if old is None and len(saved) >= 12:
            raise ValueError("You can have up to 12 personalities of your own. Delete one to make room.")
        clean = validate_custom(data, saved)
        saved = [clean if c.get("id") == clean["id"] else c for c in saved] if old else saved + [clean]
        self.config.update({"custom_personas": saved})
        load_custom(saved)
        renamed = old is not None and old.get("name") != clean["name"]
        if renamed:
            self.wakewords.delete(old["name"])
        persona = PERSONAS[clean["id"]]
        if self.persona.id == clean["id"]:  # editing the active one: apply its new voice and colour now
            changes = {"voice": persona.voice}
            if self.config.get("persona_theme", True):
                changes["theme"] = theme_for(persona, self.config)
            self.config.update(changes)
            self.emit("persona", **self.persona_info())
            self._wake_changed()
        self.emit("personas", personas=self.persona_list())
        self.emit("settings", **self.config.as_dict())
        if self.config.get("wake_word", True) and self.config.get("wake_learn_auto", True) and (old is None or renamed):
            self.wakewords.learn(persona.name)
        return self.persona_info(persona)

    def delete_persona(self, pid: str) -> None:
        if not is_custom(pid):
            raise ValueError("The built-in personalities can't be deleted.")
        saved = [c for c in self.config.get("custom_personas") or [] if c.get("id") != pid]
        gone = PERSONAS.get(pid)
        colors = {k: v for k, v in (self.config.get("persona_colors") or {}).items() if k != pid}
        if self.persona.id == pid:
            self.set_persona("jarvis", announce=False)
        self.config.update({"custom_personas": saved, "persona_colors": colors})
        load_custom(saved)
        if gone is not None:
            self.wakewords.cancel(gone.name)
            self.wakewords.delete(gone.name)
        self.emit("personas", personas=self.persona_list())

    def set_persona_color(self, pid: str, color: str | None) -> dict:
        """The HUD colour for a personality (None = back to its own)."""
        p = PERSONAS.get(pid)
        if p is None:
            raise ValueError(f"Unknown personality: {pid}")
        colors = dict(self.config.get("persona_colors") or {})
        if color:
            if re.match(r"^#[0-9a-fA-F]{6}$", color):
                color = color.upper()
            elif color not in PRESET_THEMES:
                raise ValueError("That isn't a colour.")
            colors[pid] = color
        else:
            colors.pop(pid, None)
        self.config.update({"persona_colors": colors})
        if pid == self.persona.id and self.config.get("persona_theme", True):
            self.config.update({"theme": theme_for(p, self.config)})
            self.emit("settings", **self.config.as_dict())
        return self.persona_info(p)

    # wake words ---------------------------------------------------------------
    def _wake_words(self) -> tuple[bool, list]:
        """What the listener should wake up for: "Hey Jarvis" and/or the active personality's learned name."""
        p = self.persona
        if p.id == "jarvis":
            return True, []
        model = self.wakewords.model(p.name)
        return (bool(self.config.get("wake_jarvis_always", True)) or model is None), ([model] if model else [])

    def _wake_changed(self) -> None:
        if self.wake is not None:
            self.wake.refresh_words()
        p = self.persona
        if (p.id != "jarvis" and self.config.get("wake_word", True) and self.config.get("wake_learn_auto", True)
                and self.wakewords.model(p.name) is None and self.wakewords.status(p.name)["state"] == "missing"):
            self.wakewords.learn(p.name)
        self.emit("wake_status", **self.wake_status())

    def _wake_learned(self, name: str) -> None:
        if self.wake is not None:
            self.wake.refresh_words()
        self.emit("wake_status", **self.wake_status())
        self.emit("personas", personas=self.persona_list())
        if self.persona.name == name and self.wake is not None and self.wake.running:
            self._announce(f"I've learned my name, {self.title}. Just say \"Hey {name}\" whenever you need me.")

    def wake_learn(self, pid: str) -> dict:
        p = PERSONAS.get(pid)
        if p is None or p.id == "jarvis":
            return {"ok": False, "error": "\u201cHey Jarvis\u201d is built in."}
        self.wakewords.learn(p.name)
        return {"ok": True, "wake": self.wakewords.status(p.name)}

    def wake_record(self, pid: str) -> dict:
        """Record the user saying the name once (about 2.5 seconds), to train the wake word on their voice."""
        p = PERSONAS.get(pid)
        if p is None or p.id == "jarvis":
            return {"ok": False, "error": "Pick a personality first."}
        if not self._mic.get("available"):
            return {"ok": False, "error": "I can't find a microphone."}
        try:
            source = self.wake.borrow() if self.wake is not None and self.wake.running else wakeword.WakeListener._open_pyaudio()
        except Exception as exc:
            return {"ok": False, "error": f"Couldn't open the microphone: {exc}"}
        try:
            return self.wakewords.record(p.name, source)
        except Exception as exc:
            log.exception("Recording a wake-word sample failed")
            return {"ok": False, "error": str(exc)}
        finally:
            try:
                source.close()
            except Exception:
                pass

    def wake_train_voice(self, pid: str) -> dict:
        p = PERSONAS.get(pid)
        if p is None or self.wakewords.recording_count(p.name) < 3:
            return {"ok": False, "error": "Record the name at least three times first."}
        self.wakewords.learn(p.name, with_my_voice=True)
        return {"ok": True, "wake": self.wakewords.status(p.name)}

    def wake_clear_voice(self, pid: str) -> dict:
        p = PERSONAS.get(pid)
        if p is not None:
            self.wakewords.clear_recordings(p.name)
        return {"ok": True}

    def preview_persona(self, pid: str) -> None:
        p = PERSONAS.get((pid or "").lower())
        if p:
            self.speak(p.sample.format(title=address_for(p, self.config)), voice=p.voice)

    def _switch_persona(self, pid: str) -> str:
        if pid == self.persona.id:
            return self.persona.line("already", ["I'm right here, {title}."], self.title)
        self.set_persona(pid, announce=False)
        return self.persona.intro.format(title=self.title)

    def _who_line(self, text: str) -> str:
        if re.search(r"personalities|list", text, re.I):
            others = "; ".join(f"{p.name}, {p.tagline[0].lower() + p.tagline[1:]}" for p in PERSONAS.values())
            return (f"I can be {others}. You're talking to {self.persona.name} right now. "
                    f"Just say, for example, \"switch to Harper\".")
        return self.persona.line("who", [f"I'm {self.persona.name}, {{title}}."], self.title)

    # ================================================================== long-term memory
    def _start_memory(self) -> None:
        self.memory.start(idle=lambda: self._turn is None)
        if self.memory.store is None:
            return
        self.memory.pick_embed_model(self.llm.installed)
        turns = self.memory.restore_recent()
        if turns:
            self.llm.load_history(turns)
            self._restored = [{"role": t["role"], "text": t["text"], "ts": t["ts"]} for t in turns]
        self.memory.poke()

    def _memory_context(self, text: str) -> str:
        try:
            return self.memory.context(text)
        except Exception:
            log.exception("Memory recall failed")
            return ""

    def _resume_line(self) -> str:
        return self.persona.line("resume", ["Welcome back. I've kept our last conversation, so we can pick up where we left off."], self.title)

    def _routine_line(self) -> str:
        if not (self.config.get("routine_reminders", True) and self.memory.enabled):
            return ""
        try:
            today = today_routines(self.memory.store.all("routine"))[:2]
        except Exception:
            return ""
        if not today:
            return ""
        return " A quick reminder for today: " + " and ".join(spoken(m.text) for m in today) + "."

    def _remembered_line(self, memory: Memory, status: str) -> str:
        what = first_person_echo(memory.text)
        if status == "duplicate":
            return f"I already know that, {self.title}: {what}."
        if status == "updated":
            return f"Got it, {self.title}. I've updated my memory: {what}."
        return self.persona.line("remembered", ["Very good, {title}. I'll remember that {what}.", "Noted, {title}. I'll remember that {what}."],
                                 self.title, what=what)

    def _memory_command(self, cmd: MemoryCommand) -> str | None:
        title = self.title
        if cmd.action == "call_me":
            name = re.sub(r"\s+", " ", cmd.text).strip(" .'")
            if cmd.kind == "title" and name.lower() in {"sir", "ma'am", "maam", "madam", "boss", "captain", "chief", "doctor", "doc",
                                                         "professor", "master", "miss", "mister", "commander"}:
                self.config.update({"user_title": name.lower()})
                self.emit("settings", **self.config.as_dict())
                return f"Very well, {self.title}."
            name = " ".join(w[:1].upper() + w[1:] for w in name.split())[:40]
            changes = {"user_name": name}
            if cmd.kind == "title":
                changes["user_title"] = name
            self.config.update(changes)
            self.emit("settings", **self.config.as_dict())
            if cmd.kind == "title":
                return f"{name} it is. I'll call you that from now on."
            return f"Lovely to meet you, {name}. I'll remember that."
        if cmd.action == "my_name":
            name = self.config.get("user_name")
            if name:
                return f"You're {name}, of course."
            return f"You haven't told me your name yet, {title}. Just say \"call me\" followed by your name."
        if not self.memory.enabled:
            if cmd.action in ("recall_about", "project_done"):
                return None
            return f"My long-term memory is switched off, {title}. You can turn it on in Settings, under Memory."
        store = self.memory.store
        if cmd.action == "remember":
            memory, status = self.memory.remember(cmd.text, source="said")
            if status == "secret":
                return (f"I'd rather not keep passwords, codes or card numbers in my memory, {title}. "
                        "A password manager is a much safer home for those.")
            if memory is None:
                return f"I didn't catch what to remember, {title}."
            self.emit("memory_learned", memory=memory.to_dict(), status=status)
            return self._remembered_line(memory, status)
        if cmd.action == "forget":
            gone = self.memory.forget_matching(cmd.text)
            if gone is None:
                return f"I don't have anything about {spoken(cmd.text)} in my memory, {title}."
            self.emit("memory_forgotten", memory=gone.to_dict())
            return f"Done, {title}. I've forgotten that {first_person_echo(gone.text)}."
        if cmd.action == "forget_all":
            total = store.count()["total"]
            if not total and not store.episodes(limit=1):
                return f"There's nothing in my memory to forget, {title}."
            self._pending_forget = time.time()
            things = (f"all {total} things I remember about you" if total > 1 else "the one thing I remember about you" if total
                      else "everything I remember")
            return f"Are you sure, {title}? That erases {things}, and our past conversations. Say yes to confirm."
        if cmd.action == "recall":
            self.emit("memory_open")
            return self._recall_summary()
        if cmd.action == "recall_about":
            hits = self.memory.search(cmd.text, limit=3, min_score=0.3, min_cover=0.6)
            if not hits:
                return None  # let the language model answer (it still sees the memory block)
            return "Yes: " + "; and ".join(spoken(m.text) for m in hits) + "."
        if cmd.action == "projects":
            active = [m for m in store.all("project") if m.meta.get("status") != "done"]
            done = [m for m in store.all("project") if m.meta.get("status") == "done"]
            if not active:
                extra = f" You've finished {len(done)} so far." if done else ""
                return f"You haven't told me about any ongoing projects, {title}. Tell me what you're working on and I'll keep track.{extra}"
            return (f"Here's what you're working on, {title}: " + "; ".join(spoken(m.text) for m in active[:5]) + "."
                    + (f" And you've finished {len(done)}." if done else ""))
        if cmd.action == "routines":
            routines = store.all("routine")
            if not routines:
                return f"I don't know any of your routines yet, {title}. Tell me about them, like \"I go to the gym on Mondays at 6\"."
            today = today_routines(routines)
            if today:
                return f"Today, {title}: " + "; ".join(spoken(m.text) for m in today) + "."
            return f"Nothing special today, {title}. Your routines: " + "; ".join(spoken(m.text) for m in routines[:5]) + "."
        if cmd.action == "episodes":
            episodes = store.episodes(limit=2)
            if episodes:
                said = " Before that, ".join(_about_you(e.summary) for e in episodes)
                return f"Last time, {said}"
            if self._restored:
                last = next((t["text"] for t in reversed(self._restored) if t["role"] == "user"), "")
                if last:
                    return f"We were just talking. The last thing you asked me was: \"{last[:160]}\""
            return f"I don't have any earlier conversations on record yet, {title}."
        if cmd.action == "project_done":
            done = self.memory.finish_project(cmd.text)
            if done is None:
                return None
            self.emit("memory_learned", memory=done.to_dict(), status="updated")
            return self.persona.line("project_done", ["Congratulations, {title}. I've marked {what} as done."], title,
                                     what=spoken(cmd.text))
        return None

    def _recall_summary(self) -> str:
        title = self.title
        overview = self.memory.overview()
        name = self.config.get("user_name")
        bits = []
        if name:
            bits.append(f"your name is {name}")
        bits += [spoken(m.text) for m in overview["fact"][:2]]
        bits += [spoken(m.text) for m in overview["preference"][:2]]
        bits += [spoken(m.text) for m in [p for p in overview["project"] if p.meta.get("status") != "done"][:1]]
        bits += [spoken(m.text) for m in overview["routine"][:1]]
        total = sum(len(v) for v in overview.values())
        if not bits:
            return (f"I don't know much about you yet, {title}. Tell me about yourself, or say \"remember that\" followed by "
                    "anything you'd like me to keep.")
        listed = ", ".join(bits[:-1]) + (f", and {bits[-1]}" if len(bits) > 1 else bits[-1])
        more = f" That's {total} things in all; they're on screen in the Memory Core." if total > len(bits) else " It's all on screen in the Memory Core."
        return f"Here's what I know, {title}: {listed}.{more}"

    # API for the Memory Core window
    def memory_overview(self) -> dict:
        return {"memories": [m.to_dict() for kind in self.memory.overview().values() for m in kind],
                "episodes": [e.to_dict() for e in self.memory.store.episodes(limit=30)] if self.memory.store else [],
                "stats": self.memory.stats()}

    def memory_add(self, kind: str, text: str) -> dict:
        if self.memory.store is None:
            raise ValueError("Memory is unavailable.")
        memory, status = self.memory.store.add(kind, _tidy_memory(text), source="manual")
        self.memory.poke()
        self.emit("memory_changed", stats=self.memory.stats())
        return {"memory": memory.to_dict(), "status": status}

    def memory_update(self, mid: int, fields: dict) -> dict:
        if self.memory.store is None:
            raise ValueError("Memory is unavailable.")
        allowed = {k: v for k, v in (fields or {}).items() if k in ("text", "kind", "pinned", "meta")}
        if "text" in allowed:
            allowed["text"] = _tidy_memory(allowed["text"])
        memory = self.memory.store.update(int(mid), **allowed)
        if memory is None:
            raise ValueError("That memory no longer exists.")
        self.memory.poke()
        self.emit("memory_changed", stats=self.memory.stats())
        return memory.to_dict()

    def memory_delete(self, mid: int) -> dict | None:
        gone = self.memory.store.delete(int(mid)) if self.memory.store else None
        self.emit("memory_changed", stats=self.memory.stats())
        return gone.to_dict() if gone else None

    def memory_restore(self, data: dict) -> dict:
        memory = self.memory.store.restore(data or {})
        self.memory.poke()
        self.emit("memory_changed", stats=self.memory.stats())
        return memory.to_dict()

    def memory_delete_episode(self, eid: int) -> None:
        if self.memory.store:
            self.memory.store.delete_episode(int(eid))
            self.emit("memory_changed", stats=self.memory.stats())

    def memory_clear(self) -> None:
        self.memory.clear()
        self.llm.clear_history()
        self._restored = []
        self.emit("memory_cleared")

    def list_voices(self) -> list[dict]:
        return self.tts.list_voices()

    def preview_voice(self, voice: str | None = None) -> None:
        self.speak(f"Good day, {self.title}. This is how I will sound from now on.", voice=voice)
