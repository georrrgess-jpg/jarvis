"""Protocols: named command lists made by voice or on screen and run with "Jarvis, run <name>"."""

import time
from datetime import datetime

import pytest

from core.protocols import (ProtocolError, ProtocolStore, describe_schedule, due, parse_protocol_command, parse_schedule,
                            recording_reply, split_steps, step_kind)
from tests.test_assistant import make  # noqa: F401  (a fixture)
from tests.test_personas import ask


def known(*names):
    keys = {n.lower() for n in names}
    return lambda name: name.lower().removeprefix("the ").removesuffix(" protocol") in keys


# ----------------------------------------------------------------------------- understanding
@pytest.mark.parametrize("text, name, steps", [
    ("Create a protocol called Morning: open Spotify, then what's the weather, then open Gmail", "Morning",
     ["open Spotify", "what's the weather", "open Gmail"]),
    ("make a protocol named Bedtime that turns the volume down to 20 and locks the computer", "Bedtime",
     ["turn the volume down to 20", "lock the computer"]),
    ("create a house party protocol: play some music, set the volume to 80", "House Party", ["play some music", "set the volume to 80"]),
    ("new protocol called Focus; mute; open Notion; wait 5 seconds; say time to work", "Focus",
     ["mute", "open Notion", "wait 5 seconds", "say time to work"]),
])
def test_creating_in_one_go(text, name, steps):
    cmd = parse_protocol_command(text)
    assert cmd.action == "create" and cmd.name == name and cmd.steps == steps


@pytest.mark.parametrize("text, name", [("create a new protocol called work mode", "Work Mode"), ("new protocol", ""),
                                        ("make a protocol", ""), ("set up a gaming protocol", "Gaming")])
def test_starting_to_record(text, name):
    cmd = parse_protocol_command(text)
    assert cmd.action == "record" and cmd.name == name


@pytest.mark.parametrize("text, name", [
    ("run morning", "Morning"), ("run the house party protocol", "House Party"), ("initiate house party protocol", "House Party"),
    ("house party protocol", "House Party"), ("execute protocol work mode", "Work Mode"), ("start the morning protocol", "Morning"),
    ("engage the morning protocol now", "Morning"), ("activate morning", "Morning"), ("kick off morning please", "Morning"),
])
def test_running(text, name):
    cmd = parse_protocol_command(text, find=known("morning", "house party", "work mode"))
    assert cmd.action == "run" and cmd.name == name


@pytest.mark.parametrize("text", ["run notepad", "start spotify", "what is a protocol", "tell me about protocols in networking",
                                  "what's a good protocol for studying", "open chrome", "explain the TCP protocol"])
def test_not_about_my_protocols(text):
    assert parse_protocol_command(text, find=known("morning")) is None


def test_unknown_names_still_count_when_the_word_protocol_is_used():
    cmd = parse_protocol_command("run the gaming protocol", find=known("morning"))
    assert cmd.action == "run" and cmd.name == "Gaming" and cmd.explicit


@pytest.mark.parametrize("text, action, extra", [
    ("add open discord to the morning protocol", "add", {"name": "Morning", "steps": ["open discord"]}),
    ("add a step to the morning protocol: check my email", "add", {"name": "Morning", "steps": ["check my email"]}),
    ("remove the last step from the morning protocol", "remove_step", {"name": "Morning", "index": None}),
    ("delete step 2 from morning protocol", "remove_step", {"name": "Morning", "index": 2}),
    ("remove the third step from the morning protocol", "remove_step", {"name": "Morning", "index": 3}),
    ("delete the morning protocol", "delete", {"name": "Morning"}),
    ("list my protocols", "list", {}), ("what protocols do I have", "list", {}),
    ("what's in the morning protocol", "show", {"name": "Morning"}),
    ("stop the protocol", "stop", {}), ("cancel protocol", "stop", {}), ("abort the morning protocol", "stop", {"name": "Morning"}),
    ("rename the morning protocol to Sunrise", "rename", {"name": "Morning", "new_name": "Sunrise"}),
    ("schedule the morning protocol for 7:30 am every weekday", "schedule",
     {"name": "Morning", "schedule": {"time": "07:30", "days": [0, 1, 2, 3, 4], "enabled": True}}),
    ("run the morning protocol every day at 7pm", "schedule", {"schedule": {"time": "19:00", "days": list(range(7)), "enabled": True}}),
    ("don't run the morning protocol automatically", "unschedule", {"name": "Morning"}),
])
def test_managing(text, action, extra):
    cmd = parse_protocol_command(text, find=known("morning"))
    assert cmd is not None and cmd.action == action, cmd
    for key, value in extra.items():
        assert getattr(cmd, key) == value


def test_splitting_steps_keeps_everyday_phrases_whole():
    assert split_steps("search for salt and pepper, then email Sarah saying hi, see you soon") == \
        ["search for salt and pepper", "email Sarah saying hi, see you soon"]
    assert split_steps("1. open Spotify\n2. play some jazz\n3) say hello there") == ["open Spotify", "play some jazz", "say hello there"]
    assert split_steps("open Spotify and open Gmail") == ["open Spotify", "open Gmail"]


