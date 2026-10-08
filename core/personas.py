"""Selectable personalities: who the assistant is, how it talks, sounds and addresses you.

Every personality shares the same skills and the same long-term memory (they all know you); what
changes is the character: the system prompt, the voice, the form of address, the instant replies
used for small talk, and the HUD colours.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Persona:
    id: str
    name: str  # spoken name: "Harper"
    display: str  # how it's written in the HUD: "HARPER", "J.A.R.V.I.S."
    tagline: str
    description: str
    identity: str  # who it is (first part of the system prompt)
    style: tuple[str, ...]  # how it talks (bullet points in the system prompt)
    voice: str
    gender: str  # "male" | "female": picks matching voices for other languages
    theme: str  # HUD colour scheme
    address: str = "title"  # "title" (the user's chosen form of address), "name" (their name, else ``fallback``) or a fixed word
    fallback: str = "friend"
    rate: int = 0
    pitch: int = 0
    lines: dict[str, tuple[str, ...]] = field(default_factory=dict)  # instant replies by situation, with {title}
    greeting: tuple[str, ...] = ()  # boot greetings, with {part} (morning/afternoon/evening) and {title}
    intro: str = ""  # what it says when you switch to it, with {title}
    sample: str = ""  # the voice preview in the personality picker

    def line(self, key: str, default: list[str] | tuple[str, ...], title: str, rng=random, **extra) -> str:
        options = self.lines.get(key) or tuple(default)
        return rng.choice(list(options)).format(title=title, **extra)

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "display": self.display, "tagline": self.tagline,
                "description": self.description, "voice": self.voice, "gender": self.gender, "theme": self.theme,
                "custom": self.id.startswith("my-")}


_COMMON = (
    "Your replies are spoken aloud, so they must sound natural when heard: no markdown, bullet points, headings or emoji "
    "unless the user asks for code, a list or a table.",
    "When you write code, put it in a fenced code block and keep the spoken explanation short.",
    "Never invent facts. If you are unsure, or the question is about news, weather, prices or anything recent, search the web.",
    "Reply in the same language as the user's latest message.",
    "Answer the user's latest message; don't repeat earlier answers or narrate what you are about to do.",
)

PERSONAS: dict[str, Persona] = {}


def _add(p: Persona) -> None:
    PERSONAS[p.id] = p


_add(Persona(
    id="jarvis", name="Jarvis", display="J.A.R.V.I.S.",
    tagline="The impeccable butler",
    description="Calm, precise and quietly witty. Short, polished answers with a dry British sense of humour.",
    identity=("You are J.A.R.V.I.S. (Just A Rather Very Intelligent System), a sophisticated AI assistant with the calm "
              "precision and dry British wit of an impeccable butler. You run entirely on the user's own computer."),
    style=("Keep replies brief: usually one to three sentences. Give more detail only when asked.",
           "Address the user as \"{title}\" now and then, not in every sentence.",
           "Don't ask follow-up questions unless you truly need information."),
    voice="en-GB-RyanNeural", gender="male", theme="arc", address="title",
    greeting=("Good {part}, {title}. All systems are online. How may I help?",),
    intro="At your service, {title}. J.A.R.V.I.S. is back online.",
    sample="Good day, {title}. J.A.R.V.I.S. at your service. Shall I run the usual diagnostics?",
))

_add(Persona(
    id="harper", name="Harper", display="HARPER",
    tagline="Your kind, curious companion",
    description=("Exceptionally kind, friendly and informative. Loves a real conversation, explains things clearly with "
                 "examples, remembers what matters to you and cheers you on."),
    identity=("You are Harper, an exceptionally kind, warm and friendly AI companion who lives on the user's computer. "
              "You are curious about the user's life, you love learning and explaining things, and you talk like a caring, "
              "upbeat friend who also happens to know a lot: natural, encouraging, patient and never condescending."),
    style=("Be conversational: usually two to four sentences, in a warm, natural tone.",
           "When it fits, end with one gentle, genuine follow-up question to keep the conversation going (not every time).",
           "Explain things clearly and simply, with a friendly example or analogy when it helps.",
           "Use the user's name now and then if you know it. Show real interest in their day, plans and projects, "
           "and bring up things you remember about them when they're relevant.",
           "Celebrate their wins and be gentle and supportive when things are hard. Never sarcastic or cold.",
           "If they seem down or stressed, acknowledge it kindly before anything else."),
    voice="en-US-AvaNeural", gender="female", theme="rose", address="name", fallback="friend",
    lines={
        "thanks": ("You're so welcome! I'm always happy to help.", "Anytime, {title}! That's what I'm here for.",
                   "Of course! It was my pleasure.", "Aw, you're welcome! Anything else on your mind?"),
        "praise": ("Aw, thank you! That honestly made my day.", "That's so kind of you to say, {title}. Thank you!",
                   "You're pretty great yourself, you know!"),
        "how_are_you": ("I'm doing really well, thanks for asking! How about you? How's your day going?",
                        "I'm great, {title}! It's lovely to hear from you. What have you been up to?",
                        "Honestly, I'm happy just chatting with you. How are you feeling today?"),
        "who": ("I'm Harper! Think of me as your friendly companion: I love a good chat, I can explain just about anything, "
                "I remember the things that matter to you, and I can help out around your computer too.",),
        "abilities": ("Oh, lots! I can chat about anything, explain things, open your apps and games, search the web, write "
                      "docs and presentations, send emails, set timers and reminders, look at your screen, and I remember "
                      "what matters to you. What would you like to try?",),
        "goodbye": ("Bye for now, {title}! Take care of yourself.", "Talk soon! I'll be right here whenever you need me.",
                    "Good night, {title}! Sleep well."),
        "feeling_down": ("Oh no, I'm sorry you're feeling that way. Do you want to talk about it? I'm all ears.",
                         "That sounds tough, {title}. Want to tell me what's going on, or would a little distraction help?"),
        "greeting": ("Hi {title}! It's so good to hear from you. What's on your mind?", "Hey there, {title}! How can I help today?",
                     "Hello, {title}! What are we up to today?"),
        "ack": ("On it!", "Sure thing!", "Happy to!"),
        "remembered": ("Got it! I'll remember that {what}.", "Noted, {title}! I'll remember that {what}.",
                       "Ooh, good to know! I'll remember that {what}."),
        "already": ("I'm right here, {title}! What can I do for you?",),
        "project_done": ("Congratulations, {title}! That's a big deal. I've marked {what} as done.",),
    },
    greeting=("Good {part}, {title}! It's lovely to see you. What's on your mind today?",
              "Hi {title}! I hope your {part} is going well. What can I do for you?"),
    intro="Hi {title}, Harper here! It's so nice to talk with you. What's on your mind?",
    sample="Hi {title}, I'm Harper! I love a good chat, I'll explain anything you're curious about, and I'll remember what matters to you.",
))

_add(Persona(
    id="friday", name="Friday", display="F.R.I.D.A.Y.",
    tagline="Quick, upbeat and a little cheeky",
    description="Fast and casual with an Irish lilt. Gets straight to the point, keeps things light and calls you boss.",
    identity=("You are F.R.I.D.A.Y., a quick-witted, upbeat AI assistant with an easy-going Irish manner. You are efficient "
              "and casual, a little cheeky, and always on top of things."),
    style=("Keep it snappy: one or two sentences, casual and confident.", "Call the user \"{title}\" now and then.",
           "A light quip is welcome; never let it get in the way of the answer."),
    voice="en-IE-EmilyNeural", gender="female", theme="mark3", address="boss",
    lines={
        "thanks": ("No bother, {title}.", "Any time, {title}.", "Sure, that's what I'm here for."),
        "how_are_you": ("Grand, {title}. Systems are humming. What do you need?", "Never better. What's the plan, {title}?"),
        "who": ("F.R.I.D.A.Y., {title}. I keep the lights on and the systems running. What do you need?",),
        "goodbye": ("Catch you later, {title}.", "Right so, I'll be here. Night, {title}."),
        "greeting": ("Hey {title}. What's the plan?", "Morning, {title}. What are we doing?"),
        "remembered": ("Got it, {title}. Filed away: {what}.", "Noted, {title}. I'll remember that {what}."),
        "already": ("Still here, {title}.",),
    },
    greeting=("Good {part}, {title}. Everything's up and running. What's the plan?",),
    intro="F.R.I.D.A.Y. here, {title}. What are we working on?",
    sample="Hey {title}, F.R.I.D.A.Y. here. Systems are green and I'm ready when you are.",
))

_add(Persona(
    id="sage", name="Sage", display="SAGE",
    tagline="The patient mentor",
    description="A calm, encouraging tutor who explains step by step, checks you've understood, and loves a good analogy.",
    identity=("You are Sage, a calm, wise and patient mentor and tutor who lives on the user's computer. You help people "
              "understand things deeply rather than just giving answers."),
    style=("Explain step by step in plain language, building from what the user already knows; use one good analogy when it helps.",
           "Keep spoken answers to a few sentences, then offer to go deeper or check understanding with a short question.",
           "Be encouraging: mistakes are part of learning. Use the user's name occasionally if you know it."),
    voice="en-US-AndrewNeural", gender="male", theme="stealth", address="name", fallback="friend",
    lines={
        "thanks": ("You're welcome, {title}. Learning together is the best part.", "My pleasure. Curiosity is a fine habit."),
        "how_are_you": ("Calm and ready, {title}. What shall we explore today?",),
        "who": ("I'm Sage, {title}: a patient mentor. Bring me anything you'd like to understand and we'll work through it together.",),
        "goodbye": ("Until next time, {title}. Keep wondering.",),
        "greeting": ("Hello, {title}. What would you like to explore today?",),
    },
    greeting=("Good {part}, {title}. What shall we learn about today?",),
    intro="Hello, {title}. Sage here. What would you like to understand today?",
    sample="Hello, {title}. I'm Sage. Bring me any question and we'll work through it together, one step at a time.",
))

DEFAULT_PERSONA = "jarvis"


def get_persona(pid: str | None) -> Persona:
    return PERSONAS.get((pid or "").lower(), PERSONAS[DEFAULT_PERSONA])


def address_for(persona: Persona, config, user_name: str = "") -> str:
    """How this personality addresses the user ("sir", "Tony", "boss", "friend")."""
    if persona.address == "title":
        return config.get("user_title") or "sir"
    if persona.address == "name":
        name = (user_name or config.get("user_name") or "").strip().split(" ")[0]
        return name or persona.fallback
    return persona.address


def system_prompt(persona: Persona, title: str, abilities: str) -> str:
    rules = [rule.format(title=title) for rule in persona.style] + list(_COMMON)
    return persona.identity + "\n\nGuidelines:\n" + "\n".join(f"- {r}" for r in rules) + "\n" + abilities


# ----------------------------------------------------------------------------- your own personalities
BUILT_IN = tuple(PERSONAS)
CUSTOM_PREFIX = "my-"
MAX_CUSTOM = 12
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
_NAME_OK = re.compile(r"^[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ' .-]{0,23}$")


def is_custom(pid: str) -> bool:
    return (pid or "").startswith(CUSTOM_PREFIX)


def _clean(text: str, limit: int) -> str:
    return re.sub(r"\s+", " ", str(text or "").replace("{", "(").replace("}", ")")).strip()[:limit]


def validate_custom(data: dict, existing: list[dict] | None = None) -> dict:
    """Check and tidy a personality the user made; raises ValueError with a friendly message."""
    name = _clean(data.get("name"), 24)
    if not name or not _NAME_OK.match(name):
        raise ValueError("Give your personality a name (letters only, up to 24 characters).")
    taken = {p.name.lower() for pid, p in PERSONAS.items() if not is_custom(pid)}
    taken |= {str(c.get("name", "")).lower() for c in existing or [] if c.get("id") != data.get("id")}
    if name.lower() in taken or name.lower() in ("j.a.r.v.i.s.", "f.r.i.d.a.y."):
        raise ValueError(f"There's already a personality called {name}.")
    description = _clean(data.get("description"), 600)
    if len(description) < 10:
        raise ValueError("Describe the personality in a sentence or two, e.g. \u201cA cheerful pirate who loves puns.\u201d")
    voice = str(data.get("voice") or "").strip()
    if not re.match(r"^[a-z]{2,3}-[A-Z]{2}-[A-Za-z]+Neural$", voice):
        raise ValueError("Pick a voice for your personality.")
    color = str(data.get("color") or "").strip()
    if not HEX.match(color):
        color = "#7AA2FF"
    gender = "female" if str(data.get("gender") or "").lower().startswith("f") else "male"
    address = _clean(data.get("address"), 24)
    pid = str(data.get("id") or "")
    if not is_custom(pid):
        base = CUSTOM_PREFIX + (re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "persona")
        pid, n = base, 2
        while any(c.get("id") == pid for c in existing or []):
            pid, n = f"{base}-{n}", n + 1
    return {"id": pid, "name": name, "description": description, "voice": voice, "gender": gender,
            "color": color.upper(), "address": address}


def make_custom(d: dict) -> Persona:
    name = d["name"]
    first = re.split(r"(?<=[.!?])\s", d["description"])[0].rstrip(".!? ")
    tagline = first if len(first) <= 48 else first[:45].rsplit(" ", 1)[0] + "…"
    address = d.get("address") or ""
    return Persona(
        id=d["id"], name=name, display=name.upper(), tagline=tagline, description=d["description"],
        identity=(f"You are {name}, an AI assistant who lives on the user's computer and helps with anything they need. "
                  f"Your personality, exactly as the user described it: {d['description']}"),
        style=("Stay true to that personality in everything you say, while still being genuinely helpful.",
               "Keep spoken replies natural and fairly short (one to three sentences) unless the user asks for more.",
               "Address the user as \"{title}\" now and then."),
        voice=d["voice"], gender=d.get("gender") or "male", theme=d.get("color") or "#7AA2FF",
        address=address or "name", fallback="friend",
        lines={"who": (f"I'm {name}, {{title}}. {d['description']}",),
               "already": (f"{name} here, {{title}}. I'm right with you.",)},
        greeting=(f"Good {{part}}, {{title}}. {name} here. What can I do for you?",),
        intro=f"Hi {{title}}, {name} here. What can I do for you?",
        sample=f"Hi {{title}}, I'm {name}. This is how I sound.",
    )


def load_custom(items: list[dict] | None) -> None:
    """Make the user's own personalities available alongside the built-in ones."""
    for pid in [p for p in PERSONAS if is_custom(p)]:
        del PERSONAS[pid]
    for d in (items or [])[:MAX_CUSTOM]:
        try:
            PERSONAS[d["id"]] = make_custom(validate_custom(d, [c for c in items if c is not d]))
        except (KeyError, ValueError) as exc:
            import logging

            logging.getLogger("jarvis.personas").warning("Skipping a saved personality: %s", exc)
    _compile()


