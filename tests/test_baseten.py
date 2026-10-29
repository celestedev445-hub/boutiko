"""Baseten fallback: Meta API faked, Baseten behind an httpx mock transport (no network)."""

import json
import logging
from types import SimpleNamespace

import httpx
import openai
import pytest

from app import baseten_client, meta_client
from app.config import get_settings
from app.services import vision

KEY = "bt-secret-test-key-123"
REQUEST = httpx.Request("POST", "https://api.meta.ai/v1/chat/completions")
VALID = json.dumps(
    {
        "language": "fr",
        "actions": [
            {"action": "sale", "product": "savons", "quantity": 3, "unit": None, "price": 500, "total_price": None, "customer": None}
        ],
    }
)


def server_error():
    return openai.InternalServerError("unavailable", response=httpx.Response(503, request=REQUEST), body=None)


def rate_limited():
    return openai.RateLimitError("slow down", response=httpx.Response(429, request=REQUEST), body=None)


def bad_request():
    return openai.BadRequestError("bad", response=httpx.Response(400, request=REQUEST), body=None)


def answer(content, **extra):
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content, **extra}}]})


@pytest.fixture
def meta(monkeypatch):
    """Fake Meta client. state["outcomes"]: contents (str) or exceptions, one per call."""
    state = {"outcomes": [], "calls": []}

    def create(**kwargs):
        state["calls"].append(kwargs)
        outcome = state["outcomes"].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=outcome))])

    fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(meta_client, "get_client", lambda: fake)
    return state


