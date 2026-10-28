"""Speech-to-text for voice notes, with Meta models only.

1. POST {MODEL_API_BASE_URL}/asr/transcribe (MODEL_ASR), multipart: a JSON
   "request" part and the WAV "audio" part.
2. If that fails or returns an empty transcript: chat.completions with an
   input_audio part (MODEL_ASR_FALLBACK).

Each call: 60 s timeout, one retry on 429, 5xx and network errors.
"""

import base64
import json
import logging
from dataclasses import dataclass

import httpx
import openai

from app import meta_client
from app.config import get_settings

logger = logging.getLogger(__name__)

ASR_PATH = "/asr/transcribe"
TIMEOUT_SECONDS = 60
FALLBACK_MAX_TOKENS = 4000
FALLBACK_PROMPT = (
    "Transcribe this audio exactly as spoken. It may be French, English or Cameroonian Pidgin, "
    "about shop sales, purchases, stock and debts in FCFA. Return only the transcript."
)
RETRYABLE_STATUS = {429} | set(range(500, 600))
RETRYABLE_OPENAI_ERRORS = (openai.RateLimitError, openai.InternalServerError, openai.APIConnectionError)


class TranscriptionError(RuntimeError):
    pass


@dataclass
class Transcription:
    text: str
    model: str
    session_id: str | None = None
    audio_duration_ms: int | None = None


def _http_client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT_SECONDS)


def _post_with_retry(http: httpx.Client, url: str, **kwargs) -> httpx.Response:
    try:
        response = http.post(url, **kwargs)
        if response.status_code not in RETRYABLE_STATUS:
            return response
        logger.warning("ASR returned HTTP %s, retrying once", response.status_code)
    except httpx.TransportError as exc:
        logger.warning("ASR call failed (%s), retrying once", type(exc).__name__)
    return http.post(url, **kwargs)


def transcribe_with_asr(wav: bytes) -> Transcription:
    settings = get_settings()
    if not settings.model_api_key:
        raise TranscriptionError("MODEL_API_KEY is not set")
    url = settings.model_api_base_url.rstrip("/") + ASR_PATH
    request = {"model": settings.model_asr, "audioEncoding": "WAV", "mode": "PUSH_TO_TALK"}
    files = {
        "request": (None, json.dumps(request), "application/json"),
        "audio": ("voice.wav", wav, "audio/wav"),
    }
    headers = {"Authorization": f"Bearer {settings.model_api_key}", "Accept": "application/json"}
    try:
        with _http_client() as http:
            response = _post_with_retry(http, url, files=files, headers=headers)
    except httpx.HTTPError as exc:
        raise TranscriptionError(f"{settings.model_asr}: {type(exc).__name__}") from exc
    if response.is_error:
        raise TranscriptionError(f"{settings.model_asr}: HTTP {response.status_code}: {response.text[:200]}")
    try:
        body = response.json()
    except ValueError as exc:
        raise TranscriptionError(f"{settings.model_asr}: response is not JSON") from exc
    return Transcription(
        text=(body.get("transcript") or "").strip(),
        model=settings.model_asr,
        session_id=body.get("sessionId"),
        audio_duration_ms=body.get("audioDurationMs"),
    )


def transcribe_with_chat(wav: bytes) -> Transcription:
    model = get_settings().model_asr_fallback
    kwargs = dict(
        model=model,
        max_tokens=FALLBACK_MAX_TOKENS,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": FALLBACK_PROMPT},
                    {"type": "input_audio", "input_audio": {"data": base64.b64encode(wav).decode(), "format": "wav"}},
                ],
            }
        ],
    )
    try:
        client = meta_client.get_client()
        try:
            response = client.chat.completions.create(**kwargs)
        except RETRYABLE_OPENAI_ERRORS as exc:
            logger.warning("%s call failed (%s), retrying once", model, type(exc).__name__)
            response = client.chat.completions.create(**kwargs)
    except (openai.OpenAIError, meta_client.MissingAPIKeyError) as exc:
        raise TranscriptionError(f"{model}: {type(exc).__name__}: {exc}") from exc
    text = (response.choices[0].message.content or "").strip().strip('"“”').strip()
    return Transcription(text=text, model=model)


def transcribe(wav: bytes) -> Transcription:
    """Transcript of a WAV file; text is empty when nothing intelligible was said.

    Raises TranscriptionError only when no model could be reached.
    """
    primary: Transcription | None = None
    try:
        primary = transcribe_with_asr(wav)
        if primary.text:
            logger.info("Transcribed with %s (session %s)", primary.model, primary.session_id)
            return primary
        logger.warning("%s returned an empty transcript (session %s), trying fallback", primary.model, primary.session_id)
    except TranscriptionError as exc:
        logger.warning("ASR failed (%s), trying fallback", exc)

    try:
        fallback = transcribe_with_chat(wav)
    except TranscriptionError as exc:
        if primary is not None:
            logger.warning("Fallback failed too (%s), keeping the empty transcript", exc)
            return primary
        raise
    logger.info("Transcribed with %s (fallback)", fallback.model)
    return fallback
