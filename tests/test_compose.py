"""Long-form writing: request parsing, output clean-up and the research -> write -> Google pipeline."""

import json

import pytest

from core.compose import clean_document, parse_deck, parse_write_request
from tests.test_google import URL, FakeAppsScript, bridge_with


@pytest.mark.parametrize("text, kind, topic, target", [
    ("write a bio on Lionel Messi", "biography", "Lionel Messi", "doc"),
    ("Jarvis, write a biography of Lionel Messi in a google doc", "biography", "Lionel Messi", "doc"),
    ("can you write me a short essay about climate change", "essay", "climate change", "doc"),
    ("make a presentation about the solar system", "presentation", "the solar system", "slides"),
    ("make slides about photosynthesis", "presentation", "photosynthesis", "slides"),
    ("write a report on Tesla as a presentation", "report", "Tesla", "slides"),
    ("write a poem about the sea", "poem", "the sea", "chat"),
    ("write a poem about the sea in a google doc", "poem", "the sea", "doc"),
    ("write about Messi in a google doc", "document", "Messi", "doc"),
    ("make a google doc about the history of Rome", "document", "the history of Rome", "doc"),
    ("put together a detailed study guide for the French Revolution", "study guide", "the French Revolution", "doc"),
])
def test_recognises_writing_requests(text, kind, topic, target):
    req = parse_write_request(text, google_ready=True)
    assert (req.kind, req.topic, req.target) == (kind, topic, target)


@pytest.mark.parametrize("text", ["write about Messi", "what is the weather", "open spotify", "write me an email to John",
                                  "create a google doc called Shopping list with eggs and milk", "who is Lionel Messi"])
def test_leaves_other_requests_alone(text):
    assert parse_write_request(text, google_ready=True) is None


def test_lengths_titles_and_languages():
    assert parse_write_request("write a 500 word report on electric cars", True).words == 500
    assert parse_write_request("do a two page report on Tesla", True).words == 900
    assert parse_write_request("create a 10 slide presentation on Rome", True).slides == 10
    assert parse_write_request("write a short bio of Ada Lovelace", True).words < 600
    req = parse_write_request("create a presentation called Stark Expo about the arc reactor", True)
    assert (req.title, req.topic) == ("Stark Expo", "the arc reactor")
    req = parse_write_request("write an essay on Messi in Spanish", True)
    assert (req.topic, req.language) == ("Messi", "Spanish")
    assert parse_write_request("write a bio on Lionel Messi", google_ready=False).target == "chat"


def test_clean_document_strips_chatter():
    title, body = clean_document("Sure! Here is the biography you asked for:\n\n# Lionel Messi\n\nBorn in Rosario...\n\n"
                                 "I hope this helps! Let me know if you need changes.")
    assert title == "Lionel Messi" and body == "# Lionel Messi\n\nBorn in Rosario..."
    title, body = clean_document("**Lionel Messi: A Life in Football**\nBorn in 1987.")
    assert title == "Lionel Messi: A Life in Football" and body.startswith("# Lionel Messi: A Life in Football\n")


def test_parse_deck_handles_model_variations():
    title, subtitle, slides = parse_deck(
        "TITLE: The Solar System\nSUBTITLE: Our cosmic neighbourhood\n\n**Slide 1: The Sun**\n* A star\n* 99.8% of the mass\n\n"
        "## Planets\n1. Mercury\n2. Venus\n\n---\n\nConclusion\n- Vast and beautiful")
    assert (title, subtitle) == ("The Solar System", "Our cosmic neighbourhood")
    assert slides == [{"title": "The Sun", "body": ["A star", "99.8% of the mass"]},
                      {"title": "Planets", "body": ["Mercury", "Venus"]},
                      {"title": "Conclusion", "body": ["Vast and beautiful"]}]
    title, _, slides = parse_deck("Rome\n\nFounding\n- 753 BC")
    assert title == "Rome" and slides == [{"title": "Founding", "body": ["753 BC"]}]


# ------------------------------------------------------------------ end to end with a mock model and Google

BIO = ("Certainly! Here is the biography:\n\n# Lionel Messi: The Little Genius\n\n## Early life\n"
       "Lionel Andrés Messi was born on **24 June 1987** in Rosario, Argentina.\n\n## Career\n"
       "- Barcelona (2004–2021)\n- Paris Saint-Germain (2021–2023)\n- Inter Miami (2023–)\n\nI hope you enjoy it!")


