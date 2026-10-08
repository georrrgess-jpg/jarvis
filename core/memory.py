"""Long-term memory: what the assistant knows about you, across sessions, kept only on this computer.

Four kinds of memory, each a short sentence written to the user ("You love jazz."):

* ``fact``        name of your dog, where you live, your job...
* ``preference``  what you like, dislike, and how you like things done
* ``routine``     things you do regularly ("You go to yoga on Tuesdays at 6 PM"), with days and a time
* ``project``     what you're working on, until it's done

They come from three places: you ask ("remember that my sister is called Ana"), the assistant picks
them up from conversation on its own (a small background step with the local model, only when the
conversation sounds personal), or you add and edit them in the Memory Core. Conversations are also
logged and summarised into short *episodes*, so the assistant can recall "what we talked about
yesterday" and carry on a conversation after a restart.

Recall is hybrid: a BM25 keyword ranking always, blended with semantic similarity when Ollama has an
embedding model installed (``nomic-embed-text`` and friends), plus a nudge for pinned and often-used
memories. Everything lives in ``memory.db`` (SQLite) next to the settings file.
"""

from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

log = logging.getLogger("jarvis.memory")

KINDS = ("fact", "preference", "routine", "project")
KIND_LABELS = {"fact": "About you", "preference": "Preferences", "routine": "Routines", "project": "Projects"}
EMBED_MODELS = ("nomic-embed-text", "mxbai-embed-large", "snowflake-arctic-embed", "all-minilm", "bge-m3", "bge-large",
                "granite-embedding", "paraphrase-multilingual")
RESUME_WINDOW = 3 * 3600  # carry on the last conversation if it ended less than this long ago
TURN_RETENTION_DAYS = 90
MAX_TEXT = 400

DAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_STOP = set("""a an the and or but if then so to of in on at by for with from up about into over after before is are was were be
been being am do does did have has had i you your yours me my mine we our us it its this that these those there here what which who
whom when where why how all any some no not just very really also too can could will would should may might must shall
s t don doesn didn isn aren wasn weren won wouldn im ive id youre""".split())
_SECRET = re.compile(r"\b(password|passcode|pass code|pin(?: number| code)?|cvv|security code|card number|credit card|bank account|"
                     r"account number|sort code|routing number|social security|ssn|seed phrase|recovery phrase|private key|api key|token)\b"
                     r"|\b\d{7,}\b|\b(?:\d[ -]?){13,19}\b", re.I)


# ----------------------------------------------------------------------------- data
@dataclass
class Memory:
    id: int
    kind: str
    text: str
    key: str = ""
    meta: dict = field(default_factory=dict)
    source: str = "said"  # said (you asked) | learned (picked up by itself) | manual (Memory Core)
    pinned: bool = False
    uses: int = 0
    created: float = 0.0
    updated: float = 0.0
    last_used: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["label"] = KIND_LABELS.get(self.kind, self.kind)
        d["when"] = describe_routine(self.meta) if self.kind == "routine" else ""
        return d


@dataclass
class Episode:
    id: int
    session: str
    summary: str
    started: float
    ended: float
    turns: int

    def to_dict(self) -> dict:
        return asdict(self)


