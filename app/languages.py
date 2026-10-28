"""Language codes: Boutiko answers in whatever language the merchant writes.

A language is a free BCP-47 / ISO 639 code ("fr", "en", "pcm", "es", "pt-br",
"zh-hant"...). French, English and Cameroonian Pidgin have hand-written
replies; every other language gets them translated once by a Meta model
(app/services/translations.py).
"""

import re
from typing import ClassVar

MAX_CODE_LENGTH = 16
_CODE = re.compile(r"^[a-z]{2,3}(?:-[a-z0-9]{1,8})*$")

# Languages with hand-written texts, and their names for prompts.
HAND_WRITTEN_NAMES = {"fr": "French", "en": "English", "pcm": "Cameroonian Pidgin English"}

# English names the extraction model gave for other codes ("sw": "Swahili"), used in prompts.
_seen_names: dict[str, str] = {}


def normalize_code(code: object) -> str | None:
    """"ES" -> "es", "pt_BR" -> "pt-br", "fr-CM" -> "fr" (hand-written base). None if not a code."""
    if code is None:
        return None
    text = str(code).strip().lower().replace("_", "-")
    if len(text) > MAX_CODE_LENGTH or not _CODE.match(text):
        return None
    base = text.split("-", 1)[0]
    return base if base in HAND_WRITTEN_NAMES else text


class _HandWritten(type):
    def __iter__(cls):
        """Like the old enum: list(Language) is the hand-written languages."""
        return iter((cls.FR, cls.EN, cls.PIDGIN))


class Language(str, metaclass=_HandWritten):
    """A normalized language code. Language.FR, Language.EN and Language.PIDGIN are hand-written."""

    FR: ClassVar["Language"]
    EN: ClassVar["Language"]
    PIDGIN: ClassVar["Language"]

    def __new__(cls, code: object) -> "Language":
        normalized = normalize_code(code)
        if normalized is None:
            raise ValueError(f"Not a language code: {code!r}")
        return super().__new__(cls, normalized)

    def __repr__(self) -> str:
        return f"Language({str(self)!r})"

    @property
    def value(self) -> str:
        return str(self)

    @property
    def hand_written(self) -> bool:
        return self in HAND_WRITTEN_NAMES


Language.FR = Language("fr")
Language.EN = Language("en")
Language.PIDGIN = Language("pcm")


def as_language(code: object, default: Language = Language.FR) -> Language:
    """Language(code), or the default when the code is missing or invalid."""
    try:
        return code if isinstance(code, Language) else Language(code)
    except ValueError:
        return default


def remember_name(code: object, name: str | None) -> None:
    """Keep the English name the model gave for a language, for later prompts."""
    normalized = normalize_code(code)
    if normalized and name and name.strip() and normalized not in HAND_WRITTEN_NAMES:
        _seen_names[normalized] = name.strip()[:64]


def forget_names() -> None:
    _seen_names.clear()


def language_name(code: object, name: str | None = None) -> str:
    """English name of the language for a prompt ("Swahili"), or its code when no name is known."""
    normalized = normalize_code(code) or "en"
    if normalized in HAND_WRITTEN_NAMES:
        return HAND_WRITTEN_NAMES[normalized]
    if name and name.strip():
        return name.strip()[:64]
    if normalized in _seen_names:
        return _seen_names[normalized]
    return f'the language with ISO 639 code "{normalized}"'
