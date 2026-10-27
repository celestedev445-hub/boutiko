import hashlib
import hmac
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import ValidationError

from app.config import get_settings
from app.whatsapp import client
from app.whatsapp.payload import Message, WebhookPayload
from app.whatsapp.router import route

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook", tags=["whatsapp"])


@router.get("", response_class=PlainTextResponse)
def verify(
    mode: str = Query(alias="hub.mode"),
    token: str = Query(alias="hub.verify_token"),
    challenge: str = Query(alias="hub.challenge"),
) -> str:
    """Handshake Meta performs when the webhook URL is registered."""
    expected = get_settings().whatsapp_verify_token
    if mode == "subscribe" and expected and hmac.compare_digest(token, expected):
        return challenge
    raise HTTPException(status_code=403, detail="Verification failed")


def _check_signature(body: bytes, header: str | None) -> None:
    secret = get_settings().whatsapp_app_secret
    if not secret:
        return  # local dev without app secret
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not header or not hmac.compare_digest(header, expected):
        raise HTTPException(status_code=401, detail="Invalid signature")


def _reply(message: Message) -> None:
    try:
        client.send_text(message.sender, route(message))
    except Exception:
        logger.exception("Failed to handle message %s", message.id)


@router.post("")
async def receive(request: Request, background: BackgroundTasks) -> dict[str, str]:
    body = await request.body()
    _check_signature(body, request.headers.get("X-Hub-Signature-256"))
    try:
        payload = WebhookPayload.model_validate_json(body)
    except ValidationError:
        raise HTTPException(status_code=400, detail="Invalid payload")
    # Acknowledge immediately; Meta retries if it doesn't get a 200 quickly.
    for message in payload.messages():
        background.add_task(_reply, message)
    return {"status": "ok"}