@pytest.mark.parametrize("step, kind, seconds", [("wait 5 seconds", "wait", 5), ("wait a minute", "wait", 60), ("pause for 2 minutes", "wait", 120),
                                                 ("wait", "wait", 3), ("wait 10", "wait", 10), ("say Good morning, sir", "say", 0),
                                                 ("say something funny", "command", 0), ("open spotify", "command", 0)])
def test_step_kinds(step, kind, seconds):
    s = step_kind(step)
    assert s.kind == kind and s.seconds == seconds


@pytest.mark.parametrize("said, kind", [("done", "done"), ("that's it", "done"), ("I'm done", "done"), ("save it", "done"),
                                        ("cancel", "abandon"), ("never mind", "abandon"), ("undo that", "undo"),
                                        ("scratch that", "undo"), ("open spotify", "step"), ("stop the music", "step")])
def test_recording_replies(said, kind):
    assert recording_reply(said) == kind


def test_schedules():
    assert parse_schedule("at 6pm on Fridays") == {"time": "18:00", "days": [4], "enabled": True}
    assert parse_schedule("weekends at noon")["days"] == [5, 6]
    assert parse_schedule("at 9 in the evening")["time"] == "21:00"
    assert parse_schedule("whenever") is None
    weekdays = {"time": "07:30", "days": [0, 1, 2, 3, 4], "enabled": True}
    assert describe_schedule(weekdays) == "on weekdays at 7:30 AM"
    monday = datetime(2026, 10, 5, 7, 30, 20)
    assert due(weekdays, monday, 0)
    assert not due(weekdays, monday, monday.replace(second=5).timestamp())  # already ran in this slot
    assert not due(weekdays, monday.replace(minute=40), 0)  # too late: missed slots aren't run hours later
    assert not due(weekdays, datetime(2026, 10, 10, 7, 30, 20), 0)  # Saturday
    assert not due({**weekdays, "enabled": False}, monday, 0)


# ----------------------------------------------------------------------------- the store
def test_store_round_trip(config):
    store = ProtocolStore(config)
    p = store.save("morning", ["open Spotify", " ", "what's the weather"])
    assert p.name == "Morning" and p.steps == ["open Spotify", "what's the weather"]
    assert store.find("the Morning protocol").id == p.id and store.find("mornin").id == p.id
    again = store.save("Morning", ["open Gmail"])  # the same name replaces it
    assert again.id == p.id and store.get(p.id).steps == ["open Gmail"] and len(store.all()) == 1
    other = store.save("Night", ["lock the computer"], schedule={"time": "23:00", "days": [0, 1]})
    assert store.get(other.id).schedule == {"time": "23:00", "days": [0, 1], "enabled": True}
    with pytest.raises(ProtocolError):
        store.update(other.id, name="morning")
    with pytest.raises(ProtocolError):
        store.save("Empty", [])
    gone = store.delete(p.id)
    assert store.find("morning") is None
    store.restore(gone.to_dict())
    assert store.find("morning").steps == ["open Gmail"]


# ----------------------------------------------------------------------------- running them
def protocol_events(events, status):
    return [p for p in events.of("protocol") if p.get("status") == status]


def wait_done(events, timeout=30):
    events.wait_for(lambda: any(p["status"] in ("done", "stopped", "interrupted") for p in events.of("protocol")), timeout)
    events.wait_for(lambda: events.states()[-1:] == ["IDLE"], timeout)


def test_create_and_run_by_voice(make):
    assistant, events = make()
    reply = ask(assistant, events, "Create a protocol called Morning: what's 6 times 7, then wait 1 second, then say Rise and shine, then what's 2 plus 2")
    assert reply.startswith("Protocol Morning created, sir, with 4 steps")
    reply = ask(assistant, events, "Jarvis, run Morning")
    assert reply == "Initiating the Morning protocol, sir."
    wait_done(events)
    events.wait_for(lambda: any("protocol is complete" in s for s in assistant.tts.spoken), 15)
    steps = [p["text"] for p in events.of("user_message") if p["source"] == "protocol"]
    assert steps == ["what's 6 times 7", "what's 2 plus 2"]
    spoken = " ".join(assistant.tts.spoken)
    assert "42" in spoken and "Rise and shine" in spoken and "4" in spoken
    assert [p["index"] for p in protocol_events(events, "step")] == [0, 1, 2, 3]
    assert assistant.protocols.find("morning").last_run > 0 and assistant._protocol_run is None


def test_recording_step_by_step(make):
    assistant, events = make()
    assert "What shall I call" in ask(assistant, events, "create a new protocol")
    assert ask(assistant, events, "call it Study").startswith("The Study protocol it is")
    assert ask(assistant, events, "open Notion").startswith("Step 1: open Notion")
    assert ask(assistant, events, "set the volume to 20, then wait 5 seconds").startswith("Steps 2 to 3")
    assert ask(assistant, events, "scratch that").startswith("Removed")
    assert events.of("protocols")[-1]["recording"] == {"name": "Study", "steps": ["open Notion", "set the volume to 20"]}
    assert ask(assistant, events, "that's it").startswith("Protocol Study saved with 2 steps")
    assert assistant.protocols.find("study").steps == ["open Notion", "set the volume to 20"]
    assert assistant._recording is None
    assert "42" in ask(assistant, events, "what's 6 times 7")  # back to normal