@pytest.fixture
def writer(config, mock_ollama, monkeypatch):
    from core.assistant import Assistant
    from core.tools import FileIndex, Toolbox
    from tests.test_assistant import Events, FakeTTS

    config.update({"ollama_host": mock_ollama.url, "voice_enabled": True, "google_script_url": URL})
    server = FakeAppsScript(lambda p: {"ok": True, "id": "doc1", "title": p.get("title"),
                                       "url": "https://docs.google.com/document/d/doc1/edit"})
    opened = []
    tools = Toolbox(config, index=FileIndex(lambda: [], apps_provider=lambda: []), url_launcher=opened.append)
    tools.google = bridge_with(config, server)
    tools.google.token()
    researched = []
    monkeypatch.setattr(tools, "research", lambda topic, language="en": researched.append((topic, language)) or
                        "Wikipedia, Lionel Messi:\nLionel Andrés Messi (born 24 June 1987) is an Argentine footballer.")
    events, tts = Events(), FakeTTS()
    assistant = Assistant(config, events, tts=tts, tools=tools)
    assistant.start()
    yield assistant, events, tts, mock_ollama, server, opened, researched
    assistant.shutdown()


def test_bio_on_messi_becomes_a_google_doc(writer):
    assistant, events, tts, mock, server, opened, researched = writer
    mock.reply = BIO
    assistant.submit_text("write a bio on Lionel Messi")
    events.wait_for(events.finished, timeout=20)
    assert researched == [("Lionel Messi", "en")]
    prompt = [b for p, b in mock.requests if p == "/api/chat"][-1]
    assert "born 24 June 1987" in prompt["messages"][1]["content"], "research notes reach the writer"
    assert "tools" not in prompt or not prompt["tools"]
    create = server.posts[-1]
    assert create["action"] == "doc_create" and create["title"] == "Lionel Messi: The Little Genius"
    assert create["text"].startswith("# Lionel Messi: The Little Genius\n") and "I hope" not in create["text"]
    assert "## Career" in create["text"] and "**24 June 1987**" in create["text"]
    assert opened == ["https://docs.google.com/document/d/doc1/edit"], "the new doc is opened"
    doc = events.of("document")[0]
    assert doc["doc_kind"] == "doc" and doc["url"].endswith("/edit")
    said = "".join(t["text"] for t in events.of("assistant_token"))
    assert said.startswith("Right away") and "biography of Lionel Messi" in said and "opened it" in said
    assert any("words" in a["label"] for a in events.of("activity")), "progress is shown while writing"
    assert events.states()[-1] == "IDLE"
    # the conversation remembers the document, so "add a paragraph to it" works next
    assert "Lionel Messi: The Little Genius" in assistant.llm._history[-1]["content"]


def test_presentation_becomes_google_slides(writer):
    assistant, events, tts, mock, server, opened, researched = writer
    mock.reply = ("TITLE: The Solar System\nSUBTITLE: A tour\n\nThe Sun\n- A star\n- Very hot\n\n"
                  "Planets\n- Eight of them\n\nConclusion\n- Space is big")
    assistant.submit_text("make a presentation about the solar system")
    events.wait_for(events.finished, timeout=20)
    create = server.posts[-1]
    assert create["action"] == "slides_create" and create["title"] == "The Solar System" and create["subtitle"] == "A tour"
    assert [s["title"] for s in create["slides"]] == ["The Sun", "Planets", "Conclusion"]
    assert "4-slide presentation" in "".join(t["text"] for t in events.of("assistant_token"))


def test_google_failure_still_delivers_the_writing(writer):
    assistant, events, tts, mock, server, opened, researched = writer
    server.reply = lambda p: {"ok": False, "error": "unknown action: doc_create"}
    mock.reply = BIO
    assistant.submit_text("write a bio on Lionel Messi")
    events.wait_for(events.finished, timeout=20)
    text = "".join(t["text"] for t in events.of("assistant_token"))
    assert "couldn't save it to Google" in text and "Rosario" in text
    assert any("out of date" in m["text"] for m in events.of("system_message"))
    assert not opened


