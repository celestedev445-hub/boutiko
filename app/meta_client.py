"""Client for the Meta Model API (OpenAI-compatible).

TODO: every call below follows the OpenAI wire format. Check each one against
the Meta Model API docs before relying on it — model names, the structured
output format, the transcription endpoint and the SAM endpoint in particular.
"""

import base64
from functools import lru_cache

from openai import OpenAI

from app.config import get_settings
from app.schemas import Extraction

TEXT_MODEL = "muse-spark-1.3"  # JSON extraction, vision, tool use
VOICE_MODEL = "muse-voice-transcribe-1.0"  # voice notes
SEGMENT_MODEL = "sam-3.1"  # counting products on a shelf

DEFAULT_BASE_URL = "https://api.meta.ai/v1"

EXTRACTION_PROMPT = """You help small shopkeepers in Cameroon keep their books.
The merchant writes in French, English or Cameroonian Pidgin, often with local
product names and abbreviations. Extract every stock, sale or debt operation
from the message. All amounts are in FCFA.

Prices: "price" is the price of ONE unit; "total_price" is the amount for the
whole line. Fill only the one the merchant actually says:
- "3 savons à 250" -> price 250, total_price null
- "3 sacs pour 25 000" -> total_price 25000, price null

Never calculate, convert or guess a value (no multiplying, no dividing, no
filling a price from memory). If something is not said, leave the field null."""


class MissingAPIKeyError(RuntimeError):
    pass


@lru_cache
def get_client() -> OpenAI:
    settings = get_settings()
    if not settings.model_api_key:
        raise MissingAPIKeyError("MODEL_API_KEY is not set")
    return OpenAI(api_key=settings.model_api_key, base_url=settings.model_api_base_url or DEFAULT_BASE_URL)


def _json_schema_format() -> dict:
    # TODO: confirm Meta supports response_format={"type": "json_schema"} with strict mode,
    # otherwise fall back to {"type": "json_object"} and validate with Pydantic only.
    return {
        "type": "json_schema",
        "json_schema": {"name": "extraction", "schema": Extraction.model_json_schema()},
    }


def extract_from_text(text: str) -> Extraction:
    response = get_client().chat.completions.create(
        model=TEXT_MODEL,
        messages=[
            {"role": "system", "content": EXTRACTION_PROMPT},
            {"role": "user", "content": text},
        ],
        response_format=_json_schema_format(),
        temperature=0,
    )
    return Extraction.model_validate_json(response.choices[0].message.content or "{}")


def extract_from_receipt(image: bytes, mime_type: str = "image/jpeg") -> Extraction:
    """Read a supplier receipt / handwritten ticket photo."""
    # TODO: check the accepted image input format (data URL vs uploaded file id) and size limits.
    data_url = f"data:{mime_type};base64,{base64.b64encode(image).decode()}"
    response = get_client().chat.completions.create(
        model=TEXT_MODEL,
        messages=[
            {"role": "system", "content": EXTRACTION_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "This is a receipt. Extract each line as an action."},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ],
        response_format=_json_schema_format(),
        temperature=0,
    )
    return Extraction.model_validate_json(response.choices[0].message.content or "{}")


def transcribe(audio: bytes, filename: str = "voice.ogg") -> str:
    """Transcribe a WhatsApp voice note (OGG/Opus)."""
    # TODO: confirm the endpoint is /audio/transcriptions, that OGG/Opus is accepted as-is,
    # and whether a language hint (fr/en/pcm) improves results.
    result = get_client().audio.transcriptions.create(model=VOICE_MODEL, file=(filename, audio))
    return result.text


def count_shelf(image: bytes, product_hints: list[str]) -> dict[str, int]:
    """Count visible units per product on a shelf photo."""
    # TODO: SAM 3.1 is a segmentation model, not a chat model — find out how the Meta Model API
    # exposes it (endpoint, text-prompt format, response shape with masks/boxes) and implement.
    raise NotImplementedError("Shelf counting with sam-3.1 is not wired yet")
