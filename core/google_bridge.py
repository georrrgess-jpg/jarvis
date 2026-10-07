"""Client for the user's own Google Apps Script bridge (integrations/jarvis_google_bridge.gs).

The bridge runs inside the user's Google account, so editing Docs/Slides needs no API key,
no Cloud project and no payment: the user deploys the script once and pastes its URL here.
"""

from __future__ import annotations

import re
import secrets
from pathlib import Path

import httpx

from .config import resource_path

SCRIPT_URL = re.compile(r"^https://script\.google\.com/macros/s/[A-Za-z0-9_-]{20,}/exec$")
SETUP_URL = "https://script.google.com/home/projects/create"


class BridgeError(Exception):
    pass


def script_template() -> str:
    for candidate in (resource_path("integrations", "jarvis_google_bridge.gs"),
                      Path(__file__).resolve().parent.parent / "integrations" / "jarvis_google_bridge.gs"):
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    raise BridgeError("bridge script is missing from this build")


def parse_outline(text: str) -> list[dict]:
    """Turn a model-friendly outline into slides.

    Slides are separated by blank lines (or lines starting with "Slide N:"); the first line of
    each block is the title and the remaining lines are bullet points.
    """
    blocks, current = [], []
    for raw in str(text or "").replace("\r", "").split("\n"):
        line = raw.strip()
        header = re.match(r"^(?:slide\s*\d+\s*[:.\-]\s*)(.*)$", line, re.I)
        if not line or header:
            if current:
                blocks.append(current)
            current = [header.group(1)] if header and header.group(1) else []
            continue
        current.append(re.sub(r"^[-*•]\s*|^\d+[.)]\s*", "", line))
    if current:
        blocks.append(current)
    return [{"title": b[0], "body": b[1:]} for b in blocks if b]


class GoogleBridge:
    def __init__(self, config, http: httpx.Client | None = None) -> None:
        self._config = config
        self._http = http

    @property
    def configured(self) -> bool:
        return bool(SCRIPT_URL.match(self._config.get("google_script_url") or "")) and bool(self.token(create=False))

    def token(self, create: bool = True) -> str:
        token = self._config.get("google_bridge_token") or ""
        if not token and create:
            token = secrets.token_urlsafe(24)
            self._config.update({"google_bridge_token": token})
        return token

    def script_source(self) -> str:
        return script_template().replace("__JARVIS_TOKEN__", self.token())

    def _client(self) -> httpx.Client:
        if self._http is None:
            # Apps Script answers a POST with a 302 to script.googleusercontent.com, fetched with GET.
            self._http = httpx.Client(timeout=httpx.Timeout(45.0, connect=10.0), follow_redirects=True)
        return self._http

    def call(self, action: str, url: str | None = None, **params) -> dict:
        url = url or self._config.get("google_script_url") or ""
        if not SCRIPT_URL.match(url):
            raise BridgeError("Google Docs isn't connected yet: open Settings > Google Docs & Slides to set it up")
        payload = {"token": self.token(), "action": action, **{k: v for k, v in params.items() if v not in (None, "")}}
        try:
            response = self._client().post(url, json=payload, headers={"Content-Type": "text/plain;charset=utf-8"})
        except httpx.HTTPError as exc:
            raise BridgeError(f"couldn't reach your Google bridge ({exc.__class__.__name__})") from exc
        try:
            data = response.json()
        except ValueError:
            if "accounts.google.com" in str(response.url) or "<html" in response.text[:200].lower():
                raise BridgeError("Google asked for a sign-in: the deployment's access must be set to 'Anyone'") from None
            raise BridgeError(f"unexpected reply from the bridge (HTTP {response.status_code})") from None
        if not data.get("ok"):
            error = data.get("error") or "unknown error"
            if error == "unauthorized":
                error = "the bridge rejected JARVIS's token: re-copy the script from Settings and redeploy"
            raise BridgeError(error)
        return data

    def connect(self, url: str) -> dict:
        url = (url or "").strip()
        if not SCRIPT_URL.match(url):
            raise BridgeError("that doesn't look like a web app URL (it should end in /exec)")
        info = self.call("ping", url=url)
        self._config.update({"google_script_url": url})
        return info

    # ------------------------------------------------------------------ tool entry points
    def doc(self, action: str, document: str = "", text: str = "", find: str = "", title: str = "") -> dict:
        action = (action or "").lower().strip()
        if action == "create":
            return self.call("doc_create", title=title or document or "Untitled document", text=text)
        if action == "append":
            return self.call("doc_append", document=document, text=text)
        if action == "replace":
            return self.call("doc_replace", document=document, find=find, replace=text)
        if action == "read":
            return self.call("doc_read", document=document)
        if action == "list":
            return self.call("list", kind="docs", query=document)
        raise BridgeError("action must be one of create, append, replace, read, list")

    def slides(self, action: str, presentation: str = "", title: str = "", text: str = "") -> dict:
        action = (action or "").lower().strip()
        if action == "create":
            outline = parse_outline(text)
            return self.call("slides_create", title=title or presentation or "Untitled presentation", slides=outline)
        if action == "add":
            outline = parse_outline(text) or [{"title": title, "body": []}]
            result = {}
            for slide in outline:
                result = self.call("slides_add", presentation=presentation, title=slide["title"] or title, body=slide["body"])
            return result
        if action == "read":
            return self.call("slides_read", presentation=presentation)
        if action == "list":
            return self.call("list", kind="slides", query=presentation)
        raise BridgeError("action must be one of create, add, read, list")
