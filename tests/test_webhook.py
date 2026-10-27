import hashlib
import hmac
import json

from app.config import get_settings
from tests.conftest import APP_SECRET, VERIFY_TOKEN


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


def _message(type_: str, **content) -> dict:
    return {"from": "237690000000", "id": "wamid.TEST", "timestamp": "1761562800", "type": type_, **content}


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


def test_audio_message_is_acknowledged(client, sent):
    client.post("/webhook", json=_payload(_message("audio", audio={"id": "MEDIA1", "mime_type": "audio/ogg"})))
    assert sent[0][1].startswith("Note vocale reçue")


def test_image_routes_to_receipt_by_default(client, sent):
    client.post("/webhook", json=_payload(_message("image", image={"id": "MEDIA2"})))
    assert "ticket" in sent[0][1]


def test_image_with_shelf_caption_routes_to_shelf(client, sent):
    client.post("/webhook", json=_payload(_message("image", image={"id": "MEDIA3", "caption": "Mon étagère"})))
    assert "étagère" in sent[0][1]


def test_unsupported_type_gets_help_message(client, sent):
    client.post("/webhook", json=_payload(_message("sticker", sticker={"id": "S"})))
    assert "Désolé" in sent[0][1]


def test_status_update_without_messages_sends_nothing(client, sent):
    payload = _payload(_message("text", text={"body": "x"}))
    value = payload["entry"][0]["changes"][0]["value"]
    del value["messages"]
    value["statuses"] = [{"id": "wamid.X", "status": "delivered"}]
    assert client.post("/webhook", json=payload).status_code == 200
    assert sent == []


def test_invalid_payload_is_rejected(client, sent):
    assert client.post("/webhook", json={"hello": "world"}).status_code == 400
    assert sent == []


def test_send_failure_still_returns_200(client, monkeypatch):
    def boom(to, body):
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
    assert _signed_post(client, body, "sha256=deadbeef").status_code == 401
    assert _signed_post(client, body, None).status_code == 401
    assert sent == []
