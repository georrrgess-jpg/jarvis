import time

import pytest

from core.feedback import Corrections, complete_command, learn_rule, parse_feedback
from tests.test_assistant import make  # noqa: F401  (a fixture)


def test_parse_feedback():
    for t in ("that was wrong", "No, that's wrong.", "you misheard me", "wrong", "that's not what I said", "not that one", "Wrong app"):
        assert parse_feedback(t).kind == "wrong", t
    fb = parse_feedback("no, I said open Discord")
    assert fb.kind == "meant" and fb.meant == "open Discord"
    assert parse_feedback("I meant Spotify").meant == "Spotify"
    for t in ("open discord", "what's wrong with my code", "is that wrong?", "the wrong trousers"):
        assert parse_feedback(t) is None, t


def test_learn_the_smallest_change():
    assert learn_rule("open this cord", "open discord") == {"heard": "this cord", "meant": "discord", "whole": False}
    assert learn_rule("play some music by cold play", "play some music by coldplay") == {"heard": "cold play", "meant": "coldplay", "whole": False}
    # added words: only for that exact sentence
    assert learn_rule("play lo-fi", "play lo-fi on youtube") == {"heard": "play lo fi", "meant": "play lo fi on youtube", "whole": True}
    # a little word is never rewritten everywhere
    assert learn_rule("set a timer to ten minutes", "set a timer for ten minutes")["whole"] is True
    assert learn_rule("open discord", "Open Discord") is None


def test_complete_command():
    assert complete_command("open this cord", "discord") == "open discord"
    assert complete_command("open this cord", "play some jazz") == "play some jazz"
    assert complete_command("what's the time", "the date") == "the date"


def test_rules_apply_as_whole_words_and_only_to_speech_when_learnt_from_speech(config):
    c = Corrections(config)
    c.add({"heard": "this cord", "meant": "discord", "whole": False}, voice=True)
    assert c.apply("open this cord please", voice=True)[0] == "open discord please"
    assert c.apply("open this cord please", voice=False)[0] == "open this cord please"  # typed: it was what they meant
    assert c.apply("open this cordless phone shop", voice=True)[0] == "open this cordless phone shop"  # not inside a word
    c.add({"heard": "discord", "meant": "discord ptb", "whole": False}, voice=False)
    assert c.apply("open discord ptb", voice=False)[0] == "open discord ptb"  # no "discord ptb ptb"
    rules = c.all()
    assert rules[0]["uses"] >= 1 and c.remove(rules[0]["id"]) and len(c.all()) == 1


def ask(assistant, events, text, source="text"):
    n = len(events.of("assistant_end"))
    assistant.submit_text(text, source=source)
    events.wait_for(lambda: len(events.of("assistant_end")) >= n + 1 and events.states()[-1] == "IDLE", 20)
    end = events.of("assistant_end")[n]
    return "".join(t["text"] for t in events.of("assistant_token") if t["id"] == end["id"])


@pytest.fixture
def jarvis(make, no_real_browser):
    assistant, events = make()
    opened = []
    assistant.tools.open_target = lambda target, **k: opened.append(target) or {"name": target.title(), "kind": "app"}
    return assistant, events, opened


def test_that_was_wrong_asks_learns_and_does_the_right_thing(jarvis):
    assistant, events, opened = jarvis
    assert ask(assistant, events, "open this cord", source="voice") == "Opening This Cord, sir."
    reply = ask(assistant, events, "that was wrong", source="voice")
    assert reply.startswith("Sorry, sir. I heard “open this cord” and opened This Cord") and reply.endswith("What did you mean?")
    n = len(events.of("assistant_end"))
    assert ask(assistant, events, "discord", source="voice") == "Sorry about that, sir. I'll remember that “this cord” means “discord”."
    events.wait_for(lambda: len(events.of("assistant_end")) >= n + 2 and events.states()[-1] == "IDLE", 20)
    assert opened[-1] == "discord"  # and it did what was meant
    # next time it's understood straight away
    assert ask(assistant, events, "open this cord", source="voice") == "Opening Discord, sir."
    assert opened[-1] == "discord"
    assert any("from your corrections" in m["text"] for m in events.of("system_message"))
    assert assistant.corrections_list()["items"][0]["heard"] == "this cord"


def test_i_meant_in_one_go_and_never_mind(jarvis):
    assistant, events, opened = jarvis
    ask(assistant, events, "open spot if i", source="voice")
    n = len(events.of("assistant_end"))
    assert "remember that “spot if i” means “spotify”" in ask(assistant, events, "no, I said open spotify", source="voice")
    events.wait_for(lambda: len(events.of("assistant_end")) >= n + 2 and events.states()[-1] == "IDLE", 20)
    assert opened[-1] == "spotify"
    ask(assistant, events, "open calculator")
    assert "What did you mean?" in ask(assistant, events, "wrong")
    assert ask(assistant, events, "never mind") == "No problem, sir."
    assert len(assistant.corrections.all()) == 1


def test_wrong_about_a_chat_answer_goes_to_the_model(jarvis):
    assistant, events, opened = jarvis
    ask(assistant, events, "tell me a fun fact about owls")
    reply = ask(assistant, events, "that's wrong")
    assert "What did you mean?" not in reply and not assistant.corrections.all()
