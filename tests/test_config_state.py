import json

import pytest

from core.config import DEFAULTS, Config
from core.state import State, StateMachine


def test_defaults_when_missing(config):
    assert config.as_dict() == DEFAULTS
    assert config["voice"] == "en-GB-RyanNeural"


def test_update_coerces_clamps_and_persists(tmp_path):
    cfg = Config(tmp_path / "c.json")
    applied = cfg.update({"speech_rate": "75", "voice_enabled": "false", "sfx_volume": 0.3, "unknown": 1})
    assert applied == {"speech_rate": 50, "voice_enabled": False, "sfx_volume": 0.3}
    reloaded = Config(tmp_path / "c.json")
    assert reloaded["speech_rate"] == 50 and reloaded["voice_enabled"] is False


def test_update_returns_only_changed_keys(config):
    assert config.update({"voice": DEFAULTS["voice"]}) == {}


def test_invalid_choice_rejected(config):
    with pytest.raises(ValueError):
        config.update({"stt_engine": "cloud-paid"})
    assert config["stt_engine"] == "auto"


def test_nan_rejected(config):
    with pytest.raises(ValueError):
        config.update({"temperature": float("nan")})


def test_corrupt_file_falls_back_to_defaults(tmp_path):
    path = tmp_path / "c.json"
    path.write_text("{not json")
    assert Config(path).as_dict() == DEFAULTS
    path.write_text(json.dumps({"speech_rate": "fast", "user_title": "boss"}))
    cfg = Config(path)
    assert cfg["speech_rate"] == 0 and cfg["user_title"] == "boss"


def test_state_machine_happy_path_and_listener():
    seen = []
    sm = StateMachine(lambda old, new, reason: seen.append((old, new)))
    for s in (State.LISTENING, State.THINKING, State.SPEAKING, State.IDLE):
        assert sm.transition(s)
    assert [n for _, n in seen] == [State.LISTENING, State.THINKING, State.SPEAKING, State.IDLE]


def test_state_machine_rejects_illegal_transition():
    sm = StateMachine()
    sm.transition(State.LISTENING)
    assert not sm.transition(State.SPEAKING)
    assert sm.state == State.LISTENING
    assert sm.transition(State.IDLE)


def test_barge_in_allowed_while_speaking():
    sm = StateMachine()
    sm.transition(State.SPEAKING)
    assert sm.transition(State.LISTENING)


def test_listener_errors_do_not_break_transitions():
    def boom(*_):
        raise RuntimeError("ui gone")

    sm = StateMachine(boom)
    assert sm.transition(State.THINKING)
    assert sm.state == State.THINKING
