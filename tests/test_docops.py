"""Working on a Google Doc in several steps ("rename the doc to Pizza recipe and then type out a pizza recipe"),
through the Google link, in the browser by keyboard, and typing generated text into any app."""

import pytest

from core.docops import docs_tab_title, parse_doc_command, to_plain
from core.screen import Window
from tests.test_google import URL, FakeAppsScript, bridge_with

PIZZA = "can you rename the Google Doc to Pizza recipe and then after you rename it to Pizza recipe can you type out a pizza recipe that I can make"
RECIPE = ("# Homemade Pizza\n\n## Ingredients\n\n- 500 g bread flour\n- 7 g yeast\n- **Mozzarella** and tomato sauce\n\n"
          "## Method\n\n1. Mix the dough and knead for 10 minutes.\n2. Rise for an hour, top it, and bake at 250 C for 10 minutes.")


# ----------------------------------------------------------------------------- understanding
@pytest.mark.parametrize("text, steps", [
    (PIZZA, [("rename", "Pizza recipe"), ("write", "a pizza recipe that I can make")]),
    ("rename the doc to Pizza recipe", [("rename", "Pizza recipe")]),
    ("call it Trip plans", [("rename", "Trip plans")]),
    ("change the title to weekly plan", [("rename", "Weekly plan")]),
    ("rename the doc to Pizza recipe, then write a pizza recipe in it", [("rename", "Pizza recipe"), ("write", "a pizza recipe")]),
    ("rename it to Notes after that type a to-do list for tomorrow", [("rename", "Notes"), ("write", "a to-do list for tomorrow")]),
    ("create a new doc called Shopping list and write a shopping list for a week in it",
     [("create", "Shopping list"), ("write", "a shopping list for a week")]),
    ("open the google doc and type out a poem about the sea", [("open", ""), ("write", "a poem about the sea")]),
    ("type 'hello world' into the doc", [("type", "hello world")]),
    ("type the words: see you at five", [("type", "see you at five")]),
    ("clear the document and write a haiku about rain", [("clear", ""), ("write", "a haiku about rain")]),
    ("type out a story about a cat and a dog", [("write", "a story about a cat and a dog")]),
    ("write a recipe with cheese and tomatoes in the doc", [("write", "a recipe with cheese and tomatoes")]),
])
def test_understands_document_steps(text, steps):
    cmd = parse_doc_command(text)
    assert cmd is not None and [(s.action, s.text) for s in cmd.steps] == steps


@pytest.mark.parametrize("text", ["type hello world", "what's the weather", "create a new document", "open the doc",
                                  "add a slide about pricing to my pitch deck", "write an email to Sarah saying hi", "rename"])
def test_leaves_other_requests_alone(text):
    assert parse_doc_command(text) is None


def test_named_document():
    cmd = parse_doc_command("rename my messi doc to Messi bio")
    assert cmd.name == "Messi" and cmd.steps[0].text == "Messi bio"


def test_helpers():
    assert docs_tab_title("Pizza recipe - Google Docs - Google Chrome") == "Pizza recipe"
    assert docs_tab_title("Inbox - Gmail - Google Chrome") is None
    plain = to_plain(RECIPE)
    assert "#" not in plain and "**" not in plain and "- 500 g bread flour" in plain and "1. Mix the dough" in plain


# ----------------------------------------------------------------------------- doing it
class Drive:
    """A fake Google account behind the bridge: docs with text, renames, a version that may be old."""

    def __init__(self, version=5):
        self.version = version
        self.docs = {"d1" + "x" * 24: {"title": "Untitled document", "text": ""}}

    def __call__(self, p):
        a = p["action"]
        if a == "file_rename":
            if self.version < 5:
                return {"ok": False, "error": "unknown action: file_rename"}
            doc = self.docs[p["file"]]
            old, doc["title"] = doc["title"], p["title"]
            return {"ok": True, "id": p["file"], "title": p["title"], "url": self.url(p["file"]), "old_title": old}
        if a == "list":
            q = (p.get("query") or "").lower()
            files = [{"id": i, "title": d["title"], "url": self.url(i), "updated": "2026-10-08T10:00:00Z"} for i, d in self.docs.items()
                     if all(w in d["title"].lower() for w in q.split())]
            return {"ok": True, "files": files}
        if a == "doc_create":
            i = f"n{len(self.docs)}" + "x" * 24
            self.docs[i] = {"title": p.get("title"), "text": p.get("text", "")}
            return {"ok": True, "id": i, "title": p.get("title"), "url": self.url(i)}
        if a == "doc_read":
            d = self.docs[p["document"]]
            return {"ok": True, "title": d["title"], "url": self.url(p["document"]), "text": d["text"]}
        if a == "doc_append":
            d = self.docs[p["document"]]
            d["text"] += p.get("text", "")
            return {"ok": True, "id": p["document"], "title": d["title"], "url": self.url(p["document"])}
        if a == "doc_rewrite":
            self.docs[p["document"]]["text"] = p.get("text", "")
            return {"ok": True, "id": p["document"], "title": self.docs[p["document"]]["title"], "url": self.url(p["document"])}
        return {"ok": False, "error": "unexpected " + a}

    @staticmethod
    def url(i):
        return f"https://docs.google.com/document/d/{i}/edit"


