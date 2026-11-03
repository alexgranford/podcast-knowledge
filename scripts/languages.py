"""languages — the language an episode is spoken in: its code, its name, and a guess from text.

Every episode is processed in one stated language (an ISO 639 code: two letters, or three where there is
none, as yue and fil; English, "en", when nothing says otherwise): the speech model is told it (Qwen3-ASR told the wrong one translates instead of
transcribing: a clip in another language sent as English came back in English), the text model is told it and
writes the note in it, and the validation pass looks things up in that language's Wikipedia first.

A show's language is the user's to confirm (podshows.py language). Until then an episode whose
description, title or source says another language is held for the user's answer, never guessed.
"""
from __future__ import annotations

import re
import unicodedata
from typing import NamedTuple

DEFAULT = "en"

# The languages the pipeline handles end to end: the speech model's own list (Qwen3-ASR names 30 languages),
# and a few more it transcribes without being told the language; the text model (Gemma 4, pre-trained on
# 140+ languages) writes in all of them. Grouped by family: Germanic, Romance, Uralic and Greek, Slavic,
# Turkic, Semitic, Iranian and Indic, East and South-East Asian. Order carries no meaning.
NAMES = {"en": "English", "de": "German", "nl": "Dutch", "sv": "Swedish", "da": "Danish", "nb": "Norwegian",
         "fr": "French", "es": "Spanish", "it": "Italian", "pt": "Portuguese", "ro": "Romanian",
         "fi": "Finnish", "hu": "Hungarian", "el": "Greek",
         "pl": "Polish", "cs": "Czech", "mk": "Macedonian", "uk": "Ukrainian", "ru": "Russian", "be": "Belarusian",
         "tr": "Turkish", "ar": "Arabic", "he": "Hebrew", "fa": "Persian", "hi": "Hindi",
         "zh": "Chinese", "yue": "Cantonese", "ja": "Japanese", "ko": "Korean", "id": "Indonesian", "ms": "Malay",
         "fil": "Filipino", "vi": "Vietnamese", "th": "Thai"}
NATIVE = {"de": "deutsch", "nl": "nederlands", "sv": "svenska", "da": "dansk", "nb": "norsk",
          "fr": "français", "es": "español", "it": "italiano", "pt": "português", "ro": "română",
          "fi": "suomi", "hu": "magyar", "el": "ελληνικά",
          "pl": "polski", "cs": "čeština", "mk": "македонски", "uk": "українська", "ru": "русский", "be": "беларуская",
          "tr": "türkçe", "ar": "العربية", "he": "עברית", "fa": "فارسی", "hi": "हिन्दी",
          "zh": "中文", "yue": "粵語", "ja": "日本語", "ko": "한국어", "id": "bahasa indonesia", "ms": "bahasa melayu",
          "fil": "filipino", "vi": "tiếng việt", "th": "ไทย"}
# the languages Qwen3-ASR names (it takes the English name); for any other the speech model is not told
ASR_LANGUAGES = {"en", "de", "nl", "sv", "da", "fr", "es", "it", "pt", "ro", "fi", "hu", "el",
                 "pl", "cs", "mk", "ru", "tr", "ar", "fa", "hi", "zh", "yue", "ja", "ko", "id", "ms", "fil", "vi", "th"}

# a few words that are common in running text of one language and rare in the others
STOPWORDS = {
    "en": "the and of to is in that for with this you are on we it be about how what",
    "de": "der die und das ist nicht mit sich auf für ein eine wir über wie auch",
    "fr": "le la les et des est une pour dans que qui sur pas avec nous sont",
    "es": "el los las y que del para con una por como más pero sus este sobre",
    "it": "il che di della per con una sono non gli nel come anche questo",
    "pt": "que não uma para com por mais dos das como está são também sobre",
    "nl": "de het een van en is dat niet op voor met zijn wat ook maar",
    "pl": "nie się jest że na do to jak ale czy przez dla jego tak",
}
STOPWORDS = {code: set(words.split()) for code, words in STOPWORDS.items()}


def name(code: str) -> str:
    return NAMES.get(code, code)


def code(text: str) -> str | None:
    """The code for what a user or a source calls a language: "en", "en-US", "English", or its own name."""
    value = unicodedata.normalize("NFC", str(text or "")).strip().strip(".").lower()
    if not value:
        return None
    short = re.split(r"[-_]", value)[0]
    if short in NAMES:
        return short
    for key, english in NAMES.items():
        if value in {english.lower(), NATIVE.get(key, "")} or value.startswith(english.lower()[:5]):
            return key
    return None


