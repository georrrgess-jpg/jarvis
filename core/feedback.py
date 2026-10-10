"""'That was wrong': learning from the user's corrections.

When the user says JARVIS got something wrong ("that was wrong", "no, I said open Discord", "I meant Spotify"),
it compares what it heard with what was meant and learns the smallest change that turns one into the other:

* a substitution inside the sentence ("this cord" -> "discord", "fire fox" -> "firefox") is applied wherever those
  words come up again, as whole words, and only to speech when it was learnt from speech;
* anything else (words added or dropped) is learnt for that exact sentence only.

Every rule is visible (Automation Center > Corrections) and can be deleted; nothing is learnt from a chat answer,
only from commands, and a rule never fires inside a word or on a common little word ("to", "the", "it").
"""

from __future__ import annotations

import difflib
import re
import time
from dataclasses import dataclass

MAX_RULES = 200
_STOP = {"a", "an", "the", "to", "too", "two", "it", "its", "is", "in", "on", "of", "and", "or", "for", "my", "me", "i", "you",
         "this", "that", "be", "at", "up", "so", "do", "no", "yes", "please", "can", "could", "would", "will"}

_WRONG = re.compile(
    r"^(?:no+[,.!]?\s+|nope[,.!]?\s+|wait[,.!]?\s+)?(?:jarvis[,.!]?\s+)?"
    r"(?:that(?:'s|\s+was|\s+is)\s+(?:wrong|not\s+right|incorrect|a\s+mistake|not\s+what\s+i\s+(?:said|meant|wanted|asked(?:\s+for)?))"
    r"|you\s+(?:got\s+(?:that|it)\s+wrong|misheard(?:\s+me)?|made\s+a\s+mistake)|(?:that'?s\s+)?wrong(?:\s+one|\s+app|\s+song|\s+thing)?"
    r"|not\s+that(?:\s+one)?|that'?s\s+not\s+it)[\s.!]*$", re.I)
_MEANT = re.compile(
    r"^(?:no+[,.!]?\s+|nope[,.!]?\s+)?(?:(?:that(?:'s|\s+was)\s+(?:wrong|not\s+right)|wrong)[,.!]?\s+)?"
    r"(?:i\s+(?:said|meant|asked\s+for|wanted|was\s+saying)|i\s+meant\s+to\s+say|what\s+i\s+(?:said|meant)\s+was)[,:]?\s+(?P<meant>.+?)[\s.!]*$", re.I)
_NEVER_MIND = re.compile(r"^(?:never\s*mind|forget\s+it|it'?s\s+fine|it'?s\s+ok(?:ay)?|cancel|don'?t\s+worry(?:\s+about\s+it)?)[\s.!]*$", re.I)
_VERB = re.compile(
    r"^(?P<verb>(?:please\s+)?(?:open|launch|start|run|play|close|quit|switch\s+to|go\s+to|search(?:\s+for)?|google|show\s+me|put\s+on|"
    r"pause|resume|stop|turn\s+(?:up|down|on|off)|mute|unmute|set|text|email|call|find|type(?:\s+out)?|write)"
    r"(?:\s+(?:the|my|some|a|an))?)\s+(?P<rest>.+)$", re.I)


@dataclass
class Feedback:
    kind: str  # "wrong" (no correction given yet) | "meant" (with what was meant)
    meant: str = ""


def parse_feedback(text: str) -> Feedback | None:
    t = (text or "").strip()
    m = _MEANT.match(t)
    if m and m.group("meant").strip():
        return Feedback("meant", m.group("meant").strip(" \"'“”"))
    if _WRONG.match(t):
        return Feedback("wrong")
    return None


def is_never_mind(text: str) -> bool:
    return bool(_NEVER_MIND.match((text or "").strip()))


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", (text or "").lower())


def complete_command(heard: str, meant: str) -> str:
    """'I meant Spotify' after 'open discord' -> 'open Spotify'; a full command is used as it is."""
    meant = meant.strip()
    if _VERB.match(meant):
        return meant
    v = _VERB.match(heard.strip())
    if v and len(_words(meant)) <= 5:
        return f"{v.group('verb')} {meant}"
    return meant


def learn_rule(heard: str, meant: str) -> dict | None:
    """The smallest rewrite that turns what was heard into what was meant (None if they're the same)."""
    a, b = _words(heard), _words(meant)
    if not a or not b or a == b:
        return None
    ops = [op for op in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes() if op[0] != "equal"]
    if len(ops) == 1 and ops[0][0] == "replace":
        _, i1, i2, j1, j2 = ops[0]
        span_a, span_b = a[i1:i2], b[j1:j2]
        meaningful = [w for w in span_a if w not in _STOP and len(w) >= 3]
        if meaningful and len(span_a) <= 4 and len(span_b) <= 5:
            return {"heard": " ".join(span_a), "meant": " ".join(span_b), "whole": False}
    return {"heard": " ".join(a), "meant": " ".join(b), "whole": True}


class Corrections:
    """The learnt rules, stored in the settings file."""

    def __init__(self, config) -> None:
        self.config = config

    def all(self) -> list[dict]:
        return [dict(r) for r in (self.config.get("corrections") or []) if isinstance(r, dict) and r.get("heard") and r.get("meant")]

    def _save(self, rules: list[dict]) -> None:
        self.config.update({"corrections": rules[-MAX_RULES:]})

    def add(self, rule: dict, voice: bool) -> dict:
        rules = [r for r in self.all() if not (r["heard"] == rule["heard"] and r.get("whole") == rule.get("whole"))]
        # a rule that would undo a newer one is dropped ("discord" -> "this cord" after "this cord" -> "discord")
        rules = [r for r in rules if not (r["heard"] == rule["meant"] and r["meant"] == rule["heard"])]
        item = {"id": f"c{int(time.time() * 1000) % 10**10}", "heard": rule["heard"], "meant": rule["meant"], "whole": bool(rule.get("whole")),
                "voice_only": bool(voice), "uses": 0, "created": time.time()}
        rules.append(item)
        self._save(rules)
        return item

    def remove(self, rid: str) -> bool:
        rules = self.all()
        kept = [r for r in rules if r.get("id") != rid]
        self._save(kept)
        return len(kept) != len(rules)

    def apply(self, text: str, voice: bool) -> tuple[str, list[dict]]:
        """Rewrite what was said with the learnt rules; returns the new text and the rules used."""
        rules = [r for r in self.all() if voice or not r.get("voice_only")]
        if not rules or not text:
            return text, []
        used: list[dict] = []
        norm = " ".join(_words(text))
        for r in rules:
            if r.get("whole") and norm == r["heard"]:
                used.append(r)
                text = r["meant"]
                norm = " ".join(_words(text))
                break
        for r in rules:
            if r.get("whole"):
                continue
            pattern = r"(?<![\w'])" + r"[\s\-]+".join(re.escape(w) for w in r["heard"].split()) + r"(?![\w'])"
            if r["meant"] in norm and r["heard"] in r["meant"]:
                continue  # already says it ("discord ptb" must not become "discord ptb ptb")
            new = re.sub(pattern, r["meant"], text, flags=re.I)
            if new != text:
                text = new
                used.append(r)
                norm = " ".join(_words(text))
        if used:
            ids = {r["id"] for r in used}
            rules = self.all()
            for r in rules:
                if r.get("id") in ids:
                    r["uses"] = int(r.get("uses") or 0) + 1
                    r["last_used"] = time.time()
            self._save(rules)
        return text, used
