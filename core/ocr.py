"""Reading the text on screen, with where each word is.

Vision models describe a screen well but are poor at exact text and positions; OCR is the reverse. JARVIS
uses both. On Windows the OCR engine built into Windows 10/11 is used through a small PowerShell helper
that stays running (no install, no API key, nothing leaves the PC). Tesseract is used if it's installed.
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("jarvis.ocr")


@dataclass
class Word:
    text: str
    x: float
    y: float
    w: float
    h: float


@dataclass
class Line:
    text: str
    words: list[Word] = field(default_factory=list)

    @property
    def box(self) -> tuple[float, float, float, float]:
        """x, y, w, h of the whole line."""
        if not self.words:
            return (0.0, 0.0, 0.0, 0.0)
        x0 = min(w.x for w in self.words)
        y0 = min(w.y for w in self.words)
        x1 = max(w.x + w.w for w in self.words)
        y1 = max(w.y + w.h for w in self.words)
        return (x0, y0, x1 - x0, y1 - y0)


def lines_from_json(data: dict, scale: float = 1.0) -> list[Line]:
    """Parse the helper's output; ``scale`` undoes any resizing done before recognition."""
    out = []
    for raw in data.get("lines") or []:
        words = [Word(str(w.get("t", "")), w.get("x", 0) / scale, w.get("y", 0) / scale, w.get("w", 0) / scale,
                      w.get("h", 0) / scale) for w in raw.get("words") or []]
        text = str(raw.get("text") or " ".join(w.text for w in words)).strip()
        if text:
            out.append(Line(text, words))
    return out


def plain_text(lines: list[Line], limit: int = 6000) -> str:
    return "\n".join(line.text for line in lines)[:limit]


# ----------------------------------------------------------------------------- finding text on screen
_NOISE = re.compile(r"\b(?:the|a|an|that|this|those|these|on|button|link|tab|icon|option|item|menu|entry|field|box|bar|"
                    r"area|text|word|thing|one|please|called|labelled|labeled|named|saying|says)\b", re.I)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", (text or "").lower())).strip()


@dataclass
class TextHit:
    score: float
    box: tuple[float, float, float, float]  # x, y, w, h (image pixels)
    text: str

    @property
    def center(self) -> tuple[float, float]:
        x, y, w, h = self.box
        return (x + w / 2, y + h / 2)


def find_text(lines: list[Line], target: str) -> list[TextHit]:
    """Places on screen whose text matches ``target`` ('Send', 'sign in button', 'the search box'), best first."""
    wanted = _norm(_NOISE.sub(" ", target or "")) or _norm(target)
    if not wanted:
        return []
    n = len(wanted.split())
    hits: list[TextHit] = []
    for line in lines:
        words = line.words
        # every run of n words (or n +/- 1) is a candidate, so "Sign in" inside "Sign in to continue" is found
        for size in {max(1, n - 1), n, n + 1}:
            for i in range(0, max(0, len(words) - size + 1)):
                chunk = words[i:i + size]
                text = _norm(" ".join(w.text for w in chunk))
                if not text:
                    continue
                if text == wanted:
                    score = 1.0
                else:
                    score = difflib.SequenceMatcher(None, text, wanted).ratio()
                    if wanted in text and len(wanted) >= 3:
                        score = max(score, 0.8 + 0.2 * len(wanted) / len(text))
                if score < 0.72:
                    continue
                x0 = min(w.x for w in chunk)
                y0 = min(w.y for w in chunk)
                x1 = max(w.x + w.w for w in chunk)
                y1 = max(w.y + w.h for w in chunk)
                hits.append(TextHit(round(score, 3), (x0, y0, x1 - x0, y1 - y0), " ".join(w.text for w in chunk)))
        if not words and _norm(line.text) == wanted:
            hits.append(TextHit(1.0, line.box, line.text))
    hits.sort(key=lambda h: h.score, reverse=True)
    unique: list[TextHit] = []
    for hit in hits:  # the same spot found via different word runs counts once
        cx, cy = hit.center
        if all(abs(cx - u.center[0]) > 6 or abs(cy - u.center[1]) > 6 for u in unique):
            unique.append(hit)
    return unique


