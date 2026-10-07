"""Google Docs & Slides: the Apps Script itself (run under Node with fake Google services) and the JARVIS client."""

import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from core.google_bridge import BridgeError, GoogleBridge, parse_outline, parse_rows, script_template
from core.tools import FileIndex, Toolbox

ROOT = Path(__file__).resolve().parent.parent
GAS = ROOT / "integrations" / "jarvis_google_bridge.gs"
URL = "https://script.google.com/macros/s/AKfycbx" + "A" * 40 + "/exec"


def run_gas(requests, token="T0K3N"):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    out = subprocess.run([node, str(ROOT / "tests" / "gas_harness.js"), str(GAS), token],
                         input=json.dumps(requests), capture_output=True, text=True, encoding="utf-8", timeout=30)
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
    assert {"google_doc", "google_slides", "google_sheets"} <= names()
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


# ------------------------------------------------------------------ bridge v2: formatting, slide editing, Sheets


def test_script_formats_rich_documents():
    t = "T0K3N"
    data_requests = [
        {"token": t, "action": "doc_create", "title": "Report",
         "text": "# Q3 **Summary**\n### Detail\n1. first step\n2. *second* step\nRevenue was **up** and costs *down*.\n"
                 "| Item | Cost |\n|---|---|\n| **Suit** | 100 |\n| Repulsor |"},
        {"token": t, "action": "doc_rewrite", "document": "Report", "text": "Fresh start\n- only this"},
        {"token": t, "action": "doc_read", "document": "Report"},
    ]
    data = run_gas(data_requests)
    assert all(r["ok"] for r in data["results"]), data["results"]
    assert data["results"][2]["text"] == "Fresh start\nonly this", "rewrite replaces the whole body"

    first = run_gas(data_requests[:1])
    doc = first["files"][0]
    p = doc["paragraphs"]
    assert p[0] == {"text": "Q3 Summary", "heading": "HEADING1", "list": False, "marks": [{"kind": "bold", "text": "Summary"}]}
    assert p[1]["heading"] == "HEADING3"
    assert p[2]["glyph"] == "NUMBER" and p[3]["marks"] == [{"kind": "italic", "text": "second"}]
    assert p[4]["marks"] == [{"kind": "bold", "text": "up"}, {"kind": "italic", "text": "down"}]
    assert doc["tables"] == [[["Item", "Cost"], ["Suit", "100"], ["Repulsor", ""]]], "separator skipped, rows padded"


def test_script_edits_reorders_and_deletes_slides():
    t = "T0K3N"
    data = run_gas([
        {"token": t, "action": "slides_create", "title": "Pitch",
         "slides": [{"title": "Problem", "body": ["slow"]}, {"title": "Solution", "body": ["JARVIS"]}, {"title": "Team", "body": ["Tony"]}]},
        {"token": t, "action": "slides_move", "presentation": "Pitch", "number": 4, "to": 2},
        {"token": t, "action": "slides_delete", "presentation": "Pitch", "number": 3},
        {"token": t, "action": "slides_edit", "presentation": "Pitch", "number": 3, "title": "The fix", "body": ["JARVIS", "fast"]},
        {"token": t, "action": "slides_replace", "presentation": "Pitch", "find": "Tony", "replace": "Pepper"},
        {"token": t, "action": "slides_read", "presentation": "Pitch"},
        {"token": t, "action": "slides_delete", "presentation": "Pitch", "number": 9},
    ])
    r = data["results"]
    assert r[2]["slides"] == 3 and r[4]["replaced"] == 1
    assert [s["text"] for s in r[5]["slides"]] == ["Pitch", "Team\nPepper", "The fix\nJARVIS\nfast"]
    assert "between 1 and 3" in r[6]["error"]


