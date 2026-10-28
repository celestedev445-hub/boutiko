"""Sending the evening summary: on request, automatically in the evening, or offered later.

WhatsApp only allows free-form messages within 24 h of the merchant's last
message, and Boutiko uses no paid message templates. So the scheduler only
sends while that window is open (with a 30 min margin). When it is closed,
the summary is offered on the merchant's next message instead.
"""

import asyncio
import logging
import re
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import clock, db
from app.config import get_settings
from app.languages import Language
from app.models import DailySummary, Shop, SummaryChannel
from app.services import ledger
from app.services.normalize import normalize_command
from app.services.summary import build_daily_summary, local_now
from app.services.summary_text import hour_set, offer, write_summary
from app.whatsapp import client

logger = logging.getLogger(__name__)

WINDOW = timedelta(hours=23, minutes=30)  # WhatsApp's 24 h customer-service window, with a margin
CHECK_INTERVAL_SECONDS = 15 * 60
OFFER_UNTIL_HOUR = 12  # yesterday's summary is offered until noon

_REQUESTS: dict[str, Language | None] = {
    # French
    "resume": Language.FR, "bilan": Language.FR, "point du jour": Language.FR, "le point": Language.FR,
    "fais le point": Language.FR, "resume du jour": Language.FR, "bilan du jour": Language.FR,
    "mon resume": Language.FR, "mon bilan": Language.FR,
    # English (None: English, or Pidgin if that's the shop's language)
    "summary": None, "today summary": None, "todays summary": None, "today s summary": None,
    "summary today": None, "daily summary": None, "my summary": None,
    # Pidgin
    "how market go today": Language.PIDGIN, "how market go": Language.PIDGIN,
    "how the market go today": Language.PIDGIN, "how market today": Language.PIDGIN,
}
_HOUR_COMMAND = re.compile(
    r"^(?P<word>resume|bilan|summary)(?: du (?:soir|jour))? (?:a|at|for|by) "
    r"(?P<hour>\d{1,2})(?: ?h(?: ?00)?| 00| ?(?P<ampm>am|pm))?$"
)


def parse_summary_request(text: str) -> Language | None | bool:
    """False if the message is not a summary request; otherwise the language it was asked in (None: English word)."""
    phrase = normalize_command(text)
    return _REQUESTS[phrase] if phrase in _REQUESTS else False


def parse_summary_hour(text: str) -> tuple[int, Language | None] | None:
    """"résumé à 21h" -> (21, fr), "summary at 9pm" -> (21, None). None if not such a command."""
    match = _HOUR_COMMAND.match(normalize_command(text))
    if match is None:
        return None
    hour = int(match["hour"])
    if match["ampm"] and not 1 <= hour <= 12:  # "13pm", "0am"
        return None
    if match["ampm"] == "pm" and hour < 12:
        hour += 12
    elif match["ampm"] == "am" and hour == 12:
        hour = 0
    if hour > 23:
        return None
    return hour, (None if match["word"] == "summary" else Language.FR)


def _reply_language(asked_in: Language | None, shop: Shop) -> Language:
    if asked_in is not None:
        return asked_in
    return Language.PIDGIN if shop.language == Language.PIDGIN else Language.EN


def summary_sent(session: Session, shop_id: int, day: date) -> bool:
    return session.scalar(select(DailySummary.id).where(DailySummary.shop_id == shop_id, DailySummary.day == day)) is not None


def record_summary(session: Session, shop: Shop, day: date, channel: SummaryChannel, now: datetime) -> DailySummary | None:
    """Claim the (shop, day, channel) slot. None if it was already taken."""
    exists = session.scalar(
        select(DailySummary.id).where(
            DailySummary.shop_id == shop.id, DailySummary.day == day, DailySummary.channel == channel
        )
    )
    if exists is not None:
        return None
    row = DailySummary(shop_id=shop.id, day=day, sent_at=now, channel=channel)
    session.add(row)
    try:
        session.commit()
    except IntegrityError:  # another worker took it
        session.rollback()
        return None
    return row


