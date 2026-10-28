"""Dispatch an incoming WhatsApp message to the right handler by type.

A handler returns the reply to send, or None when nothing should be sent.
"""

import logging
from collections.abc import Callable

import httpx
from sqlalchemy import select

from app import db, meta_client
from app.logging_config import mask_phone
from app.models import Language, Shop
from app.services import audio, ledger, replies, transcribe
from app.whatsapp import media
from app.whatsapp.client import GraphAPIError
from app.whatsapp.payload import Message

logger = logging.getLogger(__name__)

SHELF_KEYWORDS = ("etagere", "étagère", "rayon", "shelf", "inventaire", "count")


def is_shelf_photo(caption: str | None) -> bool:
    # TODO: let muse-spark classify the photo (receipt vs shelf) instead of relying on the caption.
    return bool(caption) and any(k in caption.lower() for k in SHELF_KEYWORDS)


def _shop_language(owner_phone: str) -> Language:
    """Language to apologise in when the message itself couldn't be read."""
    try:
        with db.SessionLocal() as session:
            language = session.scalar(select(Shop.language).where(Shop.owner_phone == owner_phone))
    except Exception:
        logger.exception("Could not look up the shop language")
        return Language.FR
    return language or Language.FR


def _already_recorded(message_id: str) -> bool:
    try:
        with db.SessionLocal() as session:
            return ledger.is_processed(session, message_id)
    except Exception:
        logger.exception("Could not check whether message %s was already recorded", message_id)
        return False


def _with_heard(reply: str, transcript: str | None, language: Language) -> str:
    if transcript is None:
        return reply
    return replies.heard(transcript, language) + "\n" + reply


def _process_text(message: Message, text: str, transcript: str | None = None) -> str | None:
    """Extraction -> ledger -> reply, shared by text messages and transcribed voice notes.

    With a transcript, the reply starts with what was understood.
    """
    sender = mask_phone(message.sender)
    try:
        extraction = meta_client.extract_from_text(text)
    except meta_client.ExtractionError as exc:
        logger.error("Extraction failed for message %s from %s: %s", message.id, sender, exc)
        language = _shop_language(message.sender)
        return _with_heard(replies.apology(language), transcript, language)

    logger.info(
        "Extracted %d action(s) from message %s: %s",
        len(extraction.actions), message.id, ", ".join(a.action.value for a in extraction.actions) or "none",
    )
    try:
        with db.SessionLocal() as session:
            result = ledger.apply_extraction(session, message.sender, extraction, message.id)
    except Exception:
        logger.exception("Could not record message %s from %s", message.id, sender)
        language = Language(extraction.language) if extraction.language else Language.FR
        return _with_heard(replies.apology(language), transcript, language)

    if result.duplicate:
        logger.info("Message %s was already recorded, no reply", message.id)
        return None
    return _with_heard(replies.confirmation(result), transcript, result.language)


def handle_text(message: Message) -> str | None:
    return _process_text(message, message.text.body)


def handle_audio(message: Message) -> str | None:
    """Voice note: download -> WAV -> transcript -> same path as a text message."""
    sender = mask_phone(message.sender)
    # Skip the download and transcription if a redelivered note was already recorded.
    if _already_recorded(message.id):
        logger.info("Voice note %s was already recorded, no reply", message.id)
        return None
    language = _shop_language(message.sender)

    try:
        wav = audio.convert_to_wav(media.download_media(message.audio.id))
    except (media.MediaTooLargeError, audio.AudioTooLongError) as exc:
        logger.info("Voice note %s from %s refused: %s", message.id, sender, exc)
        return replies.voice_too_long(language)
    except (GraphAPIError, httpx.HTTPError, audio.AudioConversionError) as exc:
        logger.error("Could not get voice note %s from %s: %s", message.id, sender, exc)
        return replies.voice_failed(language)

    try:
        transcription = transcribe.transcribe(wav.data)
    except transcribe.TranscriptionError as exc:
        logger.error("Transcription failed for voice note %s from %s: %s", message.id, sender, exc)
        return replies.voice_failed(language)

    # The transcript itself is personal data: log its size, not its content.
    logger.info(
        "Voice note %s: %.1f s, %d characters transcribed with %s",
        message.id, wav.duration_seconds, len(transcription.text), transcription.model,
    )
    if not transcription.text:
        return replies.voice_not_heard(language)
    return _process_text(message, transcription.text, transcript=transcription.text)


def handle_receipt_photo(message: Message) -> str:
    # TODO: download_media -> meta_client.extract_from_receipt -> record purchases
    return "Photo de ticket reçue 🧾"


def handle_shelf_photo(message: Message) -> str:
    # TODO: download_media -> meta_client.count_shelf -> propose stock adjustments
    return "Photo d'étagère reçue 📦"


def handle_image(message: Message) -> str:
    if is_shelf_photo(message.image.caption):
        return handle_shelf_photo(message)
    return handle_receipt_photo(message)


def handle_unsupported(message: Message) -> str:
    return "Désolé, je comprends le texte, les notes vocales et les photos."


HANDLERS: dict[str, Callable[[Message], str | None]] = {
    "text": handle_text,
    "audio": handle_audio,
    "image": handle_image,
}


def route(message: Message) -> str | None:
    return HANDLERS.get(message.type, handle_unsupported)(message)
