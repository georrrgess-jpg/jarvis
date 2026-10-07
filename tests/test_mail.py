"""Email: request parsing, drafting, and sending only after the user confirms."""

import pytest

from core.mail import gmail_compose_url, is_cancellation, is_confirmation, parse_email, parse_email_request, spoken_address
from tests.test_google import URL, FakeAppsScript, bridge_with


@pytest.mark.parametrize("text, who, what", [
    ("send an email to John saying I will be late", "John", "I will be late"),
    ("Jarvis, email Sarah about the meeting tomorrow at 3pm", "Sarah", "the meeting tomorrow at 3pm"),
    ("send mum an email telling her I landed safely", "mum", "I landed safely"),
    ("write an email to my boss asking him for Friday off", "boss", "for Friday off"),
    ("let Sarah know by email that the report is ready", "Sarah", "the report is ready"),
    ("send an email to john@example.com", "john@example.com", ""),
])
def test_recognises_email_requests(text, who, what):
    req = parse_email_request(text)
    assert (req.who, req.what, req.share) == (who, what, False)


@pytest.mark.parametrize("text", ["what is my email", "open gmail", "check my email", "send it to John", "read my emails"])
def test_ignores_other_requests(text):
    assert parse_email_request(text) is None


def test_sharing_documents_and_spoken_addresses():
    req = parse_email_request("email it to Tom")
    assert req.share and req.who == "Tom"
    assert parse_email_request("email my Messi bio to Tom").doc_name == "Messi"
    assert parse_email_request("email john dot smith at gmail dot com saying hello").address == "john.smith@gmail.com"
    assert spoken_address("jane underscore doe at outlook dot co dot uk") == "jane_doe@outlook.co.uk"


def test_confirmation_words():
    assert all(is_confirmation(t) for t in ["yes", "send it", "Yes, send it now.", "go ahead", "yes please"])
    assert not any(is_confirmation(t) for t in ["yes but change the subject", "no", "what does it say"])
    assert all(is_cancellation(t) for t in ["no", "cancel", "don't send it", "never mind"])


def test_parse_email_output():
    subject, body = parse_email("Subject: Running late\n\nHi John,\n\nI'll be 10 minutes late.\n\nBest regards,\n[Your Name]", "x")
    assert subject == "Running late" and body == "Hi John,\n\nI'll be 10 minutes late.\n\nBest regards,"
    assert parse_email("Hi there", "Hello") == ("Hello", "Hi there")
    assert gmail_compose_url("a@b.com", "Hi & bye", "x y").startswith("https://mail.google.com/mail/?view=cm&fs=1&to=a%40b.com&su=Hi+%26+bye")


# ------------------------------------------------------------------ the assistant


EMAIL = "Subject: Running late\n\nHi Sarah,\n\nI'm running about ten minutes late. Sorry!\n\nBest regards,\nTony"


@pytest.fixture
def mailer(config, mock_ollama):
    from core.assistant import Assistant
    from core.tools import FileIndex, Toolbox
    from tests.test_assistant import Events, FakeTTS

    config.update({"ollama_host": mock_ollama.url, "voice_enabled": False, "google_script_url": URL,
                   "google_bridge_version": 3, "user_name": "Tony", "google_user_email": "tony@example.com"})
    mock_ollama.reply = EMAIL

    def google(p):
        if p["action"] == "contact_find":
            people = [{"email": "sarah.connor@example.com", "name": "Sarah Connor", "count": 3}] if "sarah" in p["name"].lower() else []
            return {"ok": True, "people": people}
        return {"ok": True, "sent": True, "shared": True}

    server = FakeAppsScript(google)
    opened = []
    tools = Toolbox(config, index=FileIndex(lambda: [], apps_provider=lambda: []), url_launcher=opened.append)
    tools.google = bridge_with(config, server)
    tools.google.token()
    events = Events()
    assistant = Assistant(config, events, tts=FakeTTS(), tools=tools)
    assistant.start()

    def turn(text):
        n = len(events.of("assistant_end"))
        assistant.submit_text(text)
        events.wait_for(lambda: len(events.of("assistant_end")) == n + 1 and events.states()[-1] == "IDLE", timeout=20)
        return events.of("assistant_end")[-1], "".join(t["text"] for t in events.of("assistant_token")[-40:])

    yield assistant, events, server, opened, turn, mock_ollama
    assistant.shutdown()


