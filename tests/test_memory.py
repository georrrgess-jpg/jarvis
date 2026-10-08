"""Long-term memory: the SQLite store, recall ranking, spoken commands, background learning, episodes and resume."""

import time
from datetime import datetime

import pytest

from core.memory import (MemoryEngine, MemoryStore, classify, describe_routine, key_for, looks_secret, parse_memory_command,
                         routine_meta, second_person, today_routines, worth_learning)
from tests.mock_ollama import MockOllama


class FakeLLM:
    def __init__(self, extract=None, summary="The user talked about their dog and planned a walk."):
        self.model = "llama3.2:latest"
        self.online = True
        self.extract = extract or {"memories": []}
        self.summary = summary
        self.json_calls = []
        self.compose_calls = []

    def json_task(self, system, prompt, timeout=120.0):
        self.json_calls.append(prompt)
        return self.extract

    def compose(self, system, prompt, cancel=None, on_progress=None, max_tokens=3000, temperature=0.7):
        self.compose_calls.append(prompt)
        return self.summary


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(tmp_path / "memory.db")
    yield s
    s.close()


@pytest.fixture
def engine(config):
    e = MemoryEngine(config, FakeLLM())
    yield e
    e.stop()


# ----------------------------------------------------------------------------- text helpers
@pytest.mark.parametrize("said, stored", [
    ("I love jazz", "You love jazz."),
    ("my sister is called Ana", "Your sister is called Ana."),
    ("I'm allergic to peanuts!", "You're allergic to peanuts."),
    ("I am training for a marathon", "You are training for a marathon."),
    ("You have a dog called Max.", "You have a dog called Max."),
    ("Max is my dog", "Max is your dog."),
])
def test_second_person(said, stored):
    assert second_person(said) == stored


@pytest.mark.parametrize("text, kind", [
    ("You go to yoga every Tuesday at 6 pm.", "routine"),
    ("You usually walk the dog in the morning.", "routine"),
    ("You're building a website for your mum.", "project"),
    ("You are learning to play the guitar.", "project"),
    ("You love sushi.", "preference"),
    ("Your favourite colour is blue.", "preference"),
    ("You have a golden retriever called Max.", "fact"),
    ("You moved into a new flat in Leeds.", "fact"),
])
def test_classify(text, kind):
    assert classify(text) == kind


def test_keys_let_newer_facts_replace_older_ones():
    assert key_for("Your favourite colour is blue.") == "favorite colour"
    assert key_for("Your dog's name is Max.") == "dog name"
    assert key_for("You live in Madrid.") == "home"
    assert key_for("You love jazz.") == ""


def test_routine_meta_and_description():
    assert routine_meta("You go to yoga every Tuesday and Thursday at 6 pm.") == {"days": [1, 3], "time": "18:00"}
    assert routine_meta("You run on weekdays at 7:30am.") == {"days": [0, 1, 2, 3, 4], "time": "07:30"}
    assert routine_meta("You play football on weekends.")["days"] == [5, 6]
    assert routine_meta("You meditate every morning.") == {"days": list(range(7)), "time": "morning"}
    assert describe_routine({"days": [1, 3], "time": "18:00"}) == "Tue, Thu · 6 PM"
    assert describe_routine({"days": [0, 1, 2, 3, 4], "time": "07:30"}) == "Weekdays · 7:30 AM"
    assert describe_routine({"days": [], "time": ""}) == "Every day"


def test_secrets_are_never_stored(engine):
    assert looks_secret("my password is hunter2") and looks_secret("my card number is 4111 1111 1111 1111")
    memory, status = engine.remember("my bank PIN is 1234")
    assert memory is None and status == "secret"
    assert engine.store.count()["total"] == 0


def test_learning_prefilter():
    assert worth_learning("I have a dog called Max and we go hiking on weekends")
    assert worth_learning("My sister Ana just moved to Madrid")
    assert not worth_learning("open chrome")
    assert not worth_learning("what's the capital of France?")
    assert not worth_learning("my password is swordfish and I like it")


# ----------------------------------------------------------------------------- spoken commands
@pytest.mark.parametrize("text, action, arg", [
    ("remember that my sister is called Ana", "remember", "my sister is called Ana"),
    ("Harper, please remember I love jazz", "remember", "I love jazz"),
    ("don't forget my mum's birthday is May 3rd", "remember", "my mum's birthday is May 3rd"),
    ("forget that I like tea", "forget", "I like tea"),
    ("forget everything you know about me", "forget_all", ""),
    ("wipe your memory", "forget_all", ""),
    ("what do you know about me?", "recall", ""),
    ("what have you learned about me", "recall", ""),
    ("do you remember my dog's name", "recall_about", "dog's name"),
    ("what am I working on?", "projects", ""),
    ("what's my routine today", "routines", ""),
    ("what did we talk about last time?", "episodes", ""),
    ("what's my name", "my_name", ""),
    ("call me Tony", "call_me", "Tony"),
    ("my name is George", "call_me", "George"),
    ("I finished my website project", "project_done", "website"),
])
def test_parse_memory_commands(text, action, arg):
    cmd = parse_memory_command(text)
    assert cmd is not None and cmd.action == action and cmd.text == arg


