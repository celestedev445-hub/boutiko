"""Greeting by the time of day in the shop's time zone, and greeting messages from the merchant."""

import enum
from datetime import datetime

from app.models import Language
from app.services.normalize import normalize_command
from app.services.summary import local_now


class Period(str, enum.Enum):
    MORNING = "morning"  # before 12:00
    AFTERNOON = "afternoon"  # 12:00 - 18:00
    EVENING = "evening"  # from 18:00


_GREETINGS = {
    Language.FR: {Period.MORNING: "Bonjour", Period.AFTERNOON: "Bon après-midi", Period.EVENING: "Bonsoir"},
    Language.EN: {Period.MORNING: "Good morning", Period.AFTERNOON: "Good afternoon", Period.EVENING: "Good evening"},
    Language.PIDGIN: {Period.MORNING: "Good morning", Period.AFTERNOON: "Good afternoon", Period.EVENING: "Good evening"},
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


def greeting(language: Language, now: datetime | None = None) -> str:
    return _GREETINGS[language][period(now)]


def other_greetings(language: Language, now: datetime | None = None) -> list[str]:
    """Greetings that would be wrong right now ("Bonjour" at 20:00)."""
    current = period(now)
    wrong = {text for p, text in _GREETINGS[language].items() if p != current}
    if language == Language.FR:
        wrong |= {"Good morning", "Good afternoon", "Good evening"} - {_GREETINGS[language][current]}
    return sorted(wrong)


def parse_greeting(text: str) -> Language | None | bool:
    """False if the message is not just a greeting; otherwise the language it was said in."""
    phrase = normalize_command(text)
    return _GREETING_MESSAGES[phrase] if phrase in _GREETING_MESSAGES else False
