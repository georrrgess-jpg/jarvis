"""Personalities and long-term memory, end to end through the real Assistant (mock Ollama, fake voice)."""

import json
import time
from datetime import datetime

import pytest

from core.language import voice_for
from core.memory import MemoryEngine
from core.personas import PERSONAS, address_for, asks_who, get_persona, parse_switch, system_prompt
from tests.test_assistant import make  # noqa: F401  (a fixture)


# ----------------------------------------------------------------------------- personalities on their own
@pytest.mark.parametrize("text, pid", [
    ("switch to Harper", "harper"), ("Switch over to F.R.I.D.A.Y.", "friday"), ("let me talk to Sage", "sage"),
    ("can I speak with harper please", "harper"), ("Harper, are you there?", "harper"), ("bring back Jarvis", "jarvis"),
    ("change your personality to sage", "sage"), ("I want to talk to Harper", "harper"), ("be Friday", "friday"),
    ("hey jarvis, switch to harper", "harper"),
])
def test_parse_switch(text, pid):
    assert parse_switch(text) == pid


@pytest.mark.parametrize("text", ["switch to the next song", "talk to me", "play Friday by Rebecca Black", "what day is it, friday?",
                                  "switch to dark mode", "open sage accounting"])
def test_not_a_switch(text):
    assert parse_switch(text) is None


def test_asks_who():
    assert asks_who("who am I talking to?") and asks_who("what personalities do you have")
    assert not asks_who("who are you")  # that one is small talk, answered in character


def test_every_persona_is_complete():
    for p in PERSONAS.values():
        assert p.intro and p.sample and p.greeting and p.voice.endswith("Neural") and p.gender in ("male", "female")
        prompt = system_prompt(p, "friend", "- abilities")
        assert prompt.startswith(p.identity) and "Reply in the same language" in prompt and "- abilities" in prompt
        for g in p.greeting:
            g.format(part="morning", title="friend")  # no stray placeholders


def test_harper_is_kind_and_conversational():
    harper = get_persona("harper")
    prompt = system_prompt(harper, "Tony", "")
    assert "kind" in prompt and "follow-up question" in prompt and "remember" in prompt.lower()
    assert harper.line("thanks", ["x"], "Tony") != "x"


def test_address(config):
    config.update({"user_title": "ma'am", "user_name": ""})
    assert address_for(get_persona("jarvis"), config) == "ma'am"
    assert address_for(get_persona("harper"), config) == "friend"
    assert address_for(get_persona("friday"), config) == "boss"
    config.update({"user_name": "Pepper Potts"})
    assert address_for(get_persona("harper"), config) == "Pepper"


def test_female_personalities_keep_a_female_voice_abroad():
    assert voice_for("es", "en-US-AvaNeural", "female") == "es-ES-ElviraNeural"
    assert voice_for("es", "en-GB-RyanNeural", "male") == "es-ES-AlvaroNeural"
    assert voice_for("fr", "fr-FR-DeniseNeural", "female") == "fr-FR-DeniseNeural"  # already speaks it


# ----------------------------------------------------------------------------- through the assistant
def ask(assistant, events, text, timeout=15.0):
    """Send ``text`` and return the full reply once the turn has finished."""
    before = len(events.of("assistant_end"))
    assert assistant.submit_text(text)
    events.wait_for(lambda: len(events.of("assistant_end")) > before and events.states()[-1:] == ["IDLE"], timeout)
    end = events.of("assistant_end")[-1]
    return "".join(p["text"] for p in events.of("assistant_token") if p["id"] == end["id"])


def last_system_prompt(mock):
    chats = [body for path, body in mock.requests if path == "/api/chat" and body.get("format") != "json"]
    return chats[-1]["messages"][0]["content"]


