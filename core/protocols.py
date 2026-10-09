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
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime

MAX_PROTOCOLS = 60
MAX_STEPS = 40
MAX_STEP_CHARS = 300
MAX_NAME_CHARS = 40
MAX_WAIT_SECONDS = 3600
DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


class ProtocolError(ValueError):
    pass


@dataclass
class Protocol:
    id: str
    name: str
    steps: list[str] = field(default_factory=list)
    schedule: dict = field(default_factory=dict)  # {"time": "07:30", "days": [0..6], "enabled": True}
    created: float = 0.0
    last_run: float = 0.0

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "steps": list(self.steps), "schedule": dict(self.schedule),
                "created": self.created, "last_run": self.last_run}

    @classmethod
    def from_dict(cls, d: dict) -> "Protocol | None":
        try:
            steps = [str(s).strip()[:MAX_STEP_CHARS] for s in d.get("steps") or [] if str(s).strip()][:MAX_STEPS]
            name = clean_name(str(d.get("name") or ""))
            if not name:
                return None
            return cls(id=str(d.get("id") or _new_id()), name=name, steps=steps, schedule=normalise_schedule(d.get("schedule")),
                       created=float(d.get("created") or 0), last_run=float(d.get("last_run") or 0))
        except (TypeError, ValueError):
            return None

    def describe_schedule(self) -> str:
        return describe_schedule(self.schedule)


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


def split_steps(text: str) -> list[str]:
    """'open Spotify, then check the weather and open Gmail; wait 5 seconds' -> 3 or 4 steps."""
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
class ProtocolStore:
    """Protocols live in the settings file under "protocols" (a list of dicts)."""

    def __init__(self, config) -> None:
        self.config = config

    def all(self) -> list[Protocol]:
        out = []
        for raw in self.config.get("protocols") or []:
            p = Protocol.from_dict(raw) if isinstance(raw, dict) else None
            if p:
                out.append(p)
        return out

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

    def names(self) -> list[str]:
        return [p.name for p in self.all()]

    def save(self, name: str, steps: list[str], pid: str | None = None, schedule: dict | None = None) -> Protocol:
        name = clean_name(name)
        if not name:
            raise ProtocolError("Give the protocol a name.")
        steps = [s.strip()[:MAX_STEP_CHARS] for s in steps if s and s.strip()]
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
        self._write(items)
        return target

    def update(self, pid: str, **fields) -> Protocol:
        items = self.all()
        target = next((p for p in items if p.id == pid), None)
        if target is None:
            raise ProtocolError("That protocol doesn't exist any more.")
        if "steps" in fields:
            steps = [s.strip()[:MAX_STEP_CHARS] for s in fields["steps"] if s and s.strip()]
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
        self._write(items)
        return target

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


def parse_protocol_command(text: str, find=None) -> ProtocolCommand | None:
    """Understand a request about protocols. ``find(name)`` says whether a protocol exists (for plain "run X")."""
    t = _strip(text)
    if not t or len(t) > 1500:
        return None
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


def spoken_steps(steps: list[str], limit: int = 8) -> str:
    shown = [describe_step(s) for s in steps[:limit]]
    text = "; ".join(f"{i + 1}, {s}" for i, s in enumerate(shown))
    if len(steps) > limit:
        text += f"; and {len(steps) - limit} more"
    return text


def join_names(names: list[str]) -> str:
    return _join(names)
