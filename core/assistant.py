"""The JARVIS brain: wires speech input, the language model and the voice together.

Every interaction runs as a *turn* on its own worker thread. Starting a new turn
(or pressing Escape) cancels the previous one, so the user can always barge in.
A cancelled turn keeps running until it notices, but it is no longer allowed to
touch the state machine or the UI.
"""

from __future__ import annotations

import itertools
import logging
import random
import sys
import queue
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
from .gdrive import GoogleRequest, parse_google_request
from .google_bridge import BridgeError
from .mail import (Draft, EmailRequest, email_prompt, first_name, gmail_compose_url, is_cancellation, is_confirmation,
                   parse_email, parse_email_request)
from .llm import LLMConnectionError, LLMEngine, LLMError, LLMModelError, OllamaStatus
from .sfx import SoundFX
from .state import State, StateMachine
from .stt import ENGINE_LABELS, Capture, SpeechInput, STTError
from .system import SystemMonitor
from .tools import Toolbox, ToolError, describe_call
from . import osctl, wakeword
from .quick import Quick, describe_duration, parse_quick, pick
from .ocr import default_ocr
from .screen import ForegroundTracker, ScreenError, default_desktop, is_private
from .vision import (DEFAULT_VISION_MODEL, ActRequest, LookRequest, VisionEngine, VisionUnavailable, Watcher, WatchRequest,
                     data_url, is_risky, parse_act, parse_look, parse_watch, wants_vision_install)
from .compose import SHORT_FORM
from .language import LANGUAGES, Detection, base_language, detect, voice_for
from .patience import looks_unfinished
from .memory import Memory, MemoryCommand, MemoryEngine, first_person_echo, parse_memory_command, spoken, today_routines
from .personas import (PERSONAS, Persona, address_for, asks_who, get_persona, is_custom, load_custom, names_pattern, parse_switch,
                       theme_for, validate_custom)
from .wakewords import WakeWords

PRESET_THEMES = ("arc", "mark3", "stealth", "violet", "rose")
from .tts import CODE_MARK, EdgeTTS, SentenceSplitter, TTSError, clean_for_speech

log = logging.getLogger("jarvis.assistant")

Emit = Callable[[str, dict], None]
_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/:]{0,120}$")
def _strip_wake(text: str) -> str:
    """'Hey Harper, what time is it' -> 'what time is it' (any personality's name)."""
    return re.sub(r"^\s*(?:(?:hey|hi|okay|ok)\s+)?(?:" + names_pattern() + r")\b[\s,.!?]*", "", text, count=1, flags=re.I)
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
    r"\b(open|launch|start|run|play|close|file|files|folder|document|doc|docs|docx|pdf|read|find|desktop|downloads|"
    r"spreadsheet|sheet|sheets|slides?|presentation|deck|app|game|search|look up|google|internet|online|web|website|"
    r"news|weather|latest|current|price|summari[sz]e|"
    r"write|create|make|add|edit|update|replace|list|delete|remove|move|reorder|rearrange|rename|table|row|rows|column|cell)\b", re.IGNORECASE)
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