def test_switching_personality_by_voice(make, mock_ollama):
    assistant, events = make()
    reply = ask(assistant, events, "switch to Harper")
    assert reply.startswith("Hi friend, Harper here!")
    cfg = assistant.config
    assert cfg.get("persona") == "harper" and cfg.get("voice") == "en-US-AvaNeural" and cfg.get("theme") == "rose"
    persona = events.of("persona")[-1]
    assert persona["id"] == "harper" and persona["display"] == "HARPER" and persona["address"] == "friend"
    assert assistant.tts.spoken[-1].startswith("What's on your mind")

    reply = ask(assistant, events, "thank you")
    assert reply in [r.format(title="friend") for r in PERSONAS["harper"].lines["thanks"]]

    ask(assistant, events, "explain the arc reactor")
    prompt = last_system_prompt(mock_ollama)
    assert prompt.startswith("You are Harper") and "J.A.R.V.I.S." not in prompt

    assert ask(assistant, events, "switch to harper").startswith("I'm right here")
    assert ask(assistant, events, "bring back jarvis") == "At your service, sir. J.A.R.V.I.S. is back online."
    assert cfg.get("voice") == "en-GB-RyanNeural" and cfg.get("theme") == "arc"


def test_persona_theme_can_stay_put(make):
    assistant, events = make(persona_theme=False, theme="violet")
    ask(assistant, events, "let me talk to Friday")
    assert assistant.config.get("persona") == "friday" and assistant.config.get("theme") == "violet"


def test_switch_from_settings_and_list(make):
    assistant, events = make()
    assistant.update_settings({"persona": "sage"})
    assert assistant.persona.id == "sage" and assistant.config.get("voice") == "en-US-AndrewNeural"
    assert events.of("persona")[-1]["id"] == "sage"
    listed = {p["id"]: p for p in assistant.persona_list()}
    assert listed["sage"]["active"] and not listed["jarvis"]["active"] and len(listed) == 4
    reply = ask(assistant, events, "what personalities do you have?")
    assert "Harper" in reply and "Sage right now" in reply and "switch to Harper" in reply


def test_greeting_is_in_character(make):
    assistant, events = make(persona="harper", user_name="Tony")
    assistant.boot_complete()
    events.wait_for(lambda: assistant.tts.spoken, 10)
    spoken = " ".join(assistant.tts.spoken)
    assert "Tony" in spoken and "All systems are online" not in spoken


# ----------------------------------------------------------------------------- memory through the assistant
def test_remember_recall_and_forget(make, mock_ollama):
    assistant, events = make()
    reply = ask(assistant, events, "remember that my dog is called Max")
    assert reply == "Very good, sir. I'll remember that your dog is called Max." or reply == "Noted, sir. I'll remember that your dog is called Max."
    learned = events.of("memory_learned")[-1]
    assert learned["memory"]["text"] == "Your dog is called Max." and learned["status"] == "added"
    assert ask(assistant, events, "remember that my dog is called Max").startswith("I already know that")

    ask(assistant, events, "explain the arc reactor")
    prompt = last_system_prompt(mock_ollama)
    assert "Long-term memory" in prompt and "- Your dog is called Max." in prompt

    reply = ask(assistant, events, "what do you know about me?")
    assert "your dog is called Max" in reply and events.of("memory_open")
    assert ask(assistant, events, "do you remember my dog's name?") == "Yes: your dog is called Max."

    reply = ask(assistant, events, "forget that my dog is called Max")
    assert reply == "Done, sir. I've forgotten that your dog is called Max."
    assert events.of("memory_forgotten")[-1]["memory"]["text"] == "Your dog is called Max."
    assert assistant.memory.store.count()["total"] == 0
    assert "anything about" in ask(assistant, events, "forget that I like cars")


def test_harper_remembers_warmly(make):
    assistant, events = make(persona="harper", user_name="Tony")
    reply = ask(assistant, events, "remember that I'm allergic to peanuts")
    options = [r.format(title="Tony", what="you're allergic to peanuts") for r in PERSONAS["harper"].lines["remembered"]]
    assert reply in options


