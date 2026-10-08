"""Guards against small-model confusion: typed-out tool calls, needless tools, stale facts, clock questions."""

import json
import re
import threading

import pytest

from core.llm import LLMEngine, parse_text_tool_calls
from tests.mock_ollama import DEFAULT_REPLY, MockOllama
from tests.test_tools import FakeToolbox, box, home  # noqa: F401 - shared fixtures

KNOWN = {"web_search", "open_file"}


@pytest.mark.parametrize("typed", [
    '{"name": "web_search", "parameters": {"query": "weather in Paris"}}',
    '<|python_tag|>{"name": "web_search", "parameters": {"query": "weather in Paris"}}<|eom_id|>',
    '```json\n{"name": "web_search", "arguments": {"query": "weather in Paris"}}\n```',
    '[{"name": "web_search", "parameters": {"query": "weather in Paris"}}]',
    '{"type": "function", "function": {"name": "web_search", "parameters": "{\\"query\\": \\"weather in Paris\\"}"}}',
])
def test_typed_tool_calls_are_recognised(typed):
    assert parse_text_tool_calls(typed, KNOWN) == [{"name": "web_search", "arguments": {"query": "weather in Paris"}}]


@pytest.mark.parametrize("text", [
    '{"name": "delete_everything", "parameters": {}}',  # not a tool we offered
    '{"name": "web_search", "parameters": {"query": "x"}} and then I will tell you',  # prose after JSON
    '{"weather": "sunny"}',
    "Certainly, sir.",
])
def test_non_calls_are_left_alone(text):
    assert parse_text_tool_calls(text, KNOWN) is None


@pytest.fixture
def engine_for(config):
    made = []

    def factory(**mock_kwargs):
        mock = MockOllama(**mock_kwargs).start()
        made.append(mock)
        config.update({"ollama_host": mock.url})
        engine = LLMEngine(config)
        engine.check()
        return engine, mock

    yield factory
    for m in made:
        m.stop()


def test_typed_tool_call_is_executed_and_never_shown(engine_for):
    engine, mock = engine_for(text_tool_calls=True)
    box, used = FakeToolbox(), []
    reply = "".join(engine.stream_reply("search the news about stark", toolbox=box, on_tool=lambda n, a: used.append(n)))
    assert used == ["web_search"] and box.calls[0][0] == "web_search"
    assert "python_tag" not in reply and '"name"' not in reply, reply
    assert "Stark Expo opens" in reply


def test_answers_that_start_with_a_brace_still_stream(engine_for):
    engine, mock = engine_for()
    mock.reply = "{curly} is just a word here, sir."
    assert "".join(engine.stream_reply("tell me about braces", toolbox=FakeToolbox())) == mock.reply


def test_no_tools_for_small_talk_and_lower_temperature_with_tools(engine_for):
    engine, mock = engine_for()
    "".join(engine.stream_reply("hello there", toolbox=FakeToolbox(), offer_tools=False))
    "".join(engine.stream_reply("search the web for news", toolbox=FakeToolbox()))
    chats = [b for p, b in mock.requests if p == "/api/chat"]
    assert not chats[0].get("tools") and chats[0]["options"]["temperature"] == 0.7
    assert chats[1]["tools"] and chats[1]["options"]["temperature"] == 0.4


def test_prefetched_results_reach_the_model(engine_for):
    engine, mock = engine_for()
    result = json.dumps({"ok": True, "results": [{"title": "Sunny in Paris"}]})
    reply = "".join(engine.stream_reply("what's the weather in Paris", toolbox=FakeToolbox(), offer_tools=False,
                                        prefetch=[("web_search", {"query": "weather in Paris"}, result)]))
    sent = [b for p, b in mock.requests if p == "/api/chat"][0]["messages"]
    assert sent[-2]["tool_calls"][0]["function"]["name"] == "web_search"
    assert sent[-1] == {"role": "tool", "content": result, "tool_name": "web_search"}
    assert "Sunny in Paris" in reply


# ------------------------------------------------------------------ assistant router


@pytest.fixture
def router(config, mock_ollama, box):
    from core.assistant import Assistant
    from tests.test_assistant import Events, FakeTTS

    config.update({"ollama_host": mock_ollama.url, "voice_enabled": False})
    events = Events()
    searches = []
    real_run = box.run

    def run(name, args):
        if name == "web_search":
            searches.append(args["query"])
            return json.dumps({"ok": True, "results": [{"title": "Rain later in London"}]})
        return real_run(name, args)

    box.run = run
    assistant = Assistant(config, events, tts=FakeTTS(), tools=box)
    assistant.start()

    def ask(text, n=[0]):
        n[0] += 1
        assistant.submit_text(text)
        events.wait_for(lambda: len(events.of("assistant_end")) == n[0] and events.states()[-1] == "IDLE")
        ends = events.of("assistant_end")
        tokens = [p["text"] for p in events.of("assistant_token")]
        return "".join(tokens[len(ask.seen):]), ends[-1]

    ask.seen = []
    yield assistant, events, ask, searches, mock_ollama
    assistant.shutdown()


