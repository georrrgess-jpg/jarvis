"""Does what the user said sound finished?

JARVIS uses this when you pause: a sentence that trails off ("open the...", "write an email to John and...",
"um...") means you're still thinking, so it keeps listening instead of answering over you.
"""

from __future__ import annotations

import re

# Words a finished sentence essentially never ends with: conjunctions, articles, prepositions,
# auxiliaries, determiners and hesitation noises (English, plus the common ones in other languages).
_DANGLING = {
    # English: conjunctions, articles, prepositions, possessives and hesitation noises. Words that often end a
    # finished sentence ("tell me", "turn it on", "yes I can", "where it is", "who am I") are deliberately absent.
    "and", "or", "but", "because", "if", "then", "the", "a", "an", "to", "of", "for", "with", "at", "by", "from", "about",
    "into", "onto", "than", "as", "when", "while", "where", "which", "who", "whom", "whose", "that's", "my", "your", "their",
    "our", "its", "i'm", "i'll", "i'd", "i've", "also", "plus", "between", "after", "before", "until", "since", "though",
    "although", "whether", "either", "neither", "some", "any", "every", "um", "uh", "er", "erm", "hmm", "umm", "uhh", "let",
    "let's", "lets", "what", "how", "why", "what's", "whats", "how's", "hows", "who's", "whos",
    # Spanish / French / German / Italian / Portuguese: conjunctions, articles, prepositions
    "y", "pero", "porque", "el", "la", "los", "las", "un", "una", "de", "del", "que", "para", "con", "por", "mi", "su",
    "et", "ou", "mais", "parce", "le", "les", "une", "des", "du", "pour", "avec", "dans", "mon", "ma", "mes",
    "und", "oder", "aber", "weil", "der", "ein", "eine", "zu", "mit", "für", "auf", "mein", "meine", "dass",
    "perché", "il", "gli", "di", "per", "che", "mio", "mia", "mas", "os", "uma", "dos", "com", "meu", "minha",
}
# A lone command verb is a request that hasn't been finished yet.
_BARE_VERBS = {
    "open", "launch", "start", "play", "write", "email", "send", "search", "find", "show", "read", "create", "make", "add", "delete",
    "remove", "tell", "call", "set", "look", "go", "get", "give", "put", "turn", "close", "check", "remind", "translate", "draft",
    "compose", "schedule", "list", "bring", "pull", "hey", "please",
}
_AUX = {"is", "are", "was", "were", "do", "does", "did", "can", "will", "would", "should", "could"}
_QUESTION = {"what", "who", "where", "how", "why", "when", "which"}
_TRAILING = re.compile(r"(?:[,;:\-–—]|\.\.\.|…)\s*$")
_WORD = re.compile(r"[\w'’]+", re.UNICODE)


def looks_unfinished(text: str) -> bool:
    """True when ``text`` reads like the speaker stopped mid-thought."""
    text = (text or "").strip()
    if not text:
        return False  # nothing was heard: there is nothing to wait for
    if _TRAILING.search(text):
        return True
    words = [w.lower().replace("’", "'") for w in _WORD.findall(text)]
    if not words:
        return False
    if len(words) == 1 and (words[0] in _BARE_VERBS or words[0] == "jarvis"):
        return True
    if len(words) >= 2 and words[-1] in _AUX and words[-2] in _QUESTION:
        return True  # "what is", "where are" ... the question's subject is still to come
    if len(words) <= 3 and words[0] in {"hey", "hi", "ok", "okay"} and words[-1] == "jarvis":
        return True  # just the name: the request is still to come
    return words[-1] in _DANGLING