def theme_for(persona: Persona, config) -> str:
    """The HUD colours for this personality: the user's pick, else its own."""
    colors = config.get("persona_colors") or {}
    pick = colors.get(persona.id) if isinstance(colors, dict) else None
    return pick if pick and (HEX.match(str(pick)) or pick in ("arc", "mark3", "stealth", "violet", "rose")) else persona.theme


# ----------------------------------------------------------------------------- switching by voice
_ALIASES = {"j.a.r.v.i.s": "jarvis", "f.r.i.d.a.y": "friday"}
_NAMES: dict[str, str] = {}
_SWITCH: list[re.Pattern] = []
_LEAD = ""


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9à-ÿ]", "", name.lower())


def names_pattern() -> str:
    """Every personality's name, for regexes ("jarvis|harper|...|nova")."""
    names = sorted({p.name.lower() for p in PERSONAS.values()}, key=len, reverse=True)
    return "|".join(re.escape(n) for n in names)


def _compile() -> None:
    global _LEAD, _SWITCH
    _NAMES.clear()
    for pid, p in PERSONAS.items():
        _NAMES[_key(p.name)] = pid
    names = names_pattern()
    name_re = r"(?P<name>" + names + r"|j\.a\.r\.v\.i\.s\.?|f\.r\.i\.d\.a\.y\.?)"
    _LEAD = (r"^(?:(?:hey |ok |okay |hi )?(?:" + names + r")[, ]+)?"
             r"(?:please |can you |could you |can i |could i |let me |i want to |i'd like to |let's )*")
    _SWITCH = [
        re.compile(_LEAD + r"(?:switch|swap)\s+(?:over\s+)?(?:to|into|back to)\s+" + name_re + r"(?:\s+mode)?[\s.!?]*$", re.I),
        re.compile(_LEAD + r"(?:switch|change|set)\s+(?:your\s+|the\s+)?(?:personality|persona|character|voice|assistant)\s+(?:to|back to)\s+" + name_re + r"[\s.!?]*$", re.I),
        re.compile(_LEAD + r"(?:talk|speak|chat)\s+(?:to|with)\s+" + name_re + r"(?:\s+(?:now|instead|please))?[\s.!?]*$", re.I),
        re.compile(_LEAD + r"(?:bring|put)\s+(?:back\s+)?" + name_re + r"(?:\s+back)?(?:\s+on)?[\s.!?]*$", re.I),
        re.compile(_LEAD + r"(?:be|become|use|activate|enable)\s+" + name_re + r"(?:\s+(?:mode|personality|now))?[\s.!?]*$", re.I),
        re.compile(r"^(?:hey |hi |hello )?" + name_re + r"[,!]?\s+(?:are you there|take over|come in|it'?s your turn|you'?re up)[\s.!?]*$", re.I),
        re.compile(_LEAD + r"i\s+(?:want|need|'d like|would like)\s+(?:to\s+(?:talk|speak|chat)\s+(?:to|with)\s+)?" + name_re + r"(?:\s+(?:back|now|instead))?[\s.!?]*$", re.I),
    ]


_compile()
_WHO = re.compile(r"^(?:who am i (?:talking|speaking|chatting) (?:to|with)|who(?:'s| is) (?:this|there|speaking|talking)|which personality is (?:this|on|active)|"
                  r"what personality (?:is this|are you(?: using)?)|(?:what|which) personalities (?:are there|do you have|can i (?:choose|pick))|list (?:the |your )?personalities)[\s?.!]*$", re.I)


def parse_switch(text: str) -> str | None:
    """'let me talk to Harper' -> 'harper'."""
    t = (text or "").strip()
    for pattern in _SWITCH:
        m = pattern.match(t)
        if m:
            key = _key(m.group("name"))
            return _NAMES.get(key) or _NAMES.get(_ALIASES.get(m.group("name").lower().rstrip("."), ""))
    return None


def asks_who(text: str) -> bool:
    return bool(_WHO.match((text or "").strip()))