# ----------------------------------------------------------------------------- text helpers
def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+(?:'[a-z]+)?", (text or "").lower())


def _stem(word: str) -> str:
    word = word.replace("'", "")
    for suffix in ("ingly", "ing", "edly", "ed", "ies", "es", "s", "ly"):
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            return word[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return word


def terms(text: str) -> list[str]:
    """Search terms: lower-case, stop words removed, lightly stemmed."""
    return [_stem(w) for w in _words(text) if w not in _STOP and len(w) > 1]


def similarity(a: str, b: str) -> float:
    """Word-overlap similarity (0..1) between two memories."""
    sa, sb = set(terms(a)), set(terms(b))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _refines(a: str, b: str) -> bool:
    """One memory says everything the other does, and more ("You have a dog called Max" / "...Max, a golden retriever")."""
    sa, sb = set(terms(a)), set(terms(b))
    return min(len(sa), len(sb)) >= 3 and (sa <= sb or sb <= sa)


_FIRST_TO_SECOND = [
    (r"\bi am\b", "you are"), (r"\bi'm\b", "you're"), (r"\bim\b", "you're"), (r"\bi was\b", "you were"),
    (r"\bi've\b", "you've"), (r"\bi'd\b", "you'd"), (r"\bi'll\b", "you'll"), (r"\bmyself\b", "yourself"),
    (r"\bmine\b", "yours"), (r"\bmy\b", "your"), (r"\bme\b", "you"), (r"\bi\b", "you"),
]


def second_person(text: str) -> str:
    """'I love jazz' -> 'You love jazz.'; 'my sister is Ana' -> 'Your sister is Ana.' (already-second-person text is kept)."""
    t = re.sub(r"\s+", " ", (text or "").strip()).strip(" .!?,;:")
    if not t:
        return ""
    for pattern, repl in _FIRST_TO_SECOND:
        t = re.sub(pattern, repl, t, flags=re.I)
    t = re.sub(r"\byou is\b", "you are", t, flags=re.I)
    if not re.match(r"^(you|your|you're|you've|you'd|you'll)\b", t, re.I):
        # "Ana is my sister" -> "Ana is your sister." is fine as it is; just tidy it up
        pass
    return t[0].upper() + t[1:] + "."


def spoken(text: str) -> str:
    """'You love jazz.' -> 'you love jazz' (to drop into a sentence)."""
    t = (text or "").strip().rstrip(".!")
    if re.match(r"^(You|Your|You're|You've|You'd|You'll)\b", t):
        t = t[0].lower() + t[1:]
    return t


def first_person_echo(text: str) -> str:
    """'You love jazz.' -> 'you love jazz' as the assistant says it back ("I'll remember that you love jazz")."""
    return spoken(text)


def looks_secret(text: str) -> bool:
    return bool(_SECRET.search(text or ""))


_ROUTINE = re.compile(r"\b(every|each)\s+(day|morning|afternoon|evening|night|week|weekday|weekend|monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
                      r"\b(daily|weekly|weekdays|weekends|mondays|tuesdays|wednesdays|thursdays|fridays|saturdays|sundays|routine|"
                      r"usually|normally|always)\b", re.I)
_PROJECT = re.compile(r"\b(working on|project|building|developing|writing (?:a|my) (?:book|novel|thesis|paper|dissertation|album)|"
                      r"studying for|learning (?:to|how|the|a|about)?|training for|planning (?:a|my|to)|preparing for|renovating|saving (?:up )?for|"
                      r"goal is|trying to)\b", re.I)
_PREFERENCE = re.compile(r"\b(like|likes|love|loves|prefer|prefers|enjoy|enjoys|hate|hates|dislike|dislikes|can't stand|cannot stand|"
                         r"favou?rite|fan of|allergic|vegetarian|vegan|don't eat|do not eat|don't drink|would rather|keep it|"
                         r"want (?:you|it|replies|answers)|call me)\b", re.I)


def classify(text: str) -> str:
    if _ROUTINE.search(text):
        return "routine"
    if _PROJECT.search(text):
        return "project"
    if _PREFERENCE.search(text):
        return "preference"
    return "fact"


_KEYED = [
    (re.compile(r"^(?:you(?:'re| are)\s+)?(\d{1,3})\s+years old", re.I), lambda m: "age"),
    (re.compile(r"^you live in\b", re.I), lambda m: "home"),
    (re.compile(r"^you(?:'re| are) from\b", re.I), lambda m: "hometown"),
    (re.compile(r"^you work (?:as|at|for)\b", re.I), lambda m: "work"),
    (re.compile(r"^your birthday is\b", re.I), lambda m: "birthday"),
    (re.compile(r"^your ([a-z' ]{2,40}?)(?:'s)? (?:name is|is called|are called)\b", re.I), lambda m: m.group(1).lower().strip() + " name"),
    (re.compile(r"^your (favou?rite [a-z ]{2,30}?) (?:is|are)\b", re.I), lambda m: m.group(1).lower().replace("favourite", "favorite")),
    (re.compile(r"^your ([a-z' ]{2,30}?) (?:is|are) (?!a |an |the |very |really |so |not )", re.I), lambda m: m.group(1).lower().strip()),
]


def key_for(text: str) -> str:
    """A topic for facts that replace each other ("Your favourite colour is blue" then "...is green")."""
    for pattern, make in _KEYED:
        m = pattern.match(text or "")
        if m:
            return re.sub(r"\s+", " ", make(m))[:40]
    return ""


def _parse_time(text: str) -> str:
    m = re.search(r"\b(?:at|by|around|from)\s+(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?", text, re.I) or \
        re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)", text, re.I)
    if not m:
        if re.search(r"\b(?:every|each|in the)\s+morning\b", text, re.I):
            return "morning"
        if re.search(r"\b(?:every|each|in the)\s+(?:evening|night)\b", text, re.I):
            return "evening"
        return ""
    hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower().replace(".", "")
    if ampm == "pm" and hour < 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return ""
    return f"{hour:02d}:{minute:02d}"


def routine_meta(text: str) -> dict:
    """'You go to yoga every Tuesday and Thursday at 6 pm' -> {'days': [1, 3], 'time': '18:00'}."""
    low = (text or "").lower()
    days: set[int] = set()
    if re.search(r"\b(every ?day|daily|each day|every (?:morning|evening|night|afternoon)|each (?:morning|evening|night))\b", low):
        days = set(range(7))
    if re.search(r"\bweekdays?\b|\bwork ?days?\b|monday to friday|mondays? through fridays?", low):
        days |= set(range(5))
    if re.search(r"\bweekends?\b", low):
        days |= {5, 6}
    for i, name in enumerate(DAY_NAMES):
        if re.search(rf"\b{name}s?\b", low) or re.search(rf"\b{name[:3]}s?\b", low):
            days.add(i)
    return {"days": sorted(days), "time": _parse_time(text)}


def describe_routine(meta: dict) -> str:
    days = list((meta or {}).get("days") or [])
    when = (meta or {}).get("time") or ""
    if not days or len(days) == 7:
        part = "Every day"
    elif days == [0, 1, 2, 3, 4]:
        part = "Weekdays"
    elif days == [5, 6]:
        part = "Weekends"
    else:
        part = ", ".join(DAY_NAMES[d][:3].capitalize() for d in days)
    if when and re.match(r"^\d\d:\d\d$", when):
        hour, minute = int(when[:2]), int(when[3:])
        part += f" · {hour % 12 or 12}{':%02d' % minute if minute else ''} {'AM' if hour < 12 else 'PM'}"
    elif when:
        part += f" · {when}"
    return part


def today_routines(memories: Iterable[Memory], when: datetime | None = None) -> list[Memory]:
    """Routines that happen today (only those tied to particular days, so daily ones don't nag every boot)."""
    day = (when or datetime.now()).weekday()
    found = [m for m in memories if m.kind == "routine" and m.meta.get("days") and len(m.meta["days"]) < 7 and day in m.meta["days"]]
    return sorted(found, key=lambda m: m.meta.get("time") or "99")


# ----------------------------------------------------------------------------- the store
class MemoryStore:
    """SQLite storage. Thread-safe; every method takes the lock."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10)
        self._db.row_factory = sqlite3.Row
        with self._lock, self._db:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, text TEXT NOT NULL, key TEXT DEFAULT '',
                    meta TEXT DEFAULT '{}', source TEXT DEFAULT 'said', pinned INTEGER DEFAULT 0, uses INTEGER DEFAULT 0,
                    created REAL, updated REAL, last_used REAL DEFAULT 0, embedding TEXT DEFAULT NULL, embed_model TEXT DEFAULT '');
                CREATE TABLE IF NOT EXISTS turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT NOT NULL, role TEXT NOT NULL, text TEXT NOT NULL,
                    persona TEXT DEFAULT '', ts REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS episodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT NOT NULL, summary TEXT NOT NULL, first_turn INTEGER,
                    last_turn INTEGER, started REAL, ended REAL, turns INTEGER DEFAULT 0);
                CREATE INDEX IF NOT EXISTS turns_session ON turns(session, id);
                CREATE INDEX IF NOT EXISTS memories_kind ON memories(kind);
            """)

    def close(self) -> None:
        with self._lock:
            try:
                self._db.close()
            except sqlite3.Error:
                pass

    # ------------------------------------------------------------- memories
    @staticmethod
    def _row(row: sqlite3.Row) -> Memory:
        try:
            meta = json.loads(row["meta"] or "{}")
        except ValueError:
            meta = {}
        return Memory(id=row["id"], kind=row["kind"], text=row["text"], key=row["key"] or "", meta=meta, source=row["source"] or "said",
                      pinned=bool(row["pinned"]), uses=row["uses"] or 0, created=row["created"] or 0.0,
                      updated=row["updated"] or 0.0, last_used=row["last_used"] or 0.0)

    def all(self, kind: str | None = None) -> list[Memory]:
        with self._lock:
            if kind:
                rows = self._db.execute("SELECT * FROM memories WHERE kind=? ORDER BY pinned DESC, updated DESC", (kind,)).fetchall()
            else:
                rows = self._db.execute("SELECT * FROM memories ORDER BY pinned DESC, updated DESC").fetchall()
        return [self._row(r) for r in rows]

    def get(self, mid: int) -> Memory | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM memories WHERE id=?", (int(mid),)).fetchone()
        return self._row(row) if row else None

    def count(self) -> dict:
        with self._lock:
            rows = self._db.execute("SELECT kind, COUNT(*) AS n FROM memories GROUP BY kind").fetchall()
            turns = self._db.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
            episodes = self._db.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
        counts = {k: 0 for k in KINDS}
        counts.update({r["kind"]: r["n"] for r in rows})
        counts["total"] = sum(counts[k] for k in KINDS)
        counts["turns"] = turns
        counts["episodes"] = episodes
        return counts

    def add(self, kind: str, text: str, key: str = "", meta: dict | None = None, source: str = "said",
            pinned: bool = False) -> tuple[Memory, str]:
        """Store a memory. Returns it and what happened: 'added', 'updated' (replaced an older version) or 'duplicate'."""
        kind = kind if kind in KINDS else "fact"
        text = re.sub(r"\s+", " ", (text or "").strip())[:MAX_TEXT]
        if not text:
            raise ValueError("A memory needs some text.")
        if kind == "routine" and not meta:
            meta = routine_meta(text)
        if kind == "project" and not (meta or {}).get("status"):
            meta = {**(meta or {}), "status": "active"}
        key = (key or key_for(text)).strip().lower()[:40]
        now = time.time()
        with self._lock:
            existing = self.all(kind) if kind != "fact" else self.all("fact") + self.all("preference")
            same = None
            if key:
                same = next((m for m in existing if m.key == key and m.kind == kind), None)
            if same is None:
                best = max(existing, key=lambda m: similarity(m.text, text), default=None)
                if best is not None and (similarity(best.text, text) >= 0.7 or best.text.lower() == text.lower()
                                         or _refines(best.text, text)):
                    if len(text) > len(best.text) + 8 and source != "learned":
                        same = best
                    else:
                        with self._db:
                            self._db.execute("UPDATE memories SET updated=? WHERE id=?", (now, best.id))
                        return self.get(best.id), "duplicate"
            if same is not None:
                if same.text == text:
                    return same, "duplicate"
                merged = {**same.meta, **(meta or {})}
                with self._db:
                    self._db.execute("UPDATE memories SET text=?, meta=?, updated=?, embedding=NULL, source=? WHERE id=?",
                                     (text, json.dumps(merged), now, source if source != "learned" else same.source, same.id))
                return self.get(same.id), "updated"
            with self._db:
                cur = self._db.execute(
                    "INSERT INTO memories (kind, text, key, meta, source, pinned, uses, created, updated) VALUES (?,?,?,?,?,?,0,?,?)",
                    (kind, text, key, json.dumps(meta or {}), source, int(bool(pinned)), now, now))
            return self.get(cur.lastrowid), "added"

    def restore(self, data: dict) -> Memory:
        """Put back a memory exactly as it was (undo a delete)."""
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT INTO memories (kind, text, key, meta, source, pinned, uses, created, updated, last_used) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (data.get("kind") if data.get("kind") in KINDS else "fact", str(data.get("text") or "")[:MAX_TEXT] or "(empty)",
                 str(data.get("key") or "")[:40], json.dumps(data.get("meta") or {}), data.get("source") or "said",
                 int(bool(data.get("pinned"))), int(data.get("uses") or 0), float(data.get("created") or time.time()),
                 time.time(), float(data.get("last_used") or 0)))
        return self.get(cur.lastrowid)

    def update(self, mid: int, **fields) -> Memory | None:
        current = self.get(mid)
        if current is None:
            return None
        sets, values = [], []
        if "text" in fields:
            text = re.sub(r"\s+", " ", str(fields["text"] or "").strip())[:MAX_TEXT]
            if not text:
                raise ValueError("A memory needs some text.")
            sets += ["text=?", "embedding=NULL", "key=?"]
            values += [text, key_for(text) or current.key]
            if current.kind == "routine" and "meta" not in fields:
                fields["meta"] = {**current.meta, **{k: v for k, v in routine_meta(text).items() if v}}
        if "kind" in fields and fields["kind"] in KINDS:
            sets.append("kind=?")
            values.append(fields["kind"])
            if fields["kind"] == "routine" and "meta" not in fields:
                fields["meta"] = routine_meta(fields.get("text") or current.text)
        if "meta" in fields and isinstance(fields["meta"], dict):
            sets.append("meta=?")
            values.append(json.dumps({**current.meta, **fields["meta"]}))
        if "pinned" in fields:
            sets.append("pinned=?")
            values.append(int(bool(fields["pinned"])))
        if not sets:
            return current
        sets.append("updated=?")
        values.append(time.time())
        with self._lock, self._db:
            self._db.execute(f"UPDATE memories SET {', '.join(sets)} WHERE id=?", (*values, int(mid)))
        return self.get(mid)

    def delete(self, mid: int) -> Memory | None:
        found = self.get(mid)
        if found:
            with self._lock, self._db:
                self._db.execute("DELETE FROM memories WHERE id=?", (int(mid),))
        return found

    def touch(self, ids: Iterable[int]) -> None:
        ids = [int(i) for i in ids]
        if not ids:
            return
        with self._lock, self._db:
            self._db.executemany("UPDATE memories SET uses=uses+1, last_used=? WHERE id=?", [(time.time(), i) for i in ids])

    def clear(self) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM memories")
            self._db.execute("DELETE FROM turns")
            self._db.execute("DELETE FROM episodes")

    # ------------------------------------------------------------- embeddings
    def set_embedding(self, mid: int, vector: list[float], model: str) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE memories SET embedding=?, embed_model=? WHERE id=?", (json.dumps([round(v, 5) for v in vector]), model, mid))

    def embeddings(self, model: str) -> dict[int, list[float]]:
        with self._lock:
            rows = self._db.execute("SELECT id, embedding FROM memories WHERE embedding IS NOT NULL AND embed_model=?", (model,)).fetchall()
        out = {}
        for r in rows:
            try:
                out[r["id"]] = json.loads(r["embedding"])
            except ValueError:
                pass
        return out

    def missing_embeddings(self, model: str, limit: int = 64) -> list[Memory]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM memories WHERE embedding IS NULL OR embed_model<>? LIMIT ?", (model, limit)).fetchall()
        return [self._row(r) for r in rows]

    # ------------------------------------------------------------- conversation log
    def log_turn(self, session: str, role: str, text: str, persona: str = "") -> int:
        with self._lock, self._db:
            cur = self._db.execute("INSERT INTO turns (session, role, text, persona, ts) VALUES (?,?,?,?,?)",
                                   (session, role, (text or "")[:4000], persona, time.time()))
        return cur.lastrowid

    def turns(self, session: str | None = None, after_id: int = 0, limit: int = 200) -> list[dict]:
        with self._lock:
            if session:
                rows = self._db.execute("SELECT * FROM turns WHERE session=? AND id>? ORDER BY id LIMIT ?", (session, after_id, limit)).fetchall()
            else:
                rows = self._db.execute("SELECT * FROM turns WHERE id>? ORDER BY id LIMIT ?", (after_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def last_turns(self, limit: int = 12, exclude_session: str = "") -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM turns WHERE session<>? ORDER BY id DESC LIMIT ?", (exclude_session, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def unsummarised(self, exclude_session: str = "") -> list[tuple[str, list[dict]]]:
        """Conversation stretches not yet covered by an episode, per session."""
        with self._lock:
            done = {r["session"]: r["last"] for r in self._db.execute("SELECT session, MAX(last_turn) AS last FROM episodes GROUP BY session")}
            sessions = [r["session"] for r in self._db.execute("SELECT DISTINCT session FROM turns WHERE session<>? ORDER BY id", (exclude_session,))]
        return [(s, t) for s in sessions if (t := self.turns(s, done.get(s) or 0, 400))]

    def add_episode(self, session: str, summary: str, turns: list[dict]) -> Episode:
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT INTO episodes (session, summary, first_turn, last_turn, started, ended, turns) VALUES (?,?,?,?,?,?,?)",
                (session, summary.strip()[:600], turns[0]["id"], turns[-1]["id"], turns[0]["ts"], turns[-1]["ts"],
                 sum(1 for t in turns if t["role"] == "user")))
            row = self._db.execute("SELECT * FROM episodes WHERE id=?", (cur.lastrowid,)).fetchone()
        return Episode(row["id"], row["session"], row["summary"], row["started"], row["ended"], row["turns"])

    def episodes(self, limit: int = 20) -> list[Episode]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM episodes ORDER BY ended DESC LIMIT ?", (limit,)).fetchall()
        return [Episode(r["id"], r["session"], r["summary"], r["started"], r["ended"], r["turns"]) for r in rows]

    def delete_episode(self, eid: int) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM episodes WHERE id=?", (int(eid),))

    def prune(self, days: int = TURN_RETENTION_DAYS) -> None:
        """Drop raw conversation older than ``days`` (its episode summary stays)."""
        cutoff = time.time() - days * 86400
        with self._lock, self._db:
            self._db.execute("DELETE FROM turns WHERE ts<? AND id <= COALESCE((SELECT MAX(last_turn) FROM episodes), 0)", (cutoff,))


# ----------------------------------------------------------------------------- ranking
def bm25(query: list[str], docs: dict[int, list[str]], k1: float = 1.4, b: float = 0.75) -> dict[int, float]:
    if not query or not docs:
        return {}
    n = len(docs)
    avg = sum(len(d) for d in docs.values()) / n or 1.0
    df: dict[str, int] = {}
    for d in docs.values():
        for t in set(d):
            df[t] = df.get(t, 0) + 1
    scores: dict[int, float] = {}
    for mid, d in docs.items():
        s = 0.0
        for q in set(query):
            tf = d.count(q)
            if not tf:
                continue
            idf = math.log(1 + (n - df[q] + 0.5) / (df[q] + 0.5))
            s += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * len(d) / avg))
        if s > 0:
            scores[mid] = s
    return scores


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


