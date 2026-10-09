"""Real-desktop check of JARVIS's typing (run by the Windows CI job).

A small text editor is put in front; the real Assistant (with a stand-in language model that writes a pizza
recipe) is asked "type out a pizza recipe that I can make" and "type the words: see you at five", then a protocol made by voice
types into it too. The editor
reports exactly what arrived, so nothing is assumed.

    python tests/windows_typing_check.py report.json
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

EDITOR = r'''
import json, sys, tkinter as tk
path = sys.argv[1]
root = tk.Tk()
root.title("Typing Test - Editor")
root.geometry("900x600+150+120")
text = tk.Text(root, font=("Segoe UI", 12), wrap="word")
text.pack(fill="both", expand=True)
def save(*_):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"text": text.get("1.0", "end-1c")}, fh)
text.bind("<KeyRelease>", save)
root.after(300, save)
root.after(400, lambda: (root.lift(), root.focus_force(), text.focus_set()))
root.mainloop()
'''

RECIPE = ("# Homemade Pizza\n\n## Ingredients\n- 500 g bread flour\n- 7 g dried yeast\n- **Mozzarella**, tomato sauce and basil\n\n"
          "## Method\n1. Mix the dough and knead it for 10 minutes.\n2. Let it rise for an hour, add the toppings and bake at 250 C.")


def main() -> int:
    import ctypes

    ctypes.windll.user32.SetProcessDPIAware()
    from core.assistant import Assistant
    from core.config import Config
    from core.screen import WindowsDesktop
    from tests.mock_ollama import MockOllama
    from tests.test_assistant import Events, FakeTTS

    report: dict = {"checks": {}}
    failures: list[str] = []

    def check(name, ok, detail=None):
        report["checks"][name] = {"ok": bool(ok), "detail": detail}
        print(("PASS " if ok else "FAIL ") + name + (f": {detail}" if detail is not None else ""), flush=True)
        if not ok:
            failures.append(name)

    work = Path(tempfile.mkdtemp(prefix="jarvis-typing-"))
    state = work / "state.json"
    (work / "editor.py").write_text(EDITOR, encoding="utf-8")
    editor = subprocess.Popen([sys.executable, str(work / "editor.py"), str(state)])
    mock = MockOllama().start()
    mock.responder = lambda body: RECIPE
    config = Config(work / "config.json")
    config.update({"ollama_host": mock.url, "voice_enabled": False, "wake_word": False, "memory_auto_learn": False})
    events = Events()
    desk = WindowsDesktop()
    assistant = Assistant(config, events, tts=FakeTTS(), desktop=desk)

    def say(text, timeout=60):
        n = len(events.of("assistant_end"))
        assistant.submit_text(text)
        events.wait_for(lambda: len(events.of("assistant_end")) == n + 1 and events.states()[-1] == "IDLE", timeout)
        end = events.of("assistant_end")[-1]
        return "".join(t["text"] for t in events.of("assistant_token") if t["id"] == end["id"])

    def typed():
        try:
            return json.loads(state.read_text(encoding="utf-8"))["text"]
        except Exception:
            return ""

    try:
        assistant.start()
        find = ctypes.windll.user32.FindWindowW
        find.restype = ctypes.c_void_p
        hwnd = None
        for _ in range(100):
            hwnd = find(None, "Typing Test - Editor")
            if hwnd and state.exists():
                break
            time.sleep(0.1)
        check("editor window appeared", bool(hwnd))
        window = desk.window_info(int(hwnd))
        desk.bring_to_front(window)
        time.sleep(0.8)
        current = assistant.tracker.current()
        check("JARVIS sees the editor as the app you're working in", current is not None and current.hwnd == window.hwnd,
              current.title if current else None)

        reply = say("type out a pizza recipe that I can make")
        time.sleep(1.0)
        text = typed()
        check("typed a generated pizza recipe into the editor", "500 g bread flour" in text and "Homemade Pizza" in text,
              {"reply": reply, "chars": len(text), "start": text[:120]})
        check("typed as clean text (no Markdown symbols)", "#" not in text and "**" not in text, text[:200])
        check("all of it arrived", "bake at 250 C" in text, text[-80:])

        before = typed()
        reply = say("type the words: see you at five")
        time.sleep(0.8)
        check("typed exact dictated words", typed().endswith("see you at five") and typed().startswith(before[:40]), {"reply": reply, "end": typed()[-40:]})

        # a protocol made by voice, run by name, whose steps type into the same real app
        reply = say("create a protocol called Sign Off: type the words: best wishes, then wait 1 second, then type the words: from your assistant")
        check("protocol proposed from a sentence", reply.startswith("Here's the Sign Off protocol") and "Shall I save it?" in reply, reply)
        reply = say("yes")
        check("protocol saved after approval", reply.startswith("Protocol Sign Off saved"), reply)
        reply = say("run sign off")
        check("protocol started by name", reply.startswith("Initiating the Sign Off protocol"), reply)
        # typing on screen always asks first: answer GO AHEAD each time, as the user would on the HUD
        asked, deadline = 0, time.time() + 60
        while time.time() < deadline and not any(p.get("status") in ("done", "stopped", "interrupted", "failed") for p in events.of("protocol")):
            if assistant._protocol_confirm is not None:
                asked += 1
                assistant.protocol_answer(True)
                time.sleep(0.3)
            time.sleep(0.1)
        check("protocol asked before each typing step", asked == 2, asked)
        time.sleep(0.8)
        outcome = [p.get("status") for p in events.of("protocol")][-1]
        check("protocol ran every step in order", outcome == "done" and typed().endswith("best wishesfrom your assistant"),
              {"outcome": outcome, "end": typed()[-40:]})
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        check("no unexpected errors", False, f"{exc.__class__.__name__}: {exc}")
    finally:
        assistant.shutdown()
        mock.stop()
        editor.kill()
        report["ok"] = not failures
        Path(sys.argv[1] if len(sys.argv) > 1 else "typing-check.json").write_text(json.dumps(report, indent=2))
    print("ALL TYPING CHECKS PASSED" if not failures else f"FAILED: {failures}", flush=True)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
