"""Download media (voice notes, photos) sent to the WhatsApp number.

Two Graph API calls, both with the access token: GET /{media_id} returns a
short-lived URL, then GET on that URL returns the file.
"""

import httpx

from app.config import get_settings
from app.whatsapp.client import GRAPH_URL, GraphAPIError

MAX_MEDIA_BYTES = 16 * 1024 * 1024  # WhatsApp's own limit for audio
TIMEOUT_SECONDS = 30


class MediaTooLargeError(RuntimeError):
    pass


def _http_client() -> httpx.Client:
    return httpx.Client(
        headers={"Authorization": f"Bearer {get_settings().whatsapp_access_token}"},
        timeout=TIMEOUT_SECONDS,
    )


def download_media(media_id: str, max_bytes: int = MAX_MEDIA_BYTES) -> bytes:
    """Bytes of a media file. Raises MediaTooLargeError above max_bytes, GraphAPIError on API errors."""
    settings = get_settings()
    with _http_client() as http:
        meta = http.get(f"{GRAPH_URL}/{settings.whatsapp_api_version}/{media_id}")
        if meta.is_error:
            raise GraphAPIError.from_response(meta)
        info = meta.json()
        if (info.get("file_size") or 0) > max_bytes:
            raise MediaTooLargeError(f"{info['file_size']} bytes")

        # The announced size can be missing or wrong: count while downloading too.
        with http.stream("GET", info["url"]) as response:
            if response.is_error:
                response.read()
                raise GraphAPIError.from_response(response)
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise MediaTooLargeError(f"more than {max_bytes} bytes")
                chunks.append(chunk)
    return b"".join(chunks)