def test_talking_over_a_protocol_stops_it(make):
    assistant, events = make()
    assistant.protocols.save("Slow", ["wait 20 seconds", "say you should never hear this"])
    ask(assistant, events, "run the slow protocol")
    events.wait_for(lambda: protocol_events(events, "step"), 10)
    assert "No protocol" not in ask(assistant, events, "stop the protocol")
    wait_done(events)
    assert protocol_events(events, "stopped") or protocol_events(events, "interrupted")
    assert not any("never hear" in s for s in assistant.tts.spoken)
    assert ask(assistant, events, "stop the protocol") in ("I've stopped the Slow protocol, sir.", "No protocol is running, sir.")


def test_a_timer_going_off_does_not_stop_a_protocol(make):
    assistant, events = make()
    assistant.protocols.save("Two", ["say first", "wait 2 seconds", "say second"])
    ask(assistant, events, "run two")
    events.wait_for(lambda: any(p["index"] == 1 for p in protocol_events(events, "step")), 15)
    assistant.speak("A reminder, sir: stretch.")  # what a timer does when it fires
    wait_done(events, 40)
    assert protocol_events(events, "done")
    events.wait_for(lambda: any(s == "second" for s in assistant.tts.spoken), 15)


def test_the_hud_buttons(make):
    assistant, events = make()
    saved = assistant.protocol_save({"name": "Desk", "steps": "say hello desk\n\nwait 1 second\nsay bye desk"})
    assert saved["ok"] and saved["protocol"]["steps"] == ["say hello desk", "wait 1 second", "say bye desk"]
    assert assistant.protocol_save({"name": "", "steps": "x"})["ok"] is False
    listing = assistant.protocol_list()
    assert listing["protocols"][0]["name"] == "Desk" and listing["running"] is None
    assert assistant.protocol_run(saved["protocol"]["id"])["ok"]
    wait_done(events)
    events.wait_for(lambda: "bye desk" in assistant.tts.spoken, 15)
    assert assistant.protocol_delete(saved["protocol"]["id"])["ok"]
    assert assistant.protocol_list()["protocols"] == []


def test_a_protocol_can_include_another(make):
    assistant, events = make()
    assistant.protocols.save("Lights", ["say lights on"])
    assistant.protocols.save("Evening", ["say good evening", "run the lights protocol", "say all set"])
    ask(assistant, events, "run evening")
    wait_done(events)
    events.wait_for(lambda: "all set" in assistant.tts.spoken, 15)
    spoken = assistant.tts.spoken
    assert spoken.index("good evening") < spoken.index("lights on") < spoken.index("all set")


def test_managing_by_voice(make):
    assistant, events = make()
    assert "don't have any protocols" in ask(assistant, events, "list my protocols")
    ask(assistant, events, "create a protocol called Morning: open Spotify, then open Gmail")
    assert ask(assistant, events, "add check the weather to the morning protocol").startswith("Added to the Morning protocol")
    assert "3 steps" in ask(assistant, events, "what's in the morning protocol")
    assert ask(assistant, events, "remove the first step from the morning protocol").startswith("Removed “open Spotify”")
    assert "every day at 7 AM" in ask(assistant, events, "schedule the morning protocol for 7am every day") or \
        "on" in assistant.protocols.find("morning").describe_schedule()
    assert assistant.protocols.find("morning").schedule["time"] == "07:00"
    assert ask(assistant, events, "rename the morning protocol to Sunrise").endswith("now called Sunrise, sir.")
    assert "Sunrise" in ask(assistant, events, "what protocols do I have")
    assert "don't have a protocol called Gaming" in ask(assistant, events, "run the gaming protocol")
    assert ask(assistant, events, "delete the sunrise protocol") == "I've deleted the Sunrise protocol, sir."
    assert assistant.protocols.all() == []


def test_scheduled_protocols_run_by_themselves(make, monkeypatch):
    assistant, events = make()
    now = datetime.now()
    p = assistant.protocols.save("Auto", ["say scheduled hello"], schedule={"time": now.strftime("%H:%M")})
    assert due(p.schedule, now, 0)
    from core import assistant as module
    monkeypatch.setattr(module, "protocol_due", lambda schedule, when, last: due(schedule, now, last))
    assistant._scheduler_stop.set()  # replace the 20-second loop with a quick one for the test
    assistant._scheduler_stop = type(assistant._scheduler_stop)()
    original = assistant._scheduler_stop.wait
    assistant._scheduler_stop.wait = lambda timeout=None: original(0.05)
    import threading
    threading.Thread(target=assistant._protocol_scheduler, daemon=True).start()
    events.wait_for(lambda: "scheduled hello" in assistant.tts.spoken, 20)
    assert any("Running your Auto protocol" in s for s in assistant.tts.spoken)
    time.sleep(0.5)
    assert assistant.tts.spoken.count("scheduled hello") == 1  # only once per slot
