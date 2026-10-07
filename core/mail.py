"""Email: understand "email John saying I'll be late", write the email, and send it only once confirmed.

Sending goes through the user's own Google bridge (Gmail, free, no API key). Without it, the draft is
opened in Gmail's compose window in the browser, where the user presses Send themselves.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlencode

_LEAD = (r"^(?:(?:hey |ok |okay )?jarvis[, ]+)?(?:please |can you |could you |would you |will you |i (?:want|need) to |"
         r"i'd like to |i want you to |go ahead and |quickly )*")
_MAIL = r"(?:e-?mail|gmail|mail)"
_SAY = (r"(?:saying|that\s+says|to\s+say|and\s+say|telling\s+(?:him|her|them)|and\s+tell\s+(?:him|her|them)|"
        r"asking\s+(?:him|her|them)|and\s+ask\s+(?:him|her|them)|letting\s+(?:him|her|them)\s+know|"
        r"and\s+let\s+(?:him|her|them)\s+know|about|regarding|re|on|that|to\s+ask|to\s+tell\s+(?:him|her|them)|"
        r"with\s+the\s+subject|inviting\s+(?:him|her|them)|to\s+invite\s+(?:him|her|them)|thanking\s+(?:him|her|them)|"
        r"to\s+thank\s+(?:him|her|them))")
_DOC = r"(?:the\s+|my\s+|that\s+|this\s+)?(?:(?:google\s+)?(?:doc|document|presentation|deck|slides|slideshow|link)|(?P<docname>.+?)\s+(?:doc|document|presentation|deck|slides|bio|biography|essay|report))"
_SHARE = [
    # email it / the doc / my Messi bio to Tom
    re.compile(_LEAD + r"(?:e-?mail|mail|send)\s+(?P<thing>it|that|this|" + _DOC + r")\s+to\s+(?P<who>.+?)"
               r"(?:\s+(?:by|via|over)\s+e-?mail)?(?:\s+(?P<say>saying|and\s+say|with\s+a\s+note|and\s+tell\s+(?:him|her|them))\s+(?P<what>.+?))?[\s.!?]*$", re.I),
]
_PATTERNS = [
    # send an email to John saying ...
    re.compile(_LEAD + r"(?:send|write|draft|compose|shoot|fire\s+off|do)\s+(?:a\s+|an\s+|a\s+quick\s+|a\s+short\s+)?"
               + _MAIL + r"\s+(?:message\s+)?to\s+(?P<who>.+?)(?:\s+(?P<say>" + _SAY + r")\s+(?P<what>.+?))?[\s.!?]*$", re.I),
    # send John an email saying ...
    re.compile(_LEAD + r"(?:send|write|shoot|drop)\s+(?P<who>.+?)\s+(?:a\s+|an\s+|a\s+quick\s+)?" + _MAIL
               + r"(?:\s+(?P<say>" + _SAY + r")\s+(?P<what>.+?))?[\s.!?]*$", re.I),
    # email John saying ... / email John that ...
    re.compile(_LEAD + r"e-?mail\s+(?P<who>.+?)(?:\s+(?P<say>" + _SAY + r")\s+(?P<what>.+?))?[\s.!?]*$", re.I),
    # let John know by email that ...
    re.compile(_LEAD + r"(?:let|tell|ask|remind|invite|thank)\s+(?P<who>.+?)\s+(?:know\s+)?(?:by|via|in\s+an?|over)\s+"
               + _MAIL + r"\s*(?P<say>that|to|about)?\s*(?P<what>.+?)[\s.!?]*$", re.I),
]
_ADDRESS = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")
_CONFIRM = re.compile(r"^(?:yes|yeah|yep|yup|sure|ok(?:ay)?|go\s+ahead|do\s+it|confirm(?:ed)?|please\s+do|absolutely|"
                      r"send(?:\s+it|\s+that|\s+the\s+(?:e-?mail|message))?(?:\s+now)?|yes\s+please|please\s+send(?:\s+it)?|"
                      r"looks\s+good|perfect)\b(?:[\s,]*(?:send\s+it|please|thanks|thank\s+you|jarvis|sir|now))*[\s.!]*$", re.I)
_CANCEL = re.compile(r"^(?:no|nope|cancel(?:\s+it)?|don'?t\s+send(?:\s+it)?|do\s+not\s+send(?:\s+it)?|discard(?:\s+it)?|"
                     r"delete\s+it|scrap\s+(?:it|that)|never\s*mind|forget\s+it|not\s+now)\b[\s\w,.!]*$", re.I)


@dataclass
class EmailRequest:
    who: str
    what: str
    how: str = ""  # the connecting phrase ("saying", "asking him"...): context for the writer
    address: str | None = None
    share: bool = False  # sending a document JARVIS made ("email it to Tom")
    doc_name: str = ""


@dataclass
class Draft:
    id: str
    to: str
    to_name: str
    subject: str
    body: str
    candidates: list[dict] = field(default_factory=list)
    share_id: str = ""  # a Drive file to share with the recipient before sending

    def as_event(self) -> dict:
        return {"id": self.id, "to": self.to, "to_name": self.to_name, "subject": self.subject, "body": self.body,
                "candidates": self.candidates}


def spoken_address(text: str) -> str | None:
    """'john dot smith at gmail dot com' -> 'john.smith@gmail.com'."""
    t = (text or "").strip().lower()
    t = re.sub(r"\s+(?:at)\s+", "@", t)
    t = re.sub(r"\s+(?:dot)\s+", ".", t)
    t = re.sub(r"\s+(?:underscore)\s+", "_", t)
    t = re.sub(r"\s+(?:dash|hyphen)\s+", "-", t)
    t = t.replace(" ", "")
    match = _ADDRESS.fullmatch(t) or _ADDRESS.search(text or "")
    return match.group(0) if match else None


def parse_email_request(text: str) -> EmailRequest | None:
    raw = (text or "").strip()
    for pattern in _SHARE:
        match = pattern.match(raw)
        if match:
            thing = match.group("thing").lower()
            plain_send = re.match(_LEAD + r"send\b", raw, re.I) and not re.search(r"\be-?mail\b", raw, re.I)
            if plain_send and thing in ("it", "that", "this"):
                return None  # "send it to John" could mean anything; "email it to John" is clear
            who = match.group("who").strip(" ,\"'")
            return EmailRequest(who=who, what=(match.group("what") or "").strip(), how=(match.group("say") or "").lower(),
                                address=spoken_address(who), share=True, doc_name=(match.group("docname") or "").strip())
    for pattern in _PATTERNS:
        match = pattern.match(raw)
        if not match:
            continue
        who = re.sub(r"^(?:to\s+)?(?:my\s+)?", "", match.group("who").strip(" ,\"'"), flags=re.I)
        if not who or len(who) > 80 or re.fullmatch(r"(?:it|that|this|them|him|her|an?|the)", who, re.I):
            return None
        what = (match.group("what") or "").strip(" ,\"'")
        how = (match.group("say") or "").lower() if match.groupdict().get("say") else ""
        return EmailRequest(who=who, what=what, how=how, address=spoken_address(who))
    return None


def is_confirmation(text: str) -> bool:
    return bool(_CONFIRM.match((text or "").strip()))


def is_cancellation(text: str) -> bool:
    return bool(_CANCEL.match((text or "").strip()))


def first_name(name: str) -> str:
    name = (name or "").strip()
    if "@" in name:
        name = re.split(r"[._+-]", name.split("@")[0])[0]
    return name.split()[0].capitalize() if name else ""


def email_prompt(req: EmailRequest, to_name: str, sender: str, language: str | None) -> tuple[str, str]:
    lang = language or "English"
    signoff = (f"Finish with a short sign-off line and then the sender's name, {sender}, on its own line."
               if sender else "Finish with a short sign-off such as 'Best regards,' and no name after it.")
    system = (
        f"You write clear, friendly, natural emails in {lang} on the user's behalf. Output exactly this format and "
        "nothing else:\nSubject: <a short specific subject line>\n\n<the email body>\n"
        "The body opens with a greeting using the recipient's first name, says what the user wants in a few short "
        f"paragraphs at most, and matches the tone of the request. {signoff} "
        "Never use placeholders in square brackets and never invent facts, dates or details the user didn't give."
    )
    who = to_name or req.who
    wish = f"{req.how} {req.what}".strip() or "a short friendly hello"
    user = f'Recipient: {who}\nWhat the user wants the email to say: "{wish}"'
    return system, user


def parse_email(text: str, fallback_subject: str) -> tuple[str, str]:
    """Split the writer's output into (subject, body)."""
    text = str(text or "").replace("\r", "").strip()
    text = re.sub(r"^```\w*\n|\n```$", "", text).strip()
    subject = ""
    match = re.search(r"^\**subject\**\s*:\s*(.+)$", text, re.I | re.M)
    if match:
        subject = match.group(1).strip(" *\"'")
        text = (text[: match.start()] + text[match.end():]).strip()
    text = re.sub(r"^\**body\**\s*:\s*", "", text, flags=re.I).strip()
    text = re.sub(r"\[[^\]\n]{2,40}\]", "", text)  # stray [Your Name] placeholders
    return subject or fallback_subject, re.sub(r"\n{3,}", "\n\n", text).strip()


def gmail_compose_url(to: str, subject: str, body: str) -> str:
    return "https://mail.google.com/mail/?" + urlencode({"view": "cm", "fs": "1", "to": to, "su": subject, "body": body})
