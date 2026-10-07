"""Language auto-detection: the text detector, multi-language speech recognition and voice switching."""

import pytest

from core.language import base_language, detect, recognition_languages, voice_for
from core.stt import pick_transcript


@pytest.mark.parametrize("text, code", [
    ("what time is it", "en"), ("write a bio on Lionel Messi", "en"),
    ("¿Qué hora es?", "es"), ("Escribe una biografía de Lionel Messi", "es"), ("Jarvis, abre Spotify", "es"),
    ("Quelle heure est-il ?", "fr"), ("Wie spät ist es?", "de"), ("Che ore sono?", "it"),
    ("Que horas são?", "pt"), ("Hoe laat is het?", "nl"), ("Która jest godzina?", "pl"), ("Saat kaç?", "tr"),
    ("Сколько сейчас времени?", "ru"), ("Котра година?", "uk"), ("今何時ですか", "ja"), ("现在几点了", "zh"),
    ("지금 몇 시예요?", "ko"), ("كم الساعة الآن؟", "ar"), ("अभी कितने बजे हैं?", "hi"),
])
def test_detects_common_languages(text, code):
    assert detect(text).code == code


def test_confidence_is_low_for_tiny_or_wordless_input():
    assert detect("ok").confidence < 0.5
    assert detect("1234 !!").confidence == 0.0
    assert detect("Lionel Messi").confidence < 0.5, "names alone say nothing about the language"


def test_voice_matches_language_but_keeps_the_users_choice():
    assert voice_for("es") == "es-ES-AlvaroNeural"
    assert voice_for("en", "en-US-AndrewNeural") == "en-US-AndrewNeural"
    assert voice_for("fr", "en-US-AndrewMultilingualNeural") == "en-US-AndrewMultilingualNeural"
    assert voice_for("xx") is None
    assert base_language("nb-NO") == "nb" and base_language("no") == "nb"


def test_recognition_languages(monkeypatch):
    import core.language as lang

    monkeypatch.setattr(lang, "system_locale", lambda: "es-MX")
    assert recognition_languages("en-US", "", auto=True) == ["en-US", "es-MX"], "the PC's language is added"
    assert recognition_languages("en-US", "", auto=False) == ["en-US"]
    assert recognition_languages("en-GB", "fr, de-DE, en-US, it, pt", auto=True) == ["en-GB", "fr-FR", "de-DE"]
    monkeypatch.setattr(lang, "system_locale", lambda: "en-GB")
    assert recognition_languages("en-US", "", auto=True) == ["en-US"], "no extra request for an English PC"


def alt(text, conf=None):
    top = {"transcript": text}
    if conf is not None:
        top["confidence"] = conf
    return {"alternative": [top], "final": True}


def test_pick_transcript_prefers_the_language_that_fits():
    spanish = [("en-US", alt("I'm gonna say it", 0.62)), ("es-ES", alt("¿qué tiempo hace hoy en Madrid?", 0.91))]
    assert pick_transcript(spanish) == ("¿qué tiempo hace hoy en Madrid?", "es-ES")
    english = [("en-US", alt("what's the weather like today", 0.93)), ("es-ES", alt("guataje weather like today", 0.7))]
    assert pick_transcript(english)[1] == "en-US"
    # no confidence given (Google omits it sometimes): the transcript that reads as its language wins
    tie = [("en-US", alt("kay tempo ah say")), ("fr-FR", alt("quel temps fait-il aujourd'hui"))]
    assert pick_transcript(tie)[1] == "fr-FR"
    assert pick_transcript([("en-US", []), ("es-ES", RuntimeError("offline"))]) == ("", None)


def test_google_recogniser_queries_each_language(config, monkeypatch):
    import speech_recognition as sr

    from core.stt import Capture, SpeechInput

    config.update({"stt_language": "en-US", "stt_extra_languages": "es-ES", "auto_language": True})
    asked = []

    def fake(self, audio, language="en-US", show_all=False, **kw):
        asked.append((language, show_all))
        return alt("hola, ¿cómo estás hoy?", 0.9) if language == "es-ES" else alt("all a como estas oi", 0.55)

    monkeypatch.setattr(sr.Recognizer, "recognize_google", fake)
    stt = SpeechInput(config)
    assert stt._recognize_google(Capture(b"\0" * 32000, 16000)) == "hola, ¿cómo estás hoy?"
    assert sorted(asked) == [("en-US", True), ("es-ES", True)] and stt.last_language == "es-ES"


# ------------------------------------------------------------------ the assistant end to end


class VoiceTTS:
    def __init__(self):
        self.calls = []

    def synthesize(self, text, voice=None, **_):
        from tests.conftest import wav_bytes

        self.calls.append((text, voice))
        return wav_bytes(0.05)

    def list_voices(self):
        return []

    def close(self):
        pass


@pytest.fixture
def chat(config, mock_ollama):
    from core.assistant import Assistant
    from tests.test_assistant import Events

    config.update({"ollama_host": mock_ollama.url, "voice_enabled": True, "voice": "en-GB-RyanNeural"})
    events, tts = Events(), VoiceTTS()
    assistant = Assistant(config, events, tts=tts)
    assistant.start()
    yield assistant, events, tts, mock_ollama
    assistant.shutdown()


def test_spanish_question_gets_a_spanish_reply_in_a_spanish_voice(chat):
    assistant, events, tts, mock = chat
    mock.reply = "Lionel Messi es un futbolista argentino. Es considerado uno de los mejores jugadores de la historia."
    assistant.submit_text("¿Quién es Lionel Messi?")
    events.wait_for(events.finished)
    system = [b for p, b in mock.requests if p == "/api/chat"][-1]["messages"][0]["content"]
    assert "Reply in Spanish" in system
    assert events.of("user_message")[0]["lang"] == "es"
    assert {v for _, v in tts.calls} == {"es-ES-AlvaroNeural"}


def test_english_stays_in_the_chosen_voice(chat):
    assistant, events, tts, mock = chat
    mock.reply = "Lionel Messi is an Argentine footballer. Many consider him the greatest of all time."
    assistant.submit_text("who is Lionel Messi")
    events.wait_for(events.finished)
    assert "lang" not in events.of("user_message")[0]
    assert {v for _, v in tts.calls} == {None}, "None = the voice picked in Settings"


def test_auto_language_can_be_switched_off(chat, config):
    assistant, events, tts, mock = chat
    config.update({"auto_language": False})
    mock.reply = "Claro que sí. Aquí tienes la respuesta completa en español."
    assistant.submit_text("¿Puedes hablar español conmigo?")
    events.wait_for(events.finished)
    assert {v for _, v in tts.calls} == {None}
    assert "Reply in Spanish" not in [b for p, b in mock.requests if p == "/api/chat"][-1]["messages"][0]["content"]
