"""Local language model via Ollama (no API keys, nothing leaves the machine)."""

from __future__ import annotations

import logging
import os
import platform
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Callable, Iterator
from urllib.parse import urlparse

import httpx
import ollama

log = logging.getLogger("jarvis.llm")

DEFAULT_PULL_MODEL = "llama3.2"
PREFERRED_MODELS = ("llama3.2", "llama3.1", "llama3", "mistral", "qwen2.5", "qwen3", "gemma3", "gemma2", "phi3")
OLLAMA_DOWNLOAD_URL = "https://ollama.com/download"

PERSONA = """You are J.A.R.V.I.S. (Just A Rather Very Intelligent System), a sophisticated AI assistant with the calm precision and dry British wit of an impeccable butler. You run entirely on the user's own computer.

Guidelines:
- Address the user as "{title}" now and then, not in every sentence.
- Your replies are spoken aloud, so keep them brief and natural: usually one to three sentences. Give more detail only when asked.
- Do not use markdown, bullet points, headings or emoji unless the user asks for code, a list or a table.
- When you write code, put it in a fenced code block and keep the spoken explanation short.
- Be honest about your limits: you cannot browse the internet, see the screen or control devices. Never invent facts.

Context: it is {now}. The host computer is "{host}" running {os_name}."""


class LLMError(Exception):
    """Base error for language-model failures that should be shown to the user."""


class LLMConnectionError(LLMError):
    """Ollama is not reachable."""


class LLMModelError(LLMError):
    """Ollama is up but the requested model is missing."""


@dataclass
class OllamaStatus:
    online: bool
    host: str
    models: list[str] = field(default_factory=list)
    model: str | None = None
    error: str | None = None
    executable: str | None = None
    download_url: str = OLLAMA_DOWNLOAD_URL
    suggested_model: str = DEFAULT_PULL_MODEL

    def to_dict(self) -> dict:
        return asdict(self)


def find_ollama_executable() -> str | None:
    exe = shutil.which("ollama")
    if exe:
        return exe
    candidates: list[str] = []
    if sys.platform == "win32":
        for root in (os.environ.get("LOCALAPPDATA"), os.environ.get("ProgramFiles")):
            if root:
                candidates.append(os.path.join(root, "Programs", "Ollama", "ollama.exe"))
                candidates.append(os.path.join(root, "Ollama", "ollama.exe"))
    elif sys.platform == "darwin":
        candidates += ["/Applications/Ollama.app/Contents/Resources/ollama", "/opt/homebrew/bin/ollama", "/usr/local/bin/ollama"]
    else:
        candidates += ["/usr/local/bin/ollama", "/usr/bin/ollama"]
    return next((c for c in candidates if os.path.isfile(c)), None)


def _base_name(model: str) -> str:
    return model.split(":", 1)[0]


def resolve_model(wanted: str, available: list[str]) -> str | None:
    """Pick the installed model that best matches ``wanted`` (``llama3.2`` matches ``llama3.2:latest``)."""
    chat_models = [m for m in available if "embed" not in m.lower()]
    if not chat_models:
        return None
    wanted = (wanted or "").strip()
    if wanted:
        for m in chat_models:
            if m == wanted or m == f"{wanted}:latest":
                return m
        for m in chat_models:
            if _base_name(m) == _base_name(wanted):
                return m
    for preferred in PREFERRED_MODELS:
        for m in chat_models:
            if _base_name(m) == preferred:
                return m
    return chat_models[0]


def _is_local(host: str) -> bool:
    try:
        hostname = urlparse(host if "://" in host else f"http://{host}").hostname or ""
    except ValueError:
        return False
    return hostname in ("localhost", "127.0.0.1", "::1", "0.0.0.0")


