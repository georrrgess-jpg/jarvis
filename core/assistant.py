"""The JARVIS brain: wires speech input, the language model and the voice together.

Every interaction runs as a *turn* on its own worker thread. Starting a new turn
(or pressing Escape) cancels the previous one, so the user can always barge in.
A cancelled turn keeps running until it notices, but it is no longer allowed to
touch the state machine or the UI.
"""

from __future__ import annotations

import itertools
import logging
import queue
import re
import threading
import time
from datetime import datetime
from typing import Any, Callable, Iterable

from . import APP_VERSION
from .audio import VIS_FPS, AudioEngine, spectrum_frames
from .config import Config, app_data_dir
from .llm import LLMConnectionError, LLMEngine, LLMError, LLMModelError, OllamaStatus
from .sfx import SoundFX
from .state import State, StateMachine
from .stt import ENGINE_LABELS, SpeechInput, STTError
from .system import SystemMonitor
from .tools import Toolbox, ToolError, describe_call
from . import wakeword
from .tts import CODE_MARK, EdgeTTS, SentenceSplitter, TTSError, clean_for_speech

log = logging.getLogger("jarvis.assistant")

Emit = Callable[[str, dict], None]
_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/:]{0,120}$")
_WAKE_PREFIX = re.compile(r"^\s*(?:(?:hey|hi|okay|ok)\s+)?jarvis\b[\s,.!?]*", re.IGNORECASE)
_STOP_PHRASES = {
    "stop", "stop it", "stop talking", "stop listening", "cancel", "never mind", "nevermind", "forget it",
    "that's all", "thats all", "that is all", "nothing", "be quiet", "quiet", "shut up", "hush", "enough",
    "thank you that's all", "thanks that's all", "no thanks", "dismiss", "go to sleep", "standby", "stand by",
}
_OPEN_COMMAND = re.compile(
    r"^(?:(?:hey |ok |okay )?jarvis[, ]+)?(?:please |can you |could you |would you )*(?:open|launch|start|run|load|pull up|bring up|show me)"
    r"\s+(?:up\s+)?(?:the\s+|my\s+)?(?P<target>.+?)(?:\s+(?:please|for me|now))?[\s.!?]*$",
    re.IGNORECASE,
)


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

    def __init__(self, assistant: "Assistant", turn: Turn, voice: str | None = None) -> None:
        self._a = assistant
        self._turn = turn
        self._voice = voice
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

    def close(self) -> None:
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
                try:
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

    # ================================================================== plumbing
    @property
    def title(self) -> str:
        return self.config.get("user_title") or "sir"

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
        }

    def boot_complete(self) -> None:
        """Called by the UI once the boot animation has finished."""
        status = self._ollama
        hour = datetime.now().hour
        part = "morning" if 5 <= hour < 12 else "afternoon" if 12 <= hour < 18 else "evening"
        if status and status.online and status.model:
            text = f"Good {part}, {self.title}. All systems are online. How may I help?"
            self._spawn(self.llm.warmup, name="warmup")
        elif status and status.online:
            text = f"Good {part}, {self.title}. Ollama is running, but no language model is installed yet. I've put the details on screen."
        else:
            text = f"Good {part}, {self.title}. I'm afraid my neural core is offline. I've put instructions on screen to bring it online."
        self.sfx.play("activate")
        self.speak(text, delay=0.45)

    def shutdown(self) -> None:
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
        return {"enabled": bool(self.config.get("wake_word", True)), "active": active, "phrase": "Hey Jarvis", "reason": reason}

    def _sync_wake_word(self) -> None:
        """Start or stop the background "Hey Jarvis" listener to match settings and hardware."""
        ok, why = wakeword.available()
        want = bool(self.config.get("wake_word", True)) and ok and self._mic.get("available")
        if want and not (self.wake and self.wake.running):
            self.wake = wakeword.WakeListener(self._on_wake, lambda: float(self.config.get("wake_sensitivity", 0.5)))
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
            text = self.stt.transcribe(
                capture, on_status=lambda msg: self._emit_turn(turn, "notice", level="info", text=msg)
            )
        except STTError as exc:
            self._fail_turn(turn, str(exc))
            return
        if not self._is_current(turn):
            return
        text = _WAKE_PREFIX.sub("", text, count=1).strip()
        if not text:
            self.emit("notice", level="info", text=f"I didn't quite catch that, {self.title}.")
            self._finish_turn(turn)
            return
        if re.sub(r"[^\w' ]+", "", text.lower()).strip() in _STOP_PHRASES:
            self.sfx.play("interrupt")
            self.emit("notice", level="info", text="Standing by.")
            self._finish_turn(turn)
            return
        self._converse(turn, text, "voice")

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

    def _direct_command(self, turn: Turn, text: str) -> str | None:
        """Handle plain "open X" requests without the model: instant, and reliable even with small models."""
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
        if site:
            self.tools.open_website(site)
            self._tool_used(turn, "open_website", {"url": site})
            return f"Opening {target}, {self.title}."
        return None

    def _converse(self, turn: Turn, text: str, source: str) -> None:
        self._emit_turn(turn, "user_message", id=f"u{next(self._ids)}", text=text, source=source)
        if not self._set_state(State.THINKING, turn, "thinking"):
            return
        self.sfx.play("process")

        try:
            direct = self._direct_command(turn, text)
        except Exception:
            log.exception("Direct command failed")
            direct = None
        if direct:
            self._deliver(turn, [direct])
            return

        if not (self.llm.online and self.llm.model):
            self.check_ollama()  # maybe the user just started it
        if not (self.llm.online and self.llm.model):
            reason = "Ollama is not running" if not self.llm.online else "no language model is installed"
            self._deliver(turn, [f"I'm afraid my neural core is offline, {self.title}: {reason}. "
                                 "The setup steps are on screen."])
            return
        toolbox = self.tools if (self.tools.files_enabled or self.tools.web_enabled) else None
        self._deliver(turn, self.llm.stream_reply(
            text, turn.cancel, toolbox=toolbox, on_tool=lambda name, args: self._tool_used(turn, name, args)))

    def _deliver(self, turn: Turn, tokens: Iterable[str], voice: str | None = None) -> None:
        mid = f"a{next(self._ids)}"
        speaker = None
        if self.config.get("voice_enabled", True) and self.audio.available:
            speaker = Speaker(self, turn, voice)
        splitter = SentenceSplitter()
        error: str | None = None
        self._emit_turn(turn, "assistant_start", id=mid)
        try:
            for token in tokens:
                if turn.cancel.is_set():
                    break
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
        if error and self._is_current(turn):
            self.sfx.play("error")
            self.emit("system_message", level="error", text=error)
            if speaker:
                speaker.say(f"I'm afraid something went wrong, {self.title}. The details are on screen.")
        if speaker:
            speaker.close()
            speaker.wait()
        if self._finish_turn(turn) and not turn.cancel.is_set():
            if turn.kind == "listen" and not error and self.config.get("auto_listen"):
                self.start_listening()

    # ================================================================== ollama
    def check_ollama(self, emit_event: bool = True) -> dict:
        status = self.llm.check()
        self._ollama = status
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

    def pull_model(self, name: str) -> dict:
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
                self.emit("pull_progress", model=name, status=status, completed=completed, total=total, percent=pct)

            try:
                self.llm.pull(name, progress, cancel)
                if cancel.is_set():
                    self.emit("pull_done", model=name, ok=False, error="Download cancelled.")
                    return
                self.config.update({"model": name})
                self.emit("pull_done", model=name, ok=True, error=None)
                self.check_ollama()
                self.llm.warmup()
            except LLMError as exc:
                self.emit("pull_done", model=name, ok=False, error=str(exc))

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
        applied = self.config.update(changes)
        if {"model", "ollama_host"} & applied.keys():
            self._spawn(self.check_ollama, name="recheck")
        if {"stt_engine", "vosk_model_path", "whisper_model"} & applied.keys():
            self.emit("mic_status", **{**self._mic, "engine": self.stt.engine_label()})
        if "wake_word" in applied:
            self._spawn(self._sync_wake_word, name="wake-sync")
        self.emit("settings", **self.config.as_dict())
        return self.config.as_dict()

    def list_voices(self) -> list[dict]:
        return self.tts.list_voices()

    def preview_voice(self, voice: str | None = None) -> None:
        self.speak(f"Good day, {self.title}. This is how I will sound from now on.", voice=voice)