# ----------------------------------------------------------------------------- spoken commands
@dataclass
class MemoryCommand:
    action: str  # remember | forget | forget_all | recall | recall_about | projects | routines | call_me | my_name | episodes | project_done
    text: str = ""
    kind: str = ""


_LEAD = (r"^(?:(?:hey |ok |okay |hi )?(?:jarvis|harper|friday|sage)[, ]+)?"
         r"(?:please |can you |could you |would you |will you |i want you to |i'd like you to |i need you to )*")
_TAIL = r"(?:[,\s]+(?:please|ok|okay|thanks|thank you|jarvis|harper|friday|sage))*[\s.!?]*$"
_MEM_COMMANDS: list[tuple[str, re.Pattern]] = [
    ("forget_all", re.compile(_LEAD + r"(?:forget|erase|delete|wipe|clear)\s+(?:absolutely\s+)?(?:everything|all)(?:\s+(?:you know|you remember|you've learned|about me|of it|your memor(?:y|ies)))*" + _TAIL, re.I)),
    ("forget_all", re.compile(_LEAD + r"(?:erase|delete|wipe|clear|reset)\s+(?:all\s+)?(?:of\s+)?your\s+(?:memory|memories)" + _TAIL, re.I)),
    ("forget", re.compile(_LEAD + r"(?:forget|don't remember|stop remembering|erase the memory|delete the memory)\s+(?:that\s+|about\s+|the fact that\s+)?(?P<text>(?!it\b|about it\b|that\b\s*$)\S.*?)" + _TAIL, re.I)),
    ("recall", re.compile(_LEAD + r"(?:what do you (?:know|remember) about me|what have you (?:learned|learnt) about me|what do you remember|"
                          r"tell me what you (?:know|remember) about me|what(?:'s| is) in your memory|show (?:me )?(?:your|my) memor(?:y|ies)|"
                          r"open (?:the )?memory(?: core)?|what are my preferences|what do i like)" + _TAIL, re.I)),
    ("projects", re.compile(_LEAD + r"(?:what am i working on|what are my (?:projects|goals)|(?:list|show) (?:me )?my (?:projects|goals)|"
                            r"what projects (?:am i working on|do i have))" + _TAIL, re.I)),
    ("routines", re.compile(_LEAD + r"(?:what(?:'s| is| are) my (?:routines?|schedule)(?: (?:for )?today| like)?|what do i (?:usually )?(?:do|have) (?:on )?(?:today|this \w+|(?:mon|tues|wednes|thurs|fri|satur|sun)day)|"
                            r"(?:list|show) (?:me )?my routines?|do i have anything (?:on )?today)" + _TAIL, re.I)),
    ("episodes", re.compile(_LEAD + r"(?:what did we (?:talk|chat) about(?: (?:last time|yesterday|before|earlier|recently|the other day))?|"
                            r"remind me what we (?:talked|chatted) about|what were we (?:talking about|doing)(?: (?:last time|before|earlier))?|"
                            r"where (?:were we|did we leave off))" + _TAIL, re.I)),
    ("recall_about", re.compile(_LEAD + r"(?:what do you (?:know|remember) about|do you remember|do you know|what did i (?:say|tell you) about)\s+(?:my\s+|the\s+)?(?P<text>.+?)" + _TAIL, re.I)),
    ("my_name", re.compile(_LEAD + r"(?:what(?:'s| is) my name|who am i|do you know (?:my name|who i am))" + _TAIL, re.I)),
    ("call_me", re.compile(_LEAD + r"(?:(?:from now on |please )?call me|you can call me|my name(?:'s| is)|i(?:'m| am) called|address me as)\s+(?P<text>[a-zà-ÿ' .-]{1,40}?)" + _TAIL, re.I)),
    ("project_done", re.compile(_LEAD + r"i(?:'ve| have)?\s+(?:finished|completed|wrapped up|done with|finished with)\s+(?:my\s+|the\s+)?(?P<text>.+?)(?:\s+project)?" + _TAIL, re.I)),
    ("remember", re.compile(_LEAD + r"(?:remember|don't forget|do not forget|keep in mind|note|make a note|take note|bear in mind|save (?:to|in) (?:your )?memory)"
                            r"(?:\s+(?:that|this))?[:,]?\s+(?P<text>\S.*?)" + _TAIL, re.I)),
]
_TITLES = {"sir", "ma'am", "maam", "madam", "boss", "captain", "chief", "doctor", "doc", "professor", "master", "miss", "mister",
           "commander", "your majesty", "your highness", "my lord", "my lady", "mr stark", "miss potts", "dude", "mate", "buddy"}