class LLMEngine:
    def __init__(self, config, client_factory: Callable[..., ollama.Client] | None = None) -> None:
        self._config = config
        self._client_factory = client_factory or self._make_client
        self._history: list[dict] = []
        self._lock = threading.Lock()
        self.model: str | None = None
        self.online = False
        self.last_first_token_ms: int | None = None
        self.last_tokens_per_sec: float | None = None

    # ------------------------------------------------------------------ clients
    @property
    def host(self) -> str:
        return str(self._config.get("ollama_host") or "http://localhost:11434")

    def _make_client(self, timeout: httpx.Timeout | float | None) -> ollama.Client:
        # Never route localhost traffic through a system proxy.
        return ollama.Client(host=self.host, timeout=timeout, trust_env=not _is_local(self.host))

    # ------------------------------------------------------------------ status
    def check(self) -> OllamaStatus:
        status = OllamaStatus(online=False, host=self.host, executable=find_ollama_executable())
        client = self._client_factory(httpx.Timeout(4.0, connect=2.5))
        try:
            listing = client.list()
            names = []
            for m in getattr(listing, "models", None) or []:
                name = getattr(m, "model", None) or (m.get("model") or m.get("name") if isinstance(m, dict) else None)
                if name:
                    names.append(name)
            status.online = True
            status.models = sorted(names)
            status.model = resolve_model(self._config.get("model", DEFAULT_PULL_MODEL), status.models)
            if status.model is None:
                status.error = "No chat model is installed in Ollama."
        except (ConnectionError, httpx.TransportError) as exc:
            status.error = f"Ollama is not reachable at {self.host}."
            log.info("Ollama check failed: %s", exc)
        except ollama.ResponseError as exc:
            status.error = f"Ollama error: {exc.error}"
        except Exception as exc:  # pragma: no cover - defensive
            status.error = f"Unexpected Ollama error: {exc}"
            log.exception("Ollama check failed")
        finally:
            if hasattr(client, "close"):
                client.close()
        self.online = status.online
        self.model = status.model
        return status

    def warmup(self) -> None:
        """Load the model into memory so the first real question is answered quickly."""
        if not self.model:
            return
        client = self._client_factory(httpx.Timeout(180.0, connect=3.0))
        try:
            client.generate(model=self.model, prompt="", keep_alive="30m")
            log.info("Model %s preloaded", self.model)
        except Exception as exc:
            log.info("Model warmup skipped: %s", exc)
        finally:
            if hasattr(client, "close"):
                client.close()

    # ------------------------------------------------------------------ chat
    def system_prompt(self) -> str:
        prompt = PERSONA.format(
            title=self._config.get("user_title") or "sir",
            now=datetime.now().strftime("%A, %d %B %Y, %H:%M"),
            host=socket.gethostname(),
            os_name=f"{platform.system()} {platform.release()}",
        )
        extra = (self._config.get("custom_instructions") or "").strip()
        if extra:
            prompt += f"\n\nAdditional instructions from the user:\n{extra}"
        return prompt

    @property
    def history_turns(self) -> int:
        with self._lock:
            return len(self._history) // 2

    def clear_history(self) -> None:
        with self._lock:
            self._history.clear()

    def stream_reply(self, text: str, cancel: threading.Event | None = None) -> Iterator[str]:
        """Yield the reply token by token. The exchange is added to memory once it ends."""
        if not self.model:
            raise LLMModelError("No language model is selected.")
        cancel = cancel or threading.Event()
        with self._lock:
            history = list(self._history)
        messages = [{"role": "system", "content": self.system_prompt()}, *history, {"role": "user", "content": text}]

        client = self._client_factory(httpx.Timeout(300.0, connect=4.0))
        parts: list[str] = []
        started = time.monotonic()
        first: float | None = None
        chunks = 0
        try:
            stream = client.chat(
                model=self.model,
                messages=messages,
                stream=True,
                keep_alive="30m",
                options={"temperature": float(self._config.get("temperature", 0.7)), "num_ctx": 4096},
            )
            for chunk in stream:
                if cancel.is_set():
                    break
                piece = chunk["message"]["content"] or ""
                if piece:
                    if first is None:
                        first = time.monotonic()
                    chunks += 1
                    parts.append(piece)
                    yield piece
                if chunk.get("done"):
                    break
        except (ConnectionError, httpx.ConnectError) as exc:
            self.online = False
            raise LLMConnectionError(f"Lost connection to Ollama at {self.host}.") from exc
        except httpx.TransportError as exc:
            raise LLMConnectionError(f"Ollama stopped responding ({exc.__class__.__name__}).") from exc
        except ollama.ResponseError as exc:
            if exc.status_code == 404:
                raise LLMModelError(f"The model '{self.model}' is not installed. Run: ollama pull {self.model}") from exc
            raise LLMError(f"Ollama error: {exc.error}") from exc
        finally:
            if hasattr(client, "close"):
                client.close()
            if first is not None:
                self.last_first_token_ms = int((first - started) * 1000)
                gen_time = max(time.monotonic() - first, 1e-3)
                self.last_tokens_per_sec = round(chunks / gen_time, 1) if chunks > 1 else None
            reply = "".join(parts).strip()
            if reply:
                if cancel.is_set():
                    reply += " [interrupted]"
                self._remember(text, reply)

    def _remember(self, user: str, reply: str) -> None:
        limit = int(self._config.get("max_history_turns", 12)) * 2
        with self._lock:
            self._history += [{"role": "user", "content": user}, {"role": "assistant", "content": reply}]
            del self._history[:-limit]

    # ------------------------------------------------------------------ management
    def pull(self, name: str, on_progress: Callable[[str, int, int], None], cancel: threading.Event) -> None:
        client = self._client_factory(httpx.Timeout(None, connect=4.0))
        try:
            for progress in client.pull(name, stream=True):
                if cancel.is_set():
                    break
                on_progress(progress.get("status") or "", int(progress.get("completed") or 0), int(progress.get("total") or 0))
        except (ConnectionError, httpx.TransportError) as exc:
            raise LLMConnectionError(f"Lost connection to Ollama at {self.host}.") from exc
        except ollama.ResponseError as exc:
            raise LLMError(f"Could not download '{name}': {exc.error}") from exc
        finally:
            if hasattr(client, "close"):
                client.close()

    def start_server(self) -> bool:
        """Launch a locally installed Ollama in the background. Returns False if it isn't installed."""
        exe = find_ollama_executable()
        if not exe:
            return False
        args = [exe, "serve"]
        kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if sys.platform == "win32":
            tray = os.path.join(os.path.dirname(exe), "ollama app.exe")
            if os.path.isfile(tray):  # the desktop app manages the server and its tray icon
                args = [tray]
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        try:
            subprocess.Popen(args, **kwargs)
            log.info("Started Ollama: %s", args)
            return True
        except OSError as exc:
            log.error("Could not start Ollama: %s", exc)
            return False