def test_script_creates_appends_writes_and_reads_sheets():
    t = "T0K3N"
    data = run_gas([
        {"token": t, "action": "sheet_create", "title": "Budget", "rows": [["Item", "Cost"], ["Rent", 1200]]},
        {"token": t, "action": "sheet_append", "spreadsheet": "budget", "rows": [["Food", 300], ["Fuel"]]},
        {"token": t, "action": "sheet_write", "spreadsheet": "Budget", "range": "C1", "rows": [["Paid"], ["yes"]]},
        {"token": t, "action": "sheet_read", "spreadsheet": "Budget"},
        {"token": t, "action": "sheet_read", "spreadsheet": "Budget", "range": "A2:B3"},
        {"token": t, "action": "sheet_read", "spreadsheet": "Budget", "tab": "Nope"},
        {"token": t, "action": "list", "kind": "sheets"},
        {"token": t, "action": "sheet_read", "spreadsheet": "ghost"},
    ])
    r = data["results"]
    assert r[0]["url"].startswith("https://docs.google.com/spreadsheets/d/")
    assert r[1]["rows_added"] == 2
    assert r[3]["rows"] == [["Item", "Cost", "Paid"], ["Rent", "1200", "yes"], ["Food", "300", ""], ["Fuel", "", ""]]
    assert r[3]["tabs"] == ["Sheet1"] and r[4]["rows"] == [["Rent", "1200"], ["Food", "300"]]
    assert 'no tab named "Nope"' in r[5]["error"]
    assert [f["title"] for f in r[6]["files"]] == ["Budget"]
    assert 'no spreadsheet named "ghost"' in r[7]["error"]
    assert data["files"][0]["tabs"][0]["bold"] == [{"row": 1, "w": "bold"}], "header row is bold"


def test_parse_rows_variants():
    assert parse_rows("| Item | Cost |\n|---|:--:|\n| **Rent** | 1,200 |\n| Food | 30.5 |") == [
        ["Item", "Cost"], ["Rent", 1200], ["Food", 30.5]]
    assert parse_rows('Name, Age\n"Stark, Tony", 48') == [["Name", "Age"], ["Stark, Tony", 48]]
    assert parse_rows("a\tb\n1\t-2") == [["a", "b"], [1, -2]]
    assert parse_rows("007 | 02134 | 0.5") == [["007", "02134", 0.5]], "leading zeros stay text"
    assert parse_rows(r"Item | Cost\nRent | 1200") == [["Item", "Cost"], ["Rent", 1200]], "escaped newlines"
    assert parse_rows([["a", "1"], ["b"]]) == [["a", 1], ["b"]]
    assert parse_rows("") == []


def test_sheets_and_slide_edits_map_to_script_calls(config):
    config.update({"google_script_url": URL})
    server = FakeAppsScript()
    bridge = bridge_with(config, server)
    bridge.sheets("create", title="Budget", text="Item | Cost\nRent | 1200")
    bridge.sheets("append", spreadsheet="Budget", text="Food | 300")
    bridge.sheets("write", spreadsheet="Budget", range="c 2", text="yes")
    bridge.sheets("read", spreadsheet="Budget", tab="Q3")
    bridge.slides("delete", presentation="Pitch", number="3")
    bridge.slides("move", presentation="Pitch", number=4, to="2")
    bridge.slides("edit", presentation="Pitch", number="2", title="New", text="- one\n- two")
    bridge.slides("replace", presentation="Pitch", find="2025", text="2026")
    bridge.doc("rewrite", document="Notes", text="# Fresh")
    actions = [(p["action"], {k: v for k, v in p.items() if k not in ("token", "action")}) for p in server.posts]
    assert actions == [
        ("sheet_create", {"title": "Budget", "rows": [["Item", "Cost"], ["Rent", 1200]]}),
        ("sheet_append", {"spreadsheet": "Budget", "rows": [["Food", 300]]}),
        ("sheet_write", {"spreadsheet": "Budget", "range": "C2", "rows": [["yes"]]}),
        ("sheet_read", {"spreadsheet": "Budget", "tab": "Q3"}),
        ("slides_delete", {"presentation": "Pitch", "number": 3}),
        ("slides_move", {"presentation": "Pitch", "number": 4, "to": 2}),
        ("slides_edit", {"presentation": "Pitch", "number": 2, "title": "New", "body": ["one", "two"]}),
        ("slides_replace", {"presentation": "Pitch", "find": "2025", "replace": "2026"}),
        ("doc_rewrite", {"document": "Notes", "text": "# Fresh"}),
    ]
    for bad in (lambda: bridge.slides("delete", presentation="Pitch", number="first"),
                lambda: bridge.slides("move", presentation="Pitch", number=2),
                lambda: bridge.sheets("write", spreadsheet="Budget", range="the top", text="x"),
                lambda: bridge.sheets("append", spreadsheet="Budget", text="")):
        with pytest.raises(BridgeError):
            bad()


