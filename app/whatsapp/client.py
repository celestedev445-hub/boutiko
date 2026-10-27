"""Outgoing calls to the WhatsApp Cloud API (Graph API)."""

import httpx

from app.config import get_settings

GRAPH_URL = "https://graph.facebook.com"


class GraphAPIError(RuntimeError):
    """Error returned by the Graph API, without the request headers (and so without the token)."""

    def __init__(self, status: int, code: int | None, message: str) -> None:
        self.status = status
        self.code = code
        self.message = message
        super().__init__(f"HTTP {status}, code {code}: {message}")

    @classmethod
    def from_response(cls, response: httpx.Response) -> "GraphAPIError":
        try:
            error = response.json().get("error", {})
        except ValueError:
            error = {}
        return cls(response.status_code, error.get("code"), error.get("message") or response.text[:200])


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {get_settings().whatsapp_access_token}"}


def messages_url() -> str:
    settings = get_settings()
    return f"{GRAPH_URL}/{settings.whatsapp_api_version}/{settings.whatsapp_phone_number_id}/messages"


def send_text(to: str, body: str) -> None:
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body},
    }
    response = httpx.post(messages_url(), json=payload, headers=_headers(), timeout=10)
    if response.is_error:
        raise GraphAPIError.from_response(response)


def download_media(media_id: str) -> bytes:
    """Fetch a voice note or photo: first resolve the media URL, then download it."""
    settings = get_settings()
    with httpx.Client(headers=_headers(), timeout=30) as http:
        meta = http.get(f"{GRAPH_URL}/{settings.whatsapp_api_version}/{media_id}")
        if meta.is_error:
            raise GraphAPIError.from_response(meta)
        media = http.get(meta.json()["url"])
        if media.is_error:
            raise GraphAPIError.from_response(media)
        return media.content
