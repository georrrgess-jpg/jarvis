"""Instant answers and actions that don't need the language model.

Small talk, arithmetic, system status, volume, timers, coin flips and jokes are answered in
milliseconds from here; waiting a second or two for a model to say "You're welcome" is what makes
an assistant feel slow. ``parse_quick`` only *recognises* a request, the assistant carries it out.
"""

from __future__ import annotations

import ast
import math
import operator
import random
import re
from dataclasses import dataclass, field

_LEAD = (r"^(?:(?:hey |ok |okay |hi )?(?:jarvis|harper|friday|sage)[, ]+)?(?:please |can you |could you |would you |will you |go ahead and )*")
_END = r"(?:[,\s]+(?:please|now|for me|jarvis|harper|friday|sage|sir|ma'am|friend|boss))*[\s.!?]*$"


@dataclass
class Quick:
    kind: str
    args: dict = field(default_factory=dict)


# ----------------------------------------------------------------------------- small talk
JOKES = [
    "I would tell you a joke about UDP, {title}, but you might not get it.",
    "Why do programmers prefer dark mode? Because light attracts bugs.",
    "There are only ten kinds of people in the world: those who understand binary and those who don't.",
    "I asked the arc reactor for a joke. It said it was feeling a little drained.",
    "A SQL query walks into a bar, walks up to two tables and asks: may I join you?",
    "I'd tell you a construction joke, {title}, but I'm still working on it.",
    "Why was the computer cold? It left its Windows open.",
]
_SMALL_TALK: list[tuple[str, re.Pattern, list[str]]] = [  # (situation, pattern, default replies)
    ("thanks", re.compile(_LEAD + r"(?:thanks|thank you|thank you (?:very|so) much|thanks a (?:lot|bunch)|thanks so much|cheers|much appreciated|ta|thank you (?:jarvis|harper|friday|sage))" + _END, re.I),
     ["You're most welcome, {title}.", "Always a pleasure, {title}.", "Of course, {title}.", "Happy to help, {title}."]),
    ("praise", re.compile(_LEAD + r"(?:you(?:'re| are) (?:the best|awesome|great|amazing|brilliant|wonderful|a legend)|good job|well done|nice one|great job)" + _END, re.I),
     ["Why, thank you, {title}. I do try.", "Most kind, {title}. I shall endeavour to keep it up.", "Thank you, {title}. The credit is shared with my circuits."]),
    ("how_are_you", re.compile(_LEAD + r"(?:how are you(?: doing)?(?: today| tonight| this morning| this evening)?(?: feeling)?|how(?:'s| is) it going|how do you do|are you (?:ok|okay|alright|well))" + _END, re.I),
     ["All systems nominal, {title}. And yourself?", "Running smoothly, {title}. How may I help?", "In excellent working order, {title}. What can I do for you?"]),
    ("who", re.compile(_LEAD + r"(?:who are you|what are you|what(?:'s| is) your name|what should i call you|introduce yourself)" + _END, re.I),
     ["I'm J.A.R.V.I.S., {title}: Just A Rather Very Intelligent System. I live on your computer and I'm entirely at your service."]),
    ("abilities", re.compile(_LEAD + r"(?:what can you do|what can i ask you|help me|what do you do|what are your (?:features|abilities|capabilities)|how do you work)" + _END, re.I),
     ["I can open your apps, games and files, play music, search the web, write documents and presentations in Google Docs and Slides, "
      "send emails, set timers, do sums, control the volume and chat in your language. You can also teach me protocols: "
      "lists of commands I run when you say “run” and the name. Just ask, {title}."]),
    ("goodbye", re.compile(_LEAD + r"(?:good ?night|goodbye|bye(?: bye)?|see you(?: later| tomorrow| soon)?|talk to you later|i'?m (?:off|going to bed|leaving)|that'?s all for (?:today|now))" + _END, re.I),
     ["Good night, {title}. I'll be here if you need me.", "Goodbye, {title}. Do call if you need anything.", "Until next time, {title}."]),
    ("joke", re.compile(_LEAD + r"(?:tell me a joke|say something funny|make me laugh|got any jokes|do you know any jokes|joke)" + _END, re.I), JOKES),
    ("feeling_down", re.compile(_LEAD + r"(?:i'?m (?:bored|tired|stressed|sad))" + _END, re.I),
     ["I'm sorry to hear that, {title}. Shall I put some music on, or tell you a joke?"]),
]
_GREETING = re.compile(_LEAD + r"(?:hi|hello|hey|yo|howdy|hiya|hey there|hello there|good (?:morning|afternoon|evening)|morning|evening|"
                       r"are you there|are you awake|jarvis|wake up|you there|(?:hello |hey |hi )?(?:jarvis|harper|friday|sage))" + _END, re.I)