_NOT_NAMES = {"back", "later", "tomorrow", "tonight", "soon", "when", "if", "at", "on", "in", "a", "an", "the", "maybe", "now",
              "please", "anytime", "whenever", "after", "before", "again", "sometime", "up", "out", "not", "going", "doing", "fine",
              "good", "ok", "okay", "here", "there", "ready", "done", "home", "busy", "tired", "hungry", "back."}


def parse_memory_command(text: str) -> MemoryCommand | None:
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not t:
        return None
    for action, pattern in _MEM_COMMANDS:
        m = pattern.match(t)
        if not m:
            continue
        found = (m.groupdict().get("text") or "").strip(" ,.!?")
        if action == "remember":
            if re.match(r"^(?:to\s|me\s|when\b|where\b|what\b|how\b|if\b|it\b|this\b$|that\b$)", found, re.I):
                continue  # "remind me to...", "remember when we...", "remember it" aren't things to store
            if len(terms(found)) < 1:
                continue
        if action == "recall_about" and re.match(r"^(?:how|what|where|when|why|who|if|the way)\b", found, re.I):
            continue  # "do you know how to..." is a question, not a memory lookup
        kind = ""
        if action == "call_me":
            first = found.split()[0].lower() if found.split() else ""
            if len(found.split()) > 3 or not re.search(r"[a-zà-ÿ]", found, re.I) or first in _NOT_NAMES:
                continue
            kind = "title" if re.search(r"\b(?:call|address) me\b", t, re.I) else "name"
        if action == "project_done" and len(found.split()) > 8:
            continue
        return MemoryCommand(action, found, kind)
    return None


