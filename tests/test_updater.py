import hashlib
import json
import os
from pathlib import Path

import httpx
import pytest

from core import updater as up
from core.updater import Release, UpdateError, Updater, is_newer, parse_version
from tests.test_assistant import make  # noqa: F401  (a fixture)

BLOB = os.urandom(1_200_000)
SHA = hashlib.sha256(BLOB).hexdigest()
URL = "https://github.com/georrrgess-jpg/jarvis/releases/download/v1.1.42/Jarvis.exe"


def manifest(**over):
    return {"version": "1.1.42", "tag": "v1.1.42", "url": URL, "sha256": SHA, "size": len(BLOB), "notes": "- Faster\n- Kinder", **over}


def client(data=None, blob=BLOB, status=200):
    def handler(request: httpx.Request):
        if request.url.path.endswith("latest.json"):
            return httpx.Response(status, json=data if data is not None else manifest())
        return httpx.Response(200, content=blob)

    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


class Proc:
    def __init__(self, on_poll=None, code=None):
        self.pid, self._on_poll, self.code, self.killed = 4242, on_poll, code, False

    def poll(self):
        if self._on_poll:
            self._on_poll()
        return self.code

    def kill(self):
        self.killed = True
        self.code = -9

    def wait(self, timeout=None):
        return self.code


class Ran:
    def __init__(self, ok=True):
        self.ok, self.calls = ok, []

    def __call__(self, args, **kw):
        self.calls.append(args)
        if "--selftest-report" in args:
            Path(args[args.index("--selftest-report") + 1]).write_text(json.dumps({"ok": self.ok}))
        return type("R", (), {"returncode": 0 if self.ok else 1})()


def updater(tmp_path, http=None, run=None, popen=None, current="1.1.10"):
    app = tmp_path / "app"
    app.mkdir(exist_ok=True)
    exe = app / "Jarvis.exe"
    if not exe.exists():
        exe.write_bytes(b"OLD" * 1000)
    events = []
    u = Updater(current, exe=exe, state_dir=tmp_path, http=http or client(), emit=events.append, supported=True,
                run=run or Ran(), popen=popen or (lambda *a, **k: Proc(code=0)), sleep=lambda s: None, trusted_only=True)
    return u, exe, events


def test_versions():
    assert parse_version("1.1.42") == (1, 1, 42, 0) and is_newer("1.1.42", "1.1.9") and not is_newer("1.1.9", "1.1.42")
    assert is_newer("1.1.0", "1.0.0") and not is_newer("1.1.42", "1.1.42")


def test_manifest_must_point_at_this_projects_releases():
    assert Release.from_manifest(manifest()).version == "1.1.42"
    for bad in ({"url": "https://evil.example/Jarvis.exe"}, {"url": "http://github.com/georrrgess-jpg/jarvis/releases/x"},
                {"url": "https://github.com/someone/else/releases/download/v1/Jarvis.exe"}, {"sha256": "abc"}, {"version": "latest"}):
        with pytest.raises(ValueError):
            Release.from_manifest(manifest(**bad))


def test_check_finds_newer_and_ignores_same_or_older(tmp_path):
    u, _, events = updater(tmp_path)
    rel = u.check()
    assert rel.version == "1.1.42" and u.status.state == "available" and events[-1]["release"]["version"] == "1.1.42"
    u2, _, _ = updater(tmp_path, current="1.1.42")
    assert u2.check() is None and u2.status.state == "up_to_date"
    u3, _, _ = updater(tmp_path, http=client(status=404))
    assert u3.check() is None  # nothing published yet


def test_a_version_that_failed_to_start_is_not_offered_again(tmp_path):
    (tmp_path / "update-state.json").write_text(json.dumps({"status": "rolled_back", "bad_version": "1.1.42"}))
    u, _, _ = updater(tmp_path)
    assert u.check() is None and "didn't start" in u.status.detail


def test_download_is_checked_against_size_and_fingerprint(tmp_path):
    u, exe, _ = updater(tmp_path)
    rel = u.check()
    path = u.download(rel)
    assert path.read_bytes() == BLOB and path.name == "Jarvis.update.exe"
    path.unlink()
    bad, exe, _ = updater(tmp_path, http=client(blob=BLOB[:-1] + b"X"))
    with pytest.raises(UpdateError):
        bad.download(bad.check())
    assert not (exe.parent / "Jarvis.update.exe").exists() and not (exe.parent / "Jarvis.update.part").exists()
    assert "fingerprint" in bad.status.error


def test_prepare_runs_the_new_versions_self_test(tmp_path):
    run = Ran(ok=True)
    u, exe, _ = updater(tmp_path, run=run)
    rel = u.prepare()
    assert rel.version == "1.1.42" and u.status.state == "ready" and u.ready is rel
    assert run.calls[0][0] == str(exe.parent / "Jarvis.update.exe") and "--selftest" in run.calls[0]
    (tmp_path / "b").mkdir()
    failing, exe, _ = updater(tmp_path / "b", run=Ran(ok=False))
    with pytest.raises(UpdateError):
        failing.prepare()
    assert failing.ready is None and not (exe.parent / "Jarvis.update.exe").exists() and "self-check" in failing.status.error


