"""Dispatch an incoming WhatsApp message to the right handler by type.

For now every handler only echoes what it received, so the end-to-end loop
(WhatsApp -> webhook -> reply) can be tested before the AI pipeline is wired.
"""

from collections.abc import Callable

from app.whatsapp.payload import Message

SHELF_KEYWORDS = ("etagere", "étagère", "rayon", "shelf", "inventaire", "count")


def is_shelf_photo(caption: str | None) -> bool:
    # TODO: let muse-spark classify the photo (receipt vs shelf) instead of relying on the caption.
    return bool(caption) and any(k in caption.lower() for k in SHELF_KEYWORDS)


def handle_text(message: Message) -> str:
    # TODO: meta_client.extract_from_text -> write sales / movements / debts -> confirm to merchant
    return f"Echo: {message.text.body}"


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


HANDLERS: dict[str, Callable[[Message], str]] = {
    "text": handle_text,
    "audio": handle_audio,
    "image": handle_image,
}


def route(message: Message) -> str:
    return HANDLERS.get(message.type, handle_unsupported)(message)