_PERSONAL = re.compile(r"\b(i|i'm|im|i've|i'd|i'll|my|me|mine|myself|we|we're|our|us)\b", re.I)
_PERSONAL_CUES = re.compile(r"\b(name|live|work|job|study|school|college|university|wife|husband|partner|girlfriend|boyfriend|kid|kids|son|daughter|"
                            r"mum|mom|dad|mother|father|sister|brother|family|friend|dog|cat|pet|birthday|born|years old|like|love|prefer|enjoy|"
                            r"hate|favou?rite|allergic|every|usually|always|never|morning|weekend|monday|tuesday|wednesday|thursday|friday|"
                            r"saturday|sunday|project|working on|building|learning|training|planning|goal|trying to|started|moved|got a|"
                            r"bought|car|house|team|support|play|listen|watch|read|vegetarian|vegan|diet|gym|run|exam|test|interview|trip|"
                            r"holiday|vacation|wedding|book|novel|game|music|band)\b", re.I)


def worth_learning(user_text: str) -> bool:
    """Cheap pre-filter: only personal-sounding messages go to the (slower) extraction step."""
    t = (user_text or "").strip()
    if len(t) < 12 or len(t) > 1500 or looks_secret(t):
        return False
    return bool(_PERSONAL.search(t) and _PERSONAL_CUES.search(t))


