import re

from core.tts import CODE_MARK, SentenceSplitter, clean_for_speech, format_pitch, format_rate


def stream(text: str, size: int = 3) -> list[str]:
    """Feed ``text`` in small chunks like an LLM token stream."""
    splitter = SentenceSplitter()
    out = []
    for i in range(0, len(text), size):
        out += splitter.feed(text[i : i + size])
    return out + splitter.flush()


def test_splits_sentences_while_streaming():
    out = stream("Good evening, sir. All systems are online! Shall I begin the diagnostic? ")
    assert out == ["Good evening, sir.", "All systems are online!", "Shall I begin the diagnostic?"]


def test_emits_sentence_before_stream_ends():
    splitter = SentenceSplitter()
    assert splitter.feed("The reactor is stable. The") == ["The reactor is stable."]
    assert splitter.flush() == ["The"]


def test_does_not_split_abbreviations_decimals_or_initials():
    out = stream("Dr. Banner measured 3.14 units, e.g. a lot. J. R. R. Tolkien agreed. ")
    assert out == ["Dr. Banner measured 3.14 units, e.g. a lot.", "J. R. R. Tolkien agreed."]


def test_short_fragments_merge_with_next_sentence():
    assert stream("Yes. The suit is ready for testing now. ") == ["Yes. The suit is ready for testing now."]


def test_numbered_list_items_are_separate_lines():
    out = stream("Here you go:\n1. Calibrate the repulsors\n2. Run the flight test\n")
    assert out == ["Here you go:", "1. Calibrate the repulsors", "2. Run the flight test"]


def test_code_blocks_are_skipped_and_announced_once():
    text = "Here is the function.\n```python\ndef f():\n    return 1. \n```\nAnd another:\n```js\nx()\n```\nDone now, sir."
    out = stream(text, size=2)
    assert out.count(CODE_MARK) == 1
    assert "Here is the function." in out
    assert not any("def f" in s or "x()" in s for s in out)
    assert out[-1] == "Done now, sir."


def test_long_run_on_text_is_split_at_soft_breaks():
    text = ("word " * 30 + ", ") * 3
    out = stream(text)
    assert len(out) >= 2
    assert all(len(s) <= 230 for s in out)


def test_clean_for_speech_strips_markdown():
    raw = "## Status\n- **All** systems are `nominal`, see [docs](http://x.y). \U0001F680 J.A.R.V.I.S. online"
    cleaned = clean_for_speech(raw)
    assert cleaned == "Status All systems are nominal, see docs. Jarvis online"
    assert not re.search(r"[*#`\[\]]", cleaned)


def test_clean_for_speech_drops_code_and_urls():
    assert clean_for_speech("Run ```rm -rf build``` then visit https://ollama.com now") == "Run then visit the link on screen now"


def test_rate_and_pitch_format():
    assert format_rate(0) == "+0%"
    assert format_rate(-12.4) == "-12%"
    assert format_pitch(5) == "+5Hz"


def test_the_first_long_sentence_starts_speaking_at_a_comma():
    from core.tts import SentenceSplitter

    splitter = SentenceSplitter()
    out = []
    reply = ("Lionel Messi is widely regarded as one of the greatest footballers of all time, having won eight Ballon d'Or "
             "awards and the 2022 World Cup with Argentina. He began his career at Barcelona.")
    for i in range(0, len(reply), 7):  # streamed in small pieces, like model tokens
        out += splitter.feed(reply[i:i + 7])
    out += splitter.flush()
    assert out[0] == "Lionel Messi is widely regarded as one of the greatest footballers of all time,", out
    assert out[1].startswith("having won eight") and out[-1] == "He began his career at Barcelona."
    assert "".join(out).replace(" ", "") == reply.replace(" ", "")
    # short sentences are untouched, and only the first chunk of a reply is cut early
    short = SentenceSplitter()
    assert short.feed("Certainly, sir. All systems are online, and the coffee is warm. ") == [
        "Certainly, sir.", "All systems are online, and the coffee is warm."]
