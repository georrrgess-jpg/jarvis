"""Lightweight, offline language identification for what the user says and what JARVIS replies.

No downloads and no dependencies: non-Latin scripts are recognised by their Unicode blocks, and
Latin-script languages by their most frequent short words plus their tell-tale letters. That is
plenty for conversational sentences, and it runs in microseconds on every turn.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass

# code -> (English name, speech-recognition locale, a male Edge neural voice to match JARVIS)
LANGUAGES: dict[str, tuple[str, str, str]] = {
    "en": ("English", "en-US", "en-GB-RyanNeural"),
    "es": ("Spanish", "es-ES", "es-ES-AlvaroNeural"),
    "fr": ("French", "fr-FR", "fr-FR-HenriNeural"),
    "de": ("German", "de-DE", "de-DE-ConradNeural"),
    "it": ("Italian", "it-IT", "it-IT-DiegoNeural"),
    "pt": ("Portuguese", "pt-BR", "pt-BR-AntonioNeural"),
    "nl": ("Dutch", "nl-NL", "nl-NL-MaartenNeural"),
    "sv": ("Swedish", "sv-SE", "sv-SE-MattiasNeural"),
    "da": ("Danish", "da-DK", "da-DK-JeppeNeural"),
    "nb": ("Norwegian", "nb-NO", "nb-NO-FinnNeural"),
    "fi": ("Finnish", "fi-FI", "fi-FI-HarriNeural"),
    "pl": ("Polish", "pl-PL", "pl-PL-MarekNeural"),
    "cs": ("Czech", "cs-CZ", "cs-CZ-AntoninNeural"),
    "ro": ("Romanian", "ro-RO", "ro-RO-EmilNeural"),
    "hu": ("Hungarian", "hu-HU", "hu-HU-TamasNeural"),
    "tr": ("Turkish", "tr-TR", "tr-TR-AhmetNeural"),
    "id": ("Indonesian", "id-ID", "id-ID-ArdiNeural"),
    "vi": ("Vietnamese", "vi-VN", "vi-VN-NamMinhNeural"),
    "ru": ("Russian", "ru-RU", "ru-RU-DmitryNeural"),
    "uk": ("Ukrainian", "uk-UA", "uk-UA-OstapNeural"),
    "el": ("Greek", "el-GR", "el-GR-NestorasNeural"),
    "ar": ("Arabic", "ar-SA", "ar-SA-HamedNeural"),
    "he": ("Hebrew", "he-IL", "he-IL-AvriNeural"),
    "hi": ("Hindi", "hi-IN", "hi-IN-MadhurNeural"),
    "bn": ("Bengali", "bn-IN", "bn-IN-BashkarNeural"),
    "ta": ("Tamil", "ta-IN", "ta-IN-ValluvarNeural"),
    "th": ("Thai", "th-TH", "th-TH-NiwatNeural"),
    "zh": ("Chinese", "zh-CN", "zh-CN-YunxiNeural"),
    "ja": ("Japanese", "ja-JP", "ja-JP-KeitaNeural"),
    "ko": ("Korean", "ko-KR", "ko-KR-InJoonNeural"),
}

_SCRIPTS = [  # (language, regex of its letters); order matters: kana before Han
    ("ja", re.compile(r"[぀-ヿ]")),
    ("ko", re.compile(r"[가-힯ᄀ-ᇿ]")),
    ("zh", re.compile(r"[一-鿿]")),
    ("ru", re.compile(r"[Ѐ-ӿ]")),
    ("el", re.compile(r"[Ͱ-Ͽ]")),
    ("ar", re.compile(r"[؀-ۿ]")),
    ("he", re.compile(r"[֐-׿]")),
    ("hi", re.compile(r"[ऀ-ॿ]")),
    ("bn", re.compile(r"[ঀ-৿]")),
    ("ta", re.compile(r"[஀-௿]")),
    ("th", re.compile(r"[฀-๿]")),
]

_WORDS: dict[str, set[str]] = {
    "en": set("the a an and or is are was were be to of in on at for with that this it you i me my we he she they what "
              "how why when where who do does did not can could would will please have has from your about just like "
              "there here tell open search write send".split()),
    "es": set("el la los las un una y o es son está estoy de del en por para con que qué cómo por qué cuándo dónde "
              "quién yo tú mi me te se no sí lo le su muy pero más hola gracias favor puedes quiero dime abre busca "
              "escribe hoy tiempo hace".split()),
    "fr": set("le la les un une et ou est sont de du des en dans pour avec que qui quoi comment pourquoi quand où je "
              "tu il elle nous vous ils mon ma mes ne pas oui non ce cette bonjour merci peux veux dis ouvre cherche "
              "écris aujourd'hui quel quelle c'est".split()),
    "de": set("der die das ein eine und oder ist sind war von zu im in auf für mit dass was wie warum wann wo wer "
              "ich du er sie wir ihr mein nicht ja nein auch aber bitte danke hallo kannst kann möchte sag öffne "
              "suche schreib heute wetter wie's".split()),
    "it": set("il lo la i gli le un una e o è sono di del della in per con che cosa come perché quando dove chi io "
              "tu lui lei noi voi mio mia non sì ciao grazie puoi voglio dimmi apri cerca scrivi oggi tempo".split()),
    "pt": set("o a os as um uma e ou é são está de do da em no na por para com que como porque quando onde quem eu "
              "você tu meu minha não sim olá obrigado obrigada pode quero diga abra procure escreva hoje tempo".split()),
    "nl": set("de het een en of is zijn was van te in op voor met dat wat hoe waarom wanneer waar wie ik jij je hij "
              "zij wij mijn niet ja nee ook maar alsjeblieft dank hallo kun kunt wil zeg open zoek schrijf vandaag".split()),
    "sv": set("och eller är var av till i på för med att vad hur varför när var vem jag du han hon vi min inte ja nej "
              "också men tack hej kan vill säg öppna sök skriv idag det den en ett".split()),
    "da": set("og eller er var af til i på for med at hvad hvordan hvorfor hvornår hvor hvem jeg du han hun vi min "
              "ikke ja nej også men tak hej kan vil sig åbn søg skriv i dag det den en et".split()),
    "nb": set("og eller er var av til i på for med at hva hvordan hvorfor når hvor hvem jeg du han hun vi min ikke "
              "ja nei også men takk hei kan vil si åpne søk skriv i dag det den en et".split()),
    "fi": set("ja tai on oli ei se tämä mikä miten miksi milloin missä kuka minä sinä hän me te minun kyllä kiitos "
              "hei voitko haluan kerro avaa etsi kirjoita tänään sää".split()),
    "pl": set("i lub jest są był w na do z że co jak dlaczego kiedy gdzie kto ja ty on ona my wy mój moja nie tak "
              "też ale proszę dziękuję cześć możesz chcę powiedz otwórz szukaj napisz dzisiaj pogoda".split()),
    "cs": set("a nebo je jsou byl v na do s že co jak proč kdy kde kdo já ty on ona my vy můj moje ne ano také ale "
              "prosím děkuji ahoj můžeš chci řekni otevři hledej napiš dnes počasí".split()),
    "ro": set("și sau este sunt era de la în pe pentru cu că ce cum de ce când unde cine eu tu el ea noi voi meu "
              "mea nu da dar te rog mulțumesc salut poți vreau spune deschide caută scrie azi vremea".split()),
    "hu": set("és vagy van volt a az egy hogy mi hogyan miért mikor hol ki én te ő mi ti az enyém nem igen is de "
              "kérlek köszönöm szia tudsz akarok mondd nyisd keress írj ma időjárás".split()),
    "tr": set("ve veya bir bu şu ne nasıl neden ne zaman nerede kim ben sen o biz siz benim değil evet hayır de da "
              "ama lütfen teşekkürler merhaba yapabilir misin istiyorum söyle aç ara yaz bugün hava saat kaç nedir mi mı "
              "var yok".split()),
    "id": set("dan atau adalah ini itu yang di ke dari untuk dengan apa bagaimana mengapa kapan mana siapa saya "
              "aku kamu dia kami kita tidak ya juga tapi tolong terima kasih halo bisa mau katakan buka cari tulis "
              "hari cuaca".split()),
    "vi": set("và hoặc là của trong cho với rằng gì thế nào tại sao khi nào đâu ai tôi bạn anh chị em không có "
              "cũng nhưng xin cảm ơn chào được muốn nói mở tìm viết hôm nay thời tiết".split()),
}

_LETTERS: list[tuple[str, re.Pattern, float]] = [  # characteristic letters: (language, pattern, weight per hit)
    ("es", re.compile(r"[ñ¿¡]"), 2.0),
    ("fr", re.compile(r"[çœèêëîïûù]"), 1.0),
    ("de", re.compile(r"ß"), 2.0),
    ("de", re.compile(r"[äöü]"), 0.5),
    ("pt", re.compile(r"[ãõ]"), 2.0),
    ("pl", re.compile(r"[ąęłńśźż]"), 2.0),
    ("tr", re.compile(r"[ğşı]"), 2.0),
    ("tr", re.compile(r"[çöü]"), 0.5),
    ("cs", re.compile(r"[řůěčšž]"), 1.5),
    ("ro", re.compile(r"[ăâșțşţ]"), 1.5),
    ("hu", re.compile(r"[őű]"), 2.0),
    ("sv", re.compile(r"[å]"), 0.5),
    ("da", re.compile(r"[æø]"), 1.0),
    ("nb", re.compile(r"[æø]"), 1.0),
    ("vi", re.compile(r"[ơưđạảấầẩẫậắằẳẵặẹẻẽếềểễệỉịọỏốồổỗộớờởỡợụủứừửữựỳỵỷỹ]"), 2.0),
]
_UKRAINIAN_LETTERS = re.compile(r"[іїєґІЇЄҐ]")
_UKRAINIAN_WORDS = set("котра година що як це дякую привіт будь ласка мені скажи відкрий".split())
_TOKEN = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)?", re.UNICODE)


@dataclass(frozen=True)
class Detection:
    code: str
    confidence: float  # 0..1

    @property
    def name(self) -> str:
        return LANGUAGES.get(self.code, (self.code,))[0]


def detect(text: str) -> Detection:
    """Best guess at the language of ``text``. Confidence is low for very short or ambiguous text."""
    text = str(text or "")
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return Detection("en", 0.0)
    for code, pattern in _SCRIPTS:
        share = len(pattern.findall(text)) / len(letters)
        if share >= 0.3:
            if code == "ru" and (_UKRAINIAN_LETTERS.search(text) or _UKRAINIAN_WORDS & set(_TOKEN.findall(text.lower()))):
                code = "uk"
            return Detection(code, min(1.0, 0.5 + share))
    lowered = text.lower()
    words = _TOKEN.findall(lowered)
    if not words:
        return Detection("en", 0.0)
    scores = {code: 0.0 for code in _WORDS}
    for word in words:
        for code, vocab in _WORDS.items():
            if word in vocab:
                scores[code] += 1.0
    for code, pattern, weight in _LETTERS:
        scores[code] += weight * len(pattern.findall(lowered))
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    (best, top), (_, second) = ranked[0], ranked[1]
    if top <= 0:
        return Detection("en", 0.0)
    margin = (top - second) / top  # 0 when tied, 1 when only one language matched
    coverage = min(1.0, top / max(3.0, len(words) * 0.5))
    return Detection(best, round(margin * 0.6 + coverage * 0.4, 3))


def base_language(locale: str) -> str:
    code = (locale or "en").replace("_", "-").split("-")[0].lower()
    return {"no": "nb", "iw": "he", "in": "id"}.get(code, code)


def locale_for(code: str) -> str:
    return LANGUAGES.get(code, (None, "en-US"))[1]


def voice_for(code: str, preferred: str = "") -> str | None:
    """A voice for ``code``: the user's chosen voice if it already speaks it (or is multilingual)."""
    if preferred and (base_language(preferred) == code or "Multilingual" in preferred):
        return preferred
    entry = LANGUAGES.get(code)
    return entry[2] if entry else None


