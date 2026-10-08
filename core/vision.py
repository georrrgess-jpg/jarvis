"""JARVIS's eyes: look at the screen, find things on it, watch it, and act on it.

* **Look**: capture the app you're using, read its text (OCR) and ask a local vision model (through Ollama,
  e.g. Qwen2.5-VL on your NVIDIA GPU) about it. Without a vision model the OCR text alone goes to the
  normal language model, which still answers "what does this error mean?" well.
* **Locate**: find "the Send button": exact on-screen text first (OCR gives precise positions); otherwise the
  vision model picks from numbered marks drawn on the screenshot, then from a grid ("set-of-marks"), which
  works far more reliably than asking a small model for raw pixel coordinates.
* **Watch**: compare frames every couple of seconds; when the screen settles after a change, read it and
  check the user's condition ("my download finishes") or look for anything noteworthy.
* **Act**: click, type, press keys, scroll, close: uncertain or risky actions are confirmed first.

Nothing ever leaves the computer: screenshots go to the local Ollama server only and are never saved.
"""

from __future__ import annotations

import base64
import io
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterator

import httpx

from .ocr import Line, OcrUnavailable, TextHit, find_text, plain_text
from .screen import ScreenError, Shot, Window, is_private, parse_keys

log = logging.getLogger("jarvis.vision")

VISION_NAME = re.compile(r"llava|bakllava|vision|minicpm-v|qwen2(?:\.5)?-?vl|qwen3-vl|moondream|gemma3(?!:(?:1b|270m))|"
                         r"granite[\w.]*-vision|mistral-small3\.[12]|llama4|pixtral|gemma3n", re.IGNORECASE)
PREFERRED = ("qwen2.5vl", "qwen3-vl", "minicpm-v", "llama3.2-vision", "gemma3", "llava", "moondream")
SUGGESTED_MODELS = [
    ("qwen2.5vl:7b", "Qwen2.5-VL 7B: best for screens, ~6 GB (NVIDIA GPU)"),
    ("qwen2.5vl:3b", "Qwen2.5-VL 3B: lighter, ~3.2 GB"),
    ("gemma3:4b", "Gemma 3 4B: ~3.3 GB"),
    ("llava:7b", "LLaVA 7B: ~4.7 GB"),
]
DEFAULT_VISION_MODEL = SUGGESTED_MODELS[0][0]
NOTABLE = re.compile(r"\b(?:error|errors|failed|failure|fail|exception|crash(?:ed)?|warning|denied|unable|couldn'?t|can'?t|"
                     r"complete[d]?|finished|done|ready|success(?:ful(?:ly)?)?|saved|uploaded|downloaded|download(?:ing)? complete|"
                     r"new message|unread|mention(?:ed)?|invit(?:e|ed|ation)|reminder|incoming|calling|disconnected|"
                     r"update available|battery low|low battery|out of|expired|timed out|time'?s up)\b", re.IGNORECASE)


class VisionUnavailable(Exception):
    pass


@dataclass
class Observation:
    shot: Shot
    lines: list[Line] = field(default_factory=list)
    ocr_error: str | None = None

    @property
    def window(self) -> Window | None:
        return self.shot.window

    @property
    def text(self) -> str:
        return plain_text(self.lines)


@dataclass
class Target:
    x: int  # screen coordinates
    y: int
    box: tuple[float, float, float, float]  # image coordinates (x, y, w, h)
    method: str  # "text" | "marks" | "grid"
    sure: bool
    label: str = ""


def encode_image(image, max_side: int = 1280, quality: int = 85) -> bytes:
    """JPEG bytes for the model (vision models gain nothing from full 4K pixels, they just get slower)."""
    from PIL import Image

    image = image.convert("RGB")
    w, h = image.size
    if max(w, h) > max_side:
        k = max_side / max(w, h)
        image = image.resize((max(1, int(w * k)), max(1, int(h * k))), Image.LANCZOS)
    buf = io.BytesIO()
    image.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def data_url(image, max_side: int = 520, quality: int = 70) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(encode_image(image, max_side, quality)).decode("ascii")


def frame_signature(image):
    import numpy as np

    return np.asarray(image.convert("L").resize((96, 54)), dtype=np.float32)


def frame_change(a, b) -> float:
    import numpy as np

    if a is None or b is None or a.shape != b.shape:
        return 255.0
    return float(np.mean(np.abs(a - b)))


def first_number(text: str) -> int | None:
    m = re.search(r"\d+", text or "")
    return int(m.group(0)) if m else None