def test_forget_everything_asks_first(make):
    assistant, events = make()
    ask(assistant, events, "remember that I love jazz")
    reply = ask(assistant, events, "forget everything")
    assert reply == ("Are you sure, sir? That erases the one thing I remember about you, and our past conversations. "
                     "Say yes to confirm.")
    assert ask(assistant, events, "no").startswith("Phew")
    assert assistant.memory.store.count()["total"] == 1
    ask(assistant, events, "forget everything you know about me")
    assert ask(assistant, events, "yes").startswith("Done, sir. I've forgotten everything")
    assert assistant.memory.store.count()["total"] == 0


def test_secrets_are_refused(make):
    assistant, events = make()
    reply = ask(assistant, events, "remember that my password is hunter2")
    assert "password manager" in reply and assistant.memory.store.count()["total"] == 0


def test_call_me_and_my_name(make):
    assistant, events = make()
    assert ask(assistant, events, "what's my name?").startswith("You haven't told me your name yet")
    assert ask(assistant, events, "call me Tony") == "Tony it is. I'll call you that from now on."
    assert assistant.config.get("user_title") == "Tony" and assistant.config.get("user_name") == "Tony"
    assert ask(assistant, events, "what time is it").rstrip(".").endswith("Tony")
    assert ask(assistant, events, "call me sir") == "Very well, sir."
    assert ask(assistant, events, "what's my name").startswith("You're Tony")
    ask(assistant, events, "my name is Pepper")
    assert assistant.config.get("user_name") == "Pepper" and assistant.config.get("user_title") == "sir"


def test_projects_and_routines(make):
    assistant, events = make()
    assert "haven't told me about any ongoing projects" in ask(assistant, events, "what am I working on?")
    ask(assistant, events, "remember that I'm building a website for my mum's bakery")
    today = datetime.now().strftime("%A")
    ask(assistant, events, f"remember that I go to yoga every {today} at 6 pm")
    assert "a website for your mum's bakery" in ask(assistant, events, "what am I working on?")
    assert ask(assistant, events, "what's my routine today?") == f"Today, sir: you go to yoga every {today} at 6 pm."
    reply = ask(assistant, events, "I finished the bakery website project")
    assert "marked" in reply and "done" in reply
    assert "haven't told me about any ongoing projects" in ask(assistant, events, "what am I working on?")


def test_memory_can_be_switched_off(make, mock_ollama):
    assistant, events = make(memory_enabled=False)
    assert "switched off" in ask(assistant, events, "remember that I love jazz")
    ask(assistant, events, "explain the arc reactor")
    assert "Long-term memory" not in last_system_prompt(mock_ollama)


def test_learns_in_the_background(make, mock_ollama):
    def responder(body):
        if body.get("format") == "json":
            return json.dumps({"memories": [{"kind": "preference", "text": "You love hiking with your dog on weekends."}]})
        return "That sounds like a lovely way to spend a weekend."

    mock_ollama.responder = responder
    assistant, events = make()
    ask(assistant, events, "I love hiking with my dog on weekends")
    events.wait_for(lambda: events.of("memory_learned"), 15)
    assert events.of("memory_learned")[-1]["memory"]["text"] == "You love hiking with your dog on weekends."
    assert events.of("memory_learned")[-1]["memory"]["source"] == "learned"
    ask(assistant, events, "what's a good trail?")
    assert "You love hiking with your dog on weekends." in last_system_prompt(mock_ollama)


def test_memory_commands_are_not_learned_twice(make, mock_ollama):
    assistant, events = make()
    ask(assistant, events, "remember that I love hiking with my dog")
    time.sleep(0.6)
    assert not any(body.get("format") == "json" for path, body in mock_ollama.requests if path == "/api/chat")


def test_carries_on_after_a_restart(make, config, mock_ollama):
    earlier = MemoryEngine(config)
    earlier.log_exchange("What should I name my puppy?", "How about Max? It suits a golden retriever.", learn=False)
    earlier.stop()
    assistant, events = make()
    payload = assistant.boot_payload()
    assert [t["role"] for t in payload["restored"]] == ["user", "assistant"]
    assert assistant.llm.history_turns == 1
    assistant.boot_complete()
    deadline = time.time() + 10  # the greeting is spoken sentence by sentence
    while time.time() < deadline and "Welcome back" not in " ".join(assistant.tts.spoken):
        time.sleep(0.05)
    assert "Welcome back" in " ".join(assistant.tts.spoken)
    ask(assistant, events, "explain the arc reactor")
    chats = [b for p, b in mock_ollama.requests if p == "/api/chat" and b.get("format") != "json"]
    assert any(m["content"] == "What should I name my puppy?" for m in chats[-1]["messages"])


