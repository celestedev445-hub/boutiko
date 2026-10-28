"""Outgoing Graph API call and logging, with httpx mocked (no network)."""

import logging

import httpx
import pytest

from app.config import get_settings
from app.logging_config import RedactSecretsFilter, mask_phone
from app.whatsapp import client as wa_client
from tests.test_webhook import _message, _payload

pytestmark = pytest.mark.usefixtures("echo_handler")

ACCESS_TOKEN = "EAAG-test-access-token"


@pytest.fixture
def graph(monkeypatch):
    """Replace httpx.post in the WhatsApp client and record the calls."""
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", ACCESS_TOKEN)
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "1234567890")
    monkeypatch.delenv("WHATSAPP_API_VERSION", raising=False)
    get_settings.cache_clear()

    calls: list[dict] = []
    state = {"response": httpx.Response(200, json={"messages": [{"id": "wamid.OUT"}]})}

    def fake_post(url, json, headers, timeout):
        calls.append({"url": url, "json": json, "headers": headers})
        return state["response"]

    monkeypatch.setattr(wa_client.httpx, "post", fake_post)
    return calls, state


def test_echo_is_posted_to_graph_messages_endpoint(client, graph):
    calls, _ = graph
    client.post("/webhook", json=_payload(_message("text", text={"body": "Bonsoir"})))

    assert len(calls) == 1
    # Sent from the number that received the message (webhook metadata), not WHATSAPP_PHONE_NUMBER_ID.
    assert calls[0]["url"] == "https://graph.facebook.com/v21.0/123/messages"
    assert calls[0]["headers"] == {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    assert calls[0]["json"] == {
        "messaging_product": "whatsapp",
        "to": "237690000000",
        "type": "text",
        "text": {"body": "Echo: Bonsoir"},
    }


def test_logs_mask_phone_and_never_show_token(client, graph, caplog):
    caplog.set_level(logging.INFO)
    client.post("/webhook", json=_payload(_message("text", text={"body": "Bonsoir"})))

    assert "********0000" in caplog.text
    assert "237690000000" not in caplog.text
    assert ACCESS_TOKEN not in caplog.text
    assert "Message received" in caplog.text
    assert "Reply sent" in caplog.text


def test_graph_error_is_logged_with_code_and_message(client, graph, caplog):
    _, state = graph
    state["response"] = httpx.Response(
        401,
        json={"error": {"message": "Error validating access token", "type": "OAuthException", "code": 190}},
    )
    caplog.set_level(logging.INFO)

    response = client.post("/webhook", json=_payload(_message("text", text={"body": "hi"})))

    assert response.status_code == 200
    assert "Graph API error" in caplog.text
    assert "code 190" in caplog.text
    assert "Error validating access token" in caplog.text
    assert ACCESS_TOKEN not in caplog.text


def test_mask_phone():
    assert mask_phone("237690001234") == "********1234"
    assert mask_phone("123") == "123"


def test_access_log_redacts_verify_token():
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 0, '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5000", "GET", "/webhook?hub.mode=subscribe&hub.verify_token=s3cret&hub.challenge=42", "1.1", 200),
        None,
    )
    RedactSecretsFilter().filter(record)
    message = record.getMessage()
    assert "s3cret" not in message
    assert "hub.verify_token=***&hub.challenge=42" in message


def test_reply_without_metadata_uses_the_configured_number(client, graph):
    calls, _ = graph
    payload = _payload(_message("text", text={"body": "Bonsoir"}))
    del payload["entry"][0]["changes"][0]["value"]["metadata"]
    client.post("/webhook", json=payload)

    assert [c["url"] for c in calls] == ["https://graph.facebook.com/v21.0/1234567890/messages"]


def test_send_text_without_phone_number_id_uses_the_setting(graph):
    calls, _ = graph
    wa_client.send_text("237690000000", "Bonsoir")
    wa_client.send_text("237690000000", "Bonsoir", phone_number_id="999")
    assert [c["url"] for c in calls] == [
        "https://graph.facebook.com/v21.0/1234567890/messages",
        "https://graph.facebook.com/v21.0/999/messages",
    ]