def manual_summary(session: Session, owner_phone: str, asked_in: Language | None) -> tuple[str, Language]:
    """The merchant typed "résumé": today's summary, or yesterday's if it was offered this morning."""
    now = clock.now()
    local = local_now(now)
    shop = ledger.get_or_create_shop(session, owner_phone, asked_in.value if asked_in else None)
    language = _reply_language(asked_in, shop)
    day = local.date()
    if shop.summary_offered_day == day - timedelta(days=1) and local.hour < OFFER_UNTIL_HOUR:
        day = shop.summary_offered_day
    shop.summary_offered_day = None
    shop.summary_skipped_day = None
    session.commit()

    name = shop.language_name if language == shop.language else None
    text = write_summary(build_daily_summary(session, shop, day), language, name)
    record_summary(session, shop, day, SummaryChannel.MANUAL, now)
    logger.info("Shop %s: summary of %s sent on request", shop.id, day.isoformat())
    return text, language


def set_summary_hour(session: Session, owner_phone: str, hour: int, asked_in: Language | None) -> tuple[str, Language]:
    shop = ledger.get_or_create_shop(session, owner_phone, asked_in.value if asked_in else None)
    shop.summary_hour = hour
    language = _reply_language(asked_in, shop)
    session.commit()
    logger.info("Shop %s: summary hour set to %s", shop.id, hour)
    return hour_set(hour, language), language


def after_inbound(session: Session, owner_phone: str, phone_number_id: str | None = None) -> str | None:
    """Note the merchant's message (24 h window, number it was sent to) and, if a summary was skipped,
    the line offering it."""
    shop = session.scalar(select(Shop).where(Shop.owner_phone == owner_phone))
    if shop is None:
        return None
    now = clock.now()
    shop.last_message_at = now
    if phone_number_id:
        shop.phone_number_id = phone_number_id
    line = None
    skipped = shop.summary_skipped_day
    if skipped is not None:
        local = local_now(now)
        same_day = local.date() == skipped
        next_morning = local.date() == skipped + timedelta(days=1) and local.hour < OFFER_UNTIL_HOUR
        if (same_day or next_morning) and not summary_sent(session, shop.id, skipped):
            line = offer(shop.language, yesterday=next_morning)
            shop.summary_offered_day = skipped
            logger.info("Shop %s: summary of %s offered", shop.id, skipped.isoformat())
        shop.summary_skipped_day = None
    session.commit()
    return line


def _send_if_due(shop_id: int, now: datetime) -> bool:
    settings = get_settings()
    with db.SessionLocal() as session:
        shop = session.get(Shop, shop_id)
        local = local_now(now)
        day = local.date()
        hour = shop.summary_hour if shop.summary_hour is not None else settings.summary_hour
        if local.hour < hour:
            return False
        if shop.summary_offered_day == day or summary_sent(session, shop.id, day):
            return False
        if shop.last_message_at is None or now - clock.as_utc(shop.last_message_at) > WINDOW:
            if shop.summary_skipped_day != day:  # log once per day, not every 15 minutes
                logger.info("Shop %s: summary skipped: outside 24h window", shop.id)
                shop.summary_skipped_day = day
                session.commit()
            return False

        # Claim first, then send: two workers can never both send it.
        claim = record_summary(session, shop, day, SummaryChannel.AUTO, now)
        if claim is None:
            return False
        text = write_summary(build_daily_summary(session, shop, day), shop.language, shop.language_name)
        try:
            client.send_text(shop.owner_phone, text, phone_number_id=shop.phone_number_id)
        except Exception as exc:
            logger.error("Shop %s: summary not sent (%s), will retry", shop.id, exc)
            session.delete(claim)
            session.commit()
            return False
        logger.info("Shop %s: evening summary of %s sent", shop.id, day.isoformat())
        return True


def run_due_summaries(now: datetime | None = None) -> int:
    """One scheduler pass: send every summary that is due. Returns how many were sent."""
    now = now or clock.now()
    with db.SessionLocal() as session:
        shop_ids = list(session.scalars(select(Shop.id)))
    sent = 0
    for shop_id in shop_ids:
        try:
            sent += _send_if_due(shop_id, now)
        except Exception:
            logger.exception("Shop %s: summary check failed", shop_id)
    return sent


async def summary_loop(interval_seconds: int = CHECK_INTERVAL_SECONDS) -> None:
    """Runs in the API process (started in the FastAPI lifespan) every 15 minutes."""
    logger.info("Evening summary scheduler started (every %d min)", interval_seconds // 60)
    while True:
        try:
            await asyncio.to_thread(run_due_summaries)
        except Exception:
            logger.exception("Summary scheduler pass failed")
        await asyncio.sleep(interval_seconds)
