"""Which screen is which: "my main monitor", "the other screen", "the left monitor", "monitor 2".

Windows decides the *main* (primary) display: it's where the taskbar's clock and new windows live, set in
Settings ▸ System ▸ Display ▸ "Make this my main display". Its numbers (1, 2, 3) are the ones shown
when you press "Identify" there. "The other / second monitor" means the one that isn't the main one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class Monitor:
    rect: tuple[int, int, int, int]  # left, top, right, bottom (virtual-screen pixels)
    work: tuple[int, int, int, int]  # the same without the taskbar
    primary: bool = False
    number: int = 0  # Windows' display number (\\.\DISPLAY2 -> 2)
    name: str = ""

    @property
    def width(self) -> int:
        return self.rect[2] - self.rect[0]

    @property
    def height(self) -> int:
        return self.rect[3] - self.rect[1]

    def contains(self, x: int, y: int) -> bool:
        return self.rect[0] <= x < self.rect[2] and self.rect[1] <= y < self.rect[3]


_ORDINALS = {"first": 1, "1st": 1, "one": 1, "second": 2, "2nd": 2, "two": 2, "third": 3, "3rd": 3, "three": 3,
             "fourth": 4, "4th": 4, "four": 4}
SCREEN_WORD = r"(?:monitor|screen|display)"
WHICH = (r"(?P<which>main|primary|middle|centre|center|other|second(?:ary)?|2nd|third|3rd|fourth|4th|first|1st|left(?:-hand)?|right(?:-hand)?|"
         r"top|upper|bottom|lower|laptop|external|big|small|(?:number\s+)?(?:\d|one|two|three|four))")
ON_SCREEN = re.compile(r"\s+(?:on|onto|to|in|over\s+to|across\s+to)\s+(?:my\s+|the\s+)?" + WHICH + r"(?:\s+" + SCREEN_WORD + r")"
                       r"|\s+(?:on|onto|to|in)\s+(?:my\s+|the\s+)?" + SCREEN_WORD + r"\s+(?:number\s+)?(?P<num>\d|one|two|three|four)", re.I)


def ordered(monitors: list[Monitor]) -> list[Monitor]:
    """Left to right, then top to bottom."""
    return sorted(monitors, key=lambda m: (m.rect[0], m.rect[1]))


def resolve(which: str, monitors: list[Monitor]) -> Monitor | None:
    """'main' -> the primary display; 'other' -> the one that isn't; 'left' -> leftmost; '2' -> Windows' display 2."""
    if not monitors:
        return None
    w = re.sub(r"\s+", " ", (which or "").lower().strip()).replace("number ", "")
    primary = next((m for m in monitors if m.primary), monitors[0])
    others = [m for m in ordered(monitors) if m is not primary]
    lr = ordered(monitors)
    if w in ("main", "primary", "big"):
        return primary
    if w in ("other", "secondary", "external"):
        return others[0] if others else None
    if w.startswith("left"):
        return lr[0] if len(lr) > 1 else None
    if w.startswith("right"):
        return lr[-1] if len(lr) > 1 else None
    if w in ("middle", "centre", "center"):
        return lr[len(lr) // 2] if len(lr) >= 3 else None
    if w in ("top", "upper"):
        return min(monitors, key=lambda m: m.rect[1]) if len(monitors) > 1 else None
    if w in ("bottom", "lower"):
        return max(monitors, key=lambda m: m.rect[1]) if len(monitors) > 1 else None
    if w == "small":
        return min(monitors, key=lambda m: m.width * m.height) if len(monitors) > 1 else None
    if w == "laptop":
        return min(monitors, key=lambda m: m.width * m.height)
    n = int(w) if w.isdigit() else _ORDINALS.get(w)
    if n is None:
        return None
    if w in ("second", "2nd") and len(monitors) == 2:
        return others[0]  # with two screens, "the second monitor" simply means the other one
    if w in ("first", "1st") and len(monitors) == 2:
        return primary
    by_number = next((m for m in monitors if m.number == n), None)
    return by_number or (lr[n - 1] if n <= len(lr) else None)


def describe(monitor: Monitor, monitors: list[Monitor]) -> str:
    if monitor.primary:
        return "your main monitor"
    lr = ordered(monitors)
    if len(monitors) == 2:
        side = "left" if monitor is lr[0] else "right"
        return f"your other monitor (on the {side})"
    if monitor is lr[0]:
        return "your left monitor"
    if monitor is lr[-1]:
        return "your right monitor"
    return f"monitor {monitor.number or lr.index(monitor) + 1}"


def split_screen_phrase(text: str) -> tuple[str, str | None]:
    """'open spotify on my second monitor' -> ('open spotify', 'second')."""
    m = ON_SCREEN.search(text or "")
    if not m:
        return text, None
    which = m.group("which") or m.group("num")
    rest = (text[: m.start()] + text[m.end():]).strip()
    return rest, which


def place_on(window_rect: tuple[int, int, int, int], source: Monitor | None, target: Monitor) -> tuple[int, int, int, int]:
    """Where a window should go on ``target``: same relative spot and size, shrunk to fit if needed."""
    l, t, r, b = window_rect
    w, h = r - l, b - t
    tl, tt, tr, tb = target.work
    tw, th = tr - tl, tb - tt
    w, h = min(w, tw), min(h, th)
    if source is not None:
        sl, st, sr, sb = source.work
        fx = (l - sl) / max(1, (sr - sl) - (r - l)) if (sr - sl) > (r - l) else 0.5
        fy = (t - st) / max(1, (sb - st) - (b - t)) if (sb - st) > (b - t) else 0.5
    else:
        fx = fy = 0.5
    fx, fy = min(max(fx, 0.0), 1.0), min(max(fy, 0.0), 1.0)
    x = tl + int((tw - w) * fx)
    y = tt + int((th - h) * fy)
    return x, y, w, h


@dataclass
class MonitorCommand:
    action: str  # "move" | "info"
    app: str = ""  # "" = the window you're using
    which: str = ""


_LEAD = r"^(?:please |can you |could you |would you |i want you to |go ahead and )*"
_MOVE = re.compile(_LEAD + r"(?:move|put|send|throw|drag|shift|bring|switch|take)\s+(?P<app>.+?)\s+(?:to|onto|on|over\s+to|across\s+to|into)\s+"
                   r"(?:my\s+|the\s+)?(?:" + WHICH + r"\s+" + SCREEN_WORD + r"|" + SCREEN_WORD + r"\s+(?:number\s+)?(?P<num>\d|one|two|three|four))"
                   r"(?:\s+(?:please|for me|now))?[\s.!?]*$", re.I)
_INFO = re.compile(_LEAD + r"(?:how many (?:monitors|screens|displays) (?:do i have|have i got|are (?:there|connected))|"
                   r"which (?:one |monitor |screen |display )?is (?:my |the )?(?:main|primary)(?: (?:monitor|screen|display))?|"
                   r"what (?:monitors|screens|displays) (?:do i have|are connected)|(?:list|show) (?:my )?(?:monitors|screens|displays))[\s?.!]*$", re.I)
_THIS = re.compile(r"^(?:this|that|the|my|it|this one|the current|the active)(?:\s+(?:window|app|application|program|one))?$", re.I)


def parse_monitor_command(text: str) -> MonitorCommand | None:
    t = re.sub(r"\s+", " ", (text or "").strip())
    if _INFO.match(t):
        return MonitorCommand("info")
    m = _MOVE.match(t)
    if not m:
        return None
    app = re.sub(r"^(?:the|my)\s+", "", m.group("app").strip(), flags=re.I)
    app = re.sub(r"\s+(?:window|app|application|program)$", "", app, flags=re.I)
    if _THIS.match(m.group("app").strip()) or app.lower() in ("this", "that", "it", "window", "current window"):
        app = ""
    return MonitorCommand("move", app=app, which=m.group("which") or m.group("num") or "")
