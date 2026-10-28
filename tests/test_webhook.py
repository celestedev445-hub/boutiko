import hashlib
import hmac
import json

import pytest

from app.config import get_settings
from tests.conftest import APP_SECRET, VERIFY_TOKEN

pytestmark = pytest.mark.usefixtures("echo_handler")


def _payload(message: dict) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA_ID",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"display_phone_number": "15550000000", "phone_number_id": "123"},
                            "contacts": [{"profile": {"name": "Mama Ngono"}, "wa_id": "237690000000"}],
                            "messages": [message],
                        },
                    }
                ],
            }
        ],
    }


def _message(type_: str, id_: str = "wamid.TEST", **content) -> dict:
    return {"from": "237690000000", "id": id_, "timestamp": "1761562800", "type": type_, **content}


# --- GET verification ---


def test_verify_returns_challenge(client):
    response = client.get(
        "/webhook",
        params={"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "1158201444"},
    )
    assert response.status_code == 200
    assert response.text == "1158201444"


def test_verify_rejects_wrong_token(client):
    response = client.get(
        "/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "x"}
    )
    assert response.status_code == 403


def test_verify_rejects_when_no_token_configured(client, monkeypatch):
    monkeypatch.delenv("WHATSAPP_VERIFY_TOKEN")
    get_settings.cache_clear()
    response = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "", "hub.challenge": "x"})
    assert response.status_code == 403


def test_verify_requires_params(client):
    assert client.get("/webhook").status_code == 422


# --- POST reception ---


def test_text_message_is_echoed(client, sent):
    response = client.post("/webhook", json=_payload(_message("text", text={"body": "vendu 3 savons à 250"})))
    assert response.status_code == 200
    assert sent == [("237690000000", "Echo: vendu 3 savons à 250")]


def test_audio_message_goes_to_audio_handler(client, sent, monkeypatch):
    from app.whatsapp import router

    seen = []
    monkeypatch.setitem(router.HANDLERS, "audio", lambda message: seen.append(message) or "ok")
    audio = {"id": "MEDIA1", "mime_type": "audio/ogg; codecs=opus", "voice": True}
    assert client.post("/webhook", json=_payload(_message("audio", audio=audio))).status_code == 200
    assert seen[0].audio.id == "MEDIA1"
    assert seen[0].audio.voice is True
    assert sent == [("237690000000", "ok")]


def test_image_message_goes_to_image_handler(client, sent, monkeypatch):
    from app.whatsapp import router

    seen = []
    monkeypatch.setitem(router.HANDLERS, "image", lambda message: seen.append(message) or "ok")
    image = {"id": "MEDIA2", "mime_type": "image/jpeg", "caption": "vendu 3 à 500"}
    assert client.post("/webhook", json=_payload(_message("image", image=image))).status_code == 200
    assert (seen[0].image.id, seen[0].image.mime_type, seen[0].image.caption) == ("MEDIA2", "image/jpeg", "vendu 3 à 500")
    assert sent == [("237690000000", "ok")]


def test_unsupported_type_gets_help_message(client, sent):
    client.post("/webhook", json=_payload(_message("sticker", sticker={"id": "S"})))
    assert "Désolé" in sent[0][1]


@pytest.mark.parametrize("status", ["sent", "delivered", "read"])
def test_status_update_sends_nothing(client, sent, status):
    payload = _payload(_message("text", text={"body": "x"}))
    value = payload["entry"][0]["changes"][0]["value"]
    del value["messages"]
    value["statuses"] = [{"id": "wamid.X", "status": status, "recipient_id": "237690000000"}]
    assert client.post("/webhook", json=payload).status_code == 200
    assert sent == []


def test_unexpected_payload_still_gets_200(client, sent):
    assert client.post("/webhook", json={"hello": "world"}).status_code == 200
    assert sent == []


def test_duplicate_message_is_answered_once(client, sent):
    payload = _payload(_message("text", id_="wamid.DUP", text={"body": "bonjour"}))
    assert client.post("/webhook", json=payload).status_code == 200
    assert client.post("/webhook", json=payload).status_code == 200
    assert sent == [("237690000000", "Echo: bonjour")]


def test_different_messages_are_all_answered(client, sent):
    client.post("/webhook", json=_payload(_message("text", id_="wamid.A", text={"body": "un"})))
    client.post("/webhook", json=_payload(_message("text", id_="wamid.B", text={"body": "deux"})))
    assert [body for _, body in sent] == ["Echo: un", "Echo: deux"]


def test_signature_not_checked_without_secret(client, sent):
    body = json.dumps(_payload(_message("text", text={"body": "hi"}))).encode()
    assert _signed_post(client, body, "sha256=whatever").status_code == 200
    assert len(sent) == 1


def test_send_failure_still_returns_200(client, monkeypatch):
    def boom(to, body, phone_number_id=None):
        raise RuntimeError("Graph API down")

    monkeypatch.setattr("app.whatsapp.client.send_text", boom)
    response = client.post("/webhook", json=_payload(_message("text", text={"body": "hi"})))
    assert response.status_code == 200


# --- Signature ---


def _signed_post(client, body: bytes, signature: str | None):
    headers = {"Content-Type": "application/json"}
    if signature is not None:
        headers["X-Hub-Signature-256"] = signature
    return client.post("/webhook", content=body, headers=headers)


def test_valid_signature_is_accepted(client, sent, monkeypatch):
    monkeypatch.setenv("WHATSAPP_APP_SECRET", APP_SECRET)
    get_settings.cache_clear()
    body = json.dumps(_payload(_message("text", text={"body": "hi"}))).encode()
    signature = "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert _signed_post(client, body, signature).status_code == 200
    assert len(sent) == 1


def test_bad_or_missing_signature_is_rejected(client, sent, monkeypatch):
    monkeypatch.setenv("WHATSAPP_APP_SECRET", APP_SECRET)
    get_settings.cache_clear()
    body = json.dumps(_payload(_message("text", text={"body": "hi"}))).encode()
    assert _signed_post(client, body, "sha256=deadbeef").status_code == 403
    assert _signed_post(client, body, None).status_code == 403
    assert sent == []


# --- Receiving number ---


def test_messages_carry_the_phone_number_id_of_their_change():
    from app.whatsapp.payload import WebhookPayload

    payload = _payload(_message("text", id_="wamid.A", text={"body": "a"}))
    other = _payload(_message("text", id_="wamid.B", text={"body": "b"}))["entry"][0]["changes"][0]
    other["value"]["metadata"] = {"display_phone_number": "237622510967", "phone_number_id": "456"}
    payload["entry"][0]["changes"].append(other)

    messages = WebhookPayload.model_validate(payload).messages()
    assert [(m.id, m.phone_number_id) for m in messages] == [("wamid.A", "123"), ("wamid.B", "456")]


def test_reply_is_sent_from_the_receiving_number(client, monkeypatch):
    calls = []
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "configured")
    monkeypatch.setattr(
        "app.whatsapp.client.send_text",
        lambda to, body, phone_number_id=None: calls.append((to, phone_number_id)),
    )
    client.post("/webhook", json=_payload(_message("text", text={"body": "hi"})))
    assert calls == [("237690000000", "123")]