def test_install_hands_over_and_quits_without_touching_the_running_exe(tmp_path):
    started, quits = [], []
    u, exe, _ = updater(tmp_path, popen=lambda args, **k: started.append(args) or Proc())
    u.prepare()
    u.hooks = {"quit": lambda: quits.append(1)}
    assert u.install() is True
    assert exe.read_bytes() == b"OLD" * 1000  # untouched
    assert started[-1][1:] == ["--finish-update", str(tmp_path / "update-state.json")] and started[-1][0].endswith("Jarvis.update.exe")
    state = json.loads((tmp_path / "update-state.json").read_text())
    assert state["status"] == "handover" and state["to_version"] == "1.1.42" and state["from_version"] == "1.1.10" and quits == [1]


def finisher(tmp_path, new_bytes: bytes, healthy: bool):
    """The new exe (running from Jarvis.update.exe) finishing the job."""
    app = tmp_path / "app"
    app.mkdir(exist_ok=True)
    (app / "Jarvis.exe").write_bytes(b"OLD")
    (app / "Jarvis.update.exe").write_bytes(new_bytes)
    state_path = tmp_path / "update-state.json"
    state_path.write_text(json.dumps({"status": "handover", "from_version": "1.1.10", "to_version": "1.1.42", "exe": str(app / "Jarvis.exe"), "pids": []}))
    starts = []

    def popen(args, **kw):
        starts.append(args)
        if not healthy or len(starts) > 1:
            return Proc(code=3)  # dies at once (or: the restored old copy, which we don't watch)

        def comes_up():
            s = json.loads(state_path.read_text())
            if s["status"] == "starting":
                Updater("1.1.42", exe=app / "Jarvis.exe", state_dir=tmp_path, supported=True).mark_healthy(state_path)

        return Proc(on_poll=comes_up)

    u = Updater("1.1.42", exe=app / "Jarvis.update.exe", state_dir=tmp_path, supported=True, popen=popen, sleep=lambda s: None,
                run=lambda *a, **k: None)
    return u, app, state_path, starts


def test_finish_swaps_the_files_and_waits_for_the_new_copy(tmp_path):
    u, app, state_path, starts = finisher(tmp_path, b"NEW", healthy=True)
    assert u.finish(state_path) == 0
    assert (app / "Jarvis.exe").read_bytes() == b"NEW" and (app / "Jarvis.previous.exe").read_bytes() == b"OLD"
    assert starts[0][1:] == ["--after-update", str(state_path)] and json.loads(state_path.read_text())["status"] == "done"


def test_a_new_copy_that_dies_is_rolled_back_and_the_old_one_started(tmp_path, monkeypatch):
    monkeypatch.setattr(up, "HEALTH_TIMEOUT", 5)
    u, app, state_path, starts = finisher(tmp_path, b"NEW", healthy=False)
    assert u.finish(state_path) == 1
    state = json.loads(state_path.read_text())
    assert (app / "Jarvis.exe").read_bytes() == b"OLD" and state["status"] == "rolled_back" and state["bad_version"] == "1.1.42"
    assert len(starts) == 2  # the new one, then the old one again (told what happened)


def test_mark_healthy_only_answers_a_waiting_update(tmp_path):
    path = tmp_path / "update-state.json"
    path.write_text(json.dumps({"status": "done"}))
    Updater("1", supported=True, state_dir=tmp_path).mark_healthy(path)
    assert json.loads(path.read_text())["status"] == "done"


def test_source_runs_dont_update(tmp_path):
    u = Updater("1.1.0", exe=tmp_path / "python", state_dir=tmp_path, supported=False)
    assert u.status.state == "unsupported" and u.check() is None


# ---------------------------------------------------------------------------- through JARVIS
@pytest.fixture
def jarvis(make):
    assistant, events = make()
    return assistant, events


def ask(assistant, events, text):
    n = len(events.of("assistant_end"))
    assistant.submit_text(text)
    events.wait_for(lambda: len(events.of("assistant_end")) == n + 1 and events.states()[-1] == "IDLE", 20)
    end = events.of("assistant_end")[-1]
    return "".join(t["text"] for t in events.of("assistant_token") if t["id"] == end["id"])


def test_update_commands(jarvis, tmp_path):
    from core import APP_VERSION

    assistant, events = jarvis
    assert APP_VERSION in ask(assistant, events, "what version are you")
    assert "runs from source" in ask(assistant, events, "check for updates")
    u, exe, _ = updater(tmp_path, current="1.1.10")
    assistant.updater = u
    assert "Version 1.1.42 is downloaded and checked" in ask(assistant, events, "check for updates")
    installs = []
    assistant._install_update = lambda restore: installs.append(restore)
    assert "Updating to version 1.1.42 now" in ask(assistant, events, "update now")
    assert "Faster" in ask(assistant, events, "what's new")


def test_greeting_after_an_update_and_after_a_rollback(jarvis):
    assistant, _ = jarvis
    assistant.just_updated = {"status": "starting", "to_version": "1.1.42", "notes": "- Faster replies\n- Kinder"}
    assert assistant._updated_line() == " I've been updated to version 1.1.42. New: Faster replies."
    assistant.just_updated = {"status": "rolled_back", "to_version": "1.1.42"}
    assert "didn't start properly" in assistant._updated_line()
