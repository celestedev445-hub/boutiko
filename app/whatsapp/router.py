"""Dispatch an incoming WhatsApp message to the right handler by type.

A handler returns the reply to send, or None when nothing should be sent.
"""

import logging
from collections.abc import Callable

from sqlalchemy import select

from app import db, meta_client
from app.logging_config import mask_phone
from app.models import Language, Shop
from app.services import ledger, replies
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


def handle_text(message: Message) -> str | None:
    sender = mask_phone(message.sender)
    try:
        extraction = meta_client.extract_from_text(message.text.body)
    except meta_client.ExtractionError as exc:
        logger.error("Extraction failed for message %s from %s: %s", message.id, sender, exc)
        return replies.apology(_shop_language(message.sender))

    logger.info(
        "Extracted %d action(s) from message %s: %s",
        len(extraction.actions), message.id, ", ".join(a.action.value for a in extraction.actions) or "none",
    )
    try:
        with db.SessionLocal() as session:
            result = ledger.apply_extraction(session, message.sender, extraction, message.id)
    except Exception:
        logger.exception("Could not record message %s from %s", message.id, sender)
        return replies.apology(Language(extraction.language) if extraction.language else Language.FR)

    if result.duplicate:
        logger.info("Message %s was already recorded, no reply", message.id)
        return None
    return replies.confirmation(result)


def handle_audio(message: Message) -> str:
    # TODO: whatsapp.client.download_media -> meta_client.transcribe -> handle as text
    return "Note vocale reçue 🎙️"


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
