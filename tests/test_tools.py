"""Files, apps and web abilities (no real network or file launching: everything is faked)."""

import json
import os
import threading
import time
import zipfile
from pathlib import Path

import httpx
import pytest

from core.tools import FileIndex, Toolbox, ToolError, describe_call, html_to_text, parse_ddg_html, parse_ddg_lite

DDG_HTML = """
<html><body>
<div class="result results_links results_links_deep result--ad ">
  <a class="result__a" href="https://duckduckgo.com/y.js?ad_provider=x">Sponsored thing</a>
</div>
<div class="result results_links results_links_deep web-result ">
  <div class="links_main links_deep result__body">
    <h2 class="result__title">
      <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.bbc.co.uk%2Fweather&amp;rut=abc">BBC <b>Weather</b> - London</a>
    </h2>
    <a class="result__snippet" href="//duckduckgo.com/l/?uddg=x">Sunny spells and a gentle breeze, high of <b>21&deg;C</b>.</a>
  </div>
</div>
<div class="result results_links web-result ">
  <h2 class="result__title"><a rel="nofollow" class="result__a" href="https://metoffice.gov.uk/london">Met Office: London forecast</a></h2>
  <a class="result__snippet">Dry today, rain later.</a>
</div>
</body></html>
"""

DDG_LITE = """
<table><tr><td><a rel="nofollow" href="https://example.org/page" class='result-link'>Example <b>Org</b></a></td></tr>
<tr><td class='result-snippet'>An example result snippet.</td></tr></table>
"""

PAGE = """<html><head><title>Arc Reactor - Wiki</title><script>var x = "ignore me";</script>
<style>.a{}</style></head><body><nav>Menu Home About</nav><article><h1>Arc reactor</h1>
<p>The arc reactor is a fictional power source.</p><p>It appears in Iron Man.</p></article>
<footer>Copyright</footer></body></html>"""


