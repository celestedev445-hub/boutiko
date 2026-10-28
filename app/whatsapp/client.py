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


def messages_url(phone_number_id: str | None = None) -> str:
    """Send endpoint of the given business number, or of WHATSAPP_PHONE_NUMBER_ID by default."""
    settings = get_settings()
    sender = phone_number_id or settings.whatsapp_phone_number_id
    return f"{GRAPH_URL}/{settings.whatsapp_api_version}/{sender}/messages"


def send_text(to: str, body: str, phone_number_id: str | None = None) -> None:
    """Send from phone_number_id (the number the merchant wrote to), else from the configured number."""
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body},
    }
    response = httpx.post(messages_url(phone_number_id), json=payload, headers=_headers(), timeout=10)
    if response.is_error:
        raise GraphAPIError.from_response(response)

