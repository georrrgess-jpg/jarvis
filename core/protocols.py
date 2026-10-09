"""Protocols: named lists of commands made once and run by name.

    "Create a protocol called Morning: open Spotify, then what's the weather, then open Gmail"
    "Jarvis, run Morning"        "Initiate the house party protocol"        "Stop the protocol"

A step is anything you'd say to JARVIS ("open Spotify", "set the volume to 30", "what's on my
calendar"), plus two extras that only make sense in a list: ``wait 10 seconds`` and ``say <words>``.
A protocol can also run itself on a schedule ("every weekday at 7:30").

This module stores protocols (in the settings file) and understands the spoken commands about them;
the assistant runs the steps.
"""

from __future__ import annotations

import difflib
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

MAX_PROTOCOLS = 60
MAX_STEPS = 40
MAX_STEP_CHARS = 300
MAX_NAME_CHARS = 40
MAX_WAIT_SECONDS = 3600
DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


class ProtocolError(ValueError):
    pass


ICONS = ("bolt", "sun", "moon", "game", "code", "music", "film", "focus", "work", "home", "coffee", "rocket", "party", "book", "heart", "shield")
CATEGORIES = ("General", "Morning", "Work", "Development", "Gaming", "Entertainment", "Focus", "Home", "Evening")
FAILURE_POLICIES = ("stop", "continue")
MAX_HISTORY = 12


@dataclass
class ProtoStep:
    """One step: what to say to JARVIS, plus how to run it."""

    text: str
    when: dict = field(default_factory=dict)  # condition: {"type": "app_running"|"app_not_running"|"time"|"weekday"|"previous", ...}
    confirm: bool = False  # ask "shall I go ahead?" before this step
    approved: bool = False  # the user ticked "don't ask" for a risky step in the editor
    continue_on_error: bool | None = None  # None = the protocol's policy
    retries: int = 0
    timeout: float = 90.0
    fallback: str = ""  # tried if the step fails ("open Spotify, but use YouTube if Spotify isn't available")
    enabled: bool = True

    def to_dict(self) -> dict:
        d = {"text": self.text}
        if self.when:
            d["when"] = dict(self.when)
        for key, default in (("confirm", False), ("approved", False), ("continue_on_error", None), ("retries", 0),
                             ("timeout", 90.0), ("fallback", ""), ("enabled", True)):
            value = getattr(self, key)
            if value != default:
                d[key] = value
        return d

    @classmethod
    def from_any(cls, raw) -> "ProtoStep | None":
        if isinstance(raw, str):
            text = raw.strip()[:MAX_STEP_CHARS]
            return cls(text) if text else None
        if not isinstance(raw, dict):
            return None
        text = str(raw.get("text") or "").strip()[:MAX_STEP_CHARS]
        if not text:
            return None
        coe = raw.get("continue_on_error")
        return cls(text, when=_clean_condition(raw.get("when")), confirm=bool(raw.get("confirm")), approved=bool(raw.get("approved")),
                   continue_on_error=None if coe is None else bool(coe), retries=max(0, min(3, int(raw.get("retries") or 0))),
                   timeout=max(1.0, min(600.0, float(raw.get("timeout") or 90.0))), fallback=str(raw.get("fallback") or "")[:MAX_STEP_CHARS],
                   enabled=raw.get("enabled", True) is not False)


def _clean_condition(raw) -> dict:
    if not isinstance(raw, dict) or raw.get("type") not in ("app_running", "app_not_running", "time", "weekday", "previous"):
        return {}
    kind = raw["type"]
    if kind in ("app_running", "app_not_running"):
        app = str(raw.get("app") or "").strip()[:60]
        return {"type": kind, "app": app} if app else {}
    if kind == "time":
        out = {"type": "time"}
        for k in ("after", "before"):
            m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(raw.get(k) or "").strip())
            if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
                out[k] = f"{int(m.group(1)):02d}:{m.group(2)}"
        return out if len(out) > 1 else {}
    if kind == "weekday":
        days = sorted({int(d) for d in raw.get("days") or [] if str(d).isdigit() and 0 <= int(d) <= 6})
        return {"type": "weekday", "days": days} if days else {}
    return {"type": "previous", "ok": raw.get("ok", True) is not False}


@dataclass
class Protocol:
    id: str
    name: str
    steps: list[ProtoStep] = field(default_factory=list)
    schedule: dict = field(default_factory=dict)  # {"time": "07:30", "days": [0..6], "enabled": True}
    created: float = 0.0
    last_run: float = 0.0
    description: str = ""
    icon: str = "bolt"
    category: str = "General"
    phrases: list[str] = field(default_factory=list)  # extra ways to start it: "activate gaming mode", "game time"
    enabled: bool = True
    on_failure: str = "stop"  # stop | continue (for steps that don't say)
    triggers: dict = field(default_factory=dict)  # {"startup": bool, "app": "Steam", "hotkey": "Ctrl+Alt+G"}
    history: list[dict] = field(default_factory=list)  # the last runs: {at, status, trigger, seconds, steps: [{text, status, detail}]}

    @property
    def texts(self) -> list[str]:
        return [s.text for s in self.steps if s.enabled]

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "steps": [s.to_dict() for s in self.steps], "schedule": dict(self.schedule),
                "created": self.created, "last_run": self.last_run, "description": self.description, "icon": self.icon,
                "category": self.category, "phrases": list(self.phrases), "enabled": self.enabled, "on_failure": self.on_failure,
                "triggers": dict(self.triggers), "history": [dict(h) for h in self.history[-MAX_HISTORY:]], "version": 2}

    @classmethod
    def from_dict(cls, d: dict) -> "Protocol | None":
        """Reads both the original format (steps as plain text) and the current one."""
        try:
            steps = [s for s in (ProtoStep.from_any(x) for x in d.get("steps") or []) if s][:MAX_STEPS]
            name = clean_name(str(d.get("name") or ""))
            if not name:
                return None
            phrases = [p for p in (clean_phrase(x) for x in d.get("phrases") or []) if p][:8]
            icon = str(d.get("icon") or "bolt")
            category = str(d.get("category") or "General")
            return cls(id=str(d.get("id") or _new_id()), name=name, steps=steps, schedule=normalise_schedule(d.get("schedule")),
                       created=float(d.get("created") or 0), last_run=float(d.get("last_run") or 0),
                       description=str(d.get("description") or "")[:300], icon=icon if icon in ICONS else "bolt",
                       category=category if category in CATEGORIES else "General", phrases=phrases,
                       enabled=d.get("enabled", True) is not False,
                       on_failure=d.get("on_failure") if d.get("on_failure") in FAILURE_POLICIES else "stop",
                       triggers=normalise_triggers(d.get("triggers")),
                       history=[h for h in (d.get("history") or []) if isinstance(h, dict)][-MAX_HISTORY:])
        except (TypeError, ValueError):
            return None

    def describe_schedule(self) -> str:
        return describe_schedule(self.schedule)