# ----------------------------------------------------------------------------- arithmetic
_NUMBER_WORDS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
                 "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
                 "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
                 "eighty": 80, "ninety": 90, "hundred": 100, "thousand": 1000, "million": 1_000_000}
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv, ast.Pow: operator.pow,
        ast.Mod: operator.mod, ast.USub: operator.neg, ast.UAdd: operator.pos, ast.FloorDiv: operator.floordiv}
_CALC_LEAD = re.compile(_LEAD + r"(?:what(?:'s| is| are)|calculate|compute|work out|how much (?:is|are)|whats|tell me|figure out)?\s*", re.I)


def _words_to_digits(text: str) -> str:
    """'twenty five' -> '25', 'two hundred and fifty' -> '250'."""
    tokens = re.findall(r"[\w.%']+|[+\-*/^()=]", text)
    out, total, current, in_number, last_big = [], 0, 0, False, False
    for tok in tokens + [""]:
        low = tok.lower()
        if low in _NUMBER_WORDS and (low not in ("hundred", "thousand", "million") or in_number):
            value = _NUMBER_WORDS[low]
            last_big = value >= 100
            if value == 100:
                current = max(current, 1) * 100
            elif value >= 1000:
                total += max(current, 1) * value
                current = 0
            else:
                current += value
            in_number = True
            continue
        if low == "and" and in_number and last_big:  # "two hundred and fifty", but not "two times three and four"
            continue
        if in_number:
            out.append(str(total + current))
            total = current = 0
            in_number = False
        if tok:
            out.append(tok)
    return " ".join(out)


def _safe_eval(expr: str) -> float:
    def walk(node):
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            left, right = walk(node.left), walk(node.right)
            if isinstance(node.op, ast.Pow) and (abs(right) > 100 or abs(left) > 1e9):
                raise ValueError("too big")
            return _OPS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](walk(node.operand))
        raise ValueError("unsupported")

    return walk(ast.parse(expr, mode="eval"))


