"""Tiny, dependency-free controls for the operating system: volume, screenshots, the desktop, the lock screen.

Windows uses the same media-key events a keyboard sends; Linux uses PulseAudio/ALSA when present.
Everything takes an injectable ``runner`` so it can be tested without touching the real machine.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import time
from pathlib import Path

log = logging.getLogger("jarvis.osctl")

VK_VOLUME_MUTE, VK_VOLUME_DOWN, VK_VOLUME_UP = 0xAD, 0xAE, 0xAF
KEYEVENTF_KEYUP = 0x0002
STEP_PERCENT = 2  # one Windows volume key press changes the volume by 2%


class OsControlError(Exception):
    pass


def _press(keys: list[int], user32=None) -> None:
    if user32 is None:
        import ctypes

        user32 = ctypes.windll.user32
    for key in keys:
        user32.keybd_event(key, 0, 0, 0)
    for key in reversed(keys):
        user32.keybd_event(key, 0, KEYEVENTF_KEYUP, 0)


def change_volume(delta: int, platform: str | None = None, runner=subprocess.run, user32=None) -> None:
    """Raise or lower the system volume by roughly ``delta`` percent."""
    platform = platform or sys.platform
    if platform == "win32":
        key = VK_VOLUME_UP if delta > 0 else VK_VOLUME_DOWN
        for _ in range(max(1, round(abs(delta) / STEP_PERCENT))):
            _press([key], user32)
        return
    if shutil.which("pactl") or runner is not subprocess.run:
        runner(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{delta:+d}%"], check=False, capture_output=True)
        return
    if shutil.which("amixer"):
        runner(["amixer", "-q", "sset", "Master", f"{abs(delta)}%{'+' if delta > 0 else '-'}"], check=False, capture_output=True)
        return
    raise OsControlError("I can't control the volume on this system.")


def set_volume(percent: int, platform: str | None = None, runner=subprocess.run, user32=None, sleep=time.sleep) -> None:
    percent = max(0, min(100, int(percent)))
    platform = platform or sys.platform
    if platform == "win32":
        for _ in range(50):  # all the way down (each key press is 2%), then up to the target
            _press([VK_VOLUME_DOWN], user32)
        for _ in range(round(percent / STEP_PERCENT)):
            _press([VK_VOLUME_UP], user32)
        return
    if shutil.which("pactl") or runner is not subprocess.run:
        runner(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{percent}%"], check=False, capture_output=True)
        return
    if shutil.which("amixer"):
        runner(["amixer", "-q", "sset", "Master", f"{percent}%"], check=False, capture_output=True)
        return
    raise OsControlError("I can't control the volume on this system.")


def toggle_mute(platform: str | None = None, runner=subprocess.run, user32=None) -> None:
    platform = platform or sys.platform
    if platform == "win32":
        _press([VK_VOLUME_MUTE], user32)
    elif shutil.which("pactl") or runner is not subprocess.run:
        runner(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "toggle"], check=False, capture_output=True)
    elif shutil.which("amixer"):
        runner(["amixer", "-q", "sset", "Master", "toggle"], check=False, capture_output=True)
    else:
        raise OsControlError("I can't control the volume on this system.")


VK_MEDIA_NEXT, VK_MEDIA_PREV, VK_MEDIA_STOP, VK_MEDIA_PLAY_PAUSE = 0xB0, 0xB1, 0xB2, 0xB3
_MEDIA_KEYS = {"toggle": VK_MEDIA_PLAY_PAUSE, "next": VK_MEDIA_NEXT, "previous": VK_MEDIA_PREV, "stop": VK_MEDIA_STOP}
_PLAYERCTL = {"toggle": "play-pause", "next": "next", "previous": "previous", "stop": "stop", "play": "play", "pause": "pause"}


def media_key(action: str, platform: str | None = None, runner=subprocess.run, user32=None) -> None:
    """Press a media key: "toggle" (play/pause), "next", "previous" or "stop". Every player on Windows obeys these."""
    platform = platform or sys.platform
    if platform == "win32":
        _press([_MEDIA_KEYS.get(action, VK_MEDIA_PLAY_PAUSE)], user32)
        return
    if shutil.which("playerctl") or runner is not subprocess.run:
        runner(["playerctl", _PLAYERCTL.get(action, "play-pause")], check=False, capture_output=True)
        return
    raise OsControlError("I can't control media playback on this system.")


def set_mute(on: bool, platform: str | None = None, runner=subprocess.run, user32=None) -> None:
    """Mute or unmute (not toggle). On Windows a volume-up then volume-down unmutes and keeps the level."""
    platform = platform or sys.platform
    if platform == "win32":
        _press([VK_VOLUME_UP], user32)
        _press([VK_VOLUME_DOWN], user32)
        if on:
            _press([VK_VOLUME_MUTE], user32)
    elif shutil.which("pactl") or runner is not subprocess.run:
        runner(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "1" if on else "0"], check=False, capture_output=True)
    elif shutil.which("amixer"):
        runner(["amixer", "-q", "sset", "Master", "mute" if on else "unmute"], check=False, capture_output=True)
    else:
        raise OsControlError("I can't control the volume on this system.")


def screenshots_folder() -> Path:
    base = Path.home() / "Pictures"
    folder = base / "Screenshots" if base.is_dir() else Path.home()
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def take_screenshot(folder: Path | None = None, grabber=None) -> Path:
    """Save the whole screen as a PNG and return its path."""
    folder = folder or screenshots_folder()
    if grabber is None:
        try:
            from PIL import ImageGrab
        except ImportError as exc:
            raise OsControlError("Screenshots need the Pillow package, which isn't installed.") from exc
        grabber = lambda: ImageGrab.grab(all_screens=True)  # noqa: E731
    try:
        image = grabber()
    except Exception as exc:
        raise OsControlError(f"I couldn't capture the screen ({exc.__class__.__name__}).") from exc
    path = folder / f"JARVIS {time.strftime('%Y-%m-%d %H-%M-%S')}.png"
    image.save(path)
    return path


def show_desktop(platform: str | None = None, user32=None, runner=subprocess.run) -> None:
    platform = platform or sys.platform
    if platform == "win32":
        VK_LWIN, VK_D = 0x5B, 0x44
        _press([VK_LWIN, VK_D], user32)
    elif shutil.which("wmctrl"):
        runner(["wmctrl", "-k", "on"], check=False)
    else:
        raise OsControlError("I can't minimise windows on this system.")


def lock_screen(platform: str | None = None, runner=subprocess.run, user32=None) -> None:
    platform = platform or sys.platform
    if platform == "win32":
        if user32 is None:
            import ctypes

            user32 = ctypes.windll.user32
        user32.LockWorkStation()
    elif platform == "darwin":
        runner(["pmset", "displaysleepnow"], check=False)
    elif shutil.which("loginctl"):
        runner(["loginctl", "lock-session"], check=False)
    else:
        raise OsControlError("I can't lock the screen on this system.")


_PS_TEMPS = r"""
$out = @()
foreach ($ns in 'root/LibreHardwareMonitor','root/OpenHardwareMonitor') {
  try { Get-CimInstance -Namespace $ns -ClassName Sensor -ErrorAction Stop |
        Where-Object { $_.SensorType -eq 'Temperature' -and ($_.Name -match 'CPU|Package|Core') } |
        ForEach-Object { $out += [math]::Round($_.Value, 1) } } catch {}
}
if ($out.Count -eq 0) {
  try { Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature -ErrorAction Stop |
        ForEach-Object { $out += [math]::Round($_.CurrentTemperature / 10 - 273.15, 1) } } catch {}
}
$out -join ','
"""


def cpu_temperature(platform: str | None = None, runner=subprocess.run) -> float | None:
    """The processor temperature in °C when the PC reports one (many Windows PCs only do with admin rights
    or a monitor such as LibreHardwareMonitor running); None if it can't be read."""
    platform = platform or sys.platform
    readings: list[float] = []
    try:
        if platform == "win32":
            done = runner(["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_TEMPS], capture_output=True, text=True,
                          timeout=8, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            readings = [float(v) for v in (done.stdout or "").strip().split(",") if v.strip()]
        else:
            import psutil

            for entries in (getattr(psutil, "sensors_temperatures", lambda: {})() or {}).values():
                readings += [e.current for e in entries if e.current]
    except Exception as exc:
        log.info("Couldn't read the CPU temperature: %s", exc)
        return None
    readings = [r for r in readings if 5 < r < 120]  # ignore nonsense sensors
    return round(max(readings), 1) if readings else None
