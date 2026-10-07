"""Text-to-speech with Microsoft Edge's free neural voices (edge-tts, no API key).

Replies are spoken sentence by sentence while the model is still writing, so
JARVIS starts talking within a second instead of waiting for the full answer.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import logging
import os
import re
import threading
from pathlib import Path

log = logging.getLogger("jarvis.tts")

DEFAULT_VOICE = "en-GB-RyanNeural"

# Shown when the online voice list can't be fetched.
FALLBACK_VOICES = [
    ("en-GB-RyanNeural", "en-GB", "Male"),
    ("en-GB-ThomasNeural", "en-GB", "Male"),
    ("en-GB-SoniaNeural", "en-GB", "Female"),
    ("en-GB-LibbyNeural", "en-GB", "Female"),
    ("en-US-AndrewNeural", "en-US", "Male"),
    ("en-US-ChristopherNeural", "en-US", "Male"),
    ("en-US-GuyNeural", "en-US", "Male"),
    ("en-US-AriaNeural", "en-US", "Female"),
    ("en-US-JennyNeural", "en-US", "Female"),
    ("en-AU-WilliamNeural", "en-AU", "Male"),
    ("en-IE-ConnorNeural", "en-IE", "Male"),
    ("en-IN-PrabhatNeural", "en-IN", "Male"),
]

CODE_MARK = "\x00code\x00"  # emitted by the splitter where a code block was skipped

_ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc", "e.g", "i.e", "approx",
    "no", "fig", "inc", "ltd", "co", "mt", "dept", "est", "u.s", "u.k", "a.m", "p.m",
}
_END = re.compile(r"[.!?…]+[\"'”’)\]]*(?=\s)|\n")
_SOFT_BREAK = re.compile(r"[,;:—]\s|\s[-–]\s")
_EMOJI = re.compile(
    "[\U0001f000-\U0001faff\U00002700-\U000027bf\U0001f900-\U0001f9ff\U00002600-\U000026ff️‍]+"
)


def format_rate(percent: float) -> str:
    return f"{int(round(percent)):+d}%"


def format_pitch(hz: float) -> str:
    return f"{int(round(hz)):+d}Hz"


def clean_for_speech(text: str) -> str:
    """Strip markdown, code and symbols that sound wrong when read aloud."""
    t = re.sub(r"```.*?(?:```|$)", " ", text, flags=re.S)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"https?://\S+", "the link on screen", t)
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)
    t = re.sub(r"^\s*>\s?", "", t, flags=re.M)
    t = re.sub(r"^\s*(?:[-*+•]|\d+[.)])\s+", "", t, flags=re.M)
    t = re.sub(r"(\*\*|__|\*|~~)(?=\S)(.+?)(?<=\S)\1", r"\2", t)
    t = re.sub(r"\bJ\.A\.R\.V\.I\.S\.?", "Jarvis", t)
    t = re.sub(r"\be\.g\.", "for example", t, flags=re.I)
    t = re.sub(r"\bi\.e\.", "that is", t, flags=re.I)
    t = _EMOJI.sub("", t)
    t = re.sub(r"[*#|~`]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


class SentenceSplitter:
    """Turns a token stream into speakable sentences, skipping fenced code blocks."""

    def __init__(self, min_chars: int = 10, soft_limit: int = 220) -> None:
        self.min_chars = min_chars
        self.soft_limit = soft_limit
        self._buf = ""
        self._in_code = False
        self._code_seen = False

    def feed(self, text: str) -> list[str]:
        self._buf += text
        return self._drain(final=False)

    def flush(self) -> list[str]:
        out = self._drain(final=True)
        rest = self._buf.strip()
        self._buf = ""
        if rest and not self._in_code:
            out.append(rest)
        return out

    def _drain(self, final: bool) -> list[str]:
        out: list[str] = []
        while True:
            if self._in_code:
                end = self._buf.find("```")
                if end < 0:
                    self._buf = "" if final else self._buf[-2:]  # keep a split closing fence
                    return out
                self._buf = self._buf[end + 3 :]
                self._in_code = False
                if not self._code_seen:
                    self._code_seen = True
                    out.append(CODE_MARK)
                continue
            fence = self._buf.find("```")
            head = self._buf if fence < 0 else self._buf[:fence]
            sentences, rest = self._split(head)
            out.extend(sentences)
            if fence >= 0:
                if rest.strip():
                    out.append(rest.strip())
                self._buf = self._buf[fence + 3 :]
                self._in_code = True
                continue
            self._buf = rest
            return out

    def _split(self, text: str) -> tuple[list[str], str]:
        out: list[str] = []
        start = 0
        for m in _END.finditer(text):
            if m.group() != "\n" and not self._is_boundary(text, m.start()):
                continue
            sentence = text[start : m.end()].strip()
            if len(sentence) < self.min_chars:
                continue  # merge tiny fragments ("Yes.") with what follows
            out.append(sentence)
            start = m.end()
        rest = text[start:]
        while len(rest) > self.soft_limit:
            cut = None
            for b in _SOFT_BREAK.finditer(rest, 60, self.soft_limit):
                cut = b.end()
            if cut is None:
                space = rest.rfind(" ", 60, self.soft_limit)
                cut = space + 1 if space > 0 else self.soft_limit
            out.append(rest[:cut].strip())
            rest = rest[cut:]
        return out, rest

    @staticmethod
    def _is_boundary(text: str, idx: int) -> bool:
        if text[idx] != ".":
            return True
        m = re.search(r"(\S+)$", text[:idx])
        if not m:
            return True
        word = m.group(1)
        if word.lower().lstrip("(\"'") in _ABBREVIATIONS:
            return False
        if len(word) == 1 and word.isalpha() and word.isupper():
            return False  # an initial, as in "J. R. R. Tolkien"
        line_start = m.start() == 0 or text[m.start() - 1] == "\n"
        if word.isdigit() and line_start:
            return False  # "1. First item"
        return True


class TTSError(Exception):
    pass


def _describe(exc: BaseException) -> str:
    name = exc.__class__.__name__
    if "NoAudioReceived" in name:
        return "The voice service returned no audio (is the voice name valid?)."
    if "Connector" in name or "Connection" in name or isinstance(exc, OSError):
        return "Cannot reach Microsoft's voice service. Check your internet connection."
    if isinstance(exc, (asyncio.TimeoutError, concurrent.futures.TimeoutError)):
        return "The voice service timed out."
    return f"Voice synthesis failed ({name})."


class EdgeTTS:
    """Runs edge-tts on a private asyncio loop so callers can use it synchronously from any thread."""

    CACHE_MAX_FILES = 300
    CACHE_MAX_CHARS = 160

    def __init__(self, config, cache_dir: Path | None = None) -> None:
        self._config = config
        self._cache_dir = Path(cache_dir) if cache_dir else None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._voices: list[dict] | None = None

    # ---------------------------------------------------------------- event loop
    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None or not self._loop.is_running():
                self._loop = asyncio.new_event_loop()
                ready = threading.Event()

                def run(loop=self._loop):
                    asyncio.set_event_loop(loop)
                    loop.call_soon(ready.set)
                    loop.run_forever()

                self._thread = threading.Thread(target=run, name="tts-loop", daemon=True)
                self._thread.start()
                ready.wait(5)
            return self._loop

    def _run(self, coro, timeout: float):
        future = asyncio.run_coroutine_threadsafe(coro, self._ensure_loop())
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise TTSError(_describe(exc)) from exc

    @staticmethod
    def _proxy() -> str | None:
        return os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or None

    # ---------------------------------------------------------------- synthesis
    def synthesize(self, text: str, voice: str | None = None, rate: float | None = None,
                   pitch: float | None = None, timeout: float = 30.0) -> bytes:
        """Return MP3 bytes for ``text``."""
        text = text.strip()
        if not text:
            raise TTSError("Nothing to say.")
        voice = voice or self._config.get("voice") or DEFAULT_VOICE
        rate_s = format_rate(self._config.get("speech_rate", 0) if rate is None else rate)
        pitch_s = format_pitch(self._config.get("speech_pitch", 0) if pitch is None else pitch)

        cache_file = self._cache_file(text, voice, rate_s, pitch_s)
        if cache_file and cache_file.exists():
            try:
                return cache_file.read_bytes()
            except OSError:
                pass
        try:
            data = self._run(self._synthesize(text, voice, rate_s, pitch_s), timeout)
        except TTSError:
            raise
        except Exception as exc:
            raise TTSError(_describe(exc)) from exc
        if cache_file:
            self._cache_store(cache_file, data)
        return data

    async def _synthesize(self, text: str, voice: str, rate: str, pitch: str) -> bytes:
        import edge_tts

        communicate = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch, proxy=self._proxy())
        audio = bytearray()
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio" and chunk.get("data"):
                audio.extend(chunk["data"])
        if not audio:
            raise TTSError("The voice service returned no audio.")
        return bytes(audio)

    # ---------------------------------------------------------------- voices
    def list_voices(self) -> list[dict]:
        if self._voices is None:
            try:
                import edge_tts

                raw = self._run(edge_tts.list_voices(proxy=self._proxy()), 15.0)
                voices = [
                    (v["ShortName"], v.get("Locale", ""), v.get("Gender", ""))
                    for v in raw
                    if str(v.get("Locale", "")).startswith("en-")
                ]
                self._voices = self._format_voices(voices) if voices else None
            except Exception as exc:
                log.info("Voice list unavailable, using built-in list: %s", exc)
            if self._voices is None:
                return self._format_voices(FALLBACK_VOICES)
        return self._voices

    @staticmethod
    def _format_voices(voices) -> list[dict]:
        def rank(v):
            name, locale, _ = v
            return (name not in ("en-GB-RyanNeural", "en-GB-ThomasNeural"), locale != "en-GB", locale, name)

        out = []
        for name, locale, gender in sorted(set(voices), key=rank):
            short = name.split("-")[-1].replace("Neural", "").replace("Multilingual", " Multi")
            out.append({"id": name, "label": f"{short} · {locale} · {gender}", "locale": locale, "gender": gender})
        return out

    # ---------------------------------------------------------------- cache
    def _cache_file(self, text: str, voice: str, rate: str, pitch: str) -> Path | None:
        if not self._cache_dir or len(text) > self.CACHE_MAX_CHARS:
            return None
        digest = hashlib.sha1(f"{voice}|{rate}|{pitch}|{text}".encode("utf-8")).hexdigest()
        return self._cache_dir / f"{digest}.mp3"

    def _cache_store(self, path: Path, data: bytes) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            files = sorted(path.parent.glob("*.mp3"), key=lambda p: p.stat().st_mtime)
            for old in files[: max(0, len(files) - self.CACHE_MAX_FILES)]:
                old.unlink(missing_ok=True)
        except OSError:
            log.debug("TTS cache write failed", exc_info=True)

    def close(self) -> None:
        with self._lock:
            if self._loop is not None and self._loop.is_running():
                self._loop.call_soon_threadsafe(self._loop.stop)
            self._loop = None