def calculate(text: str) -> str | None:
    """The spoken answer to an arithmetic question, or None when ``text`` isn't one."""
    t = _CALC_LEAD.sub("", text.strip(), count=1).strip().rstrip("?.! ").lower()
    if not t or not re.search(r"\d|\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|twenty|thirty|forty|fifty|"
                              r"hundred|thousand|million)\b", t):
        return None
    bare = re.fullmatch(r"(\d+)([/-])(\d+)(?:-\d+)*", t)
    if bare and (bare.group(2) == "-" or bare.group(1) == bare.group(3) or int(bare.group(1)) % max(1, int(bare.group(3)))):
        return None  # "9/11", "24/7", "50/50", "7-11", "4-4-2": dates, names and scores, not sums
    t = t.replace(",", "") if re.search(r"\d,\d{3}", t) else t
    t = _words_to_digits(t)
    root = re.fullmatch(r"(?:the )?square root of ([\d.]+)", t)
    if root:
        return _fmt(math.sqrt(float(root.group(1))))
    t = re.sub(r"([\d.]+)\s*(?:%|percent)\s+of\s+([\d.]+)", r"(\1/100*\2)", t)
    t = re.sub(r"([\d.]+)\s*(?:%|percent)\s+(?:off|discount on)\s+([\d.]+)", r"(\2*(1-\1/100))", t)
    t = re.sub(r"half of ([\d.]+)", r"(\1/2)", t)
    t = re.sub(r"double ([\d.]+)", r"(\1*2)", t)
    t = re.sub(r"([\d.]+) squared", r"(\1**2)", t)
    t = re.sub(r"([\d.]+) cubed", r"(\1**3)", t)
    for pattern, repl in ((r"\bto the power of\b|\bto the\b", "**"), (r"\^", "**"), (r"\bplus\b|\band\b", "+"), (r"\bminus\b|\bless\b|\btake away\b", "-"),
                          (r"\bdivided by\b|\bover\b", "/"), (r"\btimes\b|\bmultiplied by\b|\bx\b", "*"), (r"\bmod(?:ulo)?\b", "%")):
        t = re.sub(pattern, f" {repl} ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if not re.fullmatch(r"[\d.+\-*/()% ]+", t) or not re.search(r"[+\-*/%]", t.lstrip("-")) or re.search(r"\d\s+\d", t):
        return None
    try:
        return _fmt(_safe_eval(t))
    except (ValueError, SyntaxError, ZeroDivisionError, OverflowError, RecursionError):
        return None


def _fmt(value: float) -> str:
    if value != value or value in (math.inf, -math.inf):
        raise ValueError("not finite")
    if abs(value - round(value)) < 1e-9:
        return f"{int(round(value)):,}".replace(",", "")
    return f"{round(value, 6):.6f}".rstrip("0").rstrip(".")


# ----------------------------------------------------------------------------- system, volume, timers, misc
_STATUS = [
    (re.compile(_LEAD + r"(?:what(?:'s| is) (?:my |the )?)?(?:battery|battery (?:level|percentage|life|status)|how much (?:battery|charge)(?: (?:do i have|is left))?|"
                r"how(?:'s| is) (?:my|the) battery)(?: (?:left|at|level|percentage))?" + _END, re.I), "battery"),
    (re.compile(_LEAD + r"(?:what(?:'s| is) (?:my |the )?)?(?:cpu|processor)(?: (?:usage|load|use|percentage))?" + _END, re.I), "cpu"),
    (re.compile(_LEAD + r"(?:what(?:'s| is) (?:my |the )?|how much )?(?:ram|memory)(?: (?:usage|use|is (?:being )?used|do i have (?:left|free)|left|free))?" + _END, re.I), "ram"),
    (re.compile(_LEAD + r"(?:how much |what(?:'s| is) (?:my |the )?)(?:disk|storage|hard drive|drive)(?: space| usage)?(?: (?:is |do i have )?(?:left|free|used|remaining))?" + _END, re.I), "disk"),
    (re.compile(_LEAD + r"(?:system (?:status|report|check|diagnostics?)|run (?:a )?(?:system )?diagnostics?|how(?:'s| is) (?:my|the) (?:computer|pc|system|laptop|machine)(?: doing)?|"
                r"status report|give me a status(?: report)?|how(?:'s| is) everything)" + _END, re.I), "status"),
    (re.compile(_LEAD + r"(?:how long has (?:my|the) (?:computer|pc|laptop|system) been (?:on|running|up)|(?:what(?:'s| is) )?(?:my )?(?:system )?uptime)" + _END, re.I), "uptime"),
]
_VOLUME_UP = re.compile(_LEAD + r"(?:turn (?:it |the volume |the sound |the music )?up|(?:volume|sound) up|(?:increase|raise|boost|bump up) the (?:volume|sound)|"
                        r"(?:volume|sound) up a (?:bit|little|lot)|louder|a bit louder|make it louder|crank it up|turn up the (?:volume|sound|music))" + _END, re.I)
_VOLUME_DOWN = re.compile(_LEAD + r"(?:turn (?:it |the volume |the sound |the music )?down|(?:volume|sound) down|(?:decrease|lower|reduce|drop) the (?:volume|sound)|"
                          r"(?:volume|sound) down a (?:bit|little|lot)|quieter|softer|a bit quieter|make it quieter|turn down the (?:volume|sound|music))" + _END, re.I)
_VOLUME_SET = re.compile(_LEAD + r"(?:set |put |change )?(?:the )?(?:volume|sound)(?: level)?(?: (?:to|at))?\s*(?P<n>\d{1,3})\s*(?:%|percent)?" + _END, re.I)
_VOLUME_MAX = re.compile(_LEAD + r"(?:(?:max(?:imum)?|full|maximum) (?:volume|sound)|volume (?:to )?(?:max|full|maximum)|turn it all the way up)" + _END, re.I)
_MUTE = re.compile(_LEAD + r"(?:mute|unmute|(?:un)?mute (?:the )?(?:sound|volume|audio|computer|pc|it)|silence the (?:computer|pc)|be quiet please)" + _END, re.I)
_UNIT_WORD = r"(?:hours?|hrs?|minutes?|mins?|seconds?|secs?)"
_DUR = r"(?:\d+(?:\.\d+)?\s*" + _UNIT_WORD + r"\s*(?:and\s*|,\s*)?)+"
_CLOCK = r"(?:\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.?|p\.m\.?)?|noon|midday|midnight)"
_TIMER = re.compile(_LEAD + r"(?:(?:set|start|create|put on|begin|make)\s+)?(?:me\s+)?(?:a |an |the )?(?:(?P<kind>timer|alarm|countdown|stopwatch)\s*)?(?:for|of|in)?\s*"
                    r"(?P<dur>" + _DUR + r")(?:\s*(?P<kind2>timer|alarm|countdown))?" + _END, re.I)
_TIMER_VERB = re.compile(_LEAD + r"(?:set|start|create|put on|begin|make)\s+(?:me\s+)?(?:a |an |the )?(?:timer|alarm|countdown)", re.I)
_REMIND = [
    re.compile(_LEAD + r"remind me (?:in (?P<dur>" + _DUR + r") )?(?:at (?P<clock0>" + _CLOCK + r") )?(?:to |that |about )(?P<what>.+?)"
               r"(?: in (?P<dur2>" + _DUR + r"))?(?: at (?P<clock>" + _CLOCK + r"))?(?: (?:today|tonight|this (?:morning|afternoon|evening)))?" + _END, re.I),
    re.compile(_LEAD + r"(?:set|create|make|add) (?:me )?(?:a |an )?reminder (?:(?:for|in) (?P<dur>" + _DUR + r")|(?:for|at) (?P<clock>" + _CLOCK + r"))"
               r"(?: (?:to|that|about) (?P<what>.+?))?" + _END, re.I),
    re.compile(_LEAD + r"(?:set|create|make|add) (?:me )?(?:a |an )?reminder (?:to|that|about) (?P<what>.+?) (?:(?:in|for) (?P<dur>" + _DUR + r")|at (?P<clock>" + _CLOCK + r"))" + _END, re.I),
]
_TIMER_CANCEL = re.compile(_LEAD + r"(?:cancel|stop|clear|delete|remove|turn off|dismiss)\s+(?:my |the |all |all my |all the )?(?:timers?|alarms?|reminders?|countdowns?)" + _END, re.I)
_TIMER_STATUS = re.compile(_LEAD + r"(?:how (?:long|much time) (?:is )?left(?: on (?:my|the) (?:timer|alarm))?|(?:what|which) timers? (?:do i have|are (?:running|set))|how(?:'s| is) (?:my|the) timer(?: doing)?|"
                           r"(?:check|show|list) (?:my |the )?(?:timers?|alarms?|reminders?)|time (?:left|remaining))" + _END, re.I)
_SCREENSHOT = re.compile(_LEAD + r"(?:take (?:a )?(?:screenshot|screen ?shot|screen capture|picture of (?:my|the) screen)|screenshot|capture (?:my |the )?screen|screen ?cap)" + _END, re.I)
_DESKTOP = re.compile(_LEAD + r"(?:show (?:me )?(?:the )?desktop|minimi[sz]e (?:all|everything)(?: windows)?|clear (?:my |the )?screen|go to (?:the )?desktop|hide (?:all )?(?:my )?windows)" + _END, re.I)
_LOCK = re.compile(_LEAD + r"(?:lock (?:my |the )?(?:computer|pc|screen|laptop|workstation|windows)?|lock it)" + _END, re.I)
_COIN = re.compile(_LEAD + r"(?:flip a coin|toss a coin|heads or tails|coin flip|flip a coin for me)" + _END, re.I)
_DICE = re.compile(_LEAD + r"(?:roll (?:a |the )?(?:(?P<n>\d+)[- ]?sided )?(?:die|dice)(?: (?:with|of) (?P<n2>\d+) sides)?|roll a d(?P<n3>\d+))" + _END, re.I)
_RANDOM = re.compile(_LEAD + r"(?:pick|give me|choose|say) (?:a )?(?:random )?number (?:between|from) (?P<a>\d+) (?:and|to) (?P<b>\d+)" + _END, re.I)

_UNIT = {"hour": 3600, "hr": 3600, "minute": 60, "min": 60, "second": 1, "sec": 1}
_ONES = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9}
_SMALL = {**_ONES, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
          "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}


def spoken_numbers(text: str) -> str:
    """'twenty-five minutes' -> '25 minutes', 'two and a half hours' -> '2.5 hours', 'five pm' -> '5 pm'."""
    t = re.sub(r"\b(" + "|".join(_TENS) + r")[\s-]+(" + "|".join(_ONES) + r")\b",
               lambda m: str(_TENS[m.group(1).lower()] + _ONES[m.group(2).lower()]), text, flags=re.I)
    t = re.sub(r"\b(" + "|".join(list(_SMALL) + list(_TENS)) + r")\b", lambda m: str({**_SMALL, **_TENS}[m.group(1).lower()]), t, flags=re.I)
    t = re.sub(r"\b(?:an?|1)\s+(" + _UNIT_WORD + r")\s+and\s+a\s+half\b", r"1.5 \1", t, flags=re.I)  # "an hour and a half"
    t = re.sub(r"\b(\d+)\s+(" + _UNIT_WORD + r")\s+and\s+a\s+half\b", lambda m: f"{int(m.group(1)) + 0.5:g} {m.group(2)}", t, flags=re.I)
    t = re.sub(r"\b(\d+)\s+and\s+a\s+half\s+(" + _UNIT_WORD + r")", lambda m: f"{int(m.group(1)) + 0.5:g} {m.group(2)}", t, flags=re.I)
    t = re.sub(r"\bhalf\s+an?\s+(hour|minute)\b", lambda m: "30 minutes" if m.group(1).lower() == "hour" else "30 seconds", t, flags=re.I)
    t = re.sub(r"\ba\s+quarter\s+of\s+an\s+hour\b|\bquarter\s+of\s+an\s+hour\b", "15 minutes", t, flags=re.I)
    t = re.sub(r"\b(?:an?)\s+(" + _UNIT_WORD + r")", r"1 \1", t, flags=re.I)
    return re.sub(r"(\d+)\s*-\s*(" + _UNIT_WORD + r")", r"\1 \2", t, flags=re.I)  # "5-minute timer"


def parse_duration(text: str) -> int | None:
    """'5 minutes', 'half an hour', 'an hour and a half', 'twenty-five minutes', '2 hours 30 minutes' -> seconds."""
    t = spoken_numbers((text or "").lower().strip())
    total = 0.0
    found = False
    for number, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|minutes?|mins?|seconds?|secs?)\b", t):
        total += float(number) * _UNIT[unit.rstrip("s")]
        found = True
    return int(total) if found and total > 0 else None


