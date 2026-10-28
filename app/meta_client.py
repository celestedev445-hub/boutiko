"""Client for the Meta Model API (OpenAI-compatible).

Verified against the API: base URL https://api.meta.ai/v1, chat.completions,
strict json_schema structured output, and reasoning_effort (the text model
reasons; "none" is rejected with a 400, so we use "low").

Voice notes are transcribed in app/services/transcribe.py.

TODO: still unverified — image input format and the SAM endpoint.
"""

import base64
import logging
from functools import lru_cache

import openai
from openai import OpenAI
from pydantic import ValidationError

from app.config import get_settings
from app.schemas import Extraction, strict_json_schema

logger = logging.getLogger(__name__)

SEGMENT_MODEL = "sam-3.1"  # counting products on a shelf

DEFAULT_BASE_URL = "https://api.meta.ai/v1"
TIMEOUT_SECONDS = 60
REASONING_EFFORT = "low"
# Retried once: network errors (timeouts included) and 5xx.
RETRYABLE_ERRORS = (openai.APIConnectionError, openai.InternalServerError)

EXTRACTION_PROMPT = """You help small shopkeepers in Cameroon keep their books.
The merchant writes in French, English or Cameroonian Pidgin, often mixing them,
with local product names and abbreviations. Extract every operation in the
message, in order. All amounts are in FCFA ("francs", "F", "frs", "k" = 1000
only when the merchant writes it, e.g. "25k").

Actions:
- sale: goods sold and paid now
- credit_sale: goods taken by a named customer without paying (crédit, "go pay later")
- debt_payment: a customer pays back money they owe; put the amount in total_price
- purchase: goods bought from a supplier to restock
- stock_check: the merchant asks how much of a product is left
- unknown: anything else (greetings, questions you can't map)

Prices: "price" is the price of ONE unit; "total_price" is the amount for the
whole line. Fill only the one the merchant actually says.

The golden rule: never calculate, convert or guess a value. No multiplying, no
dividing, no price from memory, no default quantity. If something is not said,
leave the field null. Keep product and customer names as written.

language: "fr", "en" or "pcm" (Pidgin) — the main language of the message.

Examples (only the relevant fields shown; every other field is null):
"vendu 3 savons à 250" -> fr; sale, product "savons", quantity 3, price 250
"j'ai vendu 3 sacs de riz pour 25 000" -> fr; sale, product "riz", quantity 3, unit "sac", total_price 25000
"Marie a pris 2 kg de sucre à crédit, 1500" -> fr; credit_sale, product "sucre", quantity 2, unit "kg", total_price 1500, customer "Marie"
"Papa Joseph m'a remboursé 2000" -> fr; debt_payment, customer "Papa Joseph", total_price 2000
"il reste combien de Maggi ?" -> fr; stock_check, product "Maggi"
"sold 2 crates of Guinness at 9000 each" -> en; sale, product "Guinness", quantity 2, unit "crate", price 9000
"bought 10 cartons of tomato paste, 42 000 total" -> en; purchase, product "tomato paste", quantity 10, unit "carton", total_price 42000
"I don sell 4 tin milk 600 each, Mama Ngozi take 1 bag rice for credit 23k" -> pcm;
  sale, product "milk", quantity 4, unit "tin", price 600;
  credit_sale, product "rice", quantity 1, unit "bag", total_price 23000, customer "Mama Ngozi"
"Ekane don pay 5000 for wetin e owe" -> pcm; debt_payment, customer "Ekane", total_price 5000
"how many soap remain?" -> en; stock_check, product "soap"
"I buy 5 packet sugar" -> pcm; purchase, product "sugar", quantity 5, unit "packet" (no price said: price and total_price null)
"bonjour" -> fr; one unknown action"""


class MissingAPIKeyError(RuntimeError):
    pass


class ExtractionError(RuntimeError):
    """The model could not be reached or did not return a usable extraction."""


@lru_cache
def get_client() -> OpenAI:
    settings = get_settings()
    if not settings.model_api_key:
        raise MissingAPIKeyError("MODEL_API_KEY is not set")
    return OpenAI(
        api_key=settings.model_api_key,
        base_url=settings.model_api_base_url or DEFAULT_BASE_URL,
        timeout=TIMEOUT_SECONDS,
        max_retries=0,  # retries are handled in _create, with our own policy
    )


@lru_cache
def extraction_response_format() -> dict:
    return {
        "type": "json_schema",
        "json_schema": {"name": "extraction", "strict": True, "schema": strict_json_schema(Extraction)},
    }


def _create(**kwargs):
    client = get_client()
    try:
        return client.chat.completions.create(**kwargs)
    except RETRYABLE_ERRORS as exc:
        logger.warning("Meta API call failed (%s), retrying once", type(exc).__name__)
    return client.chat.completions.create(**kwargs)


def _extract(user_content) -> Extraction:
    try:
        response = _create(
            model=get_settings().model_text,
            messages=[
                {"role": "system", "content": EXTRACTION_PROMPT},
                {"role": "user", "content": user_content},
            ],
            response_format=extraction_response_format(),
            reasoning_effort=REASONING_EFFORT,
        )
        content = response.choices[0].message.content
        if not content:
            raise ExtractionError("Empty response from the model")
        return Extraction.model_validate_json(content)
    except ExtractionError:
        raise
    except (openai.OpenAIError, MissingAPIKeyError, ValidationError) as exc:
        raise ExtractionError(f"{type(exc).__name__}: {exc}") from exc


def extract_from_text(text: str) -> Extraction:
    return _extract(text)


def extract_from_receipt(image: bytes, mime_type: str = "image/jpeg") -> Extraction:
    """Read a supplier receipt / handwritten ticket photo."""
    # TODO: check the accepted image input format (data URL vs uploaded file id) and size limits.
    data_url = f"data:{mime_type};base64,{base64.b64encode(image).decode()}"
    return _extract(
        [
            {"type": "text", "text": "This is a supplier receipt. Extract each line as a purchase."},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]
    )


def count_shelf(image: bytes, product_hints: list[str]) -> dict[str, int]:
    """Count visible units per product on a shelf photo."""
    # TODO: SAM 3.1 is a segmentation model, not a chat model — find out how the Meta Model API
    # exposes it (endpoint, text-prompt format, response shape with masks/boxes) and implement.
    raise NotImplementedError("Shelf counting with sam-3.1 is not wired yet")
