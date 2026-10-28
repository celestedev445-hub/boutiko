"""Meta Model API client, with the OpenAI client replaced by a fake (no network)."""

import json
from types import SimpleNamespace

import httpx2
import openai
import pytest

from app import meta_client
from app.config import get_settings
from app.schemas import Action, Extraction, strict_json_schema

REQUEST = httpx2.Request("POST", "https://api.meta.ai/v1/chat/completions")
VALID = json.dumps(
    {
        "language": "fr",
        "actions": [
            {"action": "sale", "product": "savon", "quantity": 3, "unit": None, "price": 250, "total_price": None, "customer": None}
        ],
    }
)


def server_error():
    return openai.InternalServerError("unavailable", response=httpx2.Response(503, request=REQUEST), body=None)


def bad_request():
    return openai.BadRequestError("bad", response=httpx2.Response(400, request=REQUEST), body=None)


class FakeCompletions:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=outcome))])


@pytest.fixture
def fake_api(monkeypatch):
    def install(*outcomes):
        completions = FakeCompletions(outcomes)
        fake = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        monkeypatch.setattr(meta_client, "get_client", lambda: fake)
        return completions

    return install


# --- strict schema ---


def _objects(node):
    if isinstance(node, dict):
        if node.get("type") == "object":
            yield node
        for value in node.values():
            yield from _objects(value)
    elif isinstance(node, list):
        for value in node:
            yield from _objects(value)


def test_strict_schema_closes_every_object_and_requires_all_fields():
    schema = strict_json_schema(Extraction)
    objects = list(_objects(schema))
    assert len(objects) == 2  # Extraction and ExtractedAction
    for obj in objects:
        assert obj["additionalProperties"] is False
        assert sorted(obj["required"]) == sorted(obj["properties"])
    assert "$ref" not in json.dumps(schema)
    assert "$defs" not in schema


def test_strict_schema_optional_fields_are_nullable():
    action = strict_json_schema(Extraction)["properties"]["actions"]["items"]["properties"]
    assert action["action"] == {"type": "string", "enum": [a.value for a in Action]}
    assert action["product"]["type"] == ["string", "null"]
    assert action["quantity"]["type"] == ["number", "null"]
    assert action["price"]["type"] == ["integer", "null"]
    assert action["total_price"]["type"] == ["integer", "null"]
    language = strict_json_schema(Extraction)["properties"]["language"]
    assert language["type"] == ["string", "null"]
    assert language["enum"] == ["fr", "en", "pcm", None]


# --- request ---


def test_request_parameters(fake_api):
    api = fake_api(VALID)
    extraction = meta_client.extract_from_text("vendu 3 savons à 250")

    assert extraction.actions[0].price == 250
    call = api.calls[0]
    assert call["model"] == "muse-spark-1.3-contributor"
    assert call["reasoning_effort"] == "low"
    assert call["response_format"]["type"] == "json_schema"
    assert call["response_format"]["json_schema"]["strict"] is True
    assert call["response_format"]["json_schema"]["schema"] == strict_json_schema(Extraction)
    assert call["messages"][0] == {"role": "system", "content": meta_client.EXTRACTION_PROMPT}
    assert call["messages"][1] == {"role": "user", "content": "vendu 3 savons à 250"}


def test_model_is_configurable(fake_api, monkeypatch):
    monkeypatch.setenv("MODEL_TEXT", "muse-spark-1.3")
    get_settings.cache_clear()
    api = fake_api(VALID)
    meta_client.extract_from_text("x")
    assert api.calls[0]["model"] == "muse-spark-1.3"


def test_client_timeout_and_no_sdk_retries(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "dummy-test-value")
    get_settings.cache_clear()
    meta_client.get_client.cache_clear()
    try:
        client = meta_client.get_client()
        assert client.timeout == 60
        assert client.max_retries == 0
    finally:
        meta_client.get_client.cache_clear()


# --- retries and errors ---


@pytest.mark.parametrize("error", [server_error(), openai.APIConnectionError(request=REQUEST), openai.APITimeoutError(request=REQUEST)])
def test_retries_once_on_network_or_5xx(fake_api, error):
    api = fake_api(error, VALID)
    assert meta_client.extract_from_text("x").actions[0].product == "savon"
    assert len(api.calls) == 2


def test_gives_up_after_one_retry(fake_api):
    api = fake_api(server_error(), server_error())
    with pytest.raises(meta_client.ExtractionError):
        meta_client.extract_from_text("x")
    assert len(api.calls) == 2


def test_no_retry_on_4xx(fake_api):
    api = fake_api(bad_request())
    with pytest.raises(meta_client.ExtractionError):
        meta_client.extract_from_text("x")
    assert len(api.calls) == 1


@pytest.mark.parametrize("content", ["", None, "not json", '{"actions": [{"action": "steal"}]}'])
def test_unusable_response_raises_extraction_error(fake_api, content):
    fake_api(content)
    with pytest.raises(meta_client.ExtractionError):
        meta_client.extract_from_text("x")


def test_missing_api_key_raises_extraction_error():
    # conftest removes MODEL_API_KEY
    with pytest.raises(meta_client.ExtractionError, match="MissingAPIKeyError"):
        meta_client.extract_from_text("x")
