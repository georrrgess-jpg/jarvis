"""One-click updates: find a newer Jarvis.exe on GitHub Releases, fetch and check it, swap it in, restart.

Safety, in order:
* the manifest (latest.json) and the exe only come from this project's GitHub releases over HTTPS;
* the download must match the manifest's exact size and SHA-256;
* the new exe must pass its own ``--selftest`` before anything is replaced;
* the running exe is never touched: it hands over to the new exe (started with --finish-update) and quits; that copy
  swaps the files, keeps the old exe as Jarvis.previous.exe, starts the new Jarvis.exe and waits for it to report that
  it has fully started; if it crashes or never does, the old version is put back and started again.

Nothing installs by itself: checking and downloading happen in the background, installing waits for the user.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

log = logging.getLogger("jarvis.updater")

UPDATE_REPO = "georrrgess-jpg/jarvis"
MANIFEST_URL = f"https://github.com/{UPDATE_REPO}/releases/latest/download/latest.json"
_TRUSTED_HOSTS = ("github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com")
CHECK_EVERY = 6 * 3600
HEALTH_TIMEOUT = 150.0  # how long the finishing step waits for the new copy to come up
SELFTEST_TIMEOUT = 300.0


def parse_version(text: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", str(text or ""))[:4]
    return tuple(int(n) for n in nums) + (0,) * (4 - len(nums))


def is_newer(candidate: str, current: str) -> bool:
    return parse_version(candidate) > parse_version(current)


@dataclass
class Release:
    version: str
    url: str
    sha256: str
    size: int
    notes: str = ""
    published: str = ""
    tag: str = ""

    @classmethod
    def from_manifest(cls, data: dict, repo: str = UPDATE_REPO, trusted: bool = True) -> "Release":
        rel = cls(version=str(data.get("version") or ""), url=str(data.get("url") or ""), sha256=str(data.get("sha256") or "").lower(),
                  size=int(data.get("size") or 0), notes=str(data.get("notes") or "")[:4000],
                  published=str(data.get("published") or ""), tag=str(data.get("tag") or ""))
        if not re.fullmatch(r"\d+(?:\.\d+){1,3}", rel.version):
            raise ValueError("the update information has no valid version")
        if not re.fullmatch(r"[0-9a-f]{64}", rel.sha256) or rel.size < 1_000_000:
            raise ValueError("the update information is incomplete")
        if trusted:
            u = urlparse(rel.url)
            if u.scheme != "https" or u.hostname not in _TRUSTED_HOSTS or (u.hostname == "github.com" and not u.path.startswith(f"/{repo}/releases/")):
                raise ValueError("the update points somewhere other than this project's GitHub releases")
        return rel


@dataclass
class Status:
    state: str = "idle"  # idle checking up_to_date available downloading verifying ready installing rolled_back error unsupported
    current: str = ""
    release: dict | None = None
    progress: float = 0.0
    error: str = ""
    checked_at: float = 0.0
    previous: str = ""  # version of Jarvis.previous.exe, if kept
    detail: str = ""
    extra: dict = field(default_factory=dict)


class UpdateError(Exception):
    pass


def _reset_env() -> dict:
    """A one-file PyInstaller app must tell the next copy it starts not to reuse its own unpacked files."""
    env = dict(os.environ)
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    for key in list(env):
        if key.startswith("_PYI_") or key == "_MEIPASS2":
            env.pop(key, None)
    return env


class Updater:
    """Checks, downloads, verifies and installs updates. All the pieces that touch the outside world are injectable."""

    def __init__(self, current: str, exe: Path | None = None, state_dir: Path | None = None, *, http=None,
                 emit: Callable[[dict], None] | None = None, manifest_url: str | None = None,
                 supported: bool | None = None, run=subprocess.run, popen=subprocess.Popen, sleep=time.sleep,
                 trusted_only: bool | None = None) -> None:
        self.current = current
        self.exe = Path(exe or sys.executable)
        self.state_dir = Path(state_dir) if state_dir else Path.home()
        self.manifest_url = manifest_url or os.environ.get("JARVIS_UPDATE_URL") or MANIFEST_URL
        self.trusted_only = (not os.environ.get("JARVIS_UPDATE_URL")) if trusted_only is None else trusted_only
        self._supported = (sys.platform == "win32" and bool(getattr(sys, "frozen", False))) if supported is None else supported
        self._http = http
        self._emit = emit or (lambda status: None)
        self._run, self._popen, self._sleep = run, popen, sleep
        self._lock = threading.RLock()
        self._busy = threading.Lock()
        self.status = Status(current=current)
        self.ready: Release | None = None
        # what the app gives us for a restart (set by app.py): hide/show the window, free/retake the single-instance lock, quit
        self.hooks: dict[str, Callable] = {}
        if not self._supported:
            self._set(state="unsupported", detail="Updates install into the Jarvis.exe app; this copy runs from source.")
        prev = self.previous_path
        if prev.exists():
            self.status.previous = self._read_state().get("previous_version", "")

    # -- paths
    @property
    def update_path(self) -> Path:
        return self.exe.with_name("Jarvis.update.exe")

    @property
    def previous_path(self) -> Path:
        return self.exe.with_name("Jarvis.previous.exe")

    @property
    def state_path(self) -> Path:
        return self.state_dir / "update-state.json"

    @property
    def supported(self) -> bool:
        return self._supported

    # -- status
    def _set(self, **fields) -> None:
        with self._lock:
            for k, v in fields.items():
                setattr(self.status, k, v)
            snapshot = asdict(self.status)
        try:
            self._emit(snapshot)
        except Exception:
            log.debug("update status emit failed", exc_info=True)

    def snapshot(self) -> dict:
        with self._lock:
            return asdict(self.status)

    def _read_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write_state(self, data: dict) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_path)

    # -- network
    def _client(self):
        if self._http is None:
            import httpx

            self._http = httpx.Client(timeout=httpx.Timeout(20.0, read=60.0), follow_redirects=True,
                                      headers={"User-Agent": f"JARVIS/{self.current} updater"})
        return self._http

    def check(self) -> Release | None:
        """Returns the newer release, or None when this is the latest. Raises UpdateError when it can't tell."""
        if not self._supported:
            return None
        self._set(state="checking", error="")
        try:
            resp = self._client().get(self.manifest_url)
            if resp.status_code == 404:
                self._set(state="up_to_date", checked_at=time.time(), release=None)
                return None  # nothing published yet
            resp.raise_for_status()
            rel = Release.from_manifest(resp.json(), trusted=self.trusted_only)
        except ValueError as exc:
            self._set(state="error", error=str(exc), checked_at=time.time())
            raise UpdateError(str(exc)) from None
        except Exception as exc:
            msg = "I couldn't reach GitHub to check for updates" if "connect" in str(exc).lower() or "timeout" in str(exc).lower() \
                else f"checking for updates failed ({exc.__class__.__name__})"
            self._set(state="error", error=msg, checked_at=time.time())
            raise UpdateError(msg) from None
        if not is_newer(rel.version, self.current):
            self._set(state="up_to_date", checked_at=time.time(), release=None, detail="")
            return None
        if rel.version == self._read_state().get("bad_version"):
            self._set(state="up_to_date", checked_at=time.time(), release=None,
                      detail=f"Version {rel.version} didn't start on this PC, so I'm waiting for the next one.")
            return None
        if self.ready is not None and self.ready.version == rel.version and self.update_path.exists():
            self._set(state="ready", checked_at=time.time(), release=asdict(rel))
            return rel
        self._set(state="available", checked_at=time.time(), release=asdict(rel))
        return rel

    def download(self, rel: Release) -> Path:
        """Fetch the new exe next to the current one and check its size and SHA-256."""
        folder = self.exe.parent
        part = self.update_path.with_suffix(".part")
        try:
            probe = folder / ".jarvis-write-test"
            probe.write_bytes(b"")
            probe.unlink()
        except OSError:
            msg = f"I can't write to the folder JARVIS is in ({folder}). Move Jarvis.exe to a folder of your own, such as Documents, and try again."
            self._set(state="error", error=msg)
            raise UpdateError(msg) from None
        self._set(state="downloading", progress=0.0, error="")
        digest, got = hashlib.sha256(), 0
        try:
            with self._client().stream("GET", rel.url) as resp:
                resp.raise_for_status()
                with open(part, "wb") as fh:
                    last = 0.0
                    for chunk in resp.iter_bytes(1 << 16):
                        fh.write(chunk)
                        digest.update(chunk)
                        got += len(chunk)
                        if got > rel.size + 1024:
                            raise UpdateError("the download is bigger than expected")
                        pct = min(1.0, got / max(1, rel.size))
                        if pct - last >= 0.02:
                            last = pct
                            self._set(progress=round(pct, 3))
        except UpdateError as exc:
            part.unlink(missing_ok=True)
            self._set(state="error", error=str(exc))
            raise
        except Exception as exc:
            part.unlink(missing_ok=True)
            msg = f"the download stopped ({exc.__class__.__name__}); I'll try again later"
            self._set(state="error", error=msg)
            raise UpdateError(msg) from None
        self._set(state="verifying", progress=1.0)
        if got != rel.size or digest.hexdigest() != rel.sha256:
            part.unlink(missing_ok=True)
            msg = "the downloaded file didn't match its fingerprint, so I threw it away"
            self._set(state="error", error=msg)
            raise UpdateError(msg)
        os.replace(part, self.update_path)
        return self.update_path

    def selftest(self, exe: Path) -> bool:
        """The new exe checks itself (imports, audio, voice, wake word...) without opening a window."""
        report = self.state_dir / "update-selftest.json"
        report.unlink(missing_ok=True)
        try:
            proc = self._run([str(exe), "--selftest", "--selftest-report", str(report)], env=_reset_env(),
                             timeout=SELFTEST_TIMEOUT, capture_output=True,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("update self-test didn't run: %s", exc)
            return False
        ok = proc.returncode == 0
        try:
            ok = ok and bool(json.loads(report.read_text(encoding="utf-8")).get("ok"))
        except Exception:
            ok = False
        log.info("update self-test %s (exit %s)", "passed" if ok else "FAILED", proc.returncode)
        return ok

    def prepare(self, rel: Release | None = None) -> Release | None:
        """Check, download, verify and self-test: afterwards one click installs it."""
        if not self._busy.acquire(blocking=False):
            return self.ready
        try:
            rel = rel or self.check()
            if rel is None:
                return None
            if self.ready is not None and self.ready.version == rel.version and self.update_path.exists():
                self._set(state="ready", release=asdict(rel))
                return rel
            path = self.download(rel)
            self._set(state="verifying", detail="The new version is checking itself")
            if not self.selftest(path):
                path.unlink(missing_ok=True)
                msg = f"version {rel.version} didn't pass its self-check on this PC, so I haven't installed it"
                self._set(state="error", error=msg, detail="")
                raise UpdateError(msg)
            self.ready = rel
            self._set(state="ready", release=asdict(rel), detail="")
            return rel
        finally:
            self._busy.release()

    # -- installing, step 1: the running (old) copy hands over and quits
    def install(self, restore: bool = False) -> bool:
        """Start the prepared exe (or the previous one) in "finish the update" mode, then quit.
        That copy swaps the files once we're gone, starts the new Jarvis.exe and waits for it to come up;
        if it doesn't, it puts this version back and starts it again. The running exe is never modified."""
        if not self._supported:
            raise UpdateError("updates only install into Jarvis.exe")
        new, prev = self.update_path, self.previous_path
        if restore:
            if not prev.exists():
                raise UpdateError("there's no previous version kept on this PC")
            import shutil

            shutil.copyfile(prev, new)
            target = self._read_state().get("previous_version") or "the previous version"
        else:
            if self.ready is None or not new.exists():
                raise UpdateError("no update is ready to install")
            target = self.ready.version
        state = {"status": "handover", "from_version": self.current, "to_version": target, "previous_version": self.current,
                 "notes": "" if restore or self.ready is None else self.ready.notes, "restore": restore,
                 "exe": str(self.exe), "pids": sorted({os.getpid(), os.getppid()}), "started": time.time()}
        self._write_state(state)
        self._set(state="installing", detail=f"Installing {target}")
        try:
            self._popen([str(new), "--finish-update", str(self.state_path)], env=_reset_env(), close_fds=True,
                        creationflags=_detached_flags())
        except OSError as exc:
            state.update(status="error", error=f"couldn't start the new version ({exc})")
            self._write_state(state)
            self._set(state="error", error="I couldn't start the new version; nothing was changed")
            return False
        log.info("handing over to %s; this copy (%s) is leaving", target, self.current)
        _call(self.hooks, "release_lock")
        _call(self.hooks, "quit")
        return True

    # -- installing, step 2: the new exe, started with --finish-update (no window)
    def finish(self, state_path: Path) -> int:
        """Runs from Jarvis.update.exe once the old copy has gone: swap, start, watch, and roll back if needed."""
        self_exe = self.exe  # Jarvis.update.exe (this process): it is never renamed or replaced while running
        state = self.after_update(state_path)
        target_exe = Path(state.get("exe") or self_exe.with_name("Jarvis.exe"))
        prev = target_exe.with_name("Jarvis.previous.exe")
        self._wait_pids(state.get("pids") or [], 60)

        def write(**fields) -> None:
            state.update(fields)
            tmp = Path(state_path).with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
            os.replace(tmp, state_path)

        swapped = self._retry(lambda: self._swap_in(self_exe, target_exe, prev), 60)
        if not swapped:
            write(status="error", error="couldn't replace Jarvis.exe (is it still open?)")
            self._start(target_exe, state_path)  # bring the old version back up so the user isn't left with nothing
            return 1
        write(status="starting", swapped_at=time.time())
        proc = self._start(target_exe, state_path)
        deadline = time.monotonic() + HEALTH_TIMEOUT
        while proc is not None and time.monotonic() < deadline:
            if self.after_update(state_path).get("status") == "healthy":
                write(status="done", finished=time.time())
                log.info("update to %s finished", state.get("to_version"))
                return 0
            if proc.poll() is not None:
                break
            self._sleep(0.5)
        log.error("the new version didn't start properly; putting %s back", state.get("from_version"))
        if proc is not None and proc.poll() is None:
            self._kill(proc)
        restored = self._retry(lambda: (os.replace(prev, target_exe), True)[1], 60)
        write(status="rolled_back", bad_version=state.get("to_version"), finished=time.time(),
              error="" if restored else "couldn't put the previous version back")
        self._start(target_exe, state_path)
        return 1

    def _swap_in(self, new: Path, exe: Path, prev: Path) -> bool:
        if exe.exists():
            if prev.exists():
                prev.unlink()
            os.replace(exe, prev)
        tmp = exe.with_name("Jarvis.exe.new")
        import shutil

        shutil.copyfile(new, tmp)
        os.replace(tmp, exe)
        return True

    def _retry(self, fn: Callable[[], bool], seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while True:
            try:
                return bool(fn())
            except OSError as exc:
                if time.monotonic() >= deadline:
                    log.error("gave up: %s", exc)
                    return False
                self._sleep(0.5)

    def _wait_pids(self, pids: list, seconds: float) -> None:
        try:
            import psutil
        except ImportError:
            self._sleep(3)
            return
        deadline = time.monotonic() + seconds
        for pid in pids:
            try:
                psutil.Process(int(pid)).wait(timeout=max(0.1, deadline - time.monotonic()))
            except Exception:
                pass

    def _start(self, exe: Path, state_path: Path):
        try:
            return self._popen([str(exe), "--after-update", str(state_path)], env=_reset_env(), close_fds=True,
                               creationflags=_detached_flags(), cwd=str(exe.parent))
        except OSError as exc:
            log.error("couldn't start %s: %s", exe, exc)
            return None

    def _kill(self, proc) -> None:
        try:
            if sys.platform == "win32":
                self._run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=20)
            else:
                proc.kill()
            proc.wait(timeout=20)
        except Exception:
            log.debug("couldn't stop the failed update", exc_info=True)

    # -- the copy started with --after-update
    def after_update(self, state_path: Path) -> dict:
        try:
            return json.loads(Path(state_path).read_text(encoding="utf-8"))
        except Exception:
            return {}

    def mark_healthy(self, state_path: Path | None = None) -> None:
        """This copy has fully started: the finishing step can stop watching."""
        path = Path(state_path) if state_path else self.state_path
        data = self.after_update(path)
        if data.get("status") != "starting":
            return
        data.update(status="healthy", running_version=self.current, healthy_at=time.time())
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def cleanup(self) -> None:
        """Leftovers once an update has finished: the installer copy, a half download, a spare file."""
        status = self._read_state().get("status")
        for p in (self.update_path.with_suffix(".part"), self.exe.with_name("Jarvis.exe.new")):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        if self.ready is None and status in (None, "done", "rolled_back", "error"):
            try:
                self.update_path.unlink(missing_ok=True)
            except OSError:
                pass  # the finishing step may still be closing; next time


def _call(hooks: dict, name: str) -> None:
    fn = hooks.get(name)
    if fn is None:
        return
    try:
        fn()
    except Exception:
        log.warning("update hook %s failed", name, exc_info=True)


def _detached_flags() -> int:
    if sys.platform != "win32":
        return 0
    return getattr(subprocess, "DETACHED_PROCESS", 0x8) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
