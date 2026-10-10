"""Real-Windows check of one-click updates, using the freshly built Jarvis.exe (run by the Windows CI job).

A local web server plays GitHub: it serves latest.json (a higher version) and the exe. The "installed" copy is started
with --install-update-when-ready, so it does what the INSTALL button does as soon as the update is ready.

1. Good update: download -> size + SHA-256 check -> the new exe's self-test -> the old copy hands over and quits ->
   the new exe (--finish-update) swaps the files (old kept as Jarvis.previous.exe), starts Jarvis.exe --after-update
   -> it boots and reports healthy.
2. Broken update (JARVIS_TEST_FAIL_AFTER_UPDATE=1 makes the new copy die at start): the finishing step notices,
   puts the old exe back and starts it again, and that version is not offered again.

    python tests/windows_update_check.py dist/Jarvis.exe update-check.json
"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def jarvis_pids() -> list[int]:
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Jarvis.exe", "/FO", "CSV", "/NH"], capture_output=True, text=True).stdout
    return [int(line.split('","')[1]) for line in out.splitlines() if line.startswith('"Jarvis.exe"')]


def main() -> int:
    built = Path(sys.argv[1] if len(sys.argv) > 1 else "dist/Jarvis.exe").resolve()
    report_path = Path(sys.argv[2] if len(sys.argv) > 2 else "update-check.json")
    report: dict = {"checks": {}}
    failures: list[str] = []

    def check(name, ok, detail=None):
        report["checks"][name] = {"ok": bool(ok), "detail": detail}
        print(("PASS " if ok else "FAIL ") + name + (f": {detail}" if detail is not None else ""), flush=True)
        if not ok:
            failures.append(name)

    work = Path(tempfile.mkdtemp(prefix="jarvis-update-"))
    srv = work / "srv"
    srv.mkdir()
    shutil.copyfile(built, srv / "Jarvis.exe")
    digest, size = sha256(built), built.stat().st_size
    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=str(srv), **k)  # noqa: E731
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    (srv / "latest.json").write_text(json.dumps({"version": "99.0.0", "tag": "v99.0.0", "url": f"{base}/Jarvis.exe", "sha256": digest,
                                                  "size": size, "notes": "- A test update from the CI check"}), encoding="utf-8")

    def scenario(name: str, broken: bool) -> None:
        app = work / name / "app"
        home = work / name / "home"
        app.mkdir(parents=True)
        home.mkdir(parents=True)
        exe = app / "Jarvis.exe"
        shutil.copyfile(built, exe)
        env = {**os.environ, "JARVIS_HOME": str(home), "JARVIS_UPDATE_URL": f"{base}/latest.json", "JARVIS_UPDATE_CHECK_DELAY": "3",
               "JARVIS_NO_DIALOGS": "1", "SDL_AUDIODRIVER": "dummy"}
        if broken:
            env["JARVIS_TEST_FAIL_AFTER_UPDATE"] = "1"
        print(f"\n== {name}: starting {exe}", flush=True)
        old = subprocess.Popen([str(exe), "--install-update-when-ready"], env=env, cwd=str(app))
        state_file = home / "update-state.json"
        want = ("done",) if not broken else ("rolled_back",)
        deadline = time.time() + 600
        state: dict = {}
        while time.time() < deadline:
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
            except Exception:
                state = {}
            if state.get("status") in want:
                break
            if old.poll() is not None and state.get("status") not in ("starting", "healthy"):
                break
            time.sleep(1)
        time.sleep(4)
        files = sorted(p.name for p in app.iterdir())
        log_tail = ""
        try:
            log_tail = (home / "jarvis.log").read_text(encoding="utf-8", errors="replace")[-3000:]
        except OSError:
            pass
        report[name] = {"state": state, "files": files, "old_exit": old.poll(), "log_tail": log_tail}
        if not broken:
            check(f"{name}: update downloaded, checked, installed and the new copy reported healthy", state.get("status") == "done", state)
            check(f"{name}: the old copy exited after handing over", old.poll() is not None, old.poll())
            check(f"{name}: the new copy is running", len(jarvis_pids()) >= 1, jarvis_pids())
            check(f"{name}: previous version kept for 'go back'", "Jarvis.previous.exe" in files and "Jarvis.exe" in files, files)
            check(f"{name}: no half-written files left", not {"Jarvis.update.part", "Jarvis.exe.new"} & set(files), files)
        else:
            check(f"{name}: a new copy that dies at start is rolled back", state.get("status") == "rolled_back"
                  and state.get("bad_version") == "99.0.0", state)
            check(f"{name}: the original exe is back in place", exe.exists() and sha256(exe) == digest, files)
            check(f"{name}: the old version was started again", len(jarvis_pids()) >= 1, jarvis_pids())
        subprocess.run(["taskkill", "/F", "/T", "/IM", "Jarvis.exe"], capture_output=True)
        time.sleep(3)
        if failures:
            print(f"----- {name} jarvis.log (tail) -----\n{log_tail}", flush=True)

    try:
        scenario("good-update", broken=False)
        scenario("broken-update", broken=True)
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        check("no unexpected errors", False, f"{exc.__class__.__name__}: {exc}")
    finally:
        subprocess.run(["taskkill", "/F", "/T", "/IM", "Jarvis.exe"], capture_output=True)
        server.shutdown()
        report["ok"] = not failures
        report_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print("ALL UPDATE CHECKS PASSED" if not failures else f"FAILED: {failures}", flush=True)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
