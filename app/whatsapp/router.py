"""Dispatch an incoming WhatsApp message to the right handler by type.

A handler returns the reply to send, or None when nothing should be sent.
"""

import logging
from collections.abc import Callable

import httpx
from sqlalchemy import select

from app import db, meta_client
from app.logging_config import mask_phone
from app.models import Language, PendingKind, Shop
from app.schemas import Action, Extraction
from app.services import audio, daily, greetings, ledger, pending, products, replies, transcribe, vision
from app.whatsapp import media
from app.whatsapp.client import GraphAPIError
from app.whatsapp.payload import Message

logger = logging.getLogger(__name__)

# Actions that only read the ledger: answered right away, no YES / NO needed.
READ_ONLY_ACTIONS = {Action.STOCK_CHECK, Action.UNKNOWN}


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


def _known_products(owner_phone: str) -> list[str]:
    """The shop's product names, for the model to reuse."""
    try:
        with db.SessionLocal() as session:
            shop_id = session.scalar(select(Shop.id).where(Shop.owner_phone == owner_phone))
            return products.known_names(session, shop_id) if shop_id is not None else []
    except Exception as exc:  # no traceback: SQL errors would print the phone number
        logger.error("Could not list the products of %s: %s", mask_phone(owner_phone), type(exc).__name__)
        return []


def _reply_to_result(result: ledger.LedgerResult, language: Language) -> str:
    if result.choice is not None:
        return replies.product_choice(result.choice, language)
    if result.duplicate:
        return replies.already_saved(language)
    return replies.confirmation(result)


def _answer_pending(message: Message, text: str) -> tuple[str, Language] | None:
    """Answer the merchant's latest pending action, if the message answers it.

    YES / NO for a photo, or the product picked for "Quel savon : Azur ou Palmida ?".
    None if there is nothing pending (or it expired) or the message is something else.
    """
    sender = mask_phone(message.sender)
    decision = pending.parse_decision(text)
    try:
        with db.SessionLocal() as session:
            waiting = pending.latest_active(session, message.sender)
            if waiting is None:
                session.commit()  # keep the expired statuses
                return None
            language = pending.language_of(waiting, session.get(Shop, waiting.shop_id).language)
            pending_id = waiting.id
            if waiting.kind == PendingKind.CHOOSE_PRODUCT:
                option = pending.parse_choice(text, waiting)
                if option is None and decision != pending.Decision.NO:
                    session.commit()
                    return None  # not an answer: a new message; the question stays open
            elif decision is None:
                session.commit()
                return None
            if decision == pending.Decision.NO:
                pending.cancel(session, waiting)
                logger.info("Pending action %s cancelled by %s", pending_id, sender)
                return replies.cancelled(language), language
            if waiting.kind == PendingKind.CHOOSE_PRODUCT:
                result = pending.choose(session, waiting, option)
            else:
                result = pending.confirm(session, waiting)
    except Exception:
        logger.exception("Could not answer the pending action of %s", sender)
        language = _shop_language(message.sender)
        return replies.apology(language), language

    logger.info("Pending action %s answered by %s", pending_id, sender)
    reply_language = language if result.choice or result.duplicate else result.language
    return _reply_to_result(result, language), reply_language


def _welcome(message: Message, text: str) -> tuple[str, Language] | None:
    """"Bonjour", "hello", "how far": a greeting for the time of day and how to use Boutiko."""
    said_in = greetings.parse_greeting(text)
    if said_in is False:
        return None
    if said_in is None:  # English word: Pidgin if that's how the merchant writes
        said_in = Language.PIDGIN if _shop_language(message.sender) == Language.PIDGIN else Language.EN
    return replies.welcome(said_in, greetings.greeting(said_in)), said_in


def _summary_command(message: Message, text: str) -> tuple[str, Language] | None:
    """"résumé", "how market go today", "résumé à 21h"... None if the text is none of them."""
    hour_command = daily.parse_summary_hour(text)
    requested = daily.parse_summary_request(text)
    if hour_command is None and requested is False:
        return None
    try:
        with db.SessionLocal() as session:
            if hour_command is not None:
                return daily.set_summary_hour(session, message.sender, *hour_command)
            return daily.manual_summary(session, message.sender, requested)
    except Exception:
        logger.exception("Summary command failed for %s", mask_phone(message.sender))
        language = _shop_language(message.sender)
        return replies.apology(language), language


def _process_text(message: Message, text: str, transcript: str | None = None) -> str | None:
    """Extraction -> ledger -> reply, shared by text messages and transcribed voice notes.

    These are answered without calling the extraction model: YES / NO for a
    pending photo, the product picked after "Quel savon ?", "résumé", a greeting.
    With a transcript, the reply starts with what was understood.
    """
    for handler in (_answer_pending, _summary_command, _welcome):
        answer = handler(message, text)
        if answer is not None:
            reply, language = answer
            return _with_heard(reply, transcript, language)

    sender = mask_phone(message.sender)
    try:
        extraction = meta_client.extract_from_text(text, known_products=_known_products(message.sender))
    except meta_client.ExtractionError as exc:
        logger.error("Extraction failed for message %s from %s: %s", message.id, sender, exc)
        language = _shop_language(message.sender)
        return _with_heard(replies.apology(language), transcript, language)

    logger.info(
        "Extracted %d action(s) from message %s: %s",
        len(extraction.actions), message.id, ", ".join(a.action.value for a in extraction.actions) or "none",
    )
    return _record(message, extraction, transcript)