def script_share(text: str) -> dict[str, float]:
    """Share of the letters in text by script: latin, cyrillic, greek, hebrew, arabic, cjk, hangul, devanagari."""
    counts: dict[str, int] = {}
    for char in text:
        if not char.isalpha():
            continue
        title = unicodedata.name(char, "")
        script = next((s for s in ("LATIN", "CYRILLIC", "GREEK", "HEBREW", "ARABIC", "HANGUL", "DEVANAGARI", "THAI")
                       if title.startswith(s)), "CJK" if re.match(r"CJK|HIRAGANA|KATAKANA", title) else "OTHER")
        counts[script] = counts.get(script, 0) + 1
    total = sum(counts.values()) or 1
    return {script.lower(): count / total for script, count in counts.items()}


def guess(text: str, least: int = 20) -> str | None:
    """The language most of the text is in, or None when there is too little to tell (fewer than least
    letters). Scripts first (a script several languages share is taken as its most common one unless
    letters only another uses show), then common words for Latin text."""
    text = re.sub(r"https?://\S+|www\.\S+|\S+@\S+|#\S+", " ", str(text or ""))
    shares = script_share(text)
    letters = sum(1 for char in text if char.isalpha())
    if letters < least:
        return None
    for script, found in (("cyrillic", None), ("greek", "el"), ("hebrew", "he"), ("arabic", "ar"), ("hangul", "ko"),
                          ("devanagari", "hi"), ("thai", "th"), ("cjk", None)):
        if shares.get(script, 0) >= 0.3:
            if script == "cyrillic":
                if len(re.findall(r"[іїєґІЇЄҐ]", text)) >= 3:
                    return "uk"
                return "be" if len(re.findall(r"[ўЎ]", text)) >= 3 else "ru"
            if script == "cjk":
                return "ja" if re.search(r"[぀-ヿ]", text) else "zh"
            return found
    words = re.findall(r"[^\W\d_]+", text.lower())
    if len(words) < 8:
        return None
    hits = {lang: sum(word in vocabulary for word in words) for lang, vocabulary in STOPWORDS.items()}
    best = max(hits, key=hits.get)
    if hits[best] < 3:
        return None
    return best if best == "en" or hits[best] >= 2 * hits["en"] else "en"


MARKERS = re.compile(r"\*\*(Topic|References|Participants):\*\*|##+ Key themes|\[(work|idea|quote)\]|\b(host|guest)\b")


def drifted(text: str, code: str) -> str | None:
    """The language a model's answer slipped into, when it is plainly not code (the notes' English
    markers, and the references with their original titles, aside); None when it is in code or too
    short to tell. Gemma, told to write another language, wrote 6 of 43 sections of two such episodes in English."""
    body = MARKERS.sub(" ", str(text or "").split("**References:**")[0])
    found = guess(body)
    return found if found and found != code else None


class Decision(NamedTuple):
    code: str | None        # the language to use; None: ask (the evidence points elsewhere but does not settle it)
    how: str                # who decided: user, show, earlier, detected, default; or "ask"
    looks: str = ""         # when asking: the language it looks like ...
    basis: str = ""         # ... and what that is based on


def decide(said: str | None, known: str = "", earlier: tuple[str, str] | None = None, hint: str = "",
           title: str = "", description: str = "") -> Decision:
    """The one order in which an episode's language is settled: the language the user gives (said, a
    code), the show's confirmed one (known), a decision already on the episode's record (earlier: code
    and who made it), a confident detection from the source's tag and the text, else English. Evidence
    that points elsewhere without settling it means asking."""
    if said:
        return Decision(said, "user")
    if known:
        return Decision(known, "show")
    if earlier and earlier[0]:
        return Decision(earlier[0], earlier[1])
    found, sure, basis = judge(hint, title, description)
    if found and found != DEFAULT:
        return Decision(found, "detected") if sure else Decision(None, "ask", found, basis)
    return Decision(DEFAULT, "default")


def judge(hint: str = "", title: str = "", description: str = "") -> tuple[str | None, bool, str]:
    """(language, confident, what it is based on) for an episode no one has named a language for.
    Confident when the source's own tag (YouTube's audio language, a feed's <language>) and the title
    and description agree, or, with no tag, when the title and the description each say the same
    language on their own. A tag the text contradicts is never confident: the language returned is
    then the one that is not English (the one the user is asked about)."""
    tagged = code(hint) if hint else None
    text = guess(" ".join(t for t in (title, description) if t))
    if tagged and text == tagged:
        return tagged, True, "the source's language tag, the title and the description agree"
    if tagged and text:
        other = text if text != DEFAULT else tagged
        return other, False, (f"the source tags it {name(tagged)} but its title and description look "
                              f"{name(text)}")
    if tagged:
        return tagged, False, "the language the source gives"
    if text and guess(title) == text and guess(description) == text:
        return text, True, "the title and the description both"
    return text, False, "its title and description"