@pytest.fixture
def baseten(monkeypatch):
    """Baseten behind a mock transport. state["responses"]: httpx responses or exceptions, one per call."""
    monkeypatch.setenv("BASETEN_API_KEY", KEY)
    get_settings.cache_clear()
    state = {"responses": [], "requests": []}

    def handler(request):
        state["requests"].append(request)
        outcome = state["responses"].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(baseten_client, "_http_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    return state


def test_meta_ok_baseten_never_called(meta, baseten, caplog):
    caplog.set_level(logging.INFO)
    meta["outcomes"] = [VALID]
    extraction = meta_client.extract_from_text("vendu 3 savons à 500")
    assert extraction.actions[0].price == 500
    assert baseten["requests"] == []
    assert "AI answer from meta" in caplog.text


def test_meta_error_goes_to_baseten(meta, baseten, caplog):
    caplog.set_level(logging.INFO)
    meta["outcomes"] = [server_error(), server_error()]
    baseten["responses"] = [answer(VALID)]
    extraction = meta_client.extract_from_text("vendu 3 savons à 500")

    assert extraction.actions[0].product == "savons"
    assert extraction.actions[0].price == 500
    assert len(meta["calls"]) == 2  # Meta retried once first
    request = baseten["requests"][0]
    assert str(request.url) == "https://inference.baseten.co/v1/chat/completions"
    assert request.headers["Authorization"] == f"Bearer {KEY}"
    body = json.loads(request.content)
    assert body["model"] == "meta-models/muse-spark-open-1026"
    assert body["messages"] == meta["calls"][0]["messages"]
    assert body["response_format"] == meta_client.extraction_response_format()
    assert "AI answer from baseten" in caplog.text


@pytest.mark.parametrize("error", [rate_limited, lambda: openai.APITimeoutError(request=REQUEST)])
def test_429_and_timeout_go_to_baseten(meta, baseten, error):
    meta["outcomes"] = [error(), error()]
    baseten["responses"] = [answer(VALID)]
    assert meta_client.extract_from_text("x").actions[0].price == 500
    assert len(baseten["requests"]) == 1


def test_invalid_meta_json_goes_to_baseten(meta, baseten):
    meta["outcomes"] = ['{"actions": "nope"}']
    baseten["responses"] = [answer(VALID)]
    assert meta_client.extract_from_text("x").actions[0].quantity == 3


def test_baseten_answer_is_validated(meta, baseten):
    meta["outcomes"] = [server_error(), server_error()]
    baseten["responses"] = [answer('{"actions": [{"action": "steal"}]}')]
    with pytest.raises(meta_client.ExtractionError, match="InternalServerError"):
        meta_client.extract_from_text("x")


def test_both_fail_same_error_as_before(meta, baseten):
    meta["outcomes"] = [server_error(), server_error()]
    baseten["responses"] = [httpx.Response(500), httpx.ConnectError("down")]
    with pytest.raises(meta_client.ExtractionError, match="InternalServerError"):
        meta_client.extract_from_text("x")
    assert len(baseten["requests"]) == 2  # Baseten retried once too


def test_baseten_retries_once_on_429(meta, baseten):
    meta["outcomes"] = [server_error(), server_error()]
    baseten["responses"] = [httpx.Response(429), answer(VALID)]
    assert meta_client.extract_from_text("x").actions[0].price == 500


def test_client_error_does_not_fall_back(meta, baseten):
    meta["outcomes"] = [bad_request()]
    with pytest.raises(meta_client.ExtractionError):
        meta_client.extract_from_text("x")
    assert baseten["requests"] == []


def test_no_key_no_fallback(meta, baseten, monkeypatch):
    monkeypatch.delenv("BASETEN_API_KEY")
    get_settings.cache_clear()
    meta["outcomes"] = [server_error(), server_error()]
    with pytest.raises(meta_client.ExtractionError):
        meta_client.extract_from_text("x")
    assert baseten["requests"] == []


def test_fallback_can_be_disabled(meta, baseten, monkeypatch):
    monkeypatch.setenv("AI_FALLBACK_ENABLED", "false")
    get_settings.cache_clear()
    meta["outcomes"] = [server_error(), server_error()]
    with pytest.raises(meta_client.ExtractionError):
        meta_client.extract_from_text("x")
    assert baseten["requests"] == []


def test_reasoning_is_ignored(meta, baseten):
    meta["outcomes"] = [server_error(), server_error()]
    baseten["responses"] = [
        answer(
            "<think>3 savons à 500, total 1500 ? Non, ne pas calculer.</think>\n" + VALID,
            reasoning_content='{"actions": [{"action": "purchase"}]}',
        )
    ]
    extraction = meta_client.extract_from_text("x")
    assert extraction.actions[0].action == "sale"
    assert extraction.actions[0].total_price is None


@pytest.mark.parametrize(
    "content",
    [
        VALID,
        "<think>reasoning</think>" + VALID,
        "reasoning without opening tag</think>\n\n" + VALID,
        "<think>a</think>```json\n" + VALID + "\n```",
        [{"type": "text", "text": "<think>a</think>"}, {"type": "text", "text": VALID}],
    ],
)
def test_final_content(content):
    assert baseten_client.final_content({"content": content, "reasoning": "ignored"}) == VALID


def test_image_is_sent_as_data_url(meta, baseten):
    meta["outcomes"] = [rate_limited(), rate_limited()]
    reading = {"image_kind": "invoice", "needs_clarification": "", **json.loads(VALID)}
    baseten["responses"] = [answer(json.dumps(reading))]
    result = vision.analyze_image(b"\xff\xd8\xffjpeg", "image/jpeg", None, "fr")
    assert result.image_kind == "invoice"
    user = json.loads(baseten["requests"][0].content)["messages"][-1]
    assert user["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_free_text_completion_falls_back(meta, baseten):
    """Evening summary and translations: create_completion."""
    meta["outcomes"] = [openai.APIConnectionError(request=REQUEST), openai.APIConnectionError(request=REQUEST)]
    baseten["responses"] = [answer("<think>hmm</think>Bonsoir !")]
    response = meta_client.create_completion(model="m", messages=[{"role": "user", "content": "hi"}])
    assert response.choices[0].message.content == "Bonsoir !"
    assert "response_format" not in json.loads(baseten["requests"][0].content)


def test_free_text_both_fail_raise_the_meta_error(meta, baseten):
    meta["outcomes"] = [server_error(), server_error()]
    baseten["responses"] = [httpx.Response(503), httpx.Response(503)]
    with pytest.raises(openai.InternalServerError):
        meta_client.create_completion(model="m", messages=[])


def test_key_never_logged(meta, baseten, caplog):
    caplog.set_level(logging.DEBUG)
    meta["outcomes"] = [server_error(), server_error(), server_error(), server_error()]
    baseten["responses"] = [httpx.Response(401, json={"error": "bad key"}), answer(VALID)]
    with pytest.raises(meta_client.ExtractionError):
        meta_client.extract_from_text("vendu 3 savons à 500")
    meta_client.extract_from_text("vendu 3 savons à 500")
    assert KEY not in caplog.text
    assert "savons" not in caplog.text
    assert KEY not in repr(get_settings())
