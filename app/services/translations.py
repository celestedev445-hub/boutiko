"""Fixed replies in any language.

French, English and Pidgin are written by hand. For any other language, the
English text of a key is translated once by a Meta model, checked, and stored
in the translations table; later messages read it from there without calling
the model.

The check: the translation must keep exactly the same {placeholders}, emojis
and numbers. Amounts, quantities and names never go through the model: they
fill the placeholders afterwards. Whenever something goes wrong (model down,
check failed, database error), the English text is used: a reply is never
blocked by a translation.
"""

import logging
import re
import time
from collections import Counter
from collections.abc import Mapping

import openai
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app import db, meta_client
from app.config import get_settings
from app.languages import Language, as_language, language_name
from app.models import Translation
from app.services.normalize import normalize_command

logger = logging.getLogger(__name__)

REASONING_EFFORT = "low"
RETRY_AFTER_SECONDS = 6 * 3600  # a rejected or failed translation is retried after that, English meanwhile
MAX_KEYWORD_CHARS = 24

TRANSLATION_PROMPT = """You translate the fixed messages of Boutiko, a WhatsApp assistant that keeps
the books of small shops, from English into {language}. The shopkeepers are
not at ease with computers: use simple, everyday, friendly words.

Rules:
- Keep every placeholder in curly braces, like {{item}} or {{total}}, exactly as
  it is: same spelling, not translated. Values are inserted later. You may move
  a placeholder where the grammar needs it.
- Keep every emoji, the same ones, the same number of times.
- Keep every number written in digits exactly as it is.
- One line in, one line out. No quotes, no notes, no explanation: only the translation."""

KEYWORD_PROMPT = """You translate a word that shopkeepers type to Boutiko, a WhatsApp bookkeeping
assistant, from English into {language}. Give the single short word (or two
words at most) a shopkeeper would naturally type for it, in capitals if the
script has capitals. Only the word, nothing else."""

_PLACEHOLDER = re.compile(r"\{(\w*)\}")
_EMOJI = re.compile("[\U0001F000-\U0001FAFF←-⇿⌀-⏿☀-➿⬀-⯿]")
_NUMBER = re.compile(r"\d+")

# (key, language) whose translation failed: monotonic time of the failure.
_failures: dict[tuple[str, str], float] = {}


def forget_failures() -> None:
    _failures.clear()


def problem(english: str, text: str) -> str | None:
    """Why a translation can't be used, or None if it keeps everything the English has."""
    if not text.strip():
        return "empty"
    if Counter(_PLACEHOLDER.findall(english)) != Counter(_PLACEHOLDER.findall(text)):
        return "placeholders differ"
    if Counter(_EMOJI.findall(english)) != Counter(_EMOJI.findall(text)):
        return "emojis differ"
    if Counter(_NUMBER.findall(english)) != Counter(_NUMBER.findall(text)):
        return "numbers differ"
    if "\n" in text and "\n" not in english:
        return "extra line break"
    if len(text) > 3 * len(english) + 60:
        return "too long"
    try:
        text.format(**{name: "" for name in _PLACEHOLDER.findall(english)})
    except (KeyError, IndexError, ValueError):
        return "stray braces"
    return None


def keyword_problem(english: str, text: str) -> str | None:
    reason = problem(english, text)
    if reason:
        return reason
    if len(text) > MAX_KEYWORD_CHARS or not normalize_command(text):
        return "not a short word"
    return None


def localized(key: str, texts: Mapping[str, str], language: object, keyword: bool = False) -> str:
    """The text of `key` in `language`: hand-written if there is one, else translated from English."""
    language = as_language(language)
    if language in texts:
        return texts[language]
    return translate(key, texts[Language.EN], language, keyword=keyword)


def _stored(key: str, language: Language) -> str | None:
    with db.SessionLocal() as session:
        return session.scalar(select(Translation.text).where(Translation.key == key, Translation.language == language))


def _store(key: str, language: Language, text: str) -> str:
    """Save the translation; if another worker saved one first, keep theirs."""
    with db.SessionLocal() as session:
        session.add(Translation(key=key, language=str(language), text=text))
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            return _stored(key, language) or text
    return text


def _ask_model(english: str, language: Language, keyword: bool) -> str:
    prompt = KEYWORD_PROMPT if keyword else TRANSLATION_PROMPT
    response = meta_client.create_completion(
        model=get_settings().model_text,
        messages=[
            {"role": "system", "content": prompt.format(language=language_name(language))},
            {"role": "user", "content": english},
        ],
        reasoning_effort=REASONING_EFFORT,
    )
    return (response.choices[0].message.content or "").strip().strip('"“”«»').strip()


def translate(key: str, english: str, language: Language, keyword: bool = False) -> str:
    """Stored translation, else a checked new one from the model; English if anything fails."""
    try:
        stored = _stored(key, language)
    except Exception as exc:
        logger.error("Translation %s/%s: lookup failed (%s), English used", key, language, type(exc).__name__)
        return english
    if stored is not None:
        return stored

    failed_at = _failures.get((key, language))
    if failed_at is not None and time.monotonic() - failed_at < RETRY_AFTER_SECONDS:
        return english
    try:
        text = _ask_model(english, language, keyword)
    except (openai.OpenAIError, meta_client.MissingAPIKeyError) as exc:
        logger.warning("Translation %s/%s: model call failed (%s), English used", key, language, type(exc).__name__)
        _failures[(key, language)] = time.monotonic()
        return english
    # The model trims its answer; keep the English spacing (" Left: {stock}." is appended to a line).
    text = english[: len(english) - len(english.lstrip())] + text + english[len(english.rstrip()):]
    reason = (keyword_problem if keyword else problem)(english, text)
    if reason:
        logger.warning("Translation %s/%s rejected (%s), English used", key, language, reason)
        _failures[(key, language)] = time.monotonic()
        return english

    try:
        text = _store(key, language, text)
        logger.info("Translation %s/%s stored", key, language)
    except Exception as exc:
        logger.error("Translation %s/%s: not stored (%s)", key, language, type(exc).__name__)
    return text


def keyword_language(text: str, key: str) -> Language | None:
    """Language whose stored translation of keyword `key` is the whole message ("RESUMEN" -> es)."""
    phrase = normalize_command(text)
    if not phrase:
        return None
    try:
        with db.SessionLocal() as session:
            rows = session.execute(select(Translation.language, Translation.text).where(Translation.key == key)).all()
    except Exception as exc:
        logger.error("Keyword lookup failed (%s)", type(exc).__name__)
        return None
    for language, word in rows:
        if normalize_command(word) == phrase:
            return as_language(language)
    return None