def test_routine_reminder_in_the_greeting(make):
    assistant, events = make()
    today = datetime.now().strftime("%A")
    assistant.memory.remember(f"I go swimming every {today} at 7 pm")
    assistant.boot_complete()
    events.wait_for(lambda: assistant.tts.spoken, 10)
    deadline = time.time() + 5
    while time.time() < deadline and "reminder" not in " ".join(assistant.tts.spoken):
        time.sleep(0.05)
    assert f"A quick reminder for today: you go swimming every {today} at 7 pm." in " ".join(assistant.tts.spoken)


def test_memory_core_api(make):
    assistant, events = make()
    added = assistant.memory_add("preference", "I love jazz")
    assert added["status"] == "added" and added["memory"]["text"] == "You love jazz."
    mid = added["memory"]["id"]
    updated = assistant.memory_update(mid, {"text": "I love jazz and blues", "pinned": True})
    assert updated["text"] == "You love jazz and blues." and updated["pinned"]
    overview = assistant.memory_overview()
    assert overview["stats"]["preference"] == 1 and overview["memories"][0]["id"] == mid
    gone = assistant.memory_delete(mid)
    assert assistant.memory_overview()["memories"] == []
    assistant.memory_restore(gone)
    assert assistant.memory_overview()["memories"][0]["text"] == "You love jazz and blues."
    assistant.memory_clear()
    assert assistant.memory_overview()["stats"]["total"] == 0 and events.of("memory_cleared")
    with pytest.raises(ValueError):
        assistant.memory_add("fact", "   ")


# ----------------------------------------------------------------------------- your own personalities
NOVA = {"name": "Nova", "description": "A cheerful space explorer who loves fun facts about the universe. Keeps answers short.",
        "voice": "en-AU-NatashaNeural", "gender": "Female", "color": "#ffaa00", "address": ""}


def test_validate_custom():
    from core.personas import validate_custom

    clean = validate_custom(NOVA, [])
    assert clean["id"] == "my-nova" and clean["color"] == "#FFAA00" and clean["gender"] == "female"
    for bad, why in (({**NOVA, "name": ""}, "name"), ({**NOVA, "name": "Harper"}, "already"), ({**NOVA, "description": "hi"}, "Describe"),
                     ({**NOVA, "voice": "robot"}, "voice"), ({**NOVA, "name": "R2-D2!!"}, "name")):
        with pytest.raises(ValueError, match=why):
            validate_custom(bad, [])
    assert validate_custom({**NOVA, "name": "Nova"}, [{"id": "my-nova", "name": "Nova2"}])["id"] == "my-nova-2"
    braces = validate_custom({**NOVA, "description": "Says {title} a lot and {weird} things"}, [])
    assert "{" not in braces["description"]


def test_create_switch_and_talk_to_a_custom_personality(make, mock_ollama):
    assistant, events = make(wake_learn_auto=False)
    info = assistant.save_persona(NOVA)
    assert info["id"] == "my-nova" and info["custom"] and info["display"] == "NOVA" and info["color"] == "#FFAA00"
    assert any(p["id"] == "my-nova" for p in assistant.persona_list())
    assert events.of("personas")
    reply = ask(assistant, events, "switch to Nova")
    assert reply == "Hi friend, Nova here. What can I do for you?"
    cfg = assistant.config
    assert cfg.get("persona") == "my-nova" and cfg.get("voice") == "en-AU-NatashaNeural" and cfg.get("theme") == "#FFAA00"
    ask(assistant, events, "explain the arc reactor")
    prompt = last_system_prompt(mock_ollama)
    assert prompt.startswith("You are Nova") and "cheerful space explorer" in prompt
    assert ask(assistant, events, "who are you?").startswith("I'm Nova")
    assert assistant.persona.gender == "female"  # female voices in other languages too

    # it survives a restart (stored in settings)
    from core.config import Config
    from core.personas import PERSONAS, load_custom
    again = Config(cfg.path)
    load_custom(again.get("custom_personas"))
    assert again.get("persona") == "my-nova" and "my-nova" in PERSONAS