# ----------------------------------------------------------------------------- engines
_PS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [Text.Encoding]::UTF8
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics, ContentType = WindowsRuntime]
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, [Type]$type) { $t = $asTask.MakeGenericMethod($type).Invoke($null, @($op)); $null = $t.Wait(-1); $t.Result }
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if ($null -eq $engine) {
  foreach ($lang in [Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang); if ($engine) { break } }
}
if ($null -eq $engine) { [Console]::Out.WriteLine('{"ready":false,"error":"no OCR language is installed in Windows"}'); exit 0 }
[Console]::Out.WriteLine((@{ ready = $true; max = [Windows.Media.Ocr.OcrEngine]::MaxImageDimension; language = $engine.RecognizerLanguage.LanguageTag } | ConvertTo-Json -Compress))
while ($true) {
  $path = [Console]::In.ReadLine()
  if ($null -eq $path -or $path -eq 'exit') { break }
  try {
    $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
    $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
    $lines = @(foreach ($line in $result.Lines) {
      @{ text = $line.Text; words = @(foreach ($w in $line.Words) { $r = $w.BoundingRect; @{ t = $w.Text; x = [int]$r.X; y = [int]$r.Y; w = [int]$r.Width; h = [int]$r.Height } }) }
    })
    $stream.Dispose()
    [Console]::Out.WriteLine((@{ lines = $lines } | ConvertTo-Json -Depth 6 -Compress))
  } catch {
    [Console]::Out.WriteLine((@{ error = $_.Exception.Message } | ConvertTo-Json -Compress))
  }
}
"""


class OcrUnavailable(Exception):
    pass


class WindowsOcr:
    """Windows 10/11's own OCR engine, kept running in a PowerShell process for fast repeated reads."""

    name = "Windows OCR"

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._max = 2600
        self.error: str | None = None
        self.language: str | None = None

    def _start(self) -> None:
        folder = Path(tempfile.gettempdir()) / "jarvis-ocr"
        folder.mkdir(exist_ok=True)
        script = folder / "ocr.ps1"
        script.write_text(_PS_SCRIPT, encoding="utf-8-sig")
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self._proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
            bufsize=1, creationflags=flags)
        hello = self._readline(timeout=40)
        info = json.loads(hello) if hello else {"ready": False, "error": "the OCR helper didn't start"}
        if not info.get("ready"):
            self.stop()
            raise OcrUnavailable(info.get("error") or "Windows OCR is unavailable")
        self._max = int(info.get("max") or 2600)
        self.language = info.get("language")

    def _readline(self, timeout: float) -> str:
        result: list[str] = []
        reader = threading.Thread(target=lambda: result.append(self._proc.stdout.readline()), daemon=True)
        reader.start()
        reader.join(timeout)
        if not result:
            self.stop()
            raise OcrUnavailable("the OCR helper stopped responding")
        return result[0].strip()

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                proc.stdin.write("exit\n")
                proc.stdin.flush()
            except Exception:
                pass
            try:
                proc.kill()
            except Exception:
                pass

    def available(self) -> bool:
        if sys.platform != "win32":
            return False
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return True
            if self.error:
                return False
            try:
                self._start()
                return True
            except (OcrUnavailable, OSError, ValueError) as exc:
                self.error = str(exc)
                log.info("Windows OCR unavailable: %s", exc)
                return False

    def read(self, image) -> list[Line]:
        if not self.available():
            raise OcrUnavailable(self.error or "Windows OCR is unavailable")
        image, scale = _prepare(image, self._max)
        fd, path = tempfile.mkstemp(suffix=".png", prefix="jarvis-ocr-")
        os.close(fd)
        try:
            image.save(path, "PNG")
            with self._lock:
                if self._proc is None:
                    self._start()
                self._proc.stdin.write(path + "\n")
                self._proc.stdin.flush()
                reply = self._readline(timeout=30)
        finally:
            try:
                os.unlink(path)  # screen contents are never kept on disk
            except OSError:
                pass
        data = json.loads(reply or "{}")
        if data.get("error"):
            raise OcrUnavailable(data["error"])
        return lines_from_json(data, scale)


class TesseractOcr:
    name = "Tesseract"

    def __init__(self, exe: str | None = None) -> None:
        self._exe = exe or shutil.which("tesseract")
        self.error = None if self._exe else "Tesseract is not installed"

    def available(self) -> bool:
        return bool(self._exe)

    def read(self, image) -> list[Line]:
        if not self._exe:
            raise OcrUnavailable("Tesseract is not installed")
        image, scale = _prepare(image, 4000)
        fd, path = tempfile.mkstemp(suffix=".png", prefix="jarvis-ocr-")
        os.close(fd)
        try:
            image.save(path, "PNG")
            out = subprocess.run([self._exe, path, "stdout", "tsv"], capture_output=True, text=True, encoding="utf-8", timeout=60)
        finally:
            os.unlink(path)
        groups: dict[tuple, list[Word]] = {}
        for row in out.stdout.splitlines()[1:]:
            cols = row.split("\t")
            if len(cols) < 12 or not cols[11].strip():
                continue
            key = (cols[2], cols[3], cols[4])  # block, paragraph, line
            groups.setdefault(key, []).append(Word(cols[11], int(cols[6]) / scale, int(cols[7]) / scale,
                                                   int(cols[8]) / scale, int(cols[9]) / scale))
        return [Line(" ".join(w.text for w in words), words) for words in groups.values()]


class NoOcr:
    name = "none"
    error = "no OCR engine is available"

    def available(self) -> bool:
        return False

    def read(self, image) -> list[Line]:
        raise OcrUnavailable(self.error)


def _prepare(image, max_side: int):
    """Small UI text reads better enlarged; huge screenshots must shrink to the engine's limit."""
    image = image.convert("RGB")
    w, h = image.size
    scale = 1.0
    if max(w, h) <= 1400:
        scale = 2.0
    if max(w, h) * scale > max_side:
        scale = max_side / max(w, h)
    if abs(scale - 1.0) > 0.01:
        from PIL import Image

        image = image.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    return image, scale


def default_ocr():
    if sys.platform == "win32":
        return WindowsOcr()
    if shutil.which("tesseract"):
        return TesseractOcr()
    return NoOcr()
