"""Client for the user's own Google Apps Script bridge (integrations/jarvis_google_bridge.gs).

The bridge runs inside the user's Google account, so editing Docs/Slides needs no API key,
no Cloud project and no payment: the user deploys the script once and pastes its URL here.
"""

from __future__ import annotations

import csv
import io
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


def script_version() -> int:
    """BRIDGE_VERSION of the script bundled with this build."""
    match = re.search(r"var BRIDGE_VERSION = (\d+);", script_template())
    return int(match.group(1)) if match else 1


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


_NUMBER = re.compile(r"^-?(?:[1-9]\d{0,2}(?:,\d{3})+|[1-9]\d*|0)(?:\.\d+)?$")  # no leading zeros: keeps 007 / zip codes as text
UPDATE_HINT = ("your Google bridge script is out of date: open Settings > Google Docs & Slides, copy the script again, "
               "paste it over the old one in Apps Script, then Deploy > Manage deployments > Edit > Version: New version > Deploy")


def _cell(value: str):
    value = value.strip()
    if _NUMBER.match(value):
        number = float(value.replace(",", ""))
        return int(number) if number.is_integer() and "." not in value else number
    return value


def parse_rows(text) -> list[list]:
    """Rows for a spreadsheet from what a model writes: a Markdown table, tab- or comma-separated lines,
    or already-structured lists. Numbers become numbers so Sheets can add them up."""
    if isinstance(text, list):
        return [[_cell(str(c)) if isinstance(c, str) else c for c in (r if isinstance(r, list) else [r])] for r in text]
    rows, text = [], str(text or "").replace("\r", "")
    if "\n" not in text and "\\n" in text:
        text = text.replace("\\n", "\n")  # the model escaped its newlines
    for raw in text.split("\n"):
        line = raw.strip()
        if not line or re.fullmatch(r"\|?[\s:|-]+\|?", line) and "-" in line:
            continue  # blank line or a Markdown table separator like |---|---|
        if "|" in line:
            cells = line.strip("|").split("|")
        elif "\t" in line:
            cells = line.split("\t")
        else:
            cells = next(csv.reader(io.StringIO(line), skipinitialspace=True))
        rows.append([_cell(re.sub(r"\*\*(.+?)\*\*", r"\1", c)) for c in cells])
    return rows


class GoogleBridge:
    def __init__(self, config, http: httpx.Client | None = None) -> None:
        self._config = config
        self._http = http

    @property
    def configured(self) -> bool:
        return bool(SCRIPT_URL.match(self._config.get("google_script_url") or "")) and bool(self.token(create=False))

    @property
    def outdated(self) -> bool:
        """True when the deployed script predates the one in this build (new actions would fail)."""
        try:
            return self.configured and int(self._config.get("google_bridge_version") or 0) < script_version()
        except BridgeError:
            return False

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
            elif error.startswith("unknown action"):
                self._config.update({"google_bridge_version": 1})
                error = UPDATE_HINT
            raise BridgeError(error)
        return data

    def connect(self, url: str) -> dict:
        url = (url or "").strip()
        if not SCRIPT_URL.match(url):
            raise BridgeError("that doesn't look like a web app URL (it should end in /exec)")
        info = self.call("ping", url=url)
        version = info.get("version")
        self._config.update({"google_script_url": url, "google_user_email": str(info.get("user") or ""),
                             "google_bridge_version": version if isinstance(version, int) and version > 0 else 1})
        return info

    # ------------------------------------------------------------------ tool entry points
    def doc(self, action: str, document: str = "", text: str = "", find: str = "", title: str = "") -> dict:
        action = (action or "").lower().strip()
        if action == "create":
            return self.call("doc_create", title=title or document or "Untitled document", text=text)
        if action == "append":
            return self.call("doc_append", document=document, text=text)
        if action == "rewrite":
            return self.call("doc_rewrite", document=document, text=text)
        if action == "replace":
            return self.call("doc_replace", document=document, find=find, replace=text)
        if action == "read":
            return self.call("doc_read", document=document)
        if action == "list":
            return self.call("list", kind="docs", query=document)
        raise BridgeError("action must be one of create, append, rewrite, replace, read, list")

    def slides(self, action: str, presentation: str = "", title: str = "", text: str = "",
               number=None, to=None, find: str = "") -> dict:
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
        if action == "replace":
            return self.call("slides_replace", presentation=presentation, find=find, replace=text)
        if action in ("delete", "move", "edit"):
            number = _slide_number(number)
            if action == "delete":
                return self.call("slides_delete", presentation=presentation, number=number)
            if action == "move":
                return self.call("slides_move", presentation=presentation, number=number, to=_slide_number(to, "to"))
            lines = [re.sub(r"^[-*•]\s*|^\d+[.)]\s*", "", ln.strip()) for ln in str(text or "").replace("\r", "").split("\n")]
            return self.call("slides_edit", presentation=presentation, number=number, title=title,
                             body=[ln for ln in lines if ln] or None)
        raise BridgeError("action must be one of create, add, edit, delete, move, replace, read, list")

    @property
    def can_email(self) -> bool:
        """The deployed script has the Gmail actions (v3+)."""
        return self.configured and int(self._config.get("google_bridge_version") or 0) >= 3

    def send_email(self, to: str, subject: str, body: str, cc: str = "") -> dict:
        return self.call("mail_send", to=to, subject=subject, body=body, cc=cc)

    def find_contacts(self, name: str) -> list[dict]:
        return list(self.call("contact_find", name=name).get("people") or [])

    def share_file(self, file_id: str, email: str, role: str = "view") -> dict:
        return self.call("share_file", file=file_id, email=email, role=role)

    def create_deck(self, title: str, subtitle: str, slides: list[dict]) -> dict:
        return self.call("slides_create", title=title or "Untitled presentation", subtitle=subtitle, slides=slides)

    def sheets(self, action: str, spreadsheet: str = "", title: str = "", text="", range: str = "", tab: str = "") -> dict:
        action = (action or "").lower().strip()
        if action == "create":
            return self.call("sheet_create", title=title or spreadsheet or "Untitled spreadsheet", rows=parse_rows(text))
        if action in ("append", "add"):
            rows = parse_rows(text)
            if not rows:
                raise BridgeError("give the rows to add in text, one row per line with cells separated by |")
            return self.call("sheet_append", spreadsheet=spreadsheet, tab=tab, rows=rows)
        if action in ("write", "update"):
            rows = parse_rows(text)
            if not rows:
                raise BridgeError("give the values to write in text")
            return self.call("sheet_write", spreadsheet=spreadsheet, tab=tab, range=_a1(range or "A1"), rows=rows)
        if action == "read":
            return self.call("sheet_read", spreadsheet=spreadsheet, tab=tab, range=_a1(range) if range else "")
        if action == "list":
            return self.call("list", kind="sheets", query=spreadsheet)
        raise BridgeError("action must be one of create, append, write, read, list")


def _slide_number(value, name: str = "number") -> int:
    try:
        n = int(str(value).strip().lstrip("#"))
    except (TypeError, ValueError):
        raise BridgeError(f"say which slide by its position ({name}=1 for the first slide)") from None
    if n < 1:
        raise BridgeError(f"{name} must be 1 or more")
    return n


def _a1(ref: str) -> str:
    ref = str(ref or "").strip().upper().replace(" ", "")
    if not re.fullmatch(r"[A-Z]{1,3}\d{1,7}(?::[A-Z]{1,3}\d{1,7})?", ref):
        raise BridgeError(f'"{ref}" isn\'t a cell range like B2 or A1:C10')
    return ref
