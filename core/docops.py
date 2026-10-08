"""Understanding requests that work on a document, possibly in several steps:

    "rename the Google Doc to Pizza recipe and then type out a pizza recipe I can make"
    "create a new doc called Shopping list and write a list for a weekly shop in it"
    "call it Trip plans"   "type out a cover letter for a barista job"   "type 'hello world' into the doc"

``parse_doc_command`` only works out the steps and which document is meant; the assistant runs them
(through the user's Google bridge, or by typing into the window when that's what's on screen).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .gdrive import parse_new_file

_FILL = r"(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?|please\s+|i\s+(?:want|need|would\s+like|'d\s+like)\s+you\s+to\s+|go\s+ahead\s+and\s+|also\s+|now\s+|just\s+|then\s+|and\s+)*"
_VERBS = (r"(?:rename|re-?title|title|call|name|change\s+(?:the\s+)?(?:name|title)|set\s+(?:the\s+)?(?:name|title)|type|write|put|add|insert|"
          r"draft|fill|jot|open|create|make|start|paste|clear|erase|delete\s+everything|empty)")
# a new step starts at "and then", "then", "after that", "and after you rename it to X", ", and" ... followed by a verb
_AFTER = (r"(?:after|once|when)\s+(?:that|this|you(?:'re|\s+are)?\s+done(?:\s+(?:with\s+)?(?:that|it))?|that(?:'s|\s+is)\s+done|"
          r"you(?:'ve|\s+have)?\s+(?:done|finished)(?:\s+(?:that|it))?|you(?:'ve|\s+have)?\s+\w+(?:ed)?\s+it(?:\s+(?:to|as)\s+.+?)?)\s*,?\s+")
_JOIN = r"(?:(?:and|then|next|also|afterwards|after\s+that|finally)\s+)"
_SPLIT = re.compile(r"(?:\s*[,;.]\s*" + _JOIN + r"*(?:" + _AFTER + r")?|\s+" + _JOIN + r"+(?:" + _AFTER + r")?|\s+" + _AFTER + r")"
                    r"(?=" + _FILL + _VERBS + r"\b)", re.I)

_DOC_WORD = r"(?:google\s+)?(?:doc|docs|document|documents|file)"
_REF = (r"(?:it|that|this|that\s+one|this\s+one|there|the\s+(?:new\s+|open\s+|current\s+|same\s+)?" + _DOC_WORD +
        r"|(?:my|the)\s+(?P<named>[^,]+?)\s+" + _DOC_WORD + r")")
_RENAME = [
    re.compile(r"^(?:rename|re-?title)\s+(?:" + _REF + r"\s+)?(?:to|as|into)\s+(?P<name>.+)$", re.I),
    re.compile(r"^(?:change|set)\s+(?:the\s+)?(?:name|title)(?:\s+of\s+" + _REF + r")?\s+(?:to|as)\s+(?P<name>.+)$", re.I),
    re.compile(r"^(?:call|name|title)\s+(?:it|that|this|the\s+(?:new\s+)?" + _DOC_WORD + r")\s+(?P<name>.+)$", re.I),
    re.compile(r"^(?:rename|re-?title)\s+" + _REF + r"\s+(?P<name>[^,]+)$", re.I),  # "rename it Pizza recipe"
]
_WHERE = r"(?:\s+(?:in|into|inside|on|to|onto|at\s+the\s+(?:end|bottom)\s+of)\s+" + _REF + r")?"
_WRITE = re.compile(r"^(?:type|write|put|add|insert|draft|jot|fill\s+(?:it|in)(?:\s+with)?|paste)(?:\s+(?:out|up|down|in))?\s+(?:me\s+|for\s+me\s+|us\s+)?"
                    r"(?P<what>.+?)" + _WHERE + r"(?:\s+(?:please|for me|now))?[\s.!?]*$", re.I)
_LITERAL = re.compile(r"^(?:[\"“'‘](?P<q>.+)[\"”'’]|(?:the\s+(?:words?|text|sentence|line|phrase)|exactly|the\s+following|this)\s*:?\s+(?P<w>.+)|"
                      r"(?P<colon>.+?):\s*(?P<after>.+))$", re.I | re.S)
_CONTENT = re.compile(r"^(?:a|an|some|the|my|our|me\s+a|another|one|two|three|\d+)\s+.+", re.I)
_REF_PLAIN = _REF.replace("?P<named>", "?:")
_CLEAR = re.compile(r"^(?:(?:clear|erase|empty|wipe)\s+(?:out\s+)?(?:" + _REF_PLAIN + r"|everything(?:\s+(?:in|from)\s+" + _REF_PLAIN + r")?)"
                    r"|delete\s+everything(?:\s+(?:in|from)\s+" + _REF_PLAIN + r")?)$", re.I)
_OPEN = re.compile(r"^(?:open|pull\s+up|bring\s+up|go\s+to)\s+(?:up\s+)?" + _REF + r"$", re.I)
_MENTIONS_DOC = re.compile(r"\b(?:google\s+doc|doc|docs|document)\b", re.I)


@dataclass
class DocStep:
    action: str  # "create" | "open" | "rename" | "write" | "type" | "clear"
    text: str = ""  # the new name, what to write, or the exact words to type
    verb: str = ""  # the word the user used ("type", "write", "add"...)


@dataclass
class DocCommand:
    steps: list[DocStep] = field(default_factory=list)
    name: str = ""  # a document the user named ("my pizza doc")
    kind: str = "doc"
    mentions_doc: bool = False  # they said "the doc" / "it" / a name: it's about a document, not just the window

    @property
    def needs_document(self) -> bool:
        return any(s.action in ("rename", "create", "open", "clear") for s in self.steps) or self.mentions_doc


def _strip(clause: str) -> str:
    c = re.sub(r"^\s*(?:hey\s+|ok\s+|okay\s+)?\w*[,]\s*(?=" + _FILL + _VERBS + r"\b)", "", clause.strip(), flags=re.I)  # "Harper, rename..."
    c = re.sub(r"^" + _FILL, "", c, flags=re.I)
    return re.sub(r"[\s.!?]+$", "", c).strip()


def clean_name(name: str) -> str:
    n = re.sub(r"\s+(?:please|for me|now|right now|thanks|thank you)$", "", name.strip(), flags=re.I)
    n = n.strip(" \"'“”‘’.,!?")
    return (n[:1].upper() + n[1:])[:120] if n else ""


def _literal(what: str) -> str | None:
    """The exact words, when the user dictated them ("type 'hello world'", "type the words: see you soon")."""
    m = _LITERAL.match(what.strip())
    if not m:
        return None
    if m.group("q"):
        return m.group("q")
    if m.group("w"):
        return m.group("w").strip()
    if m.group("colon") and re.fullmatch(r"(?:this|the following|exactly|the text|these words|it)", m.group("colon").strip(), re.I):
        return m.group("after").strip()
    return None


def parse_doc_command(text: str) -> DocCommand | None:
    raw = re.sub(r"\s+", " ", (text or "").strip())
    if not raw or len(raw) > 600:
        return None
    clauses = [c for c in (_strip(c) for c in _SPLIT.split(raw)) if c]
    cmd = DocCommand(mentions_doc=bool(_MENTIONS_DOC.search(raw)))
    for clause in clauses:
        step = _clause(clause, cmd)
        if step is None:
            return None  # one part we don't understand: let the rest of JARVIS (or the model) handle the whole request
        cmd.steps.append(step)
    if not cmd.steps:
        return None
    if len(cmd.steps) == 1 and cmd.steps[0].action in ("create", "open"):
        return None  # plain "create a new doc" / "open the doc": the existing handlers do that
    return cmd


def _clause(clause: str, cmd: DocCommand) -> DocStep | None:
    new = parse_new_file(clause)
    if new is not None:
        cmd.kind = new.kind
        return DocStep("create", new.title)
    for pattern in _RENAME:
        m = pattern.match(clause)
        if m:
            name = clean_name(m.group("name"))
            if not name or len(name.split()) > 12:
                return None
            if m.groupdict().get("named"):
                cmd.name = clean_name(m.group("named"))
            cmd.mentions_doc = True
            return DocStep("rename", name)
    m = _CLEAR.match(clause)
    if m:
        cmd.mentions_doc = True
        return DocStep("clear")
    m = _OPEN.match(clause)
    if m:
        if m.groupdict().get("named"):
            cmd.name = clean_name(m.group("named"))
        cmd.mentions_doc = True
        return DocStep("open")
    m = _WRITE.match(clause)
    if m:
        what = m.group("what").strip()
        verb = clause.split()[0].lower()
        if re.search(r"\b(?:slides?|deck|presentation|spreadsheet|sheet|row|rows|column|cell|email|e-mail)\s+(?:to|in|into|on)\b|"
                     r"\b(?:to|in|into|on)\s+(?:my|the)\s+(?:\w+\s+)?(?:slides?|deck|presentation|spreadsheet|sheet)\b", clause, re.I):
            return None  # slides, sheets and emails have their own handlers
        if m.groupdict().get("named"):
            cmd.name = clean_name(m.group("named"))
            cmd.mentions_doc = True
        if re.search(r"\b(?:in|into|on|to)\s+(?:it|the\s+(?:google\s+)?doc(?:ument)?|there)\b", clause, re.I):
            cmd.mentions_doc = True
        exact = _literal(what)
        if exact is not None:
            return DocStep("type", exact, verb)
        if _CONTENT.match(what) and len(what.split()) >= 2:
            return DocStep("write", re.sub(r"^(?:me|us)\s+", "", what, flags=re.I), verb)
        return None  # "type hello world" is plain typing (the screen-control skill does that)
    return None


# ----------------------------------------------------------------------------- writing it
def writing_prompt(what: str, document_text: str = "", language: str | None = None, plain: bool = False) -> tuple[str, str]:
    lang = language or "English"
    if plain:
        style = ("Plain text only (it will be typed into an app): a title line, short paragraphs, '- ' for bullets and "
                 "'1.' for numbered steps. No Markdown symbols like # or **.")
    else:
        style = "Markdown: '# ' title first, '## ' section headings, '- ' bullets, '1.' numbered steps, **bold** sparingly."
    system = (f"You write clear, useful, well-organised content in {lang}. Output ONLY the content itself in {style} "
              "No preamble, no remarks, no questions.")
    context = f"\n\nThe document so far:\n\"\"\"\n{document_text[:5000]}\n\"\"\"\nAdd to it without repeating it." if document_text.strip() else ""
    return system, f"Write {what}.{context}"


def to_plain(markdown: str) -> str:
    """Markdown -> text that reads well when typed into any app."""
    lines = []
    for line in (markdown or "").splitlines():
        line = re.sub(r"^#{1,6}\s*", "", line)
        line = re.sub(r"^\s*[*+]\s+", "- ", line)
        line = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m.group(1) or m.group(2), line)
        line = re.sub(r"(?<![*\w])\*(?!\s)(.+?)\*(?!\w)", r"\1", line)
        lines.append(line.rstrip())
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def docs_tab_title(window_title: str) -> str | None:
    """'Pizza recipe - Google Docs - Google Chrome' -> 'Pizza recipe' (None if it isn't a Google Doc)."""
    m = re.match(r"^(?P<t>.+?)\s+[-–—]\s+Google\s+Docs(?:\s+[-–—]\s+.*)?$", window_title or "", re.I)
    return m.group("t").strip() if m else None