def _font(size: int):
    from PIL import ImageFont

    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


class VisionEngine:
    def __init__(self, config, llm, desktop, ocr) -> None:
        self._config = config
        self._llm = llm
        self.desktop = desktop
        self.ocr = ocr
        self._models: list[str] = []
        self._capabilities: dict[str, bool] = {}

    # ------------------------------------------------------------------ models
    def refresh(self, installed: list[str]) -> None:
        self._models = list(installed or [])

    def vision_models(self) -> list[str]:
        found = [m for m in self._models if self._is_vision(m)]
        return sorted(found, key=lambda m: next((i for i, p in enumerate(PREFERRED) if p in m.lower()), 99))

    def _is_vision(self, name: str) -> bool:
        if name in self._capabilities:
            return self._capabilities[name]
        guess = bool(VISION_NAME.search(name))
        try:  # Ollama 0.6+ reports capabilities; trust it when present
            client = self._llm._client_factory(httpx.Timeout(5.0, connect=2.0))
            info = client.show(name)
            caps = getattr(info, "capabilities", None) or (info.get("capabilities") if isinstance(info, dict) else None)
            if caps:
                guess = "vision" in caps
        except Exception:
            pass
        self._capabilities[name] = guess
        return guess

    @property
    def model(self) -> str | None:
        chosen = (self._config.get("vision_model") or "").strip()
        models = self.vision_models()
        if chosen:
            for m in models:
                if m == chosen or m.split(":")[0] == chosen.split(":")[0] and (":" not in chosen or m == chosen):
                    return m
        return models[0] if models else None

    @property
    def ocr_ready(self) -> bool:
        try:
            return bool(self.ocr.available())
        except Exception:
            return False

    def status(self) -> dict:
        return {"model": self.model, "models": self.vision_models(), "ocr": self.ocr_ready,
                "ocr_engine": getattr(self.ocr, "name", "none"), "ocr_error": getattr(self.ocr, "error", None),
                "suggested": [{"name": n, "label": label} for n, label in SUGGESTED_MODELS]}

    # ------------------------------------------------------------------ seeing
    def observe(self, window: Window | None, read_text: bool = True) -> Observation:
        exclusions = self._config.get("vision_exclusions") or ""
        if is_private(window, exclusions):
            raise ScreenError(f"{window.label} looks private (it matches your privacy list), so I won't look at it.")
        shot = self.desktop.capture(window)
        obs = Observation(shot)
        if read_text and self.ocr_ready:
            try:
                obs.lines = self.ocr.read(shot.image)
            except (OcrUnavailable, OSError, ValueError) as exc:
                obs.ocr_error = str(exc)
                log.info("OCR failed: %s", exc)
        return obs

    def _client(self, timeout: float = 180.0):
        return self._llm._client_factory(httpx.Timeout(timeout, connect=4.0))

    def _ask(self, prompt: str, image=None, system: str = "", max_tokens: int = 64, model: str | None = None) -> str:
        """One short, non-streamed question to the vision model (or the text model when there's no image)."""
        model = model or (self.model if image is not None else self._llm.model)
        if not model:
            raise VisionUnavailable("no model is available")
        message: dict = {"role": "user", "content": prompt}
        if image is not None:
            message["images"] = [encode_image(image)]
        messages = ([{"role": "system", "content": system}] if system else []) + [message]
        client = self._client()
        try:
            reply = client.chat(model=model, messages=messages, stream=False, keep_alive="15m",
                                options={"temperature": 0.0, "num_predict": max_tokens, "num_ctx": 8192})
            return str(reply["message"]["content"] or "").strip()
        finally:
            if hasattr(client, "close"):
                client.close()

    def describe(self, obs: Observation, question: str, cancel: threading.Event | None = None,
                 language: str | None = None, title: str = "sir", name: str = "J.A.R.V.I.S.") -> Iterator[str]:
        """Stream an answer about what's on screen."""
        cancel = cancel or threading.Event()
        where = f" of the window \"{obs.window.title}\"" + (f" ({obs.window.app})" if obs.window.app else "") if obs.window else ""
        lang = f" Reply in {language}." if language and language != "English" else ""
        text = obs.text
        model = self.model
        if model:
            system = (f"You are {name}, the user's AI assistant, and you can see their screen: the attached image is a "
                      f"screenshot{where}. Text read from the screen by OCR is included when available; trust it for exact "
                      "words and numbers. Answer what the user asks about what is on screen. Your reply is spoken aloud, so "
                      "be brief (one to three sentences) unless they ask for detail, a summary, a translation or steps. "
                      f"Address the user as {title} occasionally. If you can't see something, say so; never invent details."
                      f"{lang}")
            images = [encode_image(obs.shot.image)]
        elif text.strip():
            system = (f"You are {name}. You can't see images, but here is the text read from the user's screen{where} "
                      "by OCR. Answer what the user asks about it. Your reply is spoken aloud, so be brief (one to three "
                      f"sentences) unless they ask for detail. If the text isn't enough to answer, say so.{lang}")
            model = self._llm.model
            images = None
        else:
            raise VisionUnavailable("I need a vision model to see the screen (there was no readable text either). "
                                    "Say \"install vision\" and I'll download one for you.")
        if not model:
            raise VisionUnavailable("my language model is offline, so I can't think about what I see yet.")
        content = question.strip() or "What is on my screen?"
        if text.strip():
            content += "\n\nText on screen (OCR):\n" + text[:5000]
        message: dict = {"role": "user", "content": content}
        if images:
            message["images"] = images
        client = self._client(300.0)
        try:
            stream = client.chat(model=model, stream=True, keep_alive="15m",
                                 messages=[{"role": "system", "content": system}, message],
                                 options={"temperature": 0.3, "num_ctx": 8192})
            for chunk in stream:
                if cancel.is_set():
                    break
                piece = chunk["message"]["content"] or ""
                if piece:
                    yield piece
                if chunk.get("done"):
                    break
        finally:
            if hasattr(client, "close"):
                client.close()

    # ------------------------------------------------------------------ finding things
    def locate(self, obs: Observation, target: str) -> Target | None:
        """Where on screen is ``target``? Text matches are exact; anything the model picks is marked unsure."""
        hits = find_text(obs.lines, target)
        if hits and hits[0].score >= 0.86:
            top = [h for h in hits if h.score >= hits[0].score - 0.04]
            if len(top) == 1:
                return self._target(obs, hits[0].box, "text", True, hits[0].text)
            if self.model:
                choice = self._choose_mark(obs, [h.box for h in top], target)
                if choice is not None:
                    return self._target(obs, top[choice].box, "marks", False, top[choice].text)
            return self._target(obs, top[0].box, "text", False, top[0].text)
        if not self.model:
            return None
        candidates = self._candidates(obs, target, hits)
        if candidates:
            choice = self._choose_mark(obs, [box for box, _ in candidates], target)
            if choice is not None:
                box, label = candidates[choice]
                return self._target(obs, box, "marks", False, label)
        return self._grid_locate(obs, target)

    def _target(self, obs: Observation, box, method: str, sure: bool, label: str) -> Target:
        x, y, w, h = box
        sx, sy = obs.shot.to_screen(x + w / 2, y + h / 2)
        return Target(sx, sy, tuple(box), method, sure, label)

    def _candidates(self, obs: Observation, target: str, hits: list[TextHit]) -> list[tuple[tuple, str]]:
        seen, out = set(), []
        for hit in hits[:10]:
            out.append((hit.box, hit.text))
            seen.add(tuple(int(v) for v in hit.box))
        for line in obs.lines:
            if len(out) >= 40:
                break
            box = line.box
            if tuple(int(v) for v in box) in seen or box[2] <= 2 or box[3] <= 2:
                continue
            out.append((box, line.text))
        return out

    def _draw_marks(self, image, boxes):
        from PIL import ImageDraw

        canvas = image.convert("RGB").copy()
        draw = ImageDraw.Draw(canvas)
        size = max(14, min(28, canvas.size[1] // 45))
        font = _font(size)
        for i, (x, y, w, h) in enumerate(boxes, start=1):
            draw.rectangle([x - 2, y - 2, x + w + 2, y + h + 2], outline=(255, 30, 60), width=2)
            label = str(i)
            tw = size * 0.62 * len(label) + 6
            lx, ly = max(0, x - 2), max(0, y - size - 6)
            draw.rectangle([lx, ly, lx + tw, ly + size + 4], fill=(255, 30, 60))
            draw.text((lx + 3, ly + 1), label, fill=(255, 255, 255), font=font)
        return canvas

    def _choose_mark(self, obs: Observation, boxes, target: str) -> int | None:
        marked = self._draw_marks(obs.shot.image, boxes)
        answer = self._ask(f"The screenshot has red numbered marks. Which number marks \"{target}\"? "
                           "Reply with the number only, or 0 if none of the marks is it.", image=marked, max_tokens=8)
        n = first_number(answer)
        return n - 1 if n and 1 <= n <= len(boxes) else None

    def _grid_locate(self, obs: Observation, target: str) -> Target | None:
        """Two-step grid: pick the cell that contains the target, then the sub-cell inside a zoomed view."""
        from PIL import Image, ImageDraw

        image = obs.shot.image.convert("RGB")
        w, h = image.size
        cols, rows = 6, 4
        cell = self._pick_cell(image, cols, rows, target)
        if cell is None:
            return None
        cw, ch = w / cols, h / rows
        c, r = (cell - 1) % cols, (cell - 1) // cols
        # zoom into the cell (with a margin) and pick one of nine sub-cells
        x0, y0 = max(0, c * cw - cw * 0.15), max(0, r * ch - ch * 0.15)
        x1, y1 = min(w, (c + 1) * cw + cw * 0.15), min(h, (r + 1) * ch + ch * 0.15)
        crop = image.crop((int(x0), int(y0), int(x1), int(y1)))
        k = 900 / max(1, crop.size[0])
        crop = crop.resize((max(1, int(crop.size[0] * k)), max(1, int(crop.size[1] * k))), Image.LANCZOS)
        sub = self._pick_cell(crop, 3, 3, target)
        if sub is None:
            sx, sy = (x0 + x1) / 2, (y0 + y1) / 2
        else:
            sc, sr = (sub - 1) % 3, (sub - 1) // 3
            sx = x0 + (sc + 0.5) * (x1 - x0) / 3
            sy = y0 + (sr + 0.5) * (y1 - y0) / 3
        bw, bh = (x1 - x0) / 3, (y1 - y0) / 3
        return self._target(obs, (sx - bw / 2, sy - bh / 2, bw, bh), "grid", False, target)

    def _pick_cell(self, image, cols: int, rows: int, target: str) -> int | None:
        from PIL import ImageDraw

        canvas = image.convert("RGB").copy()
        draw = ImageDraw.Draw(canvas)
        w, h = canvas.size
        size = max(16, min(40, h // (rows * 4)))
        font = _font(size)
        for i in range(1, cols):
            draw.line([(w * i / cols, 0), (w * i / cols, h)], fill=(255, 30, 60), width=2)
        for j in range(1, rows):
            draw.line([(0, h * j / rows), (w, h * j / rows)], fill=(255, 30, 60), width=2)
        for n in range(cols * rows):
            c, r = n % cols, n // cols
            draw.text((w * c / cols + 6, h * r / rows + 4), str(n + 1), fill=(255, 30, 60), font=font, stroke_width=2,
                      stroke_fill=(255, 255, 255))
        answer = self._ask(f"The screenshot is divided into a numbered red grid. Which cell number contains \"{target}\"? "
                           "Reply with the number only, or 0 if it isn't visible.", image=canvas, max_tokens=8)
        n = first_number(answer)
        return n if n and 1 <= n <= cols * rows else None

    def mark_target(self, obs: Observation, target: Target):
        """A cropped picture of where JARVIS is about to click, for the confirmation card."""
        from PIL import ImageDraw

        image = obs.shot.image.convert("RGB").copy()
        draw = ImageDraw.Draw(image)
        x, y, w, h = target.box
        cx, cy = x + w / 2, y + h / 2
        radius = max(18, min(60, max(w, h) / 1.6))
        for k, color in ((0, (255, 30, 60)), (3, (255, 255, 255))):
            draw.ellipse([cx - radius - k, cy - radius - k, cx + radius + k, cy + radius + k], outline=color, width=3)
        draw.line([(cx - radius * 1.5, cy), (cx - radius * 0.5, cy)], fill=(255, 30, 60), width=3)
        draw.line([(cx + radius * 0.5, cy), (cx + radius * 1.5, cy)], fill=(255, 30, 60), width=3)
        iw, ih = image.size
        half_w, half_h = min(iw, 640) / 2, min(ih, 400) / 2
        left = int(min(max(0, cx - half_w), max(0, iw - 2 * half_w)))
        top = int(min(max(0, cy - half_h), max(0, ih - 2 * half_h)))
        return image.crop((left, top, int(left + 2 * half_w), int(top + 2 * half_h)))

    # ------------------------------------------------------------------ watching
    def condition_met(self, obs: Observation, condition: str) -> bool:
        question = (f"Look at the screen. Is this true right now: \"{condition}\"? "
                    "Answer YES or NO only.")
        text = obs.text
        if self.model:
            prompt = question + (f"\n\nText on screen (OCR):\n{text[:3000]}" if text else "")
            answer = self._ask(prompt, image=obs.shot.image, max_tokens=4)
        elif text.strip():
            answer = self._ask(f"Here is the text currently on the user's screen (OCR):\n{text[:4000]}\n\n"
                               f"Based on it, is this true right now: \"{condition}\"? Answer YES or NO only.", max_tokens=4)
        else:
            raise VisionUnavailable("I need a vision model or Windows OCR to watch for that.")
        return bool(re.match(r"\s*\**\s*yes\b", answer or "", re.IGNORECASE))

    def notable_now(self, obs: Observation) -> str | None:
        """Without OCR: ask the vision model whether the screen shows something worth saying."""
        if not self.model:
            return None
        answer = self._ask("Does this screen show something the user should hear about right now, such as an error, a "
                           "finished task or download, or a new message? If so, reply with one short sentence to say to "
                           "them. Otherwise reply NONE.", image=obs.shot.image, max_tokens=60)
        answer = (answer or "").strip().strip('"')
        return None if not answer or re.match(r"^none\b", answer, re.I) else answer


class Watcher:
    """Keeps an eye on the screen in the background (only ever while the user has asked it to)."""

    def __init__(self, engine: VisionEngine, current_window: Callable[[], Window | None], notify: Callable[[str], None],
                 on_end: Callable[[str], None], condition: str = "", window: Window | None = None,
                 interval: float = 2.0, timeout: float = 4 * 3600, cooldown: float = 25.0) -> None:
        self.engine = engine
        self.condition = condition.strip()
        self.window = window  # fixed window for a condition; None = follow whatever the user is using
        self._current = current_window
        self._notify = notify
        self._on_end = on_end
        self.interval = interval
        self.timeout = timeout
        self.cooldown = cooldown
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.started = time.time()
        self.checks = 0
        self.last_error: str | None = None

    @property
    def label(self) -> str:
        return self.condition or "anything important"

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="screen-watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        prev = last_checked = None
        seen: list[str] = []
        last_notice = 0.0
        last_vlm = 0.0
        said: set[str] = set()
        reason = "stopped"
        try:
            while not self._stop.wait(self.interval):
                if time.time() - self.started > self.timeout:
                    reason = "timeout"
                    break
                window = self.window or self._current()
                if self.window is not None and not getattr(self.engine.desktop, "alive", lambda w: True)(self.window):
                    reason = "closed"
                    break
                try:
                    obs = self.engine.observe(window, read_text=False)
                except ScreenError as exc:
                    self.last_error = str(exc)
                    continue
                sig = frame_signature(obs.shot.image)
                stable = prev is not None and frame_change(sig, prev) < 2.0
                changed = last_checked is None or frame_change(sig, last_checked) >= 2.0
                prev = sig
                if not (changed and (stable or last_checked is None)):
                    continue  # nothing new, or still moving: wait for the screen to settle
                last_checked = sig
                if self.engine.ocr_ready:
                    try:
                        obs.lines = self.engine.ocr.read(obs.shot.image)
                    except (OcrUnavailable, OSError, ValueError) as exc:
                        obs.ocr_error = str(exc)
                self.checks += 1
                if self.condition:
                    if self.engine.condition_met(obs, self.condition):
                        self._notify(self.condition)
                        reason = "met"
                        break
                    continue
                lines = [re.sub(r"\s+", " ", l.text).strip() for l in obs.lines if l.text.strip()]
                if obs.lines or self.engine.ocr_ready:
                    fresh = [t for t in lines if t.lower() not in seen]
                    seen = (seen + [t.lower() for t in fresh])[-600:]
                    if self.checks == 1:
                        continue  # the first look is the baseline, not news
                    notable = [t for t in fresh if NOTABLE.search(t) and t.lower() not in said and len(t) <= 200]
                    if notable and time.time() - last_notice > self.cooldown:
                        last_notice = time.time()
                        said.add(notable[0].lower())
                        self._notify(notable[0])
                elif time.time() - last_vlm > max(12.0, self.cooldown / 2):
                    last_vlm = time.time()
                    message = self.engine.notable_now(obs)
                    if message and message.lower() not in said and time.time() - last_notice > self.cooldown:
                        said.add(message.lower())
                        last_notice = time.time()
                        self._notify(message)
        except Exception as exc:  # never let a watcher crash silently
            log.exception("Screen watch failed")
            reason = f"error: {exc}"
        finally:
            self._stop.set()
            try:
                self._on_end(reason)
            except Exception:
                log.exception("watch end callback failed")


# ============================================================================== understanding requests
_LEAD = (r"^(?:(?:hey |ok |okay )?jarvis[, ]+)?(?:please |can you |could you |would you |will you |i need you to |"
         r"i want you to |go ahead and |quickly |now )*")
_END = r"(?:\s+(?:please|for me|now|jarvis))*[\s.!?]*$"
_SCREENISH = (r"(?:screen|monitor|display|window|page|tab|website|site|article|email|e-mail|message|error|code|chart|graph|"
              r"image|picture|photo|video|form|table|document|text|paragraph|post|tweet|comment|dialog|pop-?up|this|that|it|here)")


@dataclass
class LookRequest:
    question: str
    full_screen: bool = False


@dataclass
class WatchRequest:
    action: str  # "start" | "stop" | "status"
    condition: str = ""


@dataclass
class ActRequest:
    action: str  # "click" | "double" | "right" | "type" | "keys" | "scroll" | "close"
    target: str = ""
    text: str = ""
    keys: list[int] | None = None
    amount: int = 0
    label: str = ""


_LOOK_PATTERNS = [
    re.compile(_LEAD + r"(?:what(?:'s| is) (?:on|in) (?:my|the) (?:whole |entire |full )?(?:screen|monitor|display|window)s?|what am i (?:looking at|seeing|watching|reading|doing)|"
               r"what (?:do|can) you see|(?:can|could) you see (?:my|the|this) (?:screen|window)|look at (?:my|the|this) (?:screen|window|page|tab)s?|"
               r"look at (?:this|that|it)|take a look(?: at (?:this|that|my screen|the screen))?|have a look(?: at (?:this|that|my screen))?|"
               r"(?:describe|scan|analy[sz]e) (?:my|the|this) screen)\b.*$", re.I),
    re.compile(_LEAD + r"(?:read|summari[sz]e|explain|translate|describe|check|proofread|analy[sz]e|review|fact.?check|look over|go over)"
               r"(?: out| me| aloud| to me| over)?\s+(?:this|that|the|my|what'?s on (?:my|the))\s+(?:whole |entire |current )?" + _SCREENISH +
               r"\b.*$", re.I),
    re.compile(_LEAD + r"what does (?:this|that|the|my) (?:error|message|code|word|sign|screen|button|chart|graph|warning|notification|"
               r"popup|pop-up|dialog|page|email|text|line|function|setting|option)\s*(?:\w+\s*)?(?:mean|say|do)\b.*$", re.I),
    re.compile(_LEAD + r"(?:what(?:'s| is) (?:this|that) (?:error|warning|message|thing|app|website|site|game|song|word|chart|graph|"
               r"image|picture|button|setting|notification|popup|pop-up)|how do i (?:fix|solve|get past|close|use) (?:this|that)|"
               r"help me with (?:this|that)|what should i (?:do|click|choose|pick) (?:here|now|next)|is this (?:safe|legit|real|a scam|correct|right))\b.*$", re.I),
    re.compile(_LEAD + r".*\b(?:on|in) (?:my|the) (?:screen|monitor|display)\b.*$", re.I),
    re.compile(_LEAD + r".*\b(?:this|that) (?:window|page|tab|website|site|article|email|error message|error|pop-?up|dialog)\b.*\?$", re.I),
]
_FULL_SCREEN = re.compile(r"\b(?:whole|entire|full|all (?:of )?my|both|every) (?:screen|screens|monitor|monitors|display|displays)\b|\beverything on my screen\b", re.I)
_INSTALL_VISION = re.compile(_LEAD + r"(?:install|download|get|set ?up|enable|turn on|activate|add)\s+(?:the |your |a |my )?"
                             r"(?:vision|eyes|screen vision|vision model|seeing|sight)(?: model)?" + _END, re.I)
_WATCH_STOP = re.compile(_LEAD + r"(?:stop|quit|cancel|end|finish|pause|turn off|disable)\s+(?:watching|monitoring|looking at|keeping an eye on|"
                         r"the watch|screen watch|watch mode|observing)(?:\s+(?:my|the|this|that|it)(?:\s+(?:screen|window|download|thing))?)?" + _END
                         + r"|" + _LEAD + r"(?:you can |that'?s enough,? )?stop (?:watching|looking)" + _END, re.I)
_WATCH_STATUS = re.compile(_LEAD + r"(?:are you (?:still )?watching(?: my screen)?|what are you watching(?: for)?|is the watch (?:on|running))" + _END, re.I)
_WATCH_START = re.compile(_LEAD + r"(?:(?:watch|monitor|observe|keep (?:an )?eye on|keep watch (?:on|over))\s+(?:my|the|this) (?:screen|monitor|display|window|computer|pc)"
                          r"(?:\s+for (?P<what>.+?))?|start (?:watching|monitoring)(?: my screen| the screen)?|watch mode on)" + _END, re.I)
_WATCH_WHEN = re.compile(_LEAD + r"(?:(?:tell|let|notify|alert|ping|warn|call) me (?:know )?|watch (?:my screen |the screen )?(?:and tell me |and let me know )?)"
                         r"(?:when|once|as soon as|if|the moment)\s+(?P<cond>.+?)" + _END
                         + r"|" + _LEAD + r"watch (?:out )?for (?P<cond2>.+?)" + _END, re.I)
_NOT_SCREEN_CONDITION = re.compile(r"\b(?:\d{1,2}(?::\d{2})?\s*(?:am|pm)|o'?clock|minutes?|hours?|seconds?|timer|tomorrow|tonight|"
                                   r"you(?:'re| are) (?:done|finished|ready)|it'?s time)\b", re.I)

_RISKY = re.compile(r"\b(?:send|delete|remove|buy|pay|purchase|order|checkout|check out|submit|confirm|transfer|uninstall|format|erase|"
                    r"wipe|sign out|log out|logout|unsubscribe|post|publish|tweet|reply all|accept|agree|install|reset|empty|discard|"
                    r"close|quit|exit|deny|block|report|archive|cancel subscription|place order|withdraw|donate|share)\b", re.I)
_CLICK = re.compile(_LEAD + r"(?P<kind>double[- ]?click|right[- ]?click|left[- ]?click|click|tap|hit|press|select|choose|tick|check|uncheck)"
                    r"(?:\s+on)?\s+(?P<target>.+?)" + _END, re.I)
_TYPE = re.compile(_LEAD + r"(?:type|type in|type out|enter|write|fill in|put|input|search for)\s+(?P<text>.+?)"
                   r"\s+(?:in|into|in to|on|inside)\s+(?:the |that |this |my )?(?P<target>.+?(?:box|field|bar|area|input|form|line|column|cell|chat|space))" + _END, re.I)
_TYPE_PLAIN = re.compile(_LEAD + r"(?:type|type out|type in)\s+(?P<text>.+?)" + _END, re.I)
_SCROLL = re.compile(_LEAD + r"scroll\s+(?P<dir>up|down)(?:\s+(?P<amt>a (?:little|bit)|a lot|a little bit|more|some|(?:\d+|one|two|three|four|five)(?: times)?))?" + _END
                     + r"|" + _LEAD + r"scroll (?:to )?(?:the )?(?P<edge>top|bottom)" + _END, re.I)
_CLOSE = re.compile(_LEAD + r"close\s+(?:this|that|the|the current|the active)\s+(?P<what>window|pop-?up|dialog|app|application|program|tab)" + _END, re.I)
_SHORTCUTS = [
    (re.compile(_LEAD + r"(?:go back|back)" + _END, re.I), [0x12, 0x25], "go back"),
    (re.compile(_LEAD + r"go forward" + _END, re.I), [0x12, 0x27], "go forward"),
    (re.compile(_LEAD + r"(?:refresh|reload)(?: (?:the|this) (?:page|tab|window))?" + _END, re.I), [0x74], "refresh"),
    (re.compile(_LEAD + r"(?:copy (?:that|this|it|the selection))" + _END, re.I), [0x11, ord("C")], "copy"),
    (re.compile(_LEAD + r"(?:paste(?: (?:it|that|here))?)" + _END, re.I), [0x11, ord("V")], "paste"),
    (re.compile(_LEAD + r"(?:undo(?: (?:that|it))?)" + _END, re.I), [0x11, ord("Z")], "undo"),
    (re.compile(_LEAD + r"(?:redo(?: (?:that|it))?)" + _END, re.I), [0x11, ord("Y")], "redo"),
    (re.compile(_LEAD + r"(?:select all|select everything)" + _END, re.I), [0x11, ord("A")], "select all"),
    (re.compile(_LEAD + r"(?:save (?:this|it|that|the (?:file|document)))" + _END, re.I), [0x11, ord("S")], "save"),
    (re.compile(_LEAD + r"zoom in" + _END, re.I), [0x11, 0xBB], "zoom in"),
    (re.compile(_LEAD + r"zoom out" + _END, re.I), [0x11, 0xBD], "zoom out"),
    (re.compile(_LEAD + r"(?:new tab|open a new tab)" + _END, re.I), [0x11, ord("T")], "new tab"),
    (re.compile(_LEAD + r"(?:next tab|switch tabs?)" + _END, re.I), [0x11, 0x09], "next tab"),
    (re.compile(_LEAD + r"(?:full ?screen(?: (?:it|this|the video))?|go full ?screen)" + _END, re.I), [0x7A], "full screen"),
]
_AMOUNT = {"a little": 2, "a bit": 2, "a little bit": 2, "some": 5, "more": 5, "a lot": 15, "one": 3, "two": 6, "three": 9,
           "four": 12, "five": 15}


def parse_look(text: str) -> LookRequest | None:
    t = (text or "").strip()
    if not t or len(t) > 300:
        return None
    for pattern in _LOOK_PATTERNS:
        if pattern.match(t):
            return LookRequest(t, full_screen=bool(_FULL_SCREEN.search(t)))
    return None


def wants_vision_install(text: str) -> bool:
    return bool(_INSTALL_VISION.match((text or "").strip()))


def parse_watch(text: str) -> WatchRequest | None:
    t = (text or "").strip()
    if _WATCH_STOP.match(t):
        return WatchRequest("stop")
    if _WATCH_STATUS.match(t):
        return WatchRequest("status")
    start = _WATCH_START.match(t)
    if start:
        what = (start.group("what") or "").strip()
        return WatchRequest("start", what if what and not re.fullmatch(r"me|anything|stuff|things", what, re.I) else "")
    when = _WATCH_WHEN.match(t)
    if when:
        cond = (when.group("cond") or when.group("cond2") or "").strip(" ,.")
        if cond and not _NOT_SCREEN_CONDITION.search(cond) and len(cond) <= 160:
            return WatchRequest("start", cond)
    return None


def parse_act(text: str) -> ActRequest | None:
    t = (text or "").strip()
    if not t or len(t) > 200:
        return None
    for pattern, keys, label in _SHORTCUTS:
        if pattern.match(t):
            return ActRequest("keys", keys=keys, label=label)
    close = _CLOSE.match(t)
    if close:
        what = close.group("what").lower()
        if what == "tab":
            return ActRequest("keys", keys=[0x11, ord("W")], label="close the tab")
        return ActRequest("close", label=f"close the {what}")
    scroll = _SCROLL.match(t)
    if scroll:
        if scroll.group("edge"):
            top = scroll.group("edge").lower() == "top"
            return ActRequest("keys", keys=[0x11, 0x24 if top else 0x23], label=f"scroll to the {scroll.group('edge').lower()}")
        amount = (scroll.group("amt") or "").lower().replace(" times", "")
        n = int(amount) * 3 if amount.isdigit() else _AMOUNT.get(amount, 5)
        return ActRequest("scroll", amount=n if scroll.group("dir").lower() == "up" else -n, label=f"scroll {scroll.group('dir').lower()}")
    typed = _TYPE.match(t)
    if typed:
        return ActRequest("type", target=typed.group("target").strip(), text=_unquote(typed.group("text")), label="type")
    plain = _TYPE_PLAIN.match(t)
    if plain:
        return ActRequest("type", text=_unquote(plain.group("text")), label="type")
    click = _CLICK.match(t)
    if click:
        kind = re.sub(r"[- ]", "", click.group("kind").lower())
        target = click.group("target").strip(" \"'")
        if kind in ("press", "hit") and not re.search(r"\b(?:button|link|icon|tab|option|item)\b", target, re.I):
            keys = parse_keys(target)
            if keys:
                return ActRequest("keys", keys=keys, label=f"press {target}")
        if kind in ("select", "choose", "check", "tick", "uncheck") and not re.search(
                r"\b(?:button|link|tab|option|box|checkbox|item|icon|menu)\b|^(?:the |that )", target, re.I):
            return None  # "choose a number", "check the weather" are not clicks
        if re.match(r"^(?:a|an|some|me)\b", target, re.I) or re.search(r"\bweather|news|email|time\b", target, re.I) and kind != "click":
            return None
        action = "double" if kind == "doubleclick" else "right" if kind == "rightclick" else "click"
        return ActRequest(action, target=target, label=f"{click.group('kind').lower()} {target}")
    return None


def is_risky(act: ActRequest) -> bool:
    return act.action == "close" or bool(_RISKY.search(act.target or "")) or (act.action == "keys" and act.label in ("close the tab",))


def _unquote(text: str) -> str:
    text = (text or "").strip()
    m = re.fullmatch(r"[\"'“‘](.*)[\"'”’]", text)
    return m.group(1) if m else text