def clean_phrase(text) -> str:
    t = re.sub(r"\s+", " ", str(text or "").strip(" \"'“”.,!?")).lower()
    return t[:80] if len(t) >= 3 else ""


def normalise_triggers(raw) -> dict:
    if not isinstance(raw, dict):
        return {}
    out = {}
    if raw.get("startup"):
        out["startup"] = True
    app = str(raw.get("app") or "").strip()[:60]
    if app:
        out["app"] = app
    hotkey = normalise_hotkey(raw.get("hotkey"))
    if hotkey:
        out["hotkey"] = hotkey
    return out


_MODS = {"ctrl": "Ctrl", "control": "Ctrl", "alt": "Alt", "shift": "Shift", "win": "Win", "windows": "Win"}


def normalise_hotkey(raw) -> str:
    """'ctrl + alt + g' -> 'Ctrl+Alt+G' (needs at least one of Ctrl / Alt / Win and one key)."""
    parts = [p.strip().lower() for p in str(raw or "").replace("-", "+").split("+") if p.strip()]
    mods = []
    key = ""
    for p in parts:
        if p in _MODS:
            if _MODS[p] not in mods:
                mods.append(_MODS[p])
        elif re.fullmatch(r"[a-z0-9]|f(?:[1-9]|1[0-2])", p) and not key:
            key = p.upper()
        else:
            return ""
    if not key or not ({"Ctrl", "Alt", "Win"} & set(mods)):
        return ""
    order = [m for m in ("Ctrl", "Alt", "Shift", "Win") if m in mods]
    return "+".join(order + [key])


def _new_id() -> str:
    return "p" + uuid.uuid4().hex[:10]


def clean_name(name: str) -> str:
    n = re.sub(r"\s+", " ", (name or "").strip(" \"'“”‘’.,:;!?-"))
    n = re.sub(r"^(?:the|my|a|an)\s+", "", n, flags=re.I)
    n = re.sub(r"\s+protocol$", "", n, flags=re.I).strip()
    if not n:
        return ""
    n = n[:MAX_NAME_CHARS]
    return n if any(c.isupper() for c in n) else n.title()


def _key(name: str) -> str:
    """How names are compared: 'The House-Party protocol' == 'house party'."""
    k = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower())
    k = re.sub(r"\b(?:the|my|a|an|protocol)\b", " ", k)
    return re.sub(r"\s+", " ", k).strip()


# ----------------------------------------------------------------------------- schedules
def normalise_schedule(raw) -> dict:
    if not isinstance(raw, dict) or not raw.get("time"):
        return {}
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(raw.get("time")).strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return {}
    days = sorted({int(d) for d in raw.get("days") or range(7) if str(d).lstrip("-").isdigit() and 0 <= int(d) <= 6})
    return {"time": f"{int(m.group(1)):02d}:{m.group(2)}", "days": days or list(range(7)), "enabled": bool(raw.get("enabled", True))}


def describe_schedule(schedule: dict) -> str:
    if not schedule or not schedule.get("time"):
        return ""
    h, m = (int(x) for x in schedule["time"].split(":"))
    clock = f"{h % 12 or 12}{':%02d' % m if m else ''} {'AM' if h < 12 else 'PM'}"
    days = schedule.get("days") or list(range(7))
    if len(days) == 7:
        when = "every day"
    elif days == [0, 1, 2, 3, 4]:
        when = "on weekdays"
    elif days == [5, 6]:
        when = "at weekends"
    else:
        when = "on " + _join([DAY_NAMES[d] + "s" for d in days])
    return f"{when} at {clock}"


def due(schedule: dict, now: datetime, last_run: float) -> bool:
    """Is a scheduled run due at ``now`` (checked about every 20 seconds; runs at most once a minute slot)?"""
    if not schedule or not schedule.get("enabled", True) or not schedule.get("time"):
        return False
    if now.weekday() not in (schedule.get("days") or range(7)):
        return False
    h, m = (int(x) for x in schedule["time"].split(":"))
    slot = now.replace(hour=h, minute=m, second=0, microsecond=0)
    seconds = (now - slot).total_seconds()
    return 0 <= seconds < 120 and last_run < slot.timestamp()


