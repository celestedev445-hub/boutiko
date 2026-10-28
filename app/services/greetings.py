"""Greeting by the time of day in the shop's time zone, and greeting messages from the merchant."""

import enum
from datetime import datetime

from app.languages import Language
from app.services import translations
from app.services.normalize import normalize_command
from app.services.summary import local_now


class Period(str, enum.Enum):
    MORNING = "morning"  # before 12:00
    AFTERNOON = "afternoon"  # 12:00 - 18:00
    EVENING = "evening"  # from 18:00


# Hand-written in fr, en, pcm; translated once for any other language.
_GREETINGS = {
    Period.MORNING: {Language.FR: "Bonjour", Language.EN: "Good morning", Language.PIDGIN: "Good morning"},
    Period.AFTERNOON: {Language.FR: "Bon après-midi", Language.EN: "Good afternoon", Language.PIDGIN: "Good afternoon"},
    Period.EVENING: {Language.FR: "Bonsoir", Language.EN: "Good evening", Language.PIDGIN: "Good evening"},
}
EMOJI = {Period.MORNING: "☀️", Period.AFTERNOON: "🌤️", Period.EVENING: "🌙"}

# What a merchant may send just to say hello (None: English, or Pidgin if that's the shop's language).
_GREETING_MESSAGES: dict[str, Language | None] = {
    "bonjour": Language.FR, "bonsoir": Language.FR, "salut": Language.FR, "coucou": Language.FR,
    "bjr": Language.FR, "slt": Language.FR, "bon apres midi": Language.FR, "bonne journee": Language.FR,
    "hello": None, "hi": None, "hey": None, "good morning": None, "good afternoon": None, "good evening": None,
    "how far": Language.PIDGIN, "how you dey": Language.PIDGIN, "how body": Language.PIDGIN,
    "morning o": Language.PIDGIN, "my broda": Language.PIDGIN, "my sista": Language.PIDGIN,
}


def period(now: datetime | None = None) -> Period:
    hour = local_now(now).hour
    if hour < 12:
        return Period.MORNING
    if hour < 18:
        return Period.AFTERNOON
    return Period.EVENING


def _greeting_for(at: Period, language: Language) -> str:
    return translations.localized(f"greeting.{at.value}", _GREETINGS[at], language)


def greeting(language: Language, now: datetime | None = None) -> str:
    return _greeting_for(period(now), language)


def other_greetings(language: Language, now: datetime | None = None) -> list[str]:
    """Greetings that would be wrong right now ("Bonjour" at 20:00)."""
    current = period(now)
    right = _greeting_for(current, language)
    wrong = {_greeting_for(p, language) for p in Period if p != current}
    if language == Language.FR:
        wrong |= {_GREETINGS[p][Language.EN] for p in Period}
    return sorted(wrong - {right})


def parse_greeting(text: str) -> Language | None | bool:
    """False if the message is not just a greeting; otherwise the language it was said in."""
    phrase = normalize_command(text)
    return _GREETING_MESSAGES[phrase] if phrase in _GREETING_MESSAGES else False
