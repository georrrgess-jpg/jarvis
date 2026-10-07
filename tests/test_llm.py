import socket
import threading

import pytest

from core.llm import LLMConnectionError, LLMEngine, LLMModelError, resolve_model
from tests.mock_ollama import DEFAULT_REPLY, MockOllama


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.parametrize(
    "wanted, available, expected",
    [
        ("llama3.2", ["mistral:latest", "llama3.2:latest"], "llama3.2:latest"),
        ("llama3.2:1b", ["llama3.2:3b"], "llama3.2:3b"),
        ("phi4", ["nomic-embed-text:latest", "mistral:7b"], "mistral:7b"),
        ("", ["zephyr:latest"], "zephyr:latest"),
        ("llama3.2", ["nomic-embed-text:latest"], None),
        ("llama3.2", [], None),
    ],
)
def test_resolve_model(wanted, available, expected):
    assert resolve_model(wanted, available) == expected


def test_check_online_lists_models(config, mock_ollama):
    config.update({"ollama_host": mock_ollama.url})
    status = LLMEngine(config).check()
    assert status.online and status.model == "llama3.2:latest"
    assert status.models == ["llama3.2:latest"] and status.error is None


def test_check_offline(config):
    config.update({"ollama_host": f"http://127.0.0.1:{free_port()}"})
    engine = LLMEngine(config)
    status = engine.check()
    assert not status.online and "not reachable" in status.error
    assert status.download_url.startswith("https://ollama.com")
    with pytest.raises(LLMModelError):
        list(engine.stream_reply("hello"))


def test_check_online_without_models(config):
    mock = MockOllama(models=[]).start()
    try:
        config.update({"ollama_host": mock.url})
        status = LLMEngine(config).check()
        assert status.online and status.model is None and "No chat model" in status.error
    finally:
        mock.stop()


def test_stream_reply_and_memory(config, mock_ollama):
    config.update({"ollama_host": mock_ollama.url, "user_title": "boss", "custom_instructions": "Be terse."})
    engine = LLMEngine(config)
    engine.check()
    tokens = list(engine.stream_reply("status report"))
    assert len(tokens) > 5 and "".join(tokens) == DEFAULT_REPLY
    assert engine.history_turns == 1 and engine.last_first_token_ms is not None

    list(engine.stream_reply("and the suit?"))
    path, body = [r for r in mock_ollama.requests if r[0] == "/api/chat"][-1]
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["system", "user", "assistant", "user"]
    system = body["messages"][0]["content"]
    assert '"boss"' in system and "Be terse." in system
    assert body["options"]["temperature"] == 0.7


def test_history_is_trimmed(config, mock_ollama):
    config.update({"ollama_host": mock_ollama.url, "max_history_turns": 2})
    engine = LLMEngine(config)
    engine.check()
    for i in range(4):
        list(engine.stream_reply(f"q{i}"))
    assert engine.history_turns == 2


def test_cancel_stops_stream_and_marks_memory(config):
    mock = MockOllama(token_delay=0.02).start()
    try:
        config.update({"ollama_host": mock.url})
        engine = LLMEngine(config)
        engine.check()
        cancel = threading.Event()
        got = []
        for tok in engine.stream_reply("go", cancel):
            got.append(tok)
            if len(got) == 3:
                cancel.set()
        assert len(got) == 3
        assert engine._history[-1]["content"].endswith("[interrupted]")
    finally:
        mock.stop()


def test_missing_model_raises_model_error(config, mock_ollama):
    config.update({"ollama_host": mock_ollama.url})
    engine = LLMEngine(config)
    engine.check()
    engine.model = "ghost:latest"
    with pytest.raises(LLMModelError):
        list(engine.stream_reply("hi"))


def test_connection_lost_raises_connection_error(config, mock_ollama):
    config.update({"ollama_host": mock_ollama.url})
    engine = LLMEngine(config)
    engine.check()
    config.update({"ollama_host": f"http://127.0.0.1:{free_port()}"})
    with pytest.raises(LLMConnectionError):
        list(engine.stream_reply("hi"))
    assert engine.online is False


def test_pull_reports_progress(config):
    mock = MockOllama(models=[]).start()
    try:
        config.update({"ollama_host": mock.url})
        engine = LLMEngine(config)
        seen = []
        engine.pull("llama3.2", lambda s, c, t: seen.append((s, c, t)), threading.Event())
        assert seen[0][0] == "pulling manifest" and seen[-1][0] == "success"
        assert any(t and c == t for _, c, t in seen)
        assert engine.check().model == "llama3.2:latest"
    finally:
        mock.stop()