def describe_duration(seconds: int) -> str:
    seconds = int(round(seconds))
    parts = []
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60), ("second", 1)):
        if seconds >= size:
            n, seconds = divmod(seconds, size)
            parts.append(f"{n} {unit}{'s' if n != 1 else ''}")
    return " and ".join(parts[:2]) if parts else "0 seconds"


def parse_quick(text: str) -> Quick | None:
    t = (text or "").strip()
    if not t or len(t) > 160:
        return None
    if _GREETING.match(t):
        return Quick("greeting")
    for category, pattern, replies in _SMALL_TALK:
        if pattern.match(t):
            return Quick("talk", {"replies": replies, "category": category})
    for pattern, what in _STATUS:
        if pattern.match(t):
            return Quick("status", {"what": what})
    if _TIMER_CANCEL.match(t):
        return Quick("timer_cancel")
    if _TIMER_STATUS.match(t):
        return Quick("timer_status")
    n = spoken_numbers(t)
    for pattern in _REMIND:
        remind = pattern.match(n)
        if not remind:
            continue
        g = remind.groupdict()
        dur = g.get("dur") or g.get("dur2")
        clock = g.get("clock") or g.get("clock0")
        if dur or clock:
            return Quick("timer", {"seconds": parse_duration(dur) if dur else None, "clock": clock,
                                   "label": (g.get("what") or "").strip(" .,!"), "reminder": True})
    timer = _TIMER.match(n)
    kind = (timer.group("kind") or timer.group("kind2")) if timer else None
    if timer and (kind or _TIMER_VERB.match(n) or re.match(_LEAD + r"(?:in|for) ", n)) and kind != "stopwatch":
        seconds = parse_duration(timer.group("dur"))
        if seconds:
            return Quick("timer", {"seconds": seconds, "label": "", "reminder": False})
    if _VOLUME_MAX.match(t):
        return Quick("volume", {"set": 100})
    vol = _VOLUME_SET.match(t)
    if vol and int(vol.group("n")) <= 100:
        return Quick("volume", {"set": int(vol.group("n"))})
    if _VOLUME_UP.match(t):
        return Quick("volume", {"delta": 10})
    if _VOLUME_DOWN.match(t):
        return Quick("volume", {"delta": -10})
    if _MUTE.match(t):
        return Quick("volume", {"mute": True, "word": "unmute" if re.search(r"\bunmute\b", t, re.I) else "mute"})
    if _SCREENSHOT.match(t):
        return Quick("screenshot")
    if _DESKTOP.match(t):
        return Quick("desktop")
    if _LOCK.match(t):
        return Quick("lock")
    if _COIN.match(t):
        return Quick("coin")
    dice = _DICE.match(t)
    if dice:
        return Quick("dice", {"sides": int(dice.group("n") or dice.group("n2") or dice.group("n3") or 6)})
    rnd = _RANDOM.match(t)
    if rnd:
        return Quick("random", {"a": int(rnd.group("a")), "b": int(rnd.group("b"))})
    answer = calculate(t)
    if answer is not None:
        return Quick("math", {"answer": answer})
    return None


def pick(replies: list[str], title: str, rng: random.Random | None = None) -> str:
    return (rng or random).choice(replies).format(title=title)