def test_call_me_title_versus_name():
    assert parse_memory_command("call me Tony").kind == "title"
    assert parse_memory_command("my name is George").kind == "name"


@pytest.mark.parametrize("text", [
    "remind me to call mum at 5", "remember when we went to Paris?", "forget it", "call me back later",
    "do you know how to cook pasta", "do you remember how to fix it", "what's the weather like", "open my notes",
])
def test_not_memory_commands(text):
    assert parse_memory_command(text) is None


# ----------------------------------------------------------------------------- the store
def test_add_dedupes_and_replaces_by_key(store):
    first, status = store.add("preference", "Your favourite colour is blue.")
    assert status == "added"
    same, status = store.add("preference", "Your favourite colour is blue.")
    assert status == "duplicate" and same.id == first.id
    newer, status = store.add("preference", "Your favourite colour is green.")
    assert status == "updated" and newer.id == first.id and newer.text.endswith("green.")
    assert store.count()["total"] == 1


def test_near_duplicates_are_merged(store):
    store.add("fact", "You have a dog called Max.")
    _, status = store.add("fact", "You have a dog called Max!", source="learned")
    assert status == "duplicate"
    longer, status = store.add("fact", "You have a dog called Max, a golden retriever.")
    assert status == "updated" and "golden retriever" in longer.text
    assert store.count()["fact"] == 1


def test_update_pin_delete_restore(store):
    m, _ = store.add("routine", "You go swimming on Mondays at 7 am.")
    assert m.meta == {"days": [0], "time": "07:00"}
    m = store.update(m.id, text="You go swimming on Mondays and Fridays at 7 am.", pinned=True)
    assert m.pinned and m.meta["days"] == [0, 4]
    gone = store.delete(m.id)
    assert store.get(m.id) is None
    back = store.restore(gone.to_dict())
    assert back.text == gone.text and back.pinned and back.meta["days"] == [0, 4]


def test_projects_start_active(store):
    m, _ = store.add("project", "You're building a treehouse.")
    assert m.meta["status"] == "active"


def test_today_routines(store):
    store.add("routine", "You go to yoga every Tuesday at 6 pm.")
    store.add("routine", "You walk the dog every day.")
    tuesday = datetime(2026, 10, 6, 9, 0)  # a Tuesday
    found = today_routines(store.all(), tuesday)
    assert [m.text for m in found] == ["You go to yoga every Tuesday at 6 pm."]  # daily ones don't nag
    assert today_routines(store.all(), datetime(2026, 10, 7)) == []


def test_conversation_log_and_unsummarised(store):
    for i in range(3):
        store.log_turn("s1", "user", f"question {i}")
        store.log_turn("s1", "assistant", f"answer {i}")
    store.log_turn("s2", "user", "hello")
    pending = dict(store.unsummarised(exclude_session="s2"))
    assert list(pending) == ["s1"] and len(pending["s1"]) == 6
    store.add_episode("s1", "The user asked three questions.", pending["s1"])
    assert dict(store.unsummarised(exclude_session="s2")) == {}
    assert store.episodes()[0].turns == 3


# ----------------------------------------------------------------------------- recall
def test_search_ranks_by_relevance(engine):
    for text in ("You have a golden retriever called Max.", "You love jazz, especially Miles Davis.",
                 "Your sister Ana lives in Madrid.", "You're allergic to peanuts."):
        engine.remember(text)
    assert engine.search("what's my dog called")[0].text.startswith("You have a golden retriever")  # "dog" isn't in it...
    hits = engine.search("Miles Davis records")
    assert hits[0].text.startswith("You love jazz")
    assert engine.search("where does Ana live")[0].text.startswith("Your sister Ana")
    assert engine.search("quantum chromodynamics") == []


def test_semantic_recall_with_an_embedding_model(config):
    mock = MockOllama(models=["llama3.2:latest", "nomic-embed-text:latest"]).start()
    try:
        from core.llm import LLMEngine

        config.update({"ollama_host": mock.url})
        llm = LLMEngine(config)
        llm.check()
        e = MemoryEngine(config, llm)
        assert e.pick_embed_model(llm.installed) == "nomic-embed-text:latest"
        e.remember("You have a dog called Max.")
        e.remember("You love jazz.")
        assert e.embed_missing() == 2
        # no shared words, but the (toy) embedding knows a puppy is a dog
        hits = e.search("tell me about my puppy")
        assert hits and hits[0].text == "You have a dog called Max."
        e.embed_model = None
        assert e.search("tell me about my puppy") == []  # keyword recall alone can't make that leap
        assert any(path == "/api/embed" for path, _ in mock.requests)
        e.stop()
    finally:
        mock.stop()


def test_context_block(engine, config):
    config.update({"user_name": "Tony"})
    engine.remember("I love jazz")
    engine.remember("I'm building a website for my mum")
    m, _ = engine.remember("I have a golden retriever called Max")
    engine.store.update(m.id, pinned=True)
    block = engine.context("recommend some music")
    assert "The user's name is Tony." in block
    assert "- You love jazz." in block and "(ongoing)" in block and "golden retriever" in block
    assert "without reciting it" in block


