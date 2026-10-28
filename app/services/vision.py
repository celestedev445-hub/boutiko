"""Read a photo (supplier invoice, receipt, sales notebook, product) with a Meta vision model.

Same actions and strict JSON schema as text extraction, plus image_kind and
needs_clarification. 60 s timeout, one retry on 429, 5xx and network errors.
"""

import base64
import logging
from functools import lru_cache

import openai
from pydantic import ValidationError

from app import meta_client
from app.config import get_settings
from app.languages import Language, language_name
from app.schemas import VisionExtraction, strict_json_schema

logger = logging.getLogger(__name__)

SUPPORTED_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024
REASONING_EFFORT = "low"
RETRYABLE_ERRORS = (openai.RateLimitError, openai.InternalServerError, openai.APIConnectionError)

VISION_PROMPT = """You help small shopkeepers keep their books. The merchant sent a photo on
WhatsApp, sometimes with a caption, in any language. Read it and return the
operations to record. All amounts are in FCFA.

image_kind and what to extract:
- invoice: a supplier invoice or delivery note -> one purchase per line:
  product, quantity, unit, and the unit price in "price" or the line total in
  "total_price", exactly as printed.
- receipt: a till receipt or a page of the merchant's sales notebook -> one sale
  per line; credit_sale when a customer name is written with "crédit", "doit"
  or "credit"; debt_payment for a repayment.
- handwritten_note: any other handwritten list -> the matching actions
  (sale, credit_sale, purchase, debt_payment).
- product: a photo of goods -> the caption decides: "vendu 3 à 500" is a sale of
  3 at 500, "j'ai reçu 2 cartons" a purchase, "il me reste combien ?" a
  stock_check, "j'en ai 20" a set_stock (quantity 20: what the merchant has now). Name the product from what you see (brand and product, e.g.
  "Savon Azur"). Without a caption: no action, and needs_clarification asks
  briefly what to do (sold? bought? check the stock?).
- other: anything else -> no action.

Words naming the shop itself (shop, boutique, magasin, comptoir, store) are never
products.

The golden rule: never invent, calculate or guess a value. If a quantity or a
price is unreadable, cut off or missing, leave it null and say which one in
needs_clarification. needs_clarification is "" when there is nothing to ask.

needs_clarification is written in the merchant's language. language is the
ISO 639 code of the caption's language ("fr", "en", "pcm" for Pidgin, "es",
"ar", "sw", "zh"...) and language_name its English name, both null without a
caption. Keep product names as written or printed, never translated."""


class VisionError(RuntimeError):
    """The model could not be reached or did not return a usable reading."""


def sniff_mime_type(data: bytes) -> str | None:
    """Actual image type from the file's first bytes, or None if it's not a supported image."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


@lru_cache
def vision_response_format() -> dict:
    return {
        "type": "json_schema",
        "json_schema": {"name": "photo_reading", "strict": True, "schema": strict_json_schema(VisionExtraction)},
    }


def _instruction(caption: str | None, language: Language, name: str | None = None) -> str:
    lines = [f"The merchant usually writes in {language_name(language, name)}."]
    lines.append(f"Caption: {caption}" if caption else "No caption.")
    return "\n".join(lines)


def analyze_image(
    image: bytes,
    mime_type: str,
    caption: str | None,
    language: Language,
    known_products: list[str] | None = None,
    language_name: str | None = None,
) -> VisionExtraction:
    """language_name: English name of the merchant's language (shops.language_name), if known."""
    model = get_settings().model_vision
    data_url = f"data:{mime_type};base64,{base64.b64encode(image).decode()}"
    kwargs = dict(
        model=model,
        messages=[
            {"role": "system", "content": VISION_PROMPT},
            *meta_client.known_products_message(known_products),
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _instruction(caption, language, language_name)},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ],
        response_format=vision_response_format(),
        reasoning_effort=REASONING_EFFORT,
    )
    try:
        client = meta_client.get_client()
        try:
            response = client.chat.completions.create(**kwargs)
        except RETRYABLE_ERRORS as exc:
            logger.warning("%s call failed (%s), retrying once", model, type(exc).__name__)
            response = client.chat.completions.create(**kwargs)
        content = response.choices[0].message.content
        if not content:
            raise VisionError("Empty response from the model")
        return VisionExtraction.model_validate_json(content)
    except VisionError:
        raise
    except (openai.OpenAIError, meta_client.MissingAPIKeyError, ValidationError) as exc:
        raise VisionError(f"{type(exc).__name__}: {exc}") from exc