def chats(mock):
    return [b for p, b in mock.requests if p == "/api/chat"]


def test_clock_questions_skip_the_model(router):
    assistant, events, ask, searches, mock = router
    assistant.submit_text("what time is it?")
    events.wait_for(events.finished)
    reply = "".join(p["text"] for p in events.of("assistant_token"))
    assert re.match(r"It's \d{1,2}:\d\d (AM|PM), sir\.$", reply)
    assert not chats(mock)


def test_live_questions_are_searched_first(router):
    import httpx

    assistant, events, ask, searches, mock = router

    def offline(request):
        raise httpx.ConnectError("offline", request=request)

    # the weather skill answers weather directly; when its service can't be reached, a web search takes over
    assistant.weather._client_factory = lambda: httpx.Client(transport=httpx.MockTransport(offline))
    assistant.submit_text("what's the weather in London tomorrow?")
    events.wait_for(events.finished)
    assert searches == ["what's the weather in London tomorrow?"]
    assert events.of("tool_activity")[-1]["label"].startswith("Searching the web")
    sent = chats(mock)[0]
    assert sent["messages"][-1]["role"] == "tool" and "Rain later" in sent["messages"][-1]["content"]


def test_explicit_search_command_uses_just_the_query(router):
    assistant, events, ask, searches, mock = router
    assistant.submit_text("Jarvis, look up the tallest building in Dubai")
    events.wait_for(events.finished)
    assert searches == ["the tallest building in Dubai"]


def test_small_talk_gets_no_tools(router):
    assistant, events, ask, searches, mock = router
    assistant.submit_text("thanks, you're great")
    events.wait_for(events.finished)
    assert not chats(mock)[0].get("tools") and not searches


def test_play_launches_games_only(router, box):
    assistant, events, ask, searches, mock = router
    assistant.submit_text("let's play whatsapp")  # an app counts; documents never do
    events.wait_for(events.finished)
    assert "".join(p["text"] for p in events.of("assistant_token")).startswith("Launching WhatsApp")
    assert not chats(mock)


@pytest.mark.parametrize("request_text", ["delete slide 3 of my Pitch deck", "move the last slide to the front",
                                          "put rent 1200 in my budget sheet", "rename the title on slide 2"])
def test_office_edits_get_the_google_tools(router, config, box, request_text):
    assistant, events, ask, searches, mock = router
    config.update({"google_script_url": "https://script.google.com/macros/s/" + "A" * 40 + "/exec"})
    box.google.token()
    assistant.submit_text(request_text)
    events.wait_for(events.finished)
    offered = {t["function"]["name"] for t in chats(mock)[0].get("tools") or []}
    assert {"google_slides", "google_sheets", "google_doc"} <= offered


def test_google_app_names_are_not_web_searches():
    from core.assistant import _SEARCH_COMMAND

    assert _SEARCH_COMMAND.match("google the weather in Rome")["query"] == "the weather in Rome"
    for text in ("Google Sheets, make a budget", "google docs write a memo", "Jarvis, google slides for my pitch"):
        assert not _SEARCH_COMMAND.match(text), text


def test_plain_knowledge_questions_skip_tools_for_a_faster_answer(router):
    assistant, events, ask, searches, mock = router
    assistant.submit_text("what is the capital of France")
    events.wait_for(events.finished)
    assert not chats(mock)[0].get("tools") and not searches, "no tool round-trips for things the model already knows"


def test_chained_open_commands_run_in_order(config, mock_ollama):
    from core.assistant import Assistant
    from core.tools import FileIndex, Toolbox
    from tests.test_assistant import Events, FakeTTS
    from tests.test_tools import Launches

    config.update({"ollama_host": mock_ollama.url, "voice_enabled": False})
    launched = Launches()
    index = FileIndex(lambda: [], apps_provider=lambda: [("Spotify", "Spotify.App"), ("Notepad", "Notepad.App"), ("Calculator", "Calc.App")])
    tools = Toolbox(config, index=index, app_launcher=lambda app: launched.paths.append(app), launcher=launched.paths.append,
                    url_launcher=launched.urls.append)
    events = Events()
    assistant = Assistant(config, events, tts=FakeTTS(), tools=tools)
    assistant.start()
    try:
        assistant.submit_text("open Spotify and then launch Notepad, open calculator")
        events.wait_for(events.finished)
        said = "".join(t["text"] for t in events.of("assistant_token"))
        assert launched.paths == ["Spotify.App", "Notepad.App", "Calc.App"]
        assert said == "Opening Spotify. Opening Notepad. Opening Calculator, sir."
        assert not [1 for p, _ in mock_ollama.requests if p == "/api/chat"]
    finally:
        assistant.shutdown()
