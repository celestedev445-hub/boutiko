"""Fallback provider: Muse Spark Open (Meta open-weights model) on Baseten.

OpenAI-compatible: POST {BASETEN_BASE_URL}/chat/completions with a Bearer key.
Text and images (data: URL in the user message) use the same messages as the
Meta Model API. The model reasons before answering: only the final content is
kept, never a reasoning field or a <think> block.

60 s timeout, one retry on 429, 5xx and network errors. The key is never logged.
"""

import logging
import re

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 60
RETRYABLE_STATUS = {429} | set(range(500, 600))
THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
CODE_FENCE = re.compile(r"^```[a-zA-Z]*\s*\n?(.*?)\n?```$", re.DOTALL)


class BasetenError(RuntimeError):
    """Baseten could not be reached or did not return an answer."""


def is_configured() -> bool:
    settings = get_settings()
    return settings.ai_fallback_enabled and bool(
        settings.baseten_api_key and settings.baseten_api_key.get_secret_value()
    )


def _http_client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT_SECONDS)


def final_content(message: dict) -> str:
    """The answer without the reasoning: reasoning fields are ignored, <think> blocks removed."""
    content = message.get("content") or ""
    if isinstance(content, list):  # content parts
        content = "".join(part.get("text", "") for part in content if part.get("type") == "text")
    content = THINK_BLOCK.sub("", content)
    if "</think>" in content:  # reasoning whose opening tag was left out
        content = content.rsplit("</think>", 1)[1]
    content = content.strip()
    fenced = CODE_FENCE.match(content)
    return fenced.group(1).strip() if fenced else content


def _post_with_retry(http: httpx.Client, url: str, **kwargs) -> httpx.Response:
    try:
        response = http.post(url, **kwargs)
        if response.status_code not in RETRYABLE_STATUS:
            return response
        logger.warning("Baseten returned HTTP %s, retrying once", response.status_code)
    except httpx.TransportError as exc:
        logger.warning("Baseten call failed (%s), retrying once", type(exc).__name__)
    return http.post(url, **kwargs)


def complete(messages: list[dict], response_format: dict | None = None) -> str:
    """Final answer of BASETEN_MODEL for these OpenAI-style messages."""
    settings = get_settings()
    if not settings.baseten_api_key or not settings.baseten_api_key.get_secret_value():
        raise BasetenError("BASETEN_API_KEY is not set")
    body: dict = {"model": settings.baseten_model, "messages": messages}
    if response_format:
        body["response_format"] = response_format
    headers = {"Authorization": f"Bearer {settings.baseten_api_key.get_secret_value()}"}
    url = settings.baseten_base_url.rstrip("/") + "/chat/completions"
    try:
        with _http_client() as http:
            response = _post_with_retry(http, url, json=body, headers=headers)
    except httpx.TransportError as exc:
        raise BasetenError(type(exc).__name__) from exc
    if response.status_code != 200:
        raise BasetenError(f"HTTP {response.status_code}")
    try:
        content = final_content(response.json()["choices"][0]["message"])
    except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
        raise BasetenError(f"Unexpected response ({type(exc).__name__})") from exc
    if not content:
        raise BasetenError("Empty response")
    return content