def test_without_google_the_piece_is_written_in_chat(writer, config):
    assistant, events, tts, mock, server, opened, researched = writer
    config.update({"google_script_url": ""})
    mock.reply = BIO
    assistant.submit_text("write a bio on Lionel Messi")
    events.wait_for(events.finished, timeout=20)
    text = "".join(t["text"] for t in events.of("assistant_token"))
    assert text.startswith("# Lionel Messi") and not server.posts
    assert any("Tip: link Google" in m["text"] for m in events.of("system_message"))


def test_follow_up_edits_reach_the_same_doc(writer):
    from core.compose import parse_edit_request

    assistant, events, tts, mock, server, opened, researched = writer
    assert parse_edit_request("make it") is None
    assert parse_edit_request("add milk to my shopping list doc") is None
    state = {"text": "Lionel Messi\nBorn in Rosario."}

    def google(p):
        if p["action"] == "doc_read":
            return {"ok": True, "id": "doc1", "title": "Lionel Messi", "text": state["text"]}
        if p["action"] in ("doc_append", "doc_rewrite", "doc_create"):
            return {"ok": True, "id": "doc1", "title": "Lionel Messi", "url": "https://docs.google.com/document/d/doc1/edit"}
        return {"ok": False, "error": "unexpected"}

    server.reply = google
    mock.reply = BIO
    assistant.submit_text("write a bio on Lionel Messi")
    events.wait_for(events.finished, timeout=20)
    n = len(events.of("assistant_end"))

    mock.reply = "## Childhood\nHe grew up in **Rosario** and joined Barcelona at 13."
    assistant.submit_text("add a section about his childhood to it")
    events.wait_for(lambda: len(events.of("assistant_end")) == n + 1 and events.states()[-1] == "IDLE", timeout=20)
    append = server.posts[-1]
    assert append == {**append, "action": "doc_append", "document": "doc1"}
    assert append["text"].startswith("## Childhood")
    asked = [b for p, b in mock.requests if p == "/api/chat"][-1]["messages"][1]["content"]
    assert "Born in Rosario." in asked, "the writer sees the current document"

    mock.reply = "# Lionel Messi\n" + "A shorter biography of the Argentine forward. " * 5
    assistant.submit_text("make it shorter")
    events.wait_for(lambda: len(events.of("assistant_end")) == n + 2 and events.states()[-1] == "IDLE", timeout=20)
    assert server.posts[-1]["action"] == "doc_rewrite" and server.posts[-1]["text"].startswith("# Lionel Messi")
    said = "".join(t["text"] for t in events.of("assistant_token"))
    assert "added a section on his childhood" in said and "revised Lionel Messi" in said


def test_make_it_shorter_without_a_document_goes_to_the_model(writer):
    assistant, events, tts, mock, server, opened, researched = writer
    mock.reply = "Of course."
    assistant.submit_text("make it shorter")
    events.wait_for(events.finished, timeout=20)
    assert not server.posts


@pytest.mark.parametrize("text, target", [("give me a summary of the news", None), ("write a summary of this", None),
                                          ("write a summary of the French Revolution", "chat"),
                                          ("write a summary of the French Revolution in a google doc", "doc"),
                                          ("make a plan for the weekend", "chat")])
def test_short_answers_stay_in_chat(text, target):
    req = parse_write_request(text, google_ready=True)
    assert (req.target if req else None) == target


def test_written_markdown_formats_correctly_in_google_docs():
    from tests.test_google import run_gas

    title, body = clean_document(BIO)
    data = run_gas([{"token": "T0K3N", "action": "doc_create", "title": title, "text": body}])
    assert data["results"][0]["ok"], data["results"]
    paragraphs = data["files"][0]["paragraphs"]
    assert paragraphs[0]["text"] == "Lionel Messi: The Little Genius" and paragraphs[0]["heading"] == "HEADING1"
    assert any(p["text"] == "Early life" and p["heading"] == "HEADING2" for p in paragraphs)
    born = next(p for p in paragraphs if p["text"].startswith("Lionel Andrés Messi was born"))
    assert born["marks"] == [{"kind": "bold", "text": "24 June 1987"}], "**bold** becomes real bold, markers removed"
    assert [p["text"] for p in paragraphs if p["list"]] == ["Barcelona (2004–2021)", "Paris Saint-Germain (2021–2023)", "Inter Miami (2023–)"]