class Desk:
    """A desktop with a browser (maybe showing a Google Doc) and Notepad; records keys and typing."""

    def __init__(self, front="docs", doc_title="Untitled document"):
        title = f"{doc_title} - Google Docs - Google Chrome" if doc_title else "New Tab - Google Chrome"
        self.browser = Window(1, title, "chrome.exe", (0, 0, 1200, 800))
        self.notepad = Window(2, "Untitled - Notepad", "notepad.exe", (0, 0, 800, 600))
        self.front = {"docs": self.browser, "notepad": self.notepad, None: None}[front]
        self.keys, self.typed = [], []

    def windows(self, include_own=False):
        return [self.browser, self.notepad]

    def foreground(self):
        return self.front

    def alive(self, w):
        return True

    def bring_to_front(self, w):
        self.front = w

    def press(self, keys, window=None):
        self.keys.append((tuple(keys), window.hwnd if window else None))

    def type_text(self, text, window=None):
        self.typed.append((text, window.hwnd if window else None))
        if window is self.browser and self.keys and self.keys[-1][0] == (0x11, 0x41):  # Ctrl+A then typing = renaming the title box
            self.browser = Window(1, f"{text} - Google Docs - Google Chrome", "chrome.exe", self.browser.rect)

    def close(self, w):
        pass


@pytest.fixture
def setup(config, mock_ollama):
    from core.assistant import Assistant
    from core.tools import FileIndex, Toolbox
    from tests.test_assistant import Events, FakeTTS

    made = []

    def build(desk=None, drive=None, linked=True):
        mock_ollama.responder = lambda body: RECIPE
        config.update({"ollama_host": mock_ollama.url, "voice_enabled": False, "google_user_email": "tony@example.com",
                       "google_script_url": URL if linked else "", "google_bridge_version": drive.version if drive else 5})
        opened = []
        tools = Toolbox(config, index=FileIndex(lambda: [], apps_provider=lambda: []), url_launcher=opened.append)
        server = FakeAppsScript(drive or Drive())
        tools.google = bridge_with(config, server)
        tools.google.token()
        events = Events()
        assistant = Assistant(config, events, tts=FakeTTS(), tools=tools, desktop=desk or Desk(front=None, doc_title=None))
        assistant.start()
        made.append(assistant)

        def say(text):
            n = len(events.of("assistant_end"))
            assistant.submit_text(text)
            events.wait_for(lambda: len(events.of("assistant_end")) == n + 1 and events.states()[-1] == "IDLE", timeout=30)
            end = events.of("assistant_end")[-1]
            return "".join(t["text"] for t in events.of("assistant_token") if t["id"] == end["id"])

        return assistant, events, server, opened, say

    yield build
    for a in made:
        a.shutdown()


def test_the_exact_pizza_request_with_the_doc_open(setup):
    drive, desk = Drive(), Desk(front="docs")
    assistant, events, server, opened, say = setup(desk, drive)
    reply = say(PIZZA)
    doc = drive.docs["d1" + "x" * 24]
    assert doc["title"] == "Pizza recipe", reply
    assert "500 g bread flour" in doc["text"] and "# Homemade Pizza" in doc["text"]
    assert "Renamed it to Pizza recipe." in reply and "wrote a pizza recipe that I can make in it" in reply
    actions = [p["action"] for p in server.posts]
    assert actions.index("file_rename") < actions.index("doc_append"), "renamed first, then wrote"
    assert desk.typed == [], "the Google link did it: no keyboard needed"
    assert assistant._last_doc["title"] == "Pizza recipe"


def test_pizza_request_with_nothing_open_creates_the_doc(setup):
    drive = Drive()
    drive.docs.clear()
    assistant, events, server, opened, say = setup(Desk(front=None, doc_title=None), drive)
    reply = say(PIZZA)
    created = [p for p in server.posts if p["action"] == "doc_create"]
    assert created and created[0]["title"] == "Pizza recipe", reply
    doc = next(iter(drive.docs.values()))
    assert doc["title"] == "Pizza recipe" and "Homemade Pizza" in doc["text"]
    assert opened and opened[-1].startswith("https://docs.google.com/document/d/"), "and opened it"
    assert "created Pizza recipe" in reply


