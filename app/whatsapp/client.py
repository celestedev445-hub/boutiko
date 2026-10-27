"""Outgoing calls to the WhatsApp Cloud API (Graph API)."""

import httpx

from app.config import get_settings

GRAPH_URL = "https://graph.facebook.com"


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {get_settings().whatsapp_access_token}"}


def send_text(to: str, body: str) -> None:
    settings = get_settings()
    url = f"{GRAPH_URL}/{settings.whatsapp_api_version}/{settings.whatsapp_phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body},
    }
    response = httpx.post(url, json=payload, headers=_headers(), timeout=10)
    response.raise_for_status()


def download_media(media_id: str) -> bytes:
    """Fetch a voice note or photo: first resolve the media URL, then download it."""
    settings = get_settings()
    with httpx.Client(headers=_headers(), timeout=30) as http:
        meta = http.get(f"{GRAPH_URL}/{settings.whatsapp_api_version}/{media_id}")
        meta.raise_for_status()
        media = http.get(meta.json()["url"])
        media.raise_for_status()
        return media.content