def test_context_empty_when_disabled(engine, config):
    engine.remember("I love jazz")
    config.update({"memory_enabled": False})
    assert engine.context("music") == ""
    assert engine.remember("I love tea") == (None, "disabled")


def test_forget_matching_needs_a_real_match(engine):
    engine.remember("I love jazz")
    engine.remember("I like tea with honey")
    assert engine.forget_matching("cars") is None
    gone = engine.forget_matching("I like tea")
    assert gone.text == "You like tea with honey."
    assert [m.text for m in engine.store.all()] == ["You love jazz."]


def test_finish_project(engine):
    engine.remember("I'm building a website for my mum's bakery")
    done = engine.finish_project("the bakery website")
    assert done.meta["status"] == "done"
    assert engine.finish_project("a spaceship") is None


# ----------------------------------------------------------------------------- learning, episodes, resume
def test_learning_from_conversation(config):
    llm = FakeLLM(extract={"memories": [
        {"kind": "fact", "text": "You have a dog called Max."},
        {"kind": "preference", "text": "You love hiking on weekends."},
        {"kind": "fact", "text": "Your password is hunter2."},  # never stored
        {"kind": "nonsense", "text": "bad kind"},
        {"kind": "fact", "text": "Your name is Tony."},  # goes to settings instead
    ]})
    events = []
    e = MemoryEngine(config, llm, on_event=lambda k, p: events.append((k, p)))
    e.log_exchange("I took my dog Max hiking this weekend, I love it", "That sounds wonderful!")
    e.log_exchange("open chrome", "Opening Chrome.")  # not personal: never sent to the model
    learned = e.learn_now()
    assert sorted(m.text for m in learned) == ["You have a dog called Max.", "You love hiking on weekends."]
    assert all(m.source == "learned" for m in learned)
    assert len(llm.json_calls) == 1 and "open chrome" not in llm.json_calls[0]
    assert config.get("user_name") == "Tony"
    assert [k for k, _ in events].count("memory_learned") == 2
    e.stop()


def test_auto_learn_can_be_switched_off(config):
    config.update({"memory_auto_learn": False})
    llm = FakeLLM(extract={"memories": [{"kind": "fact", "text": "You have a dog."}]})
    e = MemoryEngine(config, llm)
    e.log_exchange("I have a dog called Max and I love him", "Lovely!")
    assert e.learn_now() == [] and llm.json_calls == []
    e.stop()


def test_episode_summaries(config):
    llm = FakeLLM()
    old = MemoryEngine(config, llm)
    old.log_exchange("Tell me about golden retrievers", "They're friendly family dogs.", learn=False)
    old.log_exchange("Should I walk Max twice a day?", "Twice a day is ideal.", learn=False)
    old.stop()
    new = MemoryEngine(config, llm)
    assert new.summarise_pending() == 1
    episode = new.store.episodes()[0]
    assert episode.summary == llm.summary and episode.turns == 2
    assert "Recent conversations:" in new.context("")
    assert new.summarise_pending() == 0  # nothing left to summarise
    new.stop()


def test_short_sessions_are_not_summarised(config):
    llm = FakeLLM()
    old = MemoryEngine(config, llm)
    old.log_exchange("hi", "Hello!", learn=False)
    old.stop()
    new = MemoryEngine(config, llm)
    assert new.summarise_pending() == 0 and llm.compose_calls == []
    new.stop()


def test_restore_recent_conversation(config):
    old = MemoryEngine(config, FakeLLM())
    old.log_exchange("What should I name my puppy?", "How about Max?", learn=False)
    old.stop()
    new = MemoryEngine(config, FakeLLM())
    new.session = "later"
    turns = new.restore_recent()
    assert [t["role"] for t in turns] == ["user", "assistant"] and turns[1]["text"] == "How about Max?"
    config.update({"memory_resume": False})
    assert new.restore_recent() == []
    new.stop()


def test_old_conversations_are_not_resumed(config):
    old = MemoryEngine(config, FakeLLM())
    old.log_exchange("What should I name my puppy?", "How about Max?", learn=False)
    with old.store._lock, old.store._db:
        old.store._db.execute("UPDATE turns SET ts=?", (time.time() - 5 * 3600,))
    old.stop()
    new = MemoryEngine(config, FakeLLM())
    new.session = "later"
    assert new.restore_recent() == []
    new.stop()


def test_clear_and_export(engine):
    engine.remember("I love jazz")
    engine.log_exchange("hi there, I love jazz", "Me too!", learn=False)
    data = engine.export()
    assert data["memories"][0]["text"] == "You love jazz." and "exported" in data
    engine.clear()
    counts = engine.store.count()
    assert counts["total"] == 0 and counts["turns"] == 0


def test_store_survives_restart(tmp_path):
    first = MemoryStore(tmp_path / "m.db")
    first.add("fact", "You have a cat called Luna.")
    first.close()
    second = MemoryStore(tmp_path / "m.db")
    assert [m.text for m in second.all()] == ["You have a cat called Luna."]
    second.close()