def test_old_script_versions_get_update_instructions(config):
    config.update({"google_script_url": URL})
    bridge = bridge_with(config, FakeAppsScript(lambda p: {"ok": False, "error": "unknown action: sheet_create"}))
    with pytest.raises(BridgeError, match="New version"):
        bridge.sheets("create", title="Budget")


def test_sheets_tool_accepts_rows_as_lists(config):
    config.update({"google_script_url": URL})
    tb = Toolbox(config, index=FileIndex(lambda: [], apps_provider=lambda: []))
    server = FakeAppsScript(lambda p: {"ok": True, "url": "https://docs.google.com/spreadsheets/d/x/edit"})
    tb.google = bridge_with(config, server)
    out = json.loads(tb.run("google_sheets", {"action": "create", "title": "Gear", "text": [["Suit", "Mark 85"], ["Cost", "1e6"]]}))
    assert out["ok"] and server.posts[0]["rows"] == [["Suit", "Mark 85"], ["Cost", "1e6"]]
    out = json.loads(tb.run("google_slides", {"action": "delete", "presentation": "Pitch", "number": 2}))
    assert out["ok"] and server.posts[1]["number"] == 2


def test_outdated_scripts_are_detected(config):
    from core.google_bridge import script_version

    assert script_version() >= 2
    old = bridge_with(config, FakeAppsScript(lambda p: {"ok": True, "user": "t@x", "version": 1}))
    old.connect(URL)
    assert old.outdated, "a v1 script can't do Sheets"
    new = bridge_with(config, FakeAppsScript(lambda p: {"ok": True, "user": "t@x", "version": script_version()}))
    new.connect(URL)
    assert not new.outdated
    stale = bridge_with(config, FakeAppsScript(lambda p: {"ok": False, "error": "unknown action: sheet_read"}))
    with pytest.raises(BridgeError):
        stale.sheets("read", spreadsheet="Budget")
    assert stale.outdated, "an unknown-action reply marks the script as needing an update"


def test_script_sends_mail_finds_contacts_and_shares_files():
    t = "T0K3N"
    data = run_gas([
        {"token": t, "action": "contact_find", "name": "sarah"},
        {"token": t, "action": "contact_find", "name": "pepper potts"},
        {"token": t, "action": "contact_find", "name": "nobody"},
        {"token": t, "action": "mail_send", "to": "sarah.connor@example.com", "subject": "Hi", "body": "Hello Sarah"},
        {"token": t, "action": "mail_send", "to": "not an address", "subject": "x", "body": "y"},
        {"token": t, "action": "doc_create", "title": "Bio"},
    ])
    r = data["results"]
    assert [p["email"] for p in r[0]["people"]] == ["sarah.connor@example.com", "slee@school.edu"], "most frequent first"
    assert r[0]["people"][0]["name"] == "Sarah Connor"
    assert r[1]["people"] == [{"email": "pepper@stark.com", "name": "Potts, Pepper", "count": 2}]
    assert r[2]["people"] == []
    assert r[3]["sent"] is True and data["sent"] == [{"to": "sarah.connor@example.com", "subject": "Hi", "body": "Hello Sarah", "options": {}}]
    assert "invalid recipient" in r[4]["error"]
    shared = run_gas([{"token": t, "action": "doc_create", "title": "Bio"},
                      {"token": t, "action": "share_file", "file": "id" + "x" * 29 + "1", "email": "tom@example.com"}])
    assert shared["results"][1]["shared"] and shared["shares"] == [{"id": "id" + "x" * 29 + "1", "e": "tom@example.com", "role": "view"}]