def test_edit_and_delete_a_custom_personality(make):
    assistant, events = make(wake_learn_auto=False)
    assistant.save_persona(NOVA)
    assistant.set_persona("my-nova", announce=False)
    edited = assistant.save_persona({**NOVA, "id": "my-nova", "voice": "en-GB-SoniaNeural", "color": "#00ff88"})
    assert edited["voice"] == "en-GB-SoniaNeural" and assistant.config.get("voice") == "en-GB-SoniaNeural"
    assert assistant.config.get("theme") == "#00FF88"  # editing the active one applies it at once
    assistant.delete_persona("my-nova")
    assert assistant.persona.id == "jarvis" and not any(p["id"] == "my-nova" for p in assistant.persona_list())
    assert parse_switch("switch to Nova") is None
    with pytest.raises(ValueError):
        assistant.delete_persona("harper")


def test_personality_colours(make):
    assistant, events = make()
    assistant.set_persona("harper", announce=False)
    assert assistant.config.get("theme") == "rose"
    info = assistant.set_persona_color("harper", "#3366ff")
    assert info["color"] == "#3366FF" and info["custom_color"] and assistant.config.get("theme") == "#3366FF"
    assistant.set_persona("jarvis", announce=False)
    assert assistant.config.get("theme") == "arc"
    assistant.set_persona("harper", announce=False)
    assert assistant.config.get("theme") == "#3366FF"  # remembered per personality
    assistant.set_persona_color("harper", "violet")  # a preset works too
    assert assistant.config.get("theme") == "violet"
    assistant.set_persona_color("harper", None)  # back to its own
    assert assistant.config.get("theme") == "rose"
    with pytest.raises(ValueError):
        assistant.set_persona_color("harper", "not a colour")


def test_wake_word_follows_the_personality(make, tmp_path):
    from core.wakelearn import TinyNet, WakeModel, model_path

    assistant, events = make(wake_learn_auto=False)
    assert assistant._wake_words() == (True, [])
    assistant.set_persona("harper", announce=False)
    assert assistant._wake_words() == (True, [])  # nothing learned yet: "Hey Jarvis" keeps working
    status = assistant.persona_info()["wake"]
    assert status["phrase"] == "Hey Harper" and status["state"] == "missing"

    net = TinyNet(hidden=4)
    net._init(16 * 96)
    WakeModel("Harper", "Hey Harper", net, 0.8, {"held_out_recall": 0.9}).save(model_path(assistant.wakewords.directory, "Harper"))
    assistant.wakewords._models.clear()
    use_jarvis, models = assistant._wake_words()
    assert use_jarvis and [m.name for m in models] == ["Harper"]
    assistant.update_settings({"wake_jarvis_always": False})
    assert assistant._wake_words()[0] is False
    st = assistant.wake_status()
    assert st["phrase"] == "Hey Harper" and st["phrases"] == ["Hey Harper"]
    assert assistant.persona_info()["wake"]["state"] == "ready"


def test_switching_starts_learning_the_name(make):
    assistant, events = make(wake_learn_auto=True)
    started = []
    assistant.wakewords.learn = lambda name, with_my_voice=False: started.append(name) or True
    assistant.set_persona("friday", announce=False)
    assert started == ["Friday"]


def test_wake_prefix_is_stripped_for_any_name(make):
    from core.assistant import _strip_wake

    assistant, _ = make(wake_learn_auto=False)
    assistant.save_persona(NOVA)
    assert _strip_wake("Hey Harper, what time is it") == "what time is it"
    assert _strip_wake("hey nova open spotify") == "open spotify"
    assert _strip_wake("Jarvis what's up") == "what's up"
    assert _strip_wake("harbour views") == "harbour views"