def test_follow_up_on_the_doc_jarvis_just_made(setup):
    drive = Drive()
    drive.docs.clear()
    assistant, events, server, opened, say = setup(Desk(front=None, doc_title=None), drive)
    say("create a new document")
    reply = say("rename it to Pizza recipe and then type out a pizza recipe")
    doc = next(iter(drive.docs.values()))
    assert doc["title"] == "Pizza recipe" and "bread flour" in doc["text"], reply


def test_old_google_script_renames_in_the_browser_instead(setup):
    drive, desk = Drive(version=4), Desk(front="docs")
    assistant, events, server, opened, say = setup(desk, drive)
    reply = say(PIZZA)
    assert (0x12, 0xBF) in [k for k, _ in desk.keys], "Alt+/ (Docs' menu search) to run File > Rename"
    assert ("Pizza recipe", 1) in desk.typed
    assert "bread flour" in drive.docs["d1" + "x" * 24]["text"], "writing still goes through the Google link"
    assert "Renamed it to Pizza recipe." in reply


def test_without_the_google_link_it_types_into_the_open_doc(setup):
    desk = Desk(front="docs")
    assistant, events, server, opened, say = setup(desk, linked=False)
    reply = say(PIZZA)
    assert ("Pizza recipe", 1) in desk.typed, reply
    body = [t for t, w in desk.typed if w == 1 and "bread flour" in t]
    assert body and "#" not in body[0] and "**" not in body[0], "plain text, no Markdown symbols"
    assert (0x11, 0x23) in [k for k, _ in desk.keys], "Ctrl+End: typed at the end of the document"
    assert not server.posts


def test_type_out_into_notepad(setup):
    desk = Desk(front="notepad")
    assistant, events, server, opened, say = setup(desk)
    reply = say("type out a pizza recipe that I can make")
    assert reply == "Done, sir. I've typed a pizza recipe that I can make into Untitled - Notepad."
    text, hwnd = desk.typed[-1]
    assert hwnd == 2 and "bread flour" in text and "#" not in text
    assert not [p for p in server.posts if p["action"] in ("doc_append", "file_rename")]


def test_type_exact_words_into_the_doc(setup):
    drive, desk = Drive(), Desk(front="docs")
    assistant, events, server, opened, say = setup(desk, drive)
    say("type 'see you at five' into the doc")
    assert drive.docs["d1" + "x" * 24]["text"] == "see you at five"


def test_rename_a_named_document(setup):
    drive = Drive()
    drive.docs["m1" + "x" * 24] = {"title": "Lionel Messi", "text": "Born in Rosario."}
    assistant, events, server, opened, say = setup(Desk(front=None), drive)
    reply = say("rename my messi doc to Messi bio")
    assert drive.docs["m1" + "x" * 24]["title"] == "Messi bio", reply


def test_missing_document_is_explained(setup):
    assistant, events, server, opened, say = setup(Desk(front=None), Drive())
    reply = say("rename my unicorn doc to Ponies")
    assert "couldn't find a document called Unicorn" in reply


def test_offline_model_is_explained_but_rename_still_happens(setup, mock_ollama):
    drive, desk = Drive(), Desk(front="docs")
    assistant, events, server, opened, say = setup(desk, drive)
    assistant.llm.online = False
    assistant.llm.check = lambda: type("S", (), {"online": False, "model": None, "models": [], "to_dict": lambda self: {}})()
    reply = say(PIZZA)
    assert drive.docs["d1" + "x" * 24]["title"] == "Pizza recipe"
    assert "neural core is offline" in reply


def test_bridge_script_renames_for_real():
    from tests.test_google import run_gas

    data = run_gas([{"token": "T0K3N", "action": "doc_create", "title": "Untitled document"},
                    {"token": "T0K3N", "action": "file_rename", "kind": "doc", "file": "Untitled document", "title": "Pizza recipe"},
                    {"token": "T0K3N", "action": "doc_append", "document": "Pizza recipe", "text": RECIPE},
                    {"token": "T0K3N", "action": "doc_read", "document": "Pizza recipe"}])
    results = data["results"]
    assert all(r["ok"] for r in results), results
    assert results[1]["title"] == "Pizza recipe" and results[1]["old_title"] == "Untitled document"
    assert "Homemade Pizza" in results[3]["text"] and "500 g bread flour" in results[3]["text"]
    assert data["files"][0]["name"] == "Pizza recipe"