EXTRACT_SYSTEM = """You maintain the long-term memory of a personal AI assistant. Read the conversation and extract only durable things worth remembering about the USER for future conversations:
- fact: personal facts (their name, family, pets, job, school, where they live, birthday)
- preference: likes, dislikes, favourites, and how they like things done
- routine: things they do regularly, with the days and time if given
- project: ongoing projects, goals or things they are working on or learning

Rules:
- Only include what the user clearly said about themselves. Never guess.
- Ignore one-off requests, questions, small talk, opinions about the assistant, and anything temporary (today's mood, what they are doing right now).
- Never store passwords, codes, card or account numbers or other secrets.
- Write each memory as one short sentence addressed to the user, in English, starting with "You" or "Your" (e.g. "You have a golden retriever called Max.").
- Skip anything already in the known list unless it changed; if it changed, give the new version.
- For a project the user says is finished, set "status": "done".
Reply with JSON only: {"memories": [{"kind": "fact|preference|routine|project", "text": "...", "status": "active|done"}]}
If there is nothing worth remembering, reply {"memories": []}."""

EPISODE_SYSTEM = """Summarise this conversation between the user and their AI assistant in one or two short sentences, for the assistant's diary.
Mention the topics, anything decided or done (documents written, emails sent, plans made) and anything left unfinished.
Write in the past tense and refer to the user as "the user". No preamble: reply with the summary only."""