def system_locale() -> str:
    """The OS display language as a BCP-47 tag such as 'es-ES' (falls back to en-US)."""
    if sys.platform == "win32":
        try:
            import ctypes

            buf = ctypes.create_unicode_buffer(85)
            if ctypes.windll.kernel32.GetUserDefaultLocaleName(buf, 85):
                return buf.value
        except Exception:
            pass
    import locale

    try:
        tag = locale.getlocale()[0] or ""
    except ValueError:
        tag = ""
    match = re.match(r"^([a-z]{2,3})[_-]([A-Za-z]{2})", tag)
    return f"{match.group(1)}-{match.group(2).upper()}" if match else "en-US"


def recognition_languages(primary: str, extra: str, auto: bool) -> list[str]:
    """Locales to try when recognising speech: the main one, then the others the user speaks."""
    out = [primary or "en-US"]
    if auto:
        wanted = [t.strip() for t in re.split(r"[,;\s]+", extra or "") if t.strip()]
        if not wanted:
            sys_tag = system_locale()
            if base_language(sys_tag) != base_language(out[0]):
                wanted = [sys_tag]
        for tag in wanted:
            tag = locale_for(tag) if len(tag) <= 3 and tag.lower() in LANGUAGES else tag
            if base_language(tag) not in {base_language(t) for t in out}:
                out.append(tag)
    return out[:3]
