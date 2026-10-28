"""Actions waiting for the merchant's YES or NO (photos are never recorded without it)."""

import enum
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import clock
from app.models import Language, PendingAction, PendingStatus
from app.schemas import Extraction
from app.services import ledger
from app.services.normalize import normalize_command

TTL = timedelta(minutes=30)


class Decision(str, enum.Enum):
    YES = "yes"
    NO = "no"


_YES = {
    "oui", "ouais", "ouai", "ok", "okay", "oki", "d accord", "daccord", "dac", "vas y", "vasy",
    "c est bon", "cest bon", "enregistre", "valide", "ok oui", "oui oui",
    "yes", "yeah", "yep", "yup", "ya", "sure", "correct", "save", "save it", "go ahead", "yes yes",
    "na so", "i gree", "i agree", "save am", "e correct",
}
_NO = {
    "non", "no", "nope", "nah", "cancel", "annule", "annuler", "laisse", "laisse tomber", "pas ca",
    "non non", "no no", "stop", "dont", "dont save",
    "no be so", "no be am", "no o", "comot am",
}
_YES_EMOJI = ("👍", "✅", "👌")
_NO_EMOJI = ("👎", "❌")


def parse_decision(text: str) -> Decision | None:
    """YES / NO if the whole message is a confirmation or a refusal, None otherwise."""
    stripped = text.strip()
    if stripped and all(c in "".join(_YES_EMOJI) + " ️" for c in stripped):
        return Decision.YES
    if stripped and all(c in "".join(_NO_EMOJI) + " ️" for c in stripped):
        return Decision.NO
    phrase = normalize_command(text)
    if phrase in _YES:
        return Decision.YES
    if phrase in _NO:
        return Decision.NO
    return None


def exists_for_message(session: Session, message_id: str) -> bool:
    return session.scalar(select(PendingAction.id).where(PendingAction.source_message_id == message_id)) is not None


def create(session: Session, wa_id: str, extraction: Extraction, message_id: str) -> PendingAction | None:
    """Store the actions read on a photo. None if this message already has a pending action."""
    if exists_for_message(session, message_id):
        return None
    shop = ledger.get_or_create_shop(session, wa_id, extraction.language)
    now = clock.now()
    pending = PendingAction(
        shop_id=shop.id,
        wa_id=wa_id,
        source_message_id=message_id,
        payload=extraction.model_dump(mode="json"),
        created_at=now,
        expires_at=now + TTL,
        status=PendingStatus.PENDING,
    )
    session.add(pending)
    try:
        session.commit()
    except IntegrityError:  # the same message processed concurrently
        session.rollback()
        return None
    return pending


def latest_active(session: Session, wa_id: str) -> PendingAction | None:
    """Most recent pending action of this merchant that hasn't expired, locked until commit.

    Expired ones are marked as such on the way.
    """
    now = clock.now()
    session.execute(
        update(PendingAction)
        .where(PendingAction.wa_id == wa_id, PendingAction.status == PendingStatus.PENDING, PendingAction.expires_at <= now)
        .values(status=PendingStatus.EXPIRED)
    )
    return session.scalar(
        select(PendingAction)
        .where(PendingAction.wa_id == wa_id, PendingAction.status == PendingStatus.PENDING, PendingAction.expires_at > now)
        .order_by(PendingAction.created_at.desc(), PendingAction.id.desc())
        .limit(1)
        .with_for_update()
    )


def extraction_of(pending: PendingAction) -> Extraction:
    return Extraction.model_validate(pending.payload)


def language_of(pending: PendingAction, default: Language) -> Language:
    language = pending.payload.get("language")
    return Language(language) if language else default


def confirm(session: Session, pending: PendingAction) -> ledger.LedgerResult:
    """Record the actions. Uses the photo's message id, so they can never be recorded twice."""
    pending.status = PendingStatus.CONFIRMED
    result = ledger.apply_extraction(session, pending.wa_id, extraction_of(pending), pending.source_message_id)
    if result.duplicate:
        session.commit()  # apply_extraction didn't commit: keep the status change
    return result


def cancel(session: Session, pending: PendingAction) -> None:
    pending.status = PendingStatus.CANCELLED
    session.commit()
