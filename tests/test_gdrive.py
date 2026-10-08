"""Opening, showing and listing the user's existing Google files by voice."""

import json

import pytest

from core.gdrive import parse_google_request
from tests.test_google import URL, FakeAppsScript, bridge_with


@pytest.mark.parametrize("text, action, kind, name, recent", [
    ("open my Messi doc", "open", "doc", "Messi", False),
    ("open the doc", "open", "doc", "", True),
    ("Jarvis, open the budget spreadsheet", "open", "sheet", "budget", False),
    ("open the pitch deck please", "open", "slides", "pitch", False),
    ("show me my shopping list doc", "view", "doc", "shopping list", False),
    ("what's in my budget sheet", "view", "sheet", "budget", False),
    ("open google doc called Lionel Messi", "open", "doc", "Lionel Messi", False),
    ("open my latest presentation", "open", "slides", "", True),
    ("pull up my meeting notes doc", "open", "doc", "meeting notes", False),
])
def test_recognises_google_file_requests(text, action, kind, name, recent):
    req = parse_google_request(text)
    assert (req.action, req.kind, req.name, req.recent) == (action, kind, name, recent)


def test_pronouns_need_something_to_refer_to():
    assert parse_google_request("open it") is None
    assert parse_google_request("open it", have_last=True).recent
    assert parse_google_request("show it here", have_last=True).action == "view"
    assert parse_google_request("read it aloud", have_last=True).aloud


@pytest.mark.parametrize("text", ["open spotify", "open my resume.docx", "open my word document", "open google docs",
                                  "open my downloads folder", "open the pdf", "open docs"])
def test_other_requests_are_left_alone(text):
    assert parse_google_request(text, have_last=True) is None


# ------------------------------------------------------------------ end to end

FILES = {
    "docs": [{"id": "d1" + "x" * 20, "title": "Lionel Messi: The Little Genius", "url": "https://docs.google.com/document/d/d1/edit", "updated": "2026-10-07T10:00:00Z"}],
    "slides": [{"id": "s1" + "x" * 20, "title": "Solar System", "url": "https://docs.google.com/presentation/d/s1/edit", "updated": "2026-10-06T10:00:00Z"}],
    "sheets": [{"id": "h1" + "x" * 20, "title": "Budget", "url": "https://docs.google.com/spreadsheets/d/h1/edit", "updated": "2026-10-05T10:00:00Z"}],
}


def google(p):
    if p["action"] == "list":
        q = (p.get("query") or "").lower()
        files = [f for f in FILES[p["kind"]] if all(w in f["title"].lower() for w in q.split())]
        return {"ok": True, "files": files}
    if p["action"] == "recent_files":
        kinds = {"docs": "doc", "slides": "slides", "sheets": "sheet"}
        return {"ok": True, "files": [{**f, "kind": kinds[k]} for k, fs in FILES.items() for f in fs]}
    if p["action"] == "doc_read":
        return {"ok": True, "title": "Lionel Messi: The Little Genius", "url": FILES["docs"][0]["url"],
                "markdown": "# Lionel Messi\n\n## Early life\n\nBorn in Rosario.", "text": "Lionel Messi\nBorn in Rosario."}
    if p["action"] == "sheet_read":
        return {"ok": True, "title": "Budget", "url": FILES["sheets"][0]["url"], "tab": "Sheet1", "tabs": ["Sheet1"],
                "rows": [["Item", "Cost"], ["Rent", "1200"]]}
    if p["action"] == "slides_read":
        return {"ok": True, "title": "Solar System", "url": FILES["slides"][0]["url"],
                "slides": [{"number": 1, "text": "Solar System\nA tour"}, {"number": 2, "text": "The Sun\nA star\nVery hot"}]}
    return {"ok": False, "error": "unexpected " + p["action"]}


@pytest.fixture
def drive(config, mock_ollama, tmp_path):
    from core.assistant import Assistant
    from core.tools import FileIndex, Toolbox
    from tests.test_assistant import Events, FakeTTS

    config.update({"ollama_host": mock_ollama.url, "voice_enabled": False, "google_script_url": URL,
                   "google_bridge_version": 4, "google_user_email": "tony@example.com"})
    server = FakeAppsScript(google)
    opened = []
    tools = Toolbox(config, index=FileIndex(lambda: [], apps_provider=lambda: []), url_launcher=opened.append)
    tools.google = bridge_with(config, server)
    tools.google.token()
    events = Events()
    assistant = Assistant(config, events, tts=FakeTTS(), tools=tools)
    assistant.start()

    def say(text):
        n = len(events.of("assistant_end"))
        assistant.submit_text(text)
        events.wait_for(lambda: len(events.of("assistant_end")) == n + 1 and events.states()[-1] == "IDLE", timeout=20)
        return "".join(t["text"] for t in events.of("assistant_token")[-60:]).split("}")[-1]

    yield assistant, events, server, opened, say, mock_ollama
    assistant.shutdown()