_DAYS = {"monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2, "thursday": 3, "thu": 3, "thurs": 3,
         "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6}


def parse_schedule(text: str) -> dict | None:
    """'every weekday at 7:30 am' / 'at 6pm on Fridays' / 'daily at 07:00' -> {"time": "07:30", "days": [...]}."""
    t = (text or "").lower()
    clock = (re.search(r"\bat\s+(\d{1,2})(?:[:.](\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?(?![\w:])", t)
             or re.search(r"\b(\d{1,2})(?:[:.](\d{2}))?\s*(a\.?m\.?|p\.?m\.?)(?!\w)", t)
             or re.search(r"\b(\d{1,2})[:.](\d{2})()(?!\w)", t))
    if not clock:
        if re.search(r"\bnoon\b|\bmidday\b", t):
            hour, minute = 12, 0
        elif re.search(r"\bmidnight\b", t):
            hour, minute = 0, 0
        else:
            return None
    else:
        hour, minute = int(clock.group(1)), int(clock.group(2) or 0)
        ampm = (clock.group(3) or "").replace(".", "")
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        elif not ampm and re.search(r"\b(?:evening|tonight|night|afternoon)\b", t) and hour < 12:
            hour += 12
        if hour > 23 or minute > 59:
            return None
    if re.search(r"\bweekdays?\b|\bwork ?days?\b|monday (?:to|through|-) friday", t):
        days = [0, 1, 2, 3, 4]
    elif re.search(r"\bweekends?\b", t):
        days = [5, 6]
    else:
        days = sorted({d for w, d in _DAYS.items() if re.search(rf"\b{w}s?\b", t)}) or list(range(7))
    return {"time": f"{hour:02d}:{minute:02d}", "days": days, "enabled": True}


# ----------------------------------------------------------------------------- steps
_VERB_START = (r"(?:open|close|launch|start|run|play|pause|resume|skip|stop|check|set|search|google|look|find|what|what's|whats|how|"
               r"when|where|who|is|are|tell|turn|mute|unmute|show|take|lock|send|email|write|type|wait|say|announce|remind|read|"
               r"switch|minimi[sz]e|maximi[sz]e|create|make|give|go|put|move|volume|next|previous|new|add|draft|summari[sz]e|"
               r"translate|calculate|flip|roll|pick|do|rename|clear|pull|bring|navigate|visit|watch|listen|shuffle|increase|"
               r"decrease|raise|lower|reopen|empty|delete)\b")
_SPLIT = re.compile(r"\s*(?:\n+|;+|,?\s*\b(?:and\s+)?then\b,?|,?\s*\bafter\s+that\b,?|,?\s*\bnext\b,|,?\s*\bfinally\b,?)\s*", re.I)

_NUMBERING = re.compile(r"^\s*(?:(?:step\s+)?\d+[.):]\s*|[-*•]\s+|first(?:ly)?,?\s+|second(?:ly)?,?\s+|third(?:ly)?,?\s+|lastly,?\s+|finally,?\s+)", re.I)
_THIRD_PERSON = {"does": "do", "goes": "go", "has": "have", "plays": "play", "says": "say", "searches": "search", "sets": "set",
                 "checks": "check", "opens": "open", "closes": "close", "launches": "launch", "starts": "start",
                 "tells": "tell", "turns": "turn", "mutes": "mute", "shows": "show", "takes": "take", "locks": "lock",
                 "sends": "send", "emails": "email", "writes": "write", "types": "type", "waits": "wait", "reads": "read",
                 "switches": "switch", "runs": "run", "gives": "give", "puts": "put", "pauses": "pause", "skips": "skip",
                 "minimises": "minimise", "minimizes": "minimize", "creates": "create", "makes": "make", "lowers": "lower",
                 "raises": "raise", "reminds": "remind", "announces": "announce", "unmutes": "unmute", "searches for": "search for"}

_SOFT_SPLIT = re.compile(r"\s*(?:,\s*(?:and\s+)?|\s+and\s+)(?=" + _VERB_START + "|(?:" + "|".join(_THIRD_PERSON) + r")\b)", re.I)


_CONDITION_ONLY = re.compile(r"^(?:if|when|only\s+if)\s+[\w .'-]+?\s+(?:is|isn'?t|is\s+not)\s+(?:open|running|on)$|^(?:only\s+)?on\s+(?:weekdays|weekends|\w+days?)$", re.I)


def split_steps(text: str) -> list[str]:
    """'open Spotify, then check the weather and open Gmail; wait 5 seconds' -> 3 or 4 steps."""
    steps = []
    merged = []
    for piece in _split_pieces(text):
        if merged and _CONDITION_ONLY.match(merged[-1]):
            merged[-1] = merged[-1] + ", " + piece  # "if Spotify is open, pause the music" is one step
        else:
            merged.append(piece)
    return merged


def _split_pieces(text: str) -> list[str]:
    steps = []
    for chunk in _SPLIT.split(text or ""):
        for piece in _SOFT_SPLIT.split(chunk or ""):
            piece = _NUMBERING.sub("", piece or "").strip(" ,.;")
            if not piece:
                continue
            first, _, rest = piece.partition(" ")
            if first.lower() in _THIRD_PERSON:  # "a protocol that opens Spotify and checks the weather"
                piece = (_THIRD_PERSON[first.lower()] + (" " + rest if rest else "")).strip()
            steps.append(piece[:MAX_STEP_CHARS])
    return steps


@dataclass
class Step:
    kind: str  # "wait" | "say" | "command"
    text: str = ""
    seconds: float = 0.0


_WAIT = re.compile(r"^(?:wait|pause|hold on|sleep|delay)(?:\s+(?:for|about))?(?:\s+(?P<dur>.+?))?[\s.!]*$", re.I)
_SAY = re.compile(r"^(?:say|announce|speak)\s*:?\s+(?P<what>.+)$", re.I)


def step_kind(step: str) -> Step:
    from .quick import parse_duration

    s = (step or "").strip()
    w = _WAIT.match(s)
    if w:
        dur = (w.group("dur") or "").strip()
        if not dur:
            return Step("wait", s, 3.0)
        if re.fullmatch(r"(?:a\s+)?(?:moment|second|sec|bit|little|little bit)", dur, re.I):
            return Step("wait", s, 3.0 if "moment" in dur or "bit" in dur else 1.0)
        n = re.fullmatch(r"(\d+(?:\.\d+)?)", dur)
        seconds = float(n.group(1)) if n else parse_duration(dur)
        if seconds:
            return Step("wait", s, float(min(MAX_WAIT_SECONDS, seconds)))
    sm = _SAY.match(s)
    if sm and not re.match(r"something\b|a joke\b|hi\b|hello\b", sm.group("what").strip(), re.I):
        return Step("say", sm.group("what").strip().strip("\"“”'"))
    return Step("command", s)


def describe_step(step: str) -> str:
    k = step_kind(step)
    if k.kind == "wait":
        from .quick import describe_duration
        return f"wait {describe_duration(int(k.seconds)) if k.seconds >= 1 else 'a moment'}"
    return step


# ----------------------------------------------------------------------------- the store
_META = ("description", "icon", "category", "phrases", "enabled", "on_failure", "triggers")


def _steps(raw) -> list[ProtoStep]:
    if isinstance(raw, str):
        raw = raw.splitlines()
    return [s for s in (ProtoStep.from_any(x) for x in raw or []) if s]


class ProtocolStore:
    """Protocols live in the settings file under "protocols" (a list of dicts)."""

    def __init__(self, config, backup_dir: Path | None = None) -> None:
        self.config = config
        self.backup_dir = backup_dir

    def all(self) -> list[Protocol]:
        out = []
        for raw in self.config.get("protocols") or []:
            p = Protocol.from_dict(raw) if isinstance(raw, dict) else None
            if p:
                out.append(p)
        return out

    def migrate(self) -> int:
        """Bring protocols saved by an older JARVIS up to date, keeping a copy of the originals first."""
        raw = [r for r in self.config.get("protocols") or [] if isinstance(r, dict)]
        old = [r for r in raw if r.get("version") != 2]
        if not old:
            return 0
        if self.backup_dir is not None:
            try:
                self.backup_dir.mkdir(parents=True, exist_ok=True)
                path = self.backup_dir / f"protocols-backup-{time.strftime('%Y%m%d-%H%M%S')}.json"
                path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
            except OSError as exc:
                raise ProtocolError(f"couldn't back up the protocols before updating them: {exc}") from exc
        self._write(self.all())
        return len(old)

    def _write(self, protocols: list[Protocol]) -> None:
        self.config.update({"protocols": [p.to_dict() for p in protocols]})

    def get(self, pid: str) -> Protocol | None:
        return next((p for p in self.all() if p.id == pid), None)

    def find(self, spoken: str) -> Protocol | None:
        """The protocol the user named, allowing for 'the', 'protocol', plurals and small mishearings."""
        want = _key(spoken)
        if not want:
            return None
        items = self.all()
        exact = next((p for p in items if _key(p.name) == want), None)
        if exact:
            return exact
        squash = want.replace(" ", "")
        exact = next((p for p in items if _key(p.name).replace(" ", "") == squash or _key(p.name).rstrip("s") == want.rstrip("s")), None)
        if exact:
            return exact
        names = {_key(p.name): p for p in items}
        close = difflib.get_close_matches(want, list(names), n=1, cutoff=0.82)
        return names[close[0]] if close else None

    def find_phrase(self, text: str) -> Protocol | None:
        """A protocol whose own activation phrase this is ("activate gaming mode")."""
        said = clean_phrase(re.sub(r"^(?:(?:please|now|ok(?:ay)?)\s+)+", "", text or "", flags=re.I))
        if not said:
            return None
        for p in self.all():
            if p.enabled and any(said == ph or difflib.SequenceMatcher(None, said, ph).ratio() > 0.92 for ph in p.phrases):
                return p
        return None

    def names(self) -> list[str]:
        return [p.name for p in self.all()]

    def save(self, name: str, steps, pid: str | None = None, schedule: dict | None = None, **meta) -> Protocol:
        name = clean_name(name)
        if not name:
            raise ProtocolError("Give the protocol a name.")
        steps = _steps(steps)
        if not steps:
            raise ProtocolError("A protocol needs at least one step.")
        if len(steps) > MAX_STEPS:
            raise ProtocolError(f"A protocol can have up to {MAX_STEPS} steps.")
        items = self.all()
        clash = next((p for p in items if _key(p.name) == _key(name) and p.id != pid), None)
        if clash and pid:
            raise ProtocolError(f"You already have a protocol called {clash.name}.")
        target = next((p for p in items if p.id == pid), None) or clash  # saving under an existing name replaces it
        if target is None:
            if len(items) >= MAX_PROTOCOLS:
                raise ProtocolError(f"You can keep up to {MAX_PROTOCOLS} protocols.")
            target = Protocol(id=_new_id(), name=name, created=time.time())
            items.append(target)
        target.name, target.steps = name, steps
        if schedule is not None:
            target.schedule = normalise_schedule(schedule)
        self._apply_meta(target, meta, items)
        self._write(items)
        return target

    def _apply_meta(self, target: Protocol, meta: dict, items: list[Protocol]) -> None:
        clean = Protocol.from_dict({**target.to_dict(), **{k: v for k, v in meta.items() if k in _META}})
        if clean is None:
            return
        for p in items:  # one phrase, one protocol
            if p.id != target.id and set(p.phrases) & set(clean.phrases):
                taken = sorted(set(p.phrases) & set(clean.phrases))[0]
                raise ProtocolError(f"“{taken}” already starts the {p.name} protocol.")
        for key in _META:
            setattr(target, key, getattr(clean, key))

    def update(self, pid: str, **fields) -> Protocol:
        items = self.all()
        target = next((p for p in items if p.id == pid), None)
        if target is None:
            raise ProtocolError("That protocol doesn't exist any more.")
        if "steps" in fields:
            steps = _steps(fields["steps"])
            if not steps:
                raise ProtocolError("A protocol needs at least one step.")
            target.steps = steps[:MAX_STEPS]
        if "name" in fields:
            name = clean_name(fields["name"])
            if not name:
                raise ProtocolError("Give the protocol a name.")
            if any(_key(p.name) == _key(name) and p.id != pid for p in items):
                raise ProtocolError(f"You already have a protocol called {name}.")
            target.name = name
        if "schedule" in fields:
            target.schedule = normalise_schedule(fields["schedule"])
        if "last_run" in fields:
            target.last_run = float(fields["last_run"])
        if "history" in fields:
            target.history = [h for h in fields["history"] if isinstance(h, dict)][-MAX_HISTORY:]
        self._apply_meta(target, fields, items)
        self._write(items)
        return target

    def record_run(self, pid: str, run: dict) -> None:
        p = self.get(pid)
        if p is not None:
            self.update(pid, history=p.history + [run], last_run=run.get("at") or time.time())

    def delete(self, pid: str) -> Protocol | None:
        items = self.all()
        gone = next((p for p in items if p.id == pid), None)
        if gone:
            self._write([p for p in items if p.id != pid])
        return gone

    def restore(self, data: dict) -> Protocol | None:
        p = Protocol.from_dict(data)
        if p is None:
            return None
        items = [x for x in self.all() if x.id != p.id and _key(x.name) != _key(p.name)]
        self._write(items + [p])
        return p


# ----------------------------------------------------------------------------- safety and conditions
_RISKY = [  # (pattern, why) - steps that change or send things need a "yes" first
    (re.compile(r"\b(?:send|email|e-mail|message|text|reply to|post|tweet|share)\b", re.I), "sends something on your behalf"),
    (re.compile(r"\b(?:delete|erase|remove|wipe|empty\s+the\s+(?:recycle\s+)?bin|trash|format|uninstall)\b", re.I), "deletes things"),
    (re.compile(r"\b(?:shut\s*down|restart|reboot|log\s*(?:off|out)|sign\s+out|sleep\s+the\s+(?:computer|pc)|hibernate)\b", re.I), "turns the computer off or signs you out"),
    (re.compile(r"\b(?:close|quit|kill|end|terminate)\s+(?!the\s+(?:other\s+)?tabs?\b|this\s+tab\b|(?:the\s+)?tab\b)", re.I), "closes programs (unsaved work could be lost)"),
    (re.compile(r"\b(?:click|press|type|buy|pay|order|purchase|book)\b", re.I), "acts on the screen or spends money"),
    (re.compile(r"\b(?:run|execute)\s+(?:a\s+|the\s+)?(?:command|script|powershell|cmd|terminal|program\s+at)\b", re.I), "runs a command"),
]


def risk(text: str) -> str:
    """Why a step needs your go-ahead ('' if it's a safe one: opening, playing, volume, questions...)."""
    t = (text or "").strip()
    if re.match(r"^(?:wait|pause|say|announce|speak)\b", t, re.I):
        return ""
    for pattern, why in _RISKY:
        if pattern.search(t):
            return why
    return ""


def condition_met(when: dict, now: datetime, running: set[str], previous_ok: bool | None) -> bool:
    if not when:
        return True
    kind = when.get("type")
    if kind in ("app_running", "app_not_running"):
        app = re.sub(r"[^a-z0-9]", "", str(when.get("app") or "").lower())
        up = any(app and (app in r or r in app) for r in running)
        return up if kind == "app_running" else not up
    if kind == "time":
        hm = now.strftime("%H:%M")
        after, before = when.get("after"), when.get("before")
        if after and before and after > before:  # overnight: 22:00-06:00
            return hm >= after or hm < before
        return (not after or hm >= after) and (not before or hm < before)
    if kind == "weekday":
        return now.weekday() in (when.get("days") or [])
    if kind == "previous":
        return previous_ok is None or previous_ok == bool(when.get("ok", True))
    return True


def describe_condition(when: dict) -> str:
    if not when:
        return ""
    kind = when.get("type")
    if kind == "app_running":
        return f"only if {when.get('app')} is open"
    if kind == "app_not_running":
        return f"only if {when.get('app')} isn't open"
    if kind == "time":
        return "only " + " ".join(x for x in (f"after {when['after']}" if when.get("after") else "", f"before {when['before']}" if when.get("before") else "") if x)
    if kind == "weekday":
        return "only on " + _join([DAY_NAMES[d] + "s" for d in when.get("days") or []])
    if kind == "previous":
        return "only if the previous step " + ("worked" if when.get("ok", True) else "failed")
    return ""


_STEP_IF = re.compile(r"^(?:if|when|only\s+if)\s+(?P<app>[\w .'-]+?)\s+(?P<neg>is\s+not|isn'?t|is)\s+(?:open|running|on)\s*,?\s*(?:then\s+)?(?P<rest>.+)$", re.I)
_STEP_DAYS = re.compile(r"^(?:only\s+)?on\s+(?P<days>weekdays|weekends|(?:mondays?|tuesdays?|wednesdays?|thursdays?|fridays?|saturdays?|sundays?)(?:\s*(?:,|and)\s*\w+)*)\s*,?\s*(?P<rest>.+)$", re.I)
_STEP_FALLBACK = re.compile(r"^(?P<step>.+?),?\s+(?:but\s+|or\s+)?(?:use|try|open|play\s+it\s+on)\s+(?P<alt>.+?)\s+if\s+(?:it|that|.+?)\s+(?:isn'?t|is\s+not|aren'?t|doesn'?t|does\s+not|can'?t|fails|won'?t)\b.*$", re.I)


def structured_step(text: str) -> ProtoStep:
    """'if Spotify is open, pause the music' / 'on weekdays, open Slack' / 'open Spotify, but use YouTube if it isn't available'."""
    t = text.strip()
    m = _STEP_IF.match(t)
    if m:
        neg = m.group("neg").lower() != "is"
        return ProtoStep(m.group("rest").strip(), when={"type": "app_not_running" if neg else "app_running", "app": m.group("app").strip()})
    m = _STEP_DAYS.match(t)
    if m:
        days = parse_schedule("at 1:00 on " + m.group("days"))
        if days:
            return ProtoStep(m.group("rest").strip(), when={"type": "weekday", "days": days["days"]})
    m = _STEP_FALLBACK.match(t)
    if m:
        alt = m.group("alt").strip()
        verb = re.match(r"^(?:open|play|start|launch)\b", m.group("step").strip(), re.I)
        if not re.match(r"^(?:open|play|launch|start|go\s+to)\b", alt, re.I):
            alt = (verb.group(0).lower() if verb else "open") + " " + alt
        return ProtoStep(m.group("step").strip(), fallback=alt)
    return ProtoStep(t)


# ----------------------------------------------------------------------------- ready-made protocols to start from
TEMPLATES = [
    {"name": "Gaming Mode", "icon": "game", "category": "Gaming", "phrases": ["activate gaming mode", "game time"],
     "description": "Discord and your game, music down, and a lively personality.",
     "steps": ["open Discord", "open Steam", "turn the music down", "switch to Friday", "say Game on, boss."]},
    {"name": "Development", "icon": "code", "category": "Development", "phrases": ["initiate development protocol", "let's code"],
     "description": "Your editor, your docs and a calm, focused assistant.",
     "steps": ["open Visual Studio Code", "open github.com", "switch to Sage", "what am I working on"]},
    {"name": "Entertainment", "icon": "film", "category": "Entertainment", "phrases": ["activate entertainment mode", "movie night"],
     "description": "YouTube up, your last music back, and Harper for company.",
     "steps": ["switch to Harper", "resume what I was listening to", "set the music volume to 60"]},
    {"name": "Focus", "icon": "focus", "category": "Focus", "phrases": ["start focus mode", "time to focus"],
     "description": "A 25-minute focus timer, quiet music and your work apps.",
     "steps": ["pause everything", "switch to Sage", "set a timer for 25 minutes", "play lo-fi beats", "set the music volume to 25"]},
    {"name": "Morning Briefing", "icon": "sun", "category": "Morning", "phrases": ["good morning jarvis", "morning briefing"],
     "description": "Weather, your day and the news.", "schedule": {"time": "07:30", "days": [0, 1, 2, 3, 4], "enabled": False},
     "steps": ["what's the weather today", "what's my routine today", "open news.google.com"]},
    {"name": "Wind Down", "icon": "moon", "category": "Evening", "phrases": ["time for bed", "wind down"],
     "description": "Music off, volume low, a reminder for tomorrow.",
     "steps": ["pause everything", "set the volume to 20", "say Sleep well. I'll be here in the morning."]},
]


# ----------------------------------------------------------------------------- what the user says
@dataclass
class ProtocolCommand:
    action: str  # run | create | record | add | remove_step | delete | list | show | stop | rename | schedule | unschedule
    name: str = ""
    steps: list[str] = field(default_factory=list)
    new_name: str = ""
    index: int | None = None  # remove_step: 1-based, None = the last one
    schedule: dict | None = None
    explicit: bool = False  # the word "protocol" was used (so an unknown name is still about protocols)
    skip: list[str] = field(default_factory=list)  # "run Development, but don't open Discord" -> ["open discord"]


_P = r"(?:protocol|routine|sequence)"
_LEAD = r"^(?:(?:please|can you|could you|would you|will you|go ahead and|i want you to|i'd like you to|let's|lets|now)\s+)*"
_END = r"(?:\s+(?:please|now|for me|right now|immediately))*[\s.!?]*$"
_NAME = r"(?:the\s+|my\s+|a\s+|an\s+)?[\"“']?(?P<name>[^\"”:;,]+?)[\"”']?"

_RUN_VERBS = r"(?:run|initiate|initiating|execute|engage|activate|start|begin|launch|kick\s+off|do|trigger|fire\s+up|commence|perform)"
_RUN_EXPLICIT = [
    re.compile(_LEAD + _RUN_VERBS + r"\s+(?:the\s+|my\s+)?" + _P + r"\s+(?:called\s+|named\s+)?" + _NAME + _END, re.I),
    re.compile(_LEAD + _RUN_VERBS + r"\s+" + _NAME + r"\s+" + _P + _END, re.I),
    re.compile(r"^" + _NAME + r"\s+" + _P + r"(?:\s*,?\s*(?:go|now|initiate|engage|activate|please))?[\s.!?]*$", re.I),  # "house party protocol!"
]
_RUN_PLAIN = re.compile(_LEAD + _RUN_VERBS + r"\s+" + _NAME + _END, re.I)

_CREATE = re.compile(
    _LEAD + r"(?:create|make|add|set\s+up|build|record|program|start|new|save)\s+(?:me\s+)?(?:a\s+|an\s+|the\s+)?(?:new\s+)?" + _P +
    r"(?:\s+(?:called|named|for|titled))?\s*[\"“']?(?P<name>[^\"”:;,]*?)[\"”']?"
    r"(?:\s*(?::|;|,|\s-\s|\s+(?:that|which|to|with\s+(?:the\s+)?steps?|where\s+you|so\s+that\s+you|and\s+it\s+should|it\s+should|"
    r"which\s+will|that\s+will|that\s+should))\s*(?P<steps>.+))?[\s.!?]*$", re.I | re.S)
_CREATE_NAMED_FIRST = re.compile(
    _LEAD + r"(?:create|make|set\s+up|build|record|save)\s+(?:a\s+|an\s+)?(?:new\s+)?[\"“']?(?P<name>[^\"”:;,]+?)[\"”']?\s+" + _P +
    r"(?:\s*(?::|;|,|\s-\s|\s+(?:that|which|to|with\s+(?:the\s+)?steps?))\s*(?P<steps>.+))?[\s.!?]*$", re.I | re.S)
_ADD = [
    re.compile(_LEAD + r"(?:add|append|put|include)\s+(?P<step>.+?)\s+(?:to|into|onto|in|at\s+the\s+end\s+of)\s+(?:the\s+|my\s+)?" + _NAME + r"\s+" + _P + _END, re.I | re.S),
    re.compile(_LEAD + r"(?:add|append|put|include)\s+(?P<step>.+?)\s+(?:to|into|onto|in)\s+(?:the\s+|my\s+)?" + _P + r"\s+(?:called\s+|named\s+)?" + _NAME + _END, re.I | re.S),
    re.compile(_LEAD + r"(?:add|append)\s+(?:a\s+|another\s+|one\s+more\s+)?step\s+(?:to|in)\s+(?:the\s+|my\s+)?" + _NAME + r"\s+" + _P + r"\s*(?::|,|-)?\s*(?:that\s+|to\s+)?(?P<step>.+?)" + _END, re.I | re.S),
]
_REMOVE_STEP = re.compile(
    _LEAD + r"(?:remove|delete|drop|take\s+out|get\s+rid\s+of)\s+(?:the\s+)?(?:(?P<last>last)\s+step|step\s+(?:number\s+)?(?P<n>\d+|one|two|three|four|five|six|seven|eight|nine|ten)|"
    r"(?P<ord>first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)\s+step)\s+(?:from|of|in)\s+(?:the\s+|my\s+)?" + _NAME + r"(?:\s+" + _P + r")?" + _END, re.I)
_DELETE = [
    re.compile(_LEAD + r"(?:delete|remove|erase|forget|get\s+rid\s+of|scrap|trash)\s+(?:the\s+|my\s+)?" + _NAME + r"\s+" + _P + _END, re.I),
    re.compile(_LEAD + r"(?:delete|remove|erase|forget|get\s+rid\s+of|scrap|trash)\s+(?:the\s+|my\s+)?" + _P + r"\s+(?:called\s+|named\s+)?" + _NAME + _END, re.I),
]
_LIST = re.compile(_LEAD + r"(?:(?:list|show|tell\s+me|read\s+(?:out|me))\s+(?:all\s+)?(?:of\s+)?(?:my|the|your)\s+" + _P + r"s|"
                   r"what\s+" + _P + r"s\s+(?:do\s+i\s+have|have\s+i\s+(?:got|made)|are\s+there|can\s+you\s+run|do\s+you\s+(?:have|know))|"
                   r"(?:do\s+i\s+have|have\s+i\s+got)\s+any\s+" + _P + r"s|my\s+" + _P + r"s)" + _END, re.I)
_SHOW = [
    re.compile(_LEAD + r"(?:what(?:'s|\s+is)\s+in|what\s+does|read\s+(?:me\s+)?|show\s+(?:me\s+)?|describe|tell\s+me\s+about)\s+(?:the\s+|my\s+)?" + _NAME + r"\s+" + _P + r"(?:\s+do)?" + _END, re.I),
    re.compile(_LEAD + r"(?:what(?:'s|\s+is)\s+in|read\s+(?:me\s+)?|show\s+(?:me\s+)?|describe)\s+(?:the\s+|my\s+)?" + _P + r"\s+(?:called\s+|named\s+)?" + _NAME + _END, re.I),
]
_STOP = re.compile(_LEAD + r"(?:stop|cancel|abort|end|halt|terminate|kill|quit|pause)\s+(?:the\s+|this\s+|that\s+|my\s+)?(?:(?:current\s+|running\s+)?" + _P + r"|"
                   r"(?P<name>[^,]+?)\s+" + _P + r")" + _END, re.I)
_RENAME = re.compile(_LEAD + r"(?:rename|call)\s+(?:the\s+|my\s+)?" + _NAME + r"\s+" + _P + r"\s+(?:to\s+|as\s+)?[\"“']?(?P<new>[^\"”]+?)[\"”']?" + _END, re.I)
_SCHEDULE = [
    re.compile(_LEAD + r"(?:schedule|set|run|have)\s+(?:the\s+|my\s+)?" + _NAME + r"\s+" + _P + r"\s+(?:to\s+run\s+|for\s+|to\s+go\s+off\s+|at\s+(?=\d)|every\s*(?=\w)|on\s+(?=\w)|daily|each\s*(?=\w))?(?P<when>.*\d.*|.*\b(?:noon|midnight|midday)\b.*)" + _END, re.I),
    re.compile(_LEAD + r"(?:schedule|set\s+up)\s+(?:the\s+|my\s+)?" + _P + r"\s+(?:called\s+|named\s+)?" + _NAME + r"\s+(?:for\s+|to\s+run\s+)?(?P<when>(?:at|every|on|daily|each)\b.+)" + _END, re.I),
]
_UNSCHEDULE = re.compile(_LEAD + r"(?:unschedule|stop\s+scheduling|cancel\s+the\s+schedule\s+(?:for|of)|turn\s+off\s+the\s+schedule\s+(?:for|of)|"
                         r"don'?t\s+run)\s+(?:the\s+|my\s+)?" + _NAME + r"(?:\s+" + _P + r")?(?:\s+(?:automatically|on\s+a\s+schedule|any\s*more|every\s+day))?" + _END, re.I)
_ORD = {"one": 1, "first": 1, "two": 2, "second": 2, "three": 3, "third": 3, "four": 4, "fourth": 4, "five": 5, "fifth": 5,
        "six": 6, "sixth": 6, "seven": 7, "seventh": 7, "eight": 8, "eighth": 8, "nine": 9, "ninth": 9, "ten": 10, "tenth": 10}
_QUESTION = re.compile(r"^(?:what|whats|what's|how|why|who|when|where|which|is|are|do|does|can|could|tell|explain|define)\b", re.I)
_NOT_A_NAME = re.compile(r"^(?:it|this|that|one|a|an|the|new|protocol|something|everything|all|them)$", re.I)


def _strip(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


_EXCEPT = re.compile(r"^(?P<main>.+?),?\s+(?:but\s+(?:please\s+)?(?:don'?t|do\s+not|skip|leave\s+out|not|no)|except(?:\s+for)?|without|minus|skipping)\s+(?P<skip>.+?)[\s.!?]*$", re.I)


def parse_protocol_command(text: str, find=None) -> ProtocolCommand | None:
    """Understand a request about protocols. ``find(name)`` says whether a protocol exists (for plain "run X")."""
    t = _strip(text)
    if not t or len(t) > 1500:
        return None
    ex = _EXCEPT.match(t)
    if ex and re.search(r"\b(?:run|start|initiate|activate|execute|engage|launch|begin|kick\s+off|do|trigger)\b", ex.group("main"), re.I):
        cmd = parse_protocol_command(ex.group("main"), find)
        if cmd is not None and cmd.action == "run":
            cmd.skip = [x.strip(" ,.") for x in re.split(r"\s*(?:,|\bor\b|\band\b|\bnor\b)\s*", ex.group("skip")) if x.strip(" ,.")]
            return cmd
    low = t.lower()
    mentions = re.search(r"\b" + _P + r"s?\b", low) is not None

    if mentions:
        m = _LIST.match(t)
        if m:
            return ProtocolCommand("list", explicit=True)
        m = _STOP.match(t)
        if m:
            return ProtocolCommand("stop", name=clean_name(m.group("name") or ""), explicit=True)
        m = _REMOVE_STEP.match(t)
        if m:
            index = None if m.group("last") else _ORD.get((m.group("n") or m.group("ord") or "").lower()) or int(m.group("n") or 0) or None
            return ProtocolCommand("remove_step", name=clean_name(m.group("name")), index=index, explicit=True)
        for pattern in _ADD:
            m = pattern.match(t)
            if m and not _NOT_A_NAME.match(m.group("name").strip()):
                return ProtocolCommand("add", name=clean_name(m.group("name")), steps=split_steps(m.group("step")), explicit=True)
        for pattern in _SCHEDULE:
            m = pattern.match(t)
            if m:
                schedule = parse_schedule(m.group("when"))
                if schedule:
                    return ProtocolCommand("schedule", name=clean_name(m.group("name")), schedule=schedule, explicit=True)
        m = _UNSCHEDULE.match(t)
        if m:
            return ProtocolCommand("unschedule", name=clean_name(m.group("name")), explicit=True)
        m = _RENAME.match(t)
        if m:
            return ProtocolCommand("rename", name=clean_name(m.group("name")), new_name=clean_name(m.group("new")), explicit=True)
        for pattern in _DELETE:
            m = pattern.match(t)
            if m and not _NOT_A_NAME.match(m.group("name").strip()):
                return ProtocolCommand("delete", name=clean_name(m.group("name")), explicit=True)
        for pattern in _SHOW:
            m = pattern.match(t)
            if m and not _NOT_A_NAME.match(m.group("name").strip()):
                return ProtocolCommand("show", name=clean_name(m.group("name")), explicit=True)
        m = _CREATE.match(t)
        if m is None or not (m.group("name") or "").strip():
            m2 = _CREATE_NAMED_FIRST.match(t)
            if m2 and not _NOT_A_NAME.match(m2.group("name").strip()) and not re.match(r"^(?:new|a|an)$", m2.group("name").strip(), re.I):
                m = m2
        if m:
            name = clean_name(m.group("name") or "")
            steps = split_steps(m.group("steps") or "") if m.group("steps") else []
            if name and _NOT_A_NAME.match(name):
                name = ""
            return ProtocolCommand("create" if steps else "record", name=name, steps=steps, explicit=True)
        for i, pattern in enumerate(_RUN_EXPLICIT):
            m = pattern.match(t)
            if not m or _NOT_A_NAME.match(m.group("name").strip()) or _QUESTION.match(m.group("name").strip()):
                continue
            if i == len(_RUN_EXPLICIT) - 1 and not (find and find(m.group("name").strip())):
                continue  # a bare "<name> protocol" only for protocols that exist ("what is a protocol" is a question)
            return ProtocolCommand("run", name=clean_name(m.group("name")), explicit=True)
        return None

    m = _RUN_PLAIN.match(t)
    if m and find is not None:
        name = m.group("name").strip()
        if find(name):
            return ProtocolCommand("run", name=clean_name(name))
    return None


# ----------------------------------------------------------------------------- recording step by step
_DONE = re.compile(r"^\W*(?:(?:ok(?:ay)?|right|alright|and)\s*,?\s+)?(?:(?:i'?m\s+|we'?re\s+)?(?:done|finished)|that'?s\s+(?:it|all|everything|the\s+lot)|"
                   r"(?:save|finish|end|stop\s+recording|complete)(?:\s+(?:it|the\s+" + _P + r"|recording|now))?|no\s+more(?:\s+steps)?|"
                   r"nothing\s+else|that'?ll\s+do|all\s+done)\W*$", re.I)
_ABANDON = re.compile(r"^\W*(?:cancel(?:\s+(?:it|that|the\s+" + _P + r"|recording))?|never\s*mind|forget\s+(?:it|the\s+" + _P + r")|scrap\s+(?:it|that)|"
                      r"discard(?:\s+it)?|don'?t\s+save(?:\s+it)?|abort)\W*$", re.I)
_UNDO = re.compile(r"^\W*(?:undo(?:\s+that)?|remove\s+(?:that|the\s+last(?:\s+step|\s+one)?)|delete\s+(?:that|the\s+last(?:\s+step|\s+one)?)|"
                   r"scratch\s+that|take\s+that\s+(?:out|back)|no,?\s+not\s+that(?:\s+one)?)\W*$", re.I)


def recording_reply(text: str) -> str:
    """While recording a protocol step by step: 'done' | 'abandon' | 'undo' | 'step'."""
    t = (text or "").strip()
    if _DONE.match(t):
        return "done"
    if _ABANDON.match(t):
        return "abandon"
    if _UNDO.match(t):
        return "undo"
    return "step"


def _join(items: list[str]) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def spoken_steps(steps, limit: int = 8) -> str:
    shown = [describe_step(getattr(s, "text", s)) for s in steps[:limit]]
    text = "; ".join(f"{i + 1}, {s}" for i, s in enumerate(shown))
    if len(steps) > limit:
        text += f"; and {len(steps) - limit} more"
    return text


def join_names(names: list[str]) -> str:
    return _join(names)
