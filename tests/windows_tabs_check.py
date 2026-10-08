"""Real-Chrome check of tab control (run by the Windows CI job).

Opens Google Chrome with three tabs, then uses JARVIS's own code to close a tab by name, bring it back,
close the active tab, and close Chrome, checking the real window titles after each step.

    python tests/windows_tabs_check.py report.json
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def page(title: str) -> str:
    return f"data:text/html,<title>{title}</title><h1>{title}</h1>"


def main() -> int:
    report_path = Path(sys.argv[1] if len(sys.argv) > 1 else "tabs-check.json")
    import ctypes

    ctypes.windll.user32.SetProcessDPIAware()
    from core import tabs
    from core.screen import WindowsDesktop

    report: dict = {"checks": {}}
    failures: list[str] = []

    def check(name, ok, detail=None):
        report["checks"][name] = {"ok": bool(ok), "detail": detail}
        print(("PASS " if ok else "FAIL ") + name + (f": {detail}" if detail is not None else ""), flush=True)
        if not ok:
            failures.append(name)

    chrome = next((p for p in (Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
                               Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
                               Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe") if p.is_file()), None)
    if chrome is None:
        print("NOTE Chrome isn't installed on this machine; skipping", flush=True)
        report_path.write_text(json.dumps({"skipped": "no chrome"}))
        return 0
    profile = tempfile.mkdtemp(prefix="jarvis-chrome-")
    proc = subprocess.Popen([str(chrome), f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
                             "--disable-features=Translate", "--new-window", page("Alpha Page"), page("Beta Page"), page("Gamma Page")])
    desk = WindowsDesktop()

    def chrome_titles() -> list[str]:
        """Every tab's title, by walking round the tabs once."""
        wins = tabs.browser_windows(desk, "chrome")
        if not wins:
            return []
        w = wins[0]
        desk.bring_to_front(w)
        start = desk.window_info(w.hwnd).title
        seen = [tabs.tab_title(start)]
        for _ in range(10):
            desk.press([0x11, 0x09], window=w)
            time.sleep(0.25)
            t = desk.window_info(w.hwnd).title
            if t == start:
                break
            seen.append(tabs.tab_title(t))
        return seen

    try:
        for _ in range(60):
            wins = tabs.browser_windows(desk, "chrome")
            if wins and "Page" in wins[0].title:
                break
            time.sleep(0.5)
        time.sleep(1.5)
        titles = sorted(chrome_titles())
        check("chrome opened with three tabs", titles == ["Alpha Page", "Beta Page", "Gamma Page"], titles)

        closed = tabs.close_named_tab(desk, "beta", "chrome")
        time.sleep(0.6)
        titles = sorted(chrome_titles())
        check("closed the tab by name", closed == "Beta Page" and titles == ["Alpha Page", "Gamma Page"], {"closed": closed, "left": titles})

        tabs.reopen_tab(desk, "chrome")
        time.sleep(1.0)
        titles = sorted(chrome_titles())
        check("reopened the closed tab", titles == ["Alpha Page", "Beta Page", "Gamma Page"], titles)

        before = tabs.tab_title(tabs.browser_windows(desk, "chrome")[0].title)
        tabs.close_current_tab(desk, "chrome")
        time.sleep(0.6)
        titles = chrome_titles()
        check("closed the active tab", before not in titles and len(titles) == 2, {"closed": before, "left": titles})

        try:
            tabs.close_named_tab(desk, "netflix", "chrome")
            check("missing tab is reported", False)
        except Exception as exc:
            check("missing tab is reported", "couldn't find" in str(exc), str(exc))
        check("nothing closed by the failed search", len(chrome_titles()) == 2)

        tabs.close_browser(desk, "chrome")
        for _ in range(20):
            if not tabs.browser_windows(desk, "chrome"):
                break
            time.sleep(0.3)
        check("closed chrome", not tabs.browser_windows(desk, "chrome"))
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        check("no unexpected errors", False, f"{exc.__class__.__name__}: {exc}")
    finally:
        proc.kill()
        subprocess.run(["taskkill", "/F", "/IM", "chrome.exe", "/T"], capture_output=True)
        report["ok"] = not failures
        report_path.write_text(json.dumps(report, indent=2))
    print("ALL TAB CHECKS PASSED" if not failures else f"FAILED: {failures}", flush=True)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
