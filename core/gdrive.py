"""Understanding requests about the user's existing Google files:

    "open my Messi doc"   "open the budget spreadsheet"   "open it"   "show me my pitch deck"
    "read my Messi doc to me"   "what's in my shopping list doc"   "what docs do I have"

``parse_google_request`` only recognises the request; the assistant decides what to do with it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_LEAD = (r"^(?:(?:hey |ok |okay )?jarvis[, ]+)?(?:please |can you |could you |would you |will you |i (?:want|need) you to |"
         r"i'd like to |i want to |go ahead and |now )*")
_VERB = (r"(?P<verb>open(?:\s+up)?|pull\s+up|bring\s+up|launch|go\s+to|take\s+me\s+to|show(?:\s+me)?|display|view|"
         r"read(?:\s+out)?|what'?s\s+in|what\s+is\s+in|what\s+does|find|get)")
_KIND = (r"(?P<kind>google\s+docs?|google\s+documents?|docs?|documents?|google\s+sheets?|sheets?|spreadsheets?|"
         r"google\s+slides?|slides?|slideshows?|slide\s+decks?|presentations?|decks?)")
_TAIL = re.compile(r"[\s,]*(?:(?P<aloud>(?:out\s+loud|aloud|to\s+me|for\s+me))|(?P<here>(?:here|in\s+jarvis|on\s+(?:the\s+)?screen|"
                   r"in\s+(?:your|the)\s+(?:window|reader|viewer)|inside\s+jarvis))|please|now|again|say)(?=[\s.!?]*$|[\s,])",
                   re.IGNORECASE)
_RECENT = r"(?:last|latest|most\s+recent|newest|previous|recent|latest)"
_NAME_BEFORE = re.compile(r"^(?:the|my|our|that|this|a|an)?\s*(?:(?P<recent>" + _RECENT + r")\s+)?(?P<name>.*?)\s*" + _KIND + r"s?$",
                          re.IGNORECASE)
_NAME_AFTER = re.compile(r"^(?:the|my|our|a|an)?\s*" + _KIND + r"s?\s+(?:called|named|titled|about|on|for|with)\s+(?P<name>.+)$",
                         re.IGNORECASE)
_PRONOUN = re.compile(r"^(?:it|that|this|the\s+last\s+one|the\s+one\s+(?:you|we)\s+(?:just\s+)?(?:made|wrote|created))$", re.IGNORECASE)
_JUST_MADE = re.compile(r"^(?:the|my|our|that|this)?\s*(?:new(?:est)?\s+|brand\s+new\s+)?(?:google\s+)?" + _KIND
                        + r"s?(?:\s+(?:that\s+|which\s+)?(?:you|we|i)\s+(?:just\s+|recently\s+)?(?:made|wrote|created|started|opened|did))?$"
                        r"|^(?:the|my|our)\s+new(?:est)?\s+(?:google\s+)?" + _KIND.replace("?P<kind>", "?:") + r"s?$", re.IGNORECASE)
_LISTING = re.compile(
    _LEAD + r"(?:(?:what|which)\s+(?:google\s+)?(?:docs?|documents|files|sheets|spreadsheets|slides|presentations|decks)\s+do\s+i\s+have"
    r"|(?:show|list|open|get|find)(?:\s+me)?\s+(?:all\s+)?(?:of\s+)?my\s+(?:recent|latest|newest|last)\s+(?:google\s+)?"
    r"(?:docs?|documents|files|work|sheets|spreadsheets|slides|presentations|decks)"
    r"|(?:show|list)(?:\s+me)?\s+my\s+(?:google\s+)?(?:docs|documents|files|drive)"
    r"|what(?:'s| is)\s+(?:in|on)\s+my\s+(?:google\s+)?drive|recent\s+(?:google\s+)?(?:docs|documents|files))[\s.!?]*$", re.IGNORECASE)
# These look like requests about Google files but are something else.
_NOT_GOOGLE = re.compile(r"\b(?:word|\.docx?|\.xlsx?|\.pptx?|pdf|file|folder|on my (?:desktop|computer|pc)|in my downloads)\b", re.IGNORECASE)


@dataclass
class GoogleRequest:
    action: str  # "open" | "view" | "list"
    kind: str | None = None  # "doc" | "slides" | "sheet" (None: whatever was used last)
    name: str = ""
    recent: bool = False  # "my last doc": no name, newest first
    google: bool = False  # the user said "google" (so don't look for a local file)
    aloud: bool = False


def _kind(word: str | None) -> str | None:
    w = (word or "").lower()
    if not w:
        return None
    if "sheet" in w:
        return "sheet"
    if "slide" in w or "presentation" in w or "deck" in w:
        return "slides"
    return "doc"


def parse_google_request(text: str, have_last: bool = False) -> GoogleRequest | None:
    raw = (text or "").strip()
    if _LISTING.match(raw):
        return GoogleRequest("list")
    verb_match = re.match(_LEAD + _VERB + r"\s+(?P<rest>.+?)[\s.!?]*$", raw, re.IGNORECASE)
    if not verb_match:
        return None
    verb = re.sub(r"\s+", " ", verb_match.group("verb").lower())
    rest = verb_match.group("rest").strip()
    aloud = here = False
    while True:  # peel "to me", "here", "please" off the end
        tail = re.search(r"[\s,]*(?:out\s+loud|aloud|to\s+me|for\s+me|here|in\s+jarvis|on\s+(?:the\s+)?screen|in\s+(?:your|the)\s+"
                         r"(?:window|reader|viewer)|inside\s+jarvis|please|now)$", rest, re.IGNORECASE)
        if not tail:
            break
        word = tail.group(0).lower()
        aloud = aloud or bool(re.search(r"loud|to me|for me", word))
        here = here or bool(re.search(r"here|jarvis|screen|window|reader|viewer", word))
        rest = rest[: tail.start()].strip()
    action = "view" if (here or aloud or verb.startswith(("read", "what", "show", "display", "view"))) else "open"
    if verb in ("find", "get") and not here:
        action = "open"
    if _NOT_GOOGLE.search(rest) or re.fullmatch(r"(?:google\s+)?(?:docs|sheets|slides|drive|forms)", rest, re.IGNORECASE):
        return None  # "open google docs" is the website: the normal open-a-site path handles it
    if _PRONOUN.match(rest):
        return GoogleRequest(action, None, "", recent=True, aloud=aloud) if have_last else None
    made = _JUST_MADE.match(rest)
    if made:  # "the document you just made", "my new doc", "the presentation we created"
        return GoogleRequest(action, _kind(made.group("kind")), "", recent=True, aloud=aloud)
    after = _NAME_AFTER.match(rest)
    if after:
        name = after.group("name").strip(" \"'")
        google = bool(re.match(r"google", after.group("kind"), re.I)) or bool(re.search(r"\bgoogle\b", rest, re.I))
        return GoogleRequest(action, _kind(after.group("kind")), name, google=google, aloud=aloud)
    before = _NAME_BEFORE.match(rest)
    if before:
        name = re.sub(r"\b(?:google|my|the|our|a|an)\b", " ", before.group("name"), flags=re.I)
        name = re.sub(r"\s+", " ", name).strip(" \"'")
        google = bool(re.search(r"\bgoogle\b", rest, re.I))
        recent = bool(before.group("recent")) or not name
        if name and before.group("recent"):
            name = ""  # "my latest doc"
        return GoogleRequest(action, _kind(before.group("kind")), name, recent=recent, google=google, aloud=aloud)
    return None


# ----------------------------------------------------------------------------- new, blank files
@dataclass
class NewFileRequest:
    kind: str  # "doc" | "slides" | "sheet"
    title: str = ""


_NEW_FILE = re.compile(
    _LEAD + r"(?:create|make|start|open|begin|set\s+up|give\s+me|new)(?:\s+(?:me|up))?\s+(?:a\s+|an\s+|another\s+)?"
    r"(?:(?:new|blank|empty|fresh)\s+)+(?P<kind>google\s+docs?|google\s+documents?|docs?|documents?|google\s+sheets?|sheets?|spreadsheets?|"
    r"google\s+slides?|slides?|slideshows?|slide\s+decks?|presentations?|decks?)"
    r"(?:\s+(?:called|named|titled|with\s+the\s+(?:title|name))\s+(?P<title>.+?))?[\s.!?]*$", re.IGNORECASE)
_NEW_FILE_PLAIN = re.compile(  # "create a document called Shopping list" (no "new", but a name)
    _LEAD + r"(?:create|make|start|set\s+up)\s+(?:me\s+)?(?:a|an)\s+(?P<kind>google\s+docs?|google\s+documents?|docs?|documents?|"
    r"google\s+sheets?|sheets?|spreadsheets?|google\s+slides?|slides?|presentations?|decks?)"
    r"(?:\s+(?:called|named|titled)\s+(?P<title>.+?))?[\s.!?]*$", re.IGNORECASE)
_NEW_BARE = re.compile(r"^(?:a\s+)?new\s+(?P<kind>google\s+docs?|google\s+sheets?|google\s+slides?|docs?|documents?|spreadsheets?|presentations?)[\s.!?]*$", re.I)


def parse_new_file(text: str) -> NewFileRequest | None:
    """'create a new document', 'make a blank presentation called Pitch', 'new google sheet' -> NewFileRequest."""
    t = re.sub(r"(?:[\s,]+(?:for\s+me|please|now|right\s+now|real\s+quick|quickly))+[\s.!?]*$", "", (text or "").strip(), flags=re.I)
    if _NOT_GOOGLE.search(t) and not re.search(r"\bgoogle\b", t, re.I):
        return None  # "create a new Word document", "make a new folder"
    m = _NEW_FILE.match(t) or _NEW_FILE_PLAIN.match(t) or _NEW_BARE.match(t)
    if not m:
        return None
    title = (m.groupdict().get("title") or "").strip(" \"'.")
    if re.match(r"^(?:about|on|for)\b", title, re.I):
        return None  # that's a writing request
    title = title[:1].upper() + title[1:] if title else ""
    return NewFileRequest(_kind(m.group("kind")) or "doc", title[:100])
