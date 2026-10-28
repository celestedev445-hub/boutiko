import hashlib
import hmac
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import ValidationError

from app.config import get_settings
from app.logging_config import mask_phone
from app.whatsapp import client
from app.whatsapp.dedup import seen_messages
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
    if mode == "subscribe" and expected and hmac.compare_digest(token.encode(), expected.encode()):
        logger.info("Webhook verified")
        return challenge
    logger.warning("Webhook verification failed (mode=%s)", mode)
    raise HTTPException(status_code=403, detail="Verification failed")


def _check_signature(body: bytes, header: str | None) -> None:
    secret = get_settings().whatsapp_app_secret
    if not secret:
        return  # local dev without app secret
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not header or not hmac.compare_digest(header.encode(), expected.encode()):
        logger.warning("Rejected webhook POST: invalid X-Hub-Signature-256")
        raise HTTPException(status_code=403, detail="Invalid signature")


def _reply(message: Message) -> None:
    to = mask_phone(message.sender)
    try:
        reply = route(message)
        if reply is None:
            return
        client.send_text(message.sender, reply)
    except client.GraphAPIError as exc:
        logger.error(
            "Graph API error replying to %s (message %s): HTTP %s, code %s: %s",
            to, message.id, exc.status, exc.code, exc.message,
        )
    except Exception:
        logger.exception("Failed to handle message %s from %s", message.id, to)
    else:
        logger.info("Reply sent to %s (message %s): %r", to, message.id, reply[:80])


@router.post("")
async def receive(request: Request, background: BackgroundTasks) -> dict[str, str]:
    body = await request.body()
    _check_signature(body, request.headers.get("X-Hub-Signature-256"))
    try:
        payload = WebhookPayload.model_validate_json(body)
    except ValidationError:
        # Still answer 200: a non-200 makes Meta retry, and repeated failures disable the webhook.
        logger.warning("Ignored webhook POST with unexpected payload")
        return {"status": "ignored"}

    statuses = payload.statuses()
    if statuses:
        logger.info("Status update(s): %s", ", ".join(s.status for s in statuses))

    # Acknowledge immediately; the reply is sent after the response.
    for message in payload.messages():
        if not seen_messages.add(message.id):
            logger.info("Duplicate message %s ignored", message.id)
            continue
        logger.info("Message received from %s: id=%s type=%s", mask_phone(message.sender), message.id, message.type)
        background.add_task(_reply, message)
    return {"status": "ok"}
