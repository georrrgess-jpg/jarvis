"""Google Docs & Slides: the Apps Script itself (run under Node with fake Google services) and the JARVIS client."""

import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from core.google_bridge import BridgeError, GoogleBridge, parse_outline, script_template
from core.tools import FileIndex, Toolbox

ROOT = Path(__file__).resolve().parent.parent
GAS = ROOT / "integrations" / "jarvis_google_bridge.gs"
URL = "https://script.google.com/macros/s/AKfycbx" + "A" * 40 + "/exec"


def run_gas(requests, token="T0K3N"):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    out = subprocess.run([node, str(ROOT / "tests" / "gas_harness.js"), str(GAS), token],
                         input=json.dumps(requests), capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


# ------------------------------------------------------------------ the Apps Script (real code, fake Google)


def test_script_creates_and_edits_docs():
    t = "T0K3N"
    data = run_gas([
        {"token": t, "action": "doc_create", "title": "Mission Log", "text": "# Mark II\nFlight test at dawn.\n- check thrusters"},
        {"token": t, "action": "doc_append", "document": "mission", "text": "## Results\nAll good."},
        {"token": t, "action": "doc_replace", "document": "Mission Log", "find": "dawn", "replace": "dusk"},
        {"token": t, "action": "doc_read", "document": "mission log"},
    ])
    created, appended, replaced, read = data["results"]
    assert created["ok"] and created["url"].startswith("https://docs.google.com/document/d/")
    assert appended["id"] == created["id"], "lookup by partial name finds the same doc"
    assert replaced["replaced"] == 1
    assert read["text"] == "Mark II\nFlight test at dusk.\ncheck thrusters\nResults\nAll good."
    paragraphs = data["files"][0]["paragraphs"]
    assert paragraphs[0] == {"text": "Mark II", "heading": "HEADING1", "list": False}, "no blank first line"
    assert paragraphs[2]["list"] is True and paragraphs[3]["heading"] == "HEADING2"


def test_script_builds_presentations():
    t = "T0K3N"
    data = run_gas([
        {"token": t, "action": "slides_create", "title": "Stark Expo", "subtitle": "2026",
         "slides": [{"title": "Agenda", "body": ["Arrival", "Keynote"]}]},
        {"token": t, "action": "slides_add", "presentation": "expo", "title": "Q&A", "body": ["Questions"]},
        {"token": t, "action": "slides_read", "presentation": "Stark Expo"},
        {"token": t, "action": "list", "kind": "slides", "query": "stark"},
    ])
    read = data["results"][2]
    assert [s["text"] for s in read["slides"]] == ["Stark Expo\n2026", "Agenda\nArrival\nKeynote", "Q&A\nQuestions"]
    assert data["results"][3]["files"][0]["title"] == "Stark Expo"


def test_script_rejects_bad_tokens_and_unknown_files():
    data = run_gas([{"token": "wrong", "action": "ping"}, {"action": "ping"},
                    {"token": "T0K3N", "action": "doc_read", "document": "ghost"},
                    {"token": "T0K3N", "action": "doc_read", "document": "https://docs.google.com/document/d/" + "z" * 30 + "/edit"},
                    {"token": "T0K3N", "action": "explode"}])
    r = data["results"]
    assert r[0] == {"ok": False, "error": "unauthorized"} and r[1]["error"] == "unauthorized"
    assert 'no document named "ghost"' in r[2]["error"]
    assert "No item with the given ID" in r[3]["error"], "links are resolved to IDs"
    assert "unknown action" in r[4]["error"]


def test_unconfigured_template_refuses_everything():
    data = run_gas([{"token": "__JARVIS_TOKEN__", "action": "ping"}], token="__JARVIS_TOKEN__")
    assert data["results"][0]["error"] == "unauthorized"


# ------------------------------------------------------------------ JARVIS client


class FakeAppsScript:
    """Mimics Apps Script's web-app protocol: POST -> 302 to googleusercontent -> GET returns JSON."""

    def __init__(self, reply=None, status_reply=None):
        self.posts = []
        self.reply = reply or (lambda payload: {"ok": True, "echo": payload["action"]})

    def __call__(self, request):
        if request.method == "POST":
            payload = json.loads(request.content)
            self.posts.append(payload)
            self._pending = payload
            return httpx.Response(302, headers={"Location": "https://script.googleusercontent.com/macros/echo?user_content_key=k"})
        return httpx.Response(200, json=self.reply(self._pending))


def bridge_with(config, server):
    return GoogleBridge(config, http=httpx.Client(transport=httpx.MockTransport(server), follow_redirects=True))


def test_script_source_embeds_a_persistent_token(config):
    bridge = GoogleBridge(config)
    source = bridge.script_source()
    token = config["google_bridge_token"]
    assert len(token) >= 24 and f"var JARVIS_TOKEN = '{token}';" in source
    assert bridge.script_source() == source, "the token is generated once"
    assert "__JARVIS_TOKEN__" in script_template()


def test_connect_follows_the_apps_script_redirect(config):
    server = FakeAppsScript(lambda p: {"ok": True, "user": "tony@example.com"})
    bridge = bridge_with(config, server)
    assert bridge.connect(URL)["user"] == "tony@example.com"
    assert config["google_script_url"] == URL and bridge.configured
    assert server.posts[0]["action"] == "ping" and server.posts[0]["token"] == config["google_bridge_token"]


def test_connect_rejects_non_web_app_urls(config):
    bridge = bridge_with(config, FakeAppsScript())
    for bad in ("https://evil.example/exec", "https://script.google.com/macros/s/short/exec", URL.replace("/exec", "/dev")):
        with pytest.raises(BridgeError, match="/exec"):
            bridge.connect(bad)
    assert not bridge.configured


def test_error_messages_are_actionable(config):
    config.update({"google_script_url": URL})
    unauthorized = bridge_with(config, FakeAppsScript(lambda p: {"ok": False, "error": "unauthorized"}))
    with pytest.raises(BridgeError, match="re-copy the script"):
        unauthorized.call("ping")

    def sign_in(request):
        return httpx.Response(200, text="<html>Sign in - Google Accounts</html>", headers={"content-type": "text/html"})

    with pytest.raises(BridgeError, match="Anyone"):
        bridge_with(config, sign_in).call("ping")

    def offline(request):
        raise httpx.ConnectError("no network")

    with pytest.raises(BridgeError, match="couldn't reach"):
        bridge_with(config, offline).call("ping")


def test_doc_and_slides_actions_map_to_script_calls(config):
    config.update({"google_script_url": URL})
    server = FakeAppsScript()
    bridge = bridge_with(config, server)
    bridge.doc("create", title="Shopping list", text="- eggs\n- milk")
    bridge.doc("replace", document="Shopping list", find="milk", text="oat milk")
    bridge.slides("create", title="Pitch", text="Problem\n- slow\n\nSolution\n- JARVIS")
    bridge.slides("add", presentation="Pitch", text="Next steps\n- ship it")
    actions = [(p["action"], {k: v for k, v in p.items() if k not in ("token", "action")}) for p in server.posts]
    assert actions[0] == ("doc_create", {"title": "Shopping list", "text": "- eggs\n- milk"})
    assert actions[1] == ("doc_replace", {"document": "Shopping list", "find": "milk", "replace": "oat milk"})
    assert actions[2] == ("slides_create", {"title": "Pitch", "slides": [{"title": "Problem", "body": ["slow"]},
                                                                         {"title": "Solution", "body": ["JARVIS"]}]})
    assert actions[3] == ("slides_add", {"presentation": "Pitch", "title": "Next steps", "body": ["ship it"]})
    with pytest.raises(BridgeError):
        bridge.doc("delete", document="x")


def test_parse_outline_variants():
    assert parse_outline("Slide 1: Intro\n- hello\nSlide 2: End\n1. bye") == [
        {"title": "Intro", "body": ["hello"]}, {"title": "End", "body": ["bye"]}]
    assert parse_outline("Only a title") == [{"title": "Only a title", "body": []}]
    assert parse_outline("") == []


def test_tools_offered_only_when_connected(config):
    tb = Toolbox(config, index=FileIndex(lambda: [], apps_provider=lambda: []))
    names = lambda: {s["function"]["name"] for s in tb.specs()}  # noqa: E731
    assert "google_doc" not in names()
    config.update({"google_script_url": URL})
    tb.google.token()
    assert {"google_doc", "google_slides"} <= names()
    google_spec = next(s for s in tb.specs() if s["function"]["name"] == "google_doc")
    assert google_spec["function"]["parameters"]["required"] == ["action"]
    config.update({"allow_internet": False})
    assert "google_doc" not in names()


def test_tool_run_reports_bridge_errors_as_json(config):
    config.update({"google_script_url": URL})
    tb = Toolbox(config, index=FileIndex(lambda: [], apps_provider=lambda: []))
    tb.google = bridge_with(config, FakeAppsScript(lambda p: {"ok": False, "error": 'no document named "ghost"'}))
    out = json.loads(tb.run("google_doc", {"action": "read", "document": "ghost"}))
    assert out == {"ok": False, "error": 'no document named "ghost"'}