# ----------------------------------------------------------------------------- the engine
class MemoryEngine:
    """Ties the store to the language model: recall for prompts, background learning, episodes and resume."""

    def __init__(self, config, llm=None, store: MemoryStore | None = None, path: Path | None = None,
                 on_event: Callable[[str, dict], None] | None = None) -> None:
        self.config = config
        self.llm = llm
        self._path = path or Path(getattr(config, "path", Path("settings.json"))).parent / "memory.db"
        self._store = store
        self._store_error: str | None = None
        self.on_event = on_event or (lambda kind, payload: None)
        self.session = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        self._pending: list[tuple[str, str]] = []  # exchanges waiting for the learning step
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._busy = threading.Lock()
        self._idle_check: Callable[[], bool] = lambda: True
        self._thread: threading.Thread | None = None
        self._last_turn_at = 0.0
        self._summarised_upto = 0
        self.embed_model: str | None = None

    # ------------------------------------------------------------- lifecycle
    @property
    def store(self) -> MemoryStore | None:
        if self._store is None and self._store_error is None:
            try:
                self._store = MemoryStore(self._path)
            except Exception as exc:  # disk full, locked database...
                self._store_error = str(exc)
                log.exception("Memory store unavailable")
        return self._store

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("memory_enabled", True)) and self.store is not None

    def start(self, idle: Callable[[], bool] | None = None) -> None:
        """Begin the background worker (learning, episodes, embeddings)."""
        if idle:
            self._idle_check = idle
        if self._thread is None:
            self._thread = threading.Thread(target=self._worker, name="memory", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self._store is not None:
            self._store.close()

    def poke(self) -> None:
        self._wake.set()

    def emit(self, kind: str, **payload) -> None:
        try:
            self.on_event(kind, payload)
        except Exception:
            log.exception("memory event %s failed", kind)

    # ------------------------------------------------------------- remembering
    def remember(self, text: str, kind: str = "", source: str = "said", meta: dict | None = None) -> tuple[Memory | None, str]:
        """Store something the user told us. ``text`` may be first person ("I love jazz")."""
        if not self.enabled:
            return None, "disabled"
        if looks_secret(text):
            return None, "secret"
        sentence = second_person(text)
        if not sentence:
            return None, "empty"
        kind = kind or classify(sentence)
        memory, status = self.store.add(kind, sentence, meta=meta, source=source)
        if status != "duplicate":
            self._embed_later()
            self.emit("memory_changed", stats=self.stats())
        return memory, status

    def forget_matching(self, query: str) -> Memory | None:
        if not self.enabled:
            return None
        hits = self.search(query, limit=1, min_score=0.3, touch=False, min_cover=0.5)
        if not hits:
            return None
        gone = self.store.delete(hits[0].id)
        self.emit("memory_changed", stats=self.stats())
        return gone

    def finish_project(self, name: str) -> Memory | None:
        if not self.enabled:
            return None
        projects = [m for m in self.store.all("project") if m.meta.get("status") != "done"]
        best = max(projects, key=lambda m: similarity(m.text, name), default=None)
        if best is None or similarity(best.text, name) < 0.15:
            return None
        updated = self.store.update(best.id, meta={"status": "done", "done_at": time.time()})
        self.emit("memory_changed", stats=self.stats())
        return updated

    # ------------------------------------------------------------- recall
    def rank(self, query: str, kinds: Iterable[str] | None = None) -> list[tuple[float, float, Memory]]:
        """(score, how much of the query it covers, memory), best first."""
        if not self.enabled:
            return []
        items = [m for m in self.store.all() if not kinds or m.kind in kinds]
        q = terms(query)
        if not items or not q:
            return []
        docs = {m.id: terms(m.text + " " + m.key) for m in items}
        keyword = bm25(q, docs)
        top = max(keyword.values(), default=0.0)
        semantic: dict[int, float] = {}
        if self.embed_model:
            vec = self._embed([query])
            if vec:
                stored = self.store.embeddings(self.embed_model)
                semantic = {mid: max(0.0, (cosine(vec[0], v) - 0.35) / 0.65) for mid, v in stored.items()}
        wanted = set(q)
        now = time.time()
        ranked = []
        for m in items:
            cover = len(wanted & set(docs[m.id])) / len(wanted)
            k = (keyword.get(m.id, 0.0) / top) if top else 0.0
            s = semantic.get(m.id, 0.0)
            score = (0.35 * k + 0.3 * cover + 0.35 * s) if semantic else (0.5 * k + 0.5 * cover)
            if score <= 0.02:
                continue
            score += 0.04 * m.pinned + 0.02 * min(m.uses, 10) / 10 + 0.02 * math.exp(-(now - m.updated) / (30 * 86400))
            ranked.append((score, max(cover, s), m))
        ranked.sort(key=lambda p: p[0], reverse=True)
        return ranked

    def search(self, query: str, limit: int = 6, kinds: Iterable[str] | None = None, min_score: float = 0.15,
               touch: bool = True, min_cover: float = 0.0) -> list[Memory]:
        found = [m for score, cover, m in self.rank(query, kinds) if score >= min_score and cover >= min_cover][:limit]
        if touch and found:
            self.store.touch(m.id for m in found)
        return found

    def context(self, query: str = "", budget: int = 1600) -> str:
        """The memory block for the system prompt: who the user is, and what's relevant to what they just said."""
        if not self.enabled:
            return ""
        everything = self.store.all()
        name = (self.config.get("user_name") or "").strip()
        lines: list[str] = []
        used: set[int] = set()

        def add(m: Memory, note: str = "") -> None:
            if m.id not in used:
                used.add(m.id)
                lines.append(f"- {m.text}{note}")

        for m in everything:
            if m.pinned:
                add(m)
        for m in self.search(query, limit=6) if query else []:
            add(m)
        for m in [m for m in everything if m.kind == "project" and m.meta.get("status") != "done"][:4]:
            add(m, " (ongoing)")
        for m in [m for m in everything if m.kind == "preference"][:6]:
            add(m)
        for m in today_routines(everything)[:3]:
            add(m, " (today)")
        for m in [m for m in everything if m.kind == "fact"][:6]:
            add(m)
        parts = []
        if name:
            parts.append(f"The user's name is {name}.")
        if lines:
            parts.append("\n".join(lines))
        episodes = self.store.episodes(limit=3)
        if episodes:
            parts.append("Recent conversations:\n" + "\n".join(f"- {_ago(e.ended)}: {e.summary}" for e in episodes))
        if not parts:
            return ""
        block = ("Long-term memory (things the user told you in earlier conversations). Use it naturally when it's relevant, "
                 "like a friend who remembers, without reciting it or mentioning this list. If something here is wrong, "
                 "trust what the user says now.\n" + "\n".join(parts))
        return block[:budget]

    def overview(self) -> dict:
        if not self.enabled:
            return {k: [] for k in KINDS}
        return {k: self.store.all(k) for k in KINDS}

    def stats(self) -> dict:
        if self.store is None:
            return {"enabled": False, "error": self._store_error}
        counts = self.store.count()
        return {**counts, "enabled": bool(self.config.get("memory_enabled", True)), "auto_learn": bool(self.config.get("memory_auto_learn", True)),
                "embed_model": self.embed_model or "", "path": str(self._path), "error": self._store_error}

    # ------------------------------------------------------------- conversation log & learning
    def log_exchange(self, user: str, reply: str, persona: str = "", learn: bool = True) -> None:
        if not self.enabled:
            return
        try:
            self.store.log_turn(self.session, "user", user, persona)
            if reply:
                self.store.log_turn(self.session, "assistant", reply, persona)
        except sqlite3.Error:
            log.exception("Couldn't log the conversation")
            return
        self._last_turn_at = time.time()
        if learn and self.config.get("memory_auto_learn", True) and worth_learning(user):
            with self._busy:
                self._pending.append((user, reply))
            self._wake.set()

    def restore_recent(self) -> list[dict]:
        """The end of the last conversation, if it was recent enough to carry on."""
        if not (self.enabled and self.config.get("memory_resume", True)):
            return []
        turns = self.store.last_turns(int(self.config.get("max_history_turns", 12)) * 2, exclude_session=self.session)
        if not turns or time.time() - turns[-1]["ts"] > RESUME_WINDOW:
            return []
        while turns and turns[0]["role"] != "user":
            turns.pop(0)
        return turns

    def learn_now(self) -> list[Memory]:
        """Run the learning step on the waiting exchanges (normally called by the background worker)."""
        with self._busy:
            batch, self._pending = self._pending[-6:], []
        if not batch or not self.enabled or self.llm is None or not getattr(self.llm, "model", None):
            return []
        known = "\n".join(f"- {m.text}" for m in self.store.all()[:40]) or "(nothing yet)"
        convo = "\n".join(f"User: {u}\nAssistant: {r[:400]}" for u, r in batch)
        data = self.llm.json_task(EXTRACT_SYSTEM, f"Already known:\n{known}\n\nConversation:\n{convo}")
        learned: list[Memory] = []
        for item in (data or {}).get("memories") or []:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            kind = str(item.get("kind") or "").lower()
            if not text or len(text) > 240 or looks_secret(text) or kind not in KINDS:
                continue
            if not re.match(r"^(you|your)\b", text, re.I):
                text = second_person(text)
            if not text.endswith((".", "!", "?")):
                text += "."
            text = text[0].upper() + text[1:]
            if kind == "fact" and re.match(r"^your name is\s+([A-Za-zÀ-ÿ' -]{1,30})\.?$", text, re.I):
                if not self.config.get("user_name"):
                    name = re.match(r"^your name is\s+([A-Za-zÀ-ÿ' -]{1,30})\.?$", text, re.I).group(1).strip()
                    self.config.update({"user_name": name.title()})
                    self.emit("settings", **self.config.as_dict())
                continue
            if kind == "project" and str(item.get("status") or "").lower() == "done":
                done = self.finish_project(text)
                if done:
                    learned.append(done)
                    continue
            memory, status = self.store.add(kind, text, source="learned")
            if status in ("added", "updated"):
                learned.append(memory)
                self.emit("memory_learned", memory=memory.to_dict(), status=status)
        if learned:
            self._embed_later()
            self.emit("memory_changed", stats=self.stats())
        return learned

    def summarise(self, session: str, turns: list[dict]) -> Episode | None:
        if not turns or self.llm is None or not getattr(self.llm, "model", None):
            return None
        if sum(1 for t in turns if t["role"] == "user") < 2:
            return None
        text = "\n".join(f"{'User' if t['role'] == 'user' else 'Assistant'}: {t['text'][:300]}" for t in turns[-40:])
        summary = self.llm.compose(EPISODE_SYSTEM, text, max_tokens=160, temperature=0.3).strip()
        summary = re.sub(r"^(?:summary|diary)\s*:\s*", "", summary, flags=re.I).strip().strip('"')
        if not summary:
            return None
        episode = self.store.add_episode(session, summary, turns)
        self.emit("memory_changed", stats=self.stats())
        return episode

    def summarise_pending(self, include_current: bool = False) -> int:
        if not self.enabled:
            return 0
        made = 0
        for session, turns in self.store.unsummarised(exclude_session="" if include_current else self.session):
            if self._stop.is_set():
                break
            try:
                if self.summarise(session, turns):
                    made += 1
            except Exception as exc:
                log.info("Episode summary skipped: %s", exc)
                break
        try:
            self.store.prune()
        except sqlite3.Error:
            pass
        return made

    # ------------------------------------------------------------- embeddings
    def _embed(self, texts: list[str]) -> list[list[float]] | None:
        if not (self.embed_model and self.llm is not None and hasattr(self.llm, "embed")):
            return None
        try:
            return self.llm.embed(self.embed_model, texts)
        except Exception as exc:
            log.info("Embedding failed (%s); keyword recall only", exc)
            self.embed_model = None
            return None

    def pick_embed_model(self, installed: Iterable[str]) -> str | None:
        names = list(installed or [])
        for want in EMBED_MODELS:
            for name in names:
                if name.split(":")[0].split("/")[-1].startswith(want):
                    self.embed_model = name
                    return name
        self.embed_model = None
        return None

    def _embed_later(self) -> None:
        if self.embed_model:
            self._wake.set()

    def embed_missing(self) -> int:
        if not (self.enabled and self.embed_model):
            return 0
        todo = self.store.missing_embeddings(self.embed_model)
        if not todo:
            return 0
        vectors = self._embed([m.text for m in todo])
        if not vectors:
            return 0
        for m, v in zip(todo, vectors):
            self.store.set_embedding(m.id, v, self.embed_model)
        return len(todo)

    # ------------------------------------------------------------- background worker
    def _worker(self) -> None:
        startup_done = False
        while not self._stop.is_set():
            self._wake.wait(timeout=30)
            self._wake.clear()
            if self._stop.is_set():
                break
            # let the user's own requests go first
            for _ in range(40):
                if self._idle_check() or self._stop.is_set():
                    break
                time.sleep(0.25)
            if self._stop.is_set() or not self.enabled:
                continue
            ready = self.llm is not None and getattr(self.llm, "model", None) and getattr(self.llm, "online", False)
            try:
                if ready and self._pending:
                    self.learn_now()
                if ready and not startup_done:
                    startup_done = True
                    self.summarise_pending()
                if ready and self._last_turn_at and time.time() - self._last_turn_at > 600 and self._idle_check():
                    self._last_turn_at = 0.0
                    self.summarise_pending(include_current=True)
                self.embed_missing()
            except Exception:
                log.exception("Memory background step failed")

    # ------------------------------------------------------------- export
    def export(self) -> dict:
        if self.store is None:
            return {}
        return {
            "exported": datetime.now().isoformat(timespec="seconds"),
            "memories": [m.to_dict() for m in self.store.all()],
            "episodes": [e.to_dict() for e in self.store.episodes(limit=1000)],
        }

    def clear(self) -> None:
        if self.store is not None:
            with self._busy:
                self._pending.clear()
            self.store.clear()
            self.emit("memory_changed", stats=self.stats())


def _ago(ts: float) -> str:
    seconds = max(0, time.time() - ts)
    if seconds < 3600:
        return "Earlier today" if datetime.fromtimestamp(ts).date() == datetime.now().date() else "Recently"
    days = (datetime.now().date() - datetime.fromtimestamp(ts).date()).days
    if days <= 0:
        return "Earlier today"
    if days == 1:
        return "Yesterday"
    if days < 7:
        return datetime.fromtimestamp(ts).strftime("Last %A") if days >= 2 else "Yesterday"
    return datetime.fromtimestamp(ts).strftime("On %d %B")