def _record(message: Message, extraction: Extraction, transcript: str | None = None) -> str | None:
    """Apply the actions to the ledger and confirm them to the merchant."""
    sender = mask_phone(message.sender)
    try:
        with db.SessionLocal() as session:
            result = ledger.apply_extraction(session, message.sender, extraction, message.id)
            if result.choice is not None:  # nothing recorded: ask which product first
                pending.save_choice(session, message.sender, extraction, message.id, result.choice)
                logger.info("Message %s: product to choose among %d", message.id, len(result.choice.options))
    except Exception:
        logger.exception("Could not record message %s from %s", message.id, sender)
        language = Language(extraction.language) if extraction.language else Language.FR
        return _with_heard(replies.apology(language), transcript, language)

    if result.duplicate:
        logger.info("Message %s was already recorded, no reply", message.id)
        return None
    return _with_heard(_reply_to_result(result, result.language), transcript, result.language)


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


def _photo_already_handled(message_id: str) -> bool:
    try:
        with db.SessionLocal() as session:
            return pending.exists_for_message(session, message_id) or ledger.is_processed(session, message_id)
    except Exception:
        logger.exception("Could not check whether photo %s was already handled", message_id)
        return False


def handle_image(message: Message) -> str | None:
    """Photo: download -> vision model -> recap and YES / NO question (nothing recorded yet)."""
    sender = mask_phone(message.sender)
    if _photo_already_handled(message.id):
        logger.info("Photo %s was already handled, no reply", message.id)
        return None
    language = _shop_language(message.sender)

    declared = (message.image.mime_type or "").split(";")[0].strip().lower()
    if declared not in vision.SUPPORTED_MIME_TYPES:
        logger.info("Photo %s from %s refused: type %s", message.id, sender, declared or "unknown")
        return replies.photo_unsupported(language)
    try:
        data = media.download_media(message.image.id, max_bytes=vision.MAX_IMAGE_BYTES)
    except media.MediaTooLargeError as exc:
        logger.info("Photo %s from %s refused: %s", message.id, sender, exc)
        return replies.photo_too_large(language)
    except (GraphAPIError, httpx.HTTPError) as exc:
        logger.error("Could not download photo %s from %s: %s", message.id, sender, exc)
        return replies.photo_failed(language)
    mime_type = vision.sniff_mime_type(data)  # trust the bytes, not the label
    if mime_type is None:
        logger.info("Photo %s from %s refused: not a JPEG, PNG or WebP file", message.id, sender)
        return replies.photo_unsupported(language)

    try:
        reading = vision.analyze_image(
            data, mime_type, message.image.caption, language, known_products=_known_products(message.sender)
        )
    except vision.VisionError as exc:
        logger.error("Vision failed for photo %s from %s: %s", message.id, sender, exc)
        return replies.photo_failed(language)

    # The caption and what was read are personal data: log only their shape.
    logger.info(
        "Photo %s: %s, %d action(s): %s%s",
        message.id, reading.image_kind.value, len(reading.actions),
        ", ".join(a.action.value for a in reading.actions) or "none",
        ", needs clarification" if reading.needs_clarification.strip() else "",
    )
    if reading.language:
        language = Language(reading.language)
    actions = [a for a in reading.actions if a.action != Action.UNKNOWN]
    extraction = Extraction(actions=actions, language=reading.language)

    if all(a.action in READ_ONLY_ACTIONS for a in actions):
        if actions:  # e.g. a product photo captioned "il me reste combien ?"
            return _record(message, extraction)
        if reading.needs_clarification.strip():
            return replies.clarification(reading.needs_clarification)
        return replies.photo_nothing(language)

    try:
        with db.SessionLocal() as session:
            created = pending.create(session, message.sender, extraction, message.id)
    except Exception:
        logger.exception("Could not store the pending action of photo %s from %s", message.id, sender)
        return replies.photo_failed(language)
    if created is None:
        logger.info("Photo %s already has a pending action, no reply", message.id)
        return None
    return replies.pending_summary(extraction, language, reading.needs_clarification)


def handle_unsupported(message: Message) -> str:
    return "Désolé, je comprends le texte, les notes vocales et les photos."


HANDLERS: dict[str, Callable[[Message], str | None]] = {
    "text": handle_text,
    "audio": handle_audio,
    "image": handle_image,
}


def _after_inbound(message: Message) -> str | None:
    try:
        with db.SessionLocal() as session:
            return daily.after_inbound(session, message.sender, message.phone_number_id)
    except Exception as exc:  # no traceback: SQL errors would print the phone number
        logger.error("Could not note the last message of %s: %s", mask_phone(message.sender), type(exc).__name__)
        return None


def route(message: Message) -> str | None:
    reply = HANDLERS.get(message.type, handle_unsupported)(message)
    # Every message reopens WhatsApp's 24 h window; a skipped summary is offered after the reply.
    offer = _after_inbound(message)
    if reply is not None and offer:
        reply = f"{reply}\n\n{offer}"
    return reply