def chat_calls(mock):
    return [1 for p, _ in mock.requests if p == "/api/chat"]


def test_open_my_doc_by_name_opens_it_as_the_linked_account(drive):
    assistant, events, server, opened, say, mock = drive
    said = say("open my Messi doc")
    assert "Opening Lionel Messi: The Little Genius" in said
    assert opened == ["https://docs.google.com/document/d/d1/edit?authuser=tony%40example.com"]
    assert not chat_calls(mock), "answered instantly, the model is never consulted"
    card = events.of("document")[-1]
    assert card["doc_kind"] == "doc" and card["title"].startswith("Lionel Messi") and card["id"].startswith("d1")
    assert "show it here" in said, "tells the user what to do if the browser asks for a login"


def test_open_the_doc_means_the_one_just_made_or_the_latest(drive):
    assistant, events, server, opened, say, mock = drive
    assistant._last_doc = {"kind": "doc", "id": "d1" + "x" * 20, "title": "Messi bio", "url": "https://docs.google.com/document/d/d1/edit"}
    say("open the doc")
    assert not [p for p in server.posts if p["action"] == "list"], "no lookup needed"
    assert opened[-1].startswith("https://docs.google.com/document/d/d1/edit")
    assistant._last_doc = None
    say("open my latest presentation")
    assert opened[-1].startswith("https://docs.google.com/presentation/d/s1/edit")


def test_show_it_here_displays_the_file_inside_jarvis(drive):
    assistant, events, server, opened, say, mock = drive
    say("show me my Messi doc")
    view = events.of("document_view")[-1]
    assert view["doc_kind"] == "doc" and "## Early life" in view["markdown"] and not opened, "no browser involved"
    say("show me my budget sheet")
    assert events.of("document_view")[-1]["rows"] == [["Item", "Cost"], ["Rent", "1200"]]
    say("show me my solar system presentation")
    assert "## Slide 2\nThe Sun\n- A star\n- Very hot" in events.of("document_view")[-1]["markdown"]
    said = say("show it here")
    assert "Here is Solar System" in said


def test_read_it_aloud_speaks_the_text(drive):
    assistant, events, server, opened, say, mock = drive
    said = say("read my Messi doc to me")
    assert "Born in Rosario." in said and "#" not in said


def test_listing_recent_files(drive):
    assistant, events, server, opened, say, mock = drive
    said = say("what docs do I have")
    files = events.of("file_list")[-1]["files"]
    assert {f["kind"] for f in files} == {"doc", "slides", "sheet"} and "3 most recent" in said


def test_missing_file_is_reported_not_guessed(drive):
    assistant, events, server, opened, say, mock = drive
    said = say("open my quantum physics doc")
    assert "couldn't find a Google document called quantum physics" in said and not opened and not chat_calls(mock)


def test_a_local_file_with_that_name_still_wins(drive, tmp_path):
    from core.tools import FileIndex, Toolbox

    assistant, events, server, opened, say, mock = drive
    notes = tmp_path / "budget notes.txt"
    notes.write_text("rent")
    launched = []
    tools = Toolbox(assistant.config, index=FileIndex(lambda: [(tmp_path, "user")], apps_provider=lambda: []),
                    launcher=launched.append, url_launcher=opened.append)
    tools.google = assistant.tools.google
    assistant.tools = tools
    said = say("open my budget notes doc")
    assert launched == [notes] and not events.of("document"), said


def test_without_the_google_link_it_opens_the_website_and_explains(drive, config):
    assistant, events, server, opened, say, mock = drive
    config.update({"google_script_url": ""})
    said = say("open the doc")
    assert opened and opened[0].startswith("https://docs.google.com/document/") and "Link Google in Settings" in said


def test_the_browser_choice_is_used(config, monkeypatch):
    import core.tools as tools_module
    from core.tools import FileIndex, Toolbox

    used = []
    monkeypatch.setattr(tools_module, "open_in_browser", lambda url, browser="default": used.append((url, browser)) or "x")
    config.update({"link_browser": "edge", "google_user_email": "tony@example.com"})
    tb = Toolbox(config, index=FileIndex(lambda: [], apps_provider=lambda: []))
    tb.open_link("https://docs.google.com/document/d/abc/edit")
    tb.open_website("gmail")
    assert used == [("https://docs.google.com/document/d/abc/edit?authuser=tony%40example.com", "edge"),
                    ("https://mail.google.com/mail/?authuser=tony%40example.com", "edge")]