def sent(server):
    return [p for p in server.posts if p["action"] == "mail_send"]


def test_email_is_drafted_then_sent_only_after_yes(mailer):
    assistant, events, server, opened, turn, mock = mailer
    _, said = turn("send an email to Sarah saying I'm running ten minutes late")
    draft = events.of("email_draft")[0]
    assert draft["to"] == "sarah.connor@example.com" and draft["subject"] == "Running late"
    assert draft["body"].startswith("Hi Sarah,") and draft["body"].endswith("Tony")
    assert "Shall I send it?" in said and not sent(server), "nothing is sent before confirmation"
    prompt = [b for p, b in mock.requests if p == "/api/chat"][-1]["messages"]
    assert "Tony" in prompt[0]["content"] and "running ten minutes late" in prompt[1]["content"]
    _, said = turn("yes, send it")
    assert sent(server) == [{**sent(server)[0], "to": "sarah.connor@example.com", "subject": "Running late"}]
    assert "Sent to Sarah Connor" in said
    assert events.of("email_status")[-1]["status"] == "sent"


def test_no_discards_the_draft(mailer):
    assistant, events, server, opened, turn, mock = mailer
    turn("email Sarah about lunch")
    _, said = turn("no")
    assert "discarded" in said and not sent(server)
    assert events.of("email_status")[-1]["status"] == "discarded"


def test_unrelated_reply_leaves_the_draft_unsent(mailer):
    assistant, events, server, opened, turn, mock = mailer
    turn("email Sarah about lunch")
    mock.reply = "It's sunny."
    turn("what's the weather like")
    mock.reply = "Of course."
    turn("yes")
    assert not sent(server), "a 'yes' two turns later doesn't send the old draft"


def test_unknown_recipient_asks_for_the_address_and_card_can_send(mailer):
    assistant, events, server, opened, turn, mock = mailer
    _, said = turn("email Bruce about the gala")
    draft = events.of("email_draft")[0]
    assert draft["to"] == "" and "couldn't find Bruce's address" in said
    assert assistant.send_email(draft["id"], to="not-an-address")["ok"] is False
    result = assistant.send_email(draft["id"], to="bruce@wayne.com", subject="Gala", body="Hi Bruce")
    assert result["ok"] and sent(server)[0] == {**sent(server)[0], "to": "bruce@wayne.com", "subject": "Gala", "body": "Hi Bruce"}


def test_without_gmail_access_the_draft_opens_in_gmail(mailer, config):
    assistant, events, server, opened, turn, mock = mailer
    config.update({"google_bridge_version": 2})
    turn("send an email to sarah@example.com saying hi")
    _, said = turn("send it")
    assert not sent(server) and opened and opened[0].startswith("https://mail.google.com/mail/?view=cm")
    assert "sarah%40example.com" in opened[0] and "press Send" in said


def test_emailing_a_document_shares_it_and_includes_the_link(mailer):
    assistant, events, server, opened, turn, mock = mailer
    assistant._last_doc = {"kind": "doc", "id": "doc1", "title": "Lionel Messi", "url": "https://docs.google.com/document/d/doc1/edit"}
    mock.reply = "Subject: Messi bio\n\nHi Sarah,\n\nHere's the bio I wrote.\n\nBest,\nTony"
    turn("email it to Sarah")
    draft = events.of("email_draft")[-1]
    assert draft["body"].endswith("https://docs.google.com/document/d/doc1/edit"), "the link is always included"
    turn("yes")
    actions = [p["action"] for p in server.posts]
    assert actions[-2:] == ["share_file", "mail_send"]
    assert server.posts[-2]["file"] == "doc1" and server.posts[-2]["email"] == "sarah.connor@example.com"


def test_email_to_me_uses_the_linked_account(mailer):
    assistant, events, server, opened, turn, mock = mailer
    turn("send an email to me saying remember the milk")
    assert events.of("email_draft")[-1]["to"] == "tony@example.com"