def _seconds_until(clock: str) -> int | None:
    """'6 pm' / '6:30 pm' -> seconds until the next time the clock shows that."""
    m = re.match(r"^\s*(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\s*$", clock or "", re.I)
    if not m:
        return None
    hour, minute = int(m.group(1)) % 12, int(m.group(2) or 0)
    if m.group(3).lower() == "p":
        hour += 12
    now = datetime.now()
    due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if due <= now:
        due = due.replace(day=now.day) + __import__("datetime").timedelta(days=1)
    return int((due - now).total_seconds())


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
        self._last_doc: dict | None = None  # the Google Doc / deck JARVIS made or edited last ("add a section to it")
        self._timers: dict[int, dict] = {}
        self._timer_ids = itertools.count(1)
        self._prewarm_stop = threading.Event()
        self._open_hints = 0  # how often we've explained the "sign in / show it here" fallback
        self._drafts: dict[str, Draft] = {}  # emails shown on screen, by id
        self._awaiting: Draft | None = None  # the draft JARVIS just asked "shall I send it?" about
        self._pending_forget = 0.0  # when JARVIS asked "forget everything?"
        self._restored: list[dict] = []  # the end of the last conversation, carried on after a restart
        self.memory = MemoryEngine(config, self.llm, on_event=lambda kind, payload: self.emit(kind, **payload))
        load_custom(config.get("custom_personas"))
        self.wakewords = WakeWords(app_data_dir() / "wakewords", self.tts, self.audio, self.emit)
        self.wakewords.on_ready = self._wake_learned
        self.llm.persona_provider = lambda: (self.persona, self.title)

    # ================================================================== plumbing
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

    def shutdown(self) -> None:
        self._prewarm_stop.set()
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
        return {"enabled": bool(self.config.get("wake_word", True)), "active": active, "phrase": phrases[0] if phrases else "Hey Jarvis",
                "phrases": phrases, "reason": reason, "learning": learning}

    def _sync_wake_word(self) -> None:
        """Start or stop the background "Hey Jarvis" listener to match settings and hardware."""
        ok, why = wakeword.available()
        want = bool(self.config.get("wake_word", True)) and ok and self._mic.get("available")
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

    def start_listening(self) -> str:
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
        if not self._is_current(turn):
            return
        text = _strip_wake(text).strip()
        if not text:
            self.emit("notice", level="info", text=f"I didn't quite catch that, {self.title}.")
            self._finish_turn(turn)
            return
        if re.sub(r"[^\w' ]+", "", text.lower()).strip() in _STOP_PHRASES and self._awaiting is None:
            self.sfx.play("interrupt")
            self.emit("notice", level="info", text="Standing by.")
            self._finish_turn(turn)
            return
        self._converse(turn, text, "voice")

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
        replies = []
        for part in parts:
            if turn.cancel.is_set():
                break
            reply = self._direct_command(turn, part)
            if reply is None:
                replies.append(f"I couldn't work out '{part}', {self.title}.")
                continue
            replies.append(reply)
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
        window = None if req.full_screen else self._target_window()
        label = window.label if window else "your screen"
        self._emit_turn(turn, "tool_activity", tool="vision", label=f"Looking at {label}")
        try:
            obs = self.vision.observe(window)
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
                osctl.toggle_mute()
                return f"Toggling the sound, {title}."
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
            return f"Very well, {title}. I'll remind you to {label} in {span}."
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
        message = f"A reminder, {title}: {label}." if label else f"Your {describe_duration(entry['seconds'])} timer is up, {title}."
        self.emit("notice", level="info", text=("Reminder: " + label) if label else "Timer finished.")
        self.sfx.play("notify")
        deadline = time.monotonic() + 90
        while self.state.state != State.IDLE and time.monotonic() < deadline:  # never talk over a conversation
            time.sleep(0.5)
        if not self._prewarm_stop.is_set():
            self.speak(message, delay=0.4)

    def _prewarm_phrases(self) -> list[str]:
        title = self.title
        phrases = [f"Good morning, {title}. How may I help?", f"Good afternoon, {title}. How may I help?", f"Good evening, {title}. How may I help?",
                   f"At your service, {title}.", f"Hello, {title}. What can I do for you?", f"Done, {title}.", f"Very well, {title}.",
                   f"Standing by, {title}.", f"Volume up, {title}.", f"Volume down, {title}.", f"Toggling the sound, {title}.",
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
        play = _PLAY_COMMAND.match(text.strip())
        if play and self.tools.files_enabled:
            target = play.group("target").strip(" \"'")
            try:
                result = self.tools.open_target(target, min_score=70, apps_only=True)
                self._tool_used(turn, "open_file", {"query": target})
                return f"Launching {result['name']}. Enjoy, {self.title}."
            except ToolError:
                return None  # "play some jazz" etc. goes to the model
        google = parse_google_request(text, have_last=self._last_doc is not None) if self.tools.web_enabled else None
        ready = self.tools.google.configured
        if google and (google.action != "open" or google.google or not google.name or google.recent):
            reply = self._google_request(turn, google, ready)  # "open the doc", "show me my budget sheet"...
            if reply:
                return reply
        match = _OPEN_COMMAND.match(text.strip())
        if not match:
            return None
        target = match.group("target").strip(" \"'")
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
            self.tools.open_website(site)
            self._tool_used(turn, "open_website", {"url": site})
            return f"Opening {target}, {self.title}."
        return None

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

        try:
            direct = self._direct_chain(turn, text) or self._direct_command(turn, text)
        except Exception:
            log.exception("Direct command failed")
            direct = None
        if direct:
            self._deliver(turn, [direct])
            return
        clock = self._clock_answer(text)
        if clock:
            self._deliver(turn, [clock])
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
            offer_tools=bool(_TOOL_CUES.search(text)), prefetch=prefetch,
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
            if turn.kind == "listen" and not error and (self.config.get("auto_listen") or listen_after):
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