# ------------------------------------------------------------------ fixtures


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A fake user profile plus a fake Start Menu."""
    home = tmp_path / "home"
    for name in ("Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos"):
        (home / name).mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("OneDrive", raising=False)
    start = tmp_path / "startmenu"
    (start / "Spotify").mkdir(parents=True)
    (start / "Spotify" / "Spotify.lnk").write_bytes(b"L\x00\x00\x00")
    (start / "Google Chrome.lnk").write_bytes(b"L\x00\x00\x00")
    docs = home / "Documents"
    (docs / "Resume 2024.docx").write_bytes(b"x")
    (docs / "budget.xlsx").write_bytes(b"x")
    (docs / "budget notes.txt").write_text("Rent 1200\nFood 300\n")
    (docs / "Projects" / "jarvis").mkdir(parents=True)
    (docs / ".hidden.txt").write_text("secret")
    (home / "Downloads" / "setup_spotify.exe").write_bytes(b"MZ")
    (home / "Pictures" / "spotify logo.png").write_bytes(b"\x89PNG")
    roots = [(start, "apps")] + [(home / n, "user") for n in ("Desktop", "Documents", "Downloads", "Pictures")]
    return {"home": home, "start": start, "roots": roots}


class Launches:
    def __init__(self):
        self.paths, self.urls = [], []


@pytest.fixture
def box(config, home):
    launched = Launches()
    tb = Toolbox(config, index=FileIndex(lambda: home["roots"]), launcher=launched.paths.append,
                 url_launcher=launched.urls.append, host_check=lambda h: h not in ("localhost", "127.0.0.1", "192.168.1.1"))
    tb.launched = launched
    return tb


def web_box(config, handler):
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    return Toolbox(config, index=FileIndex(lambda: []), http=client, url_launcher=lambda u: None,
                   host_check=lambda h: h not in ("localhost", "127.0.0.1", "10.0.0.5"))


# ------------------------------------------------------------------ local files


def test_open_app_prefers_start_menu_shortcut(box):
    result = box.open_target("spotify")
    assert box.launched.paths[-1].name == "Spotify.lnk"
    assert result["kind"] == "app" and result["name"] == "Spotify"


def test_shortcut_beats_same_named_start_menu_folder(box):
    ranked = box.index.search("spotify", limit=3)
    assert ranked[0][1].path.name == "Spotify.lnk"
    assert ranked[0][0] > next(score for score, e in ranked if e.is_dir)


def test_open_document_by_partial_name(box):
    box.open_target("my resume")
    assert box.launched.paths[-1].name == "Resume 2024.docx"


def test_type_words_match_extensions(box):
    box.open_target("budget spreadsheet")
    assert box.launched.paths[-1].name == "budget.xlsx"
    box.open_target("budget notes document")
    assert box.launched.paths[-1].name == "budget notes.txt"


def test_open_known_folder(box, home):
    result = box.open_target("downloads")
    assert box.launched.paths[-1] == home["home"] / "Downloads"
    assert result["kind"] == "folder"


def test_open_explicit_path(box, home):
    target = home["home"] / "Documents" / "budget.xlsx"
    box.open_target(str(target))
    assert box.launched.paths[-1] == target


def test_never_runs_programs_directly(box, home):
    with pytest.raises(ToolError, match="won't run programs"):
        box.open_target(str(home["home"] / "Downloads" / "setup_spotify.exe"))
    assert box.launched.paths == []


def test_unknown_thing_is_not_opened(box):
    with pytest.raises(ToolError, match="couldn't find"):
        box.open_target("pod bay doors")
    assert box.launched.paths == []


def test_hidden_files_are_not_indexed(box):
    assert not any(e.path.name.startswith(".") for e in box.index.entries())


def test_find_files_lists_paths(box):
    names = [m["name"] for m in box.find_files("budget")["matches"]]
    assert "budget.xlsx" in names and "budget notes.txt" in names


def test_read_text_and_docx(box, home):
    assert "Rent 1200" in box.read_file("budget notes")["content"]
    docx = home["home"] / "Documents" / "Plan.docx"
    with zipfile.ZipFile(docx, "w") as zf:
        zf.writestr("word/document.xml", '<w:document><w:body><w:p><w:r><w:t>Build the suit</w:t></w:r></w:p>'
                                         '<w:p><w:r><w:t>Test flight &amp; landing</w:t></w:r></w:p></w:body></w:document>')
    box.index._built = 0  # force a rescan
    text = box.read_file(str(docx))["content"]
    assert "Build the suit" in text and "Test flight & landing" in text


def test_read_refuses_binary(box, home):
    with pytest.raises(ToolError, match="can't read"):
        box.read_file(str(home["home"] / "Pictures" / "spotify logo.png"))


def test_run_returns_json_errors_instead_of_raising(box):
    out = json.loads(box.run("open_file", {"query": "pod bay doors"}))
    assert out["ok"] is False and "couldn't find" in out["error"]
    assert json.loads(box.run("nope", {}))["ok"] is False


def test_index_scan_is_bounded(config, tmp_path, monkeypatch):
    import core.tools as tools

    for i in range(50):
        (tmp_path / f"f{i}.txt").write_text("x")
    monkeypatch.setattr(tools, "MAX_SCAN_FILES", 10)
    assert len(FileIndex(lambda: [(tmp_path, "user")]).entries()) == 10


# ------------------------------------------------------------------ web


def test_parse_duckduckgo_html_skips_ads_and_unwraps_links():
    results = parse_ddg_html(DDG_HTML)
    assert [r["url"] for r in results] == ["https://www.bbc.co.uk/weather", "https://metoffice.gov.uk/london"]
    assert results[0]["title"] == "BBC Weather - London"
    assert "21°C" in results[0]["snippet"]


def test_parse_duckduckgo_lite():
    assert parse_ddg_lite(DDG_LITE) == [{"title": "Example Org", "url": "https://example.org/page",
                                        "snippet": "An example result snippet."}]


def test_web_search_uses_duckduckgo(config):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, text=DDG_HTML)

    out = web_box(config, handler).web_search("weather in london")
    assert out["results"][0]["url"] == "https://www.bbc.co.uk/weather"
    assert "untrusted" in out["note"]
    assert seen[0].startswith("https://html.duckduckgo.com/html/")


def test_web_search_falls_back_to_lite_then_wikipedia(config):
    def lite_only(request):
        if "lite.duckduckgo" in str(request.url):
            return httpx.Response(200, text=DDG_LITE)
        return httpx.Response(200, text="<html>anomaly</html>")

    assert web_box(config, lite_only).web_search("x")["results"][0]["url"] == "https://example.org/page"

    def wiki_only(request):
        if "wikipedia" in str(request.url):
            return httpx.Response(200, json={"query": {"search": [{"title": "Arc reactor", "snippet": "a <b>power</b> source"}]}})
        raise httpx.ConnectError("blocked")

    result = web_box(config, wiki_only).web_search("arc reactor")["results"][0]
    assert result["title"] == "Arc reactor" and result["snippet"] == "a power source"


def test_web_search_offline_reports_cleanly(config):
    def offline(request):
        raise httpx.ConnectError("no network")

    out = json.loads(web_box(config, offline).run("web_search", {"query": "news"}))
    assert out["ok"] is False and "no internet" in out["error"]


def test_read_webpage_extracts_main_text(config):
    out = web_box(config, lambda r: httpx.Response(200, text=PAGE, headers={"content-type": "text/html"})).read_webpage("example.com/arc")
    assert out["title"] == "Arc Reactor - Wiki"
    assert "fictional power source" in out["content"]
    assert "ignore me" not in out["content"] and "Menu Home" not in out["content"]


def test_read_webpage_blocks_local_network(config):
    tb = web_box(config, lambda r: httpx.Response(200, text="secret"))
    for url in ("http://localhost:11434/api/tags", "http://127.0.0.1/", "http://10.0.0.5/admin"):
        with pytest.raises(ToolError, match="public websites"):
            tb.read_webpage(url)


def test_real_host_check_rejects_private_addresses():
    from core.tools import _is_public_host

    assert not _is_public_host("localhost")
    assert not _is_public_host("127.0.0.1")
    assert not _is_public_host("192.168.0.10")
    assert not _is_public_host("printer.local")


def test_read_webpage_refuses_binary(config):
    tb = web_box(config, lambda r: httpx.Response(200, content=b"\x00\x01", headers={"content-type": "application/zip"}))
    with pytest.raises(ToolError, match="not text"):
        tb.read_webpage("https://example.com/file.zip")


def test_open_website_variants(box):
    box.open_website("youtube")
    box.open_website("bbc.co.uk/news")
    box.open_website("best pizza near me")
    assert box.launched.urls == ["https://www.youtube.com", "https://bbc.co.uk/news",
                                 "https://duckduckgo.com/?q=best+pizza+near+me"]
    box.open_website("javascript:alert(1)")  # never launched as a URL: it becomes a harmless web search
    assert box.launched.urls[-1].startswith("https://duckduckgo.com/?q=javascript")


def test_toggles_hide_and_block_tools(box, config):
    names = {s["function"]["name"] for s in box.specs()}
    assert names == {"open_file", "find_files", "read_file", "web_search", "read_webpage", "open_website"}
    config.update({"allow_internet": False})
    assert {s["function"]["name"] for s in box.specs()} == {"open_file", "find_files", "read_file"}
    assert json.loads(box.run("web_search", {"query": "x"}))["error"].startswith("this ability is turned off")
    config.update({"allow_files": False})
    assert box.specs() == []


def test_html_to_text_survives_garbage():
    title, text = html_to_text("<html><title>T</title><p>ok<div><<<>>")
    assert title == "T" and "ok" in text


def test_describe_call():
    assert describe_call("web_search", {"query": "weather in Paris"}) == "Searching the web: weather in Paris"
    assert describe_call("open_file", {}) == "Opening"


# ------------------------------------------------------------------ model tool loop


class FakeToolbox:
    def __init__(self, result='{"ok": true, "results": [{"title": "Stark Expo opens"}]}'):
        self.result = result
        self.calls = []

    def specs(self):
        return [{"type": "function", "function": {"name": n, "description": n,
                 "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}}
                for n in ("web_search", "read_file")]

    def run(self, name, args):
        self.calls.append((name, args))
        return self.result


def test_llm_runs_tool_and_answers_from_result(config, mock_ollama):
    from core.llm import LLMEngine

    config.update({"ollama_host": mock_ollama.url})
    engine = LLMEngine(config)
    engine.check()
    box, used = FakeToolbox(), []
    reply = "".join(engine.stream_reply("search the web for stark news", toolbox=box,
                                        on_tool=lambda n, a: used.append(n)))
    assert box.calls == [("web_search", {"query": "search the web for stark news"})]
    assert used == ["web_search"]
    assert "Stark Expo opens" in reply
    chats = [b for p, b in mock_ollama.requests if p == "/api/chat"]
    assert len(chats) == 2 and chats[0]["tools"]
    second = chats[1]["messages"]
    assert second[-2]["tool_calls"][0]["function"]["name"] == "web_search"
    assert second[-1] == {"role": "tool", "content": box.result, "tool_name": "web_search"}
    assert "search the internet" in second[0]["content"]
    assert engine.history_turns == 1 and "tool" not in [m["role"] for m in engine._history]


def test_llm_without_tool_need_streams_normally(config, mock_ollama):
    from core.llm import LLMEngine
    from tests.mock_ollama import DEFAULT_REPLY

    config.update({"ollama_host": mock_ollama.url})
    engine = LLMEngine(config)
    engine.check()
    box = FakeToolbox()
    assert "".join(engine.stream_reply("how are you", toolbox=box)) == DEFAULT_REPLY
    assert box.calls == []


def test_llm_falls_back_when_model_lacks_tool_support(config):
    from core.llm import LLMEngine
    from tests.mock_ollama import DEFAULT_REPLY, MockOllama

    mock = MockOllama(tools_supported=False).start()
    try:
        config.update({"ollama_host": mock.url})
        engine = LLMEngine(config)
        engine.check()
        assert "".join(engine.stream_reply("search for news", toolbox=FakeToolbox())) == DEFAULT_REPLY
        assert engine._tools_supported[engine.model] is False
        last = [b for p, b in mock.requests if p == "/api/chat"][-1]
        assert not last.get("tools") and "switched off" in last["messages"][0]["content"]
    finally:
        mock.stop()


# ------------------------------------------------------------------ assistant fast path


def test_open_command_bypasses_the_model(config, mock_ollama, box):
    from core.assistant import Assistant
    from tests.test_assistant import Events, FakeTTS

    config.update({"ollama_host": mock_ollama.url, "voice_enabled": False})
    events = Events()
    assistant = Assistant(config, events, tts=FakeTTS(), tools=box)
    assistant.start()
    try:
        assistant.submit_text("Jarvis, could you open Spotify please")
        events.wait_for(events.finished)
        reply = "".join(p["text"] for p in events.of("assistant_token"))
        assert reply == "Opening Spotify, sir."
        assert box.launched.paths[-1].name == "Spotify.lnk"
        assert events.of("tool_activity")[0]["label"] == "Opening: Spotify"
        assert not [r for r in mock_ollama.requests if r[0] == "/api/chat"], "model must not be called"

        assistant.submit_text("open youtube")
        events.wait_for(lambda: len(events.of("assistant_end")) == 2 and events.states()[-1] == "IDLE")
        assert box.launched.urls[-1] == "https://www.youtube.com"

        assistant.submit_text("open the pod bay doors")
        events.wait_for(lambda: len(events.of("assistant_end")) == 3 and events.states()[-1] == "IDLE")
        assert [r for r in mock_ollama.requests if r[0] == "/api/chat"], "non-file requests go to the model"
    finally:
        assistant.shutdown()


def test_model_tool_calls_reach_the_hud(config, mock_ollama, box, home):
    from core.assistant import Assistant
    from tests.test_assistant import Events, FakeTTS

    config.update({"ollama_host": mock_ollama.url, "voice_enabled": False})
    events = Events()
    assistant = Assistant(config, events, tts=FakeTTS(), tools=box)
    assistant.start()
    try:
        assistant.submit_text("please read budget notes")
        events.wait_for(events.finished)
        assert events.of("tool_activity")[0]["label"] == "Reading: budget notes"
        assert "Rent 1200" in "".join(p["text"] for p in events.of("assistant_token"))
    finally:
        assistant.shutdown()
