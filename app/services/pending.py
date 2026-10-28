"""Actions waiting for the merchant before they're recorded.

- CONFIRM: what was read on a photo, until the merchant answers YES or NO.
- CHOOSE_PRODUCT: a message naming a product that could be several ("savon":
  Azur or Palmida?), until the merchant picks one.
"""

import enum
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import clock
from app.languages import Language, as_language
from app.models import PendingAction, PendingKind, PendingStatus
from app.schemas import Extraction
from app.services import ledger, products
from app.services.products import ProductChoice
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


def latest_active(session: Session, wa_id: str, kind: PendingKind | None = None) -> PendingAction | None:
    """Most recent pending action of this merchant that hasn't expired, locked until commit.

    Expired ones are marked as such on the way.
    """
    now = clock.now()
    session.execute(
        update(PendingAction)
        .where(PendingAction.wa_id == wa_id, PendingAction.status == PendingStatus.PENDING, PendingAction.expires_at <= now)
        .values(status=PendingStatus.EXPIRED)
    )
    query = select(PendingAction).where(
        PendingAction.wa_id == wa_id, PendingAction.status == PendingStatus.PENDING, PendingAction.expires_at > now
    )
    if kind is not None:
        query = query.where(PendingAction.kind == kind)
    return session.scalar(
        query.order_by(PendingAction.created_at.desc(), PendingAction.id.desc()).limit(1).with_for_update()
    )


def extraction_of(pending: PendingAction) -> Extraction:
    return Extraction.model_validate({k: v for k, v in pending.payload.items() if k != "choice"})


def choice_of(pending: PendingAction) -> ProductChoice | None:
    choice = pending.payload.get("choice")
    return ProductChoice(**choice) if choice else None


def save_choice(session: Session, wa_id: str, extraction: Extraction, message_id: str, choice: ProductChoice) -> None:
    """Keep the message's actions until the merchant says which product they meant.

    The message's existing pending action (a photo being confirmed) becomes the question.
    """
    payload = {**extraction.model_dump(mode="json"), "choice": vars(choice)}
    now = clock.now()
    pending = session.scalar(select(PendingAction).where(PendingAction.source_message_id == message_id))
    if pending is None:
        shop = ledger.get_or_create_shop(session, wa_id, extraction.language)
        pending = PendingAction(shop_id=shop.id, wa_id=wa_id, source_message_id=message_id, created_at=now)
        session.add(pending)
    pending.payload = payload
    pending.kind = PendingKind.CHOOSE_PRODUCT
    pending.status = PendingStatus.PENDING
    pending.created_at = now  # it is the merchant's latest question
    pending.expires_at = now + TTL
    session.commit()


def _settle(session: Session, wa_id: str, extraction: Extraction, message_id: str, result: ledger.LedgerResult) -> None:
    if result.choice is not None:  # another product to pick in the same message
        save_choice(session, wa_id, extraction, message_id, result.choice)
        return
    session.execute(
        update(PendingAction).where(PendingAction.source_message_id == message_id).values(status=PendingStatus.CONFIRMED)
    )
    session.commit()


def language_of(pending: PendingAction, default: Language) -> Language:
    language = pending.payload.get("language")
    return as_language(language, default) if language else default


def confirm(session: Session, pending: PendingAction) -> ledger.LedgerResult:
    """Record the actions. Uses the photo's message id, so they can never be recorded twice.

    If a product name turns out to be ambiguous, nothing is recorded and the
    result carries the question to ask.
    """
    wa_id, message_id, extraction = pending.wa_id, pending.source_message_id, extraction_of(pending)
    result = ledger.apply_extraction(session, wa_id, extraction, message_id)
    _settle(session, wa_id, extraction, message_id, result)
    return result


def parse_choice(text: str, pending: PendingAction) -> str | None:
    choice = choice_of(pending)
    return products.parse_choice(text, choice) if choice else None


def choose(session: Session, pending: PendingAction, option: str) -> ledger.LedgerResult:
    """The merchant picked a product: record the message's actions with it."""
    wa_id, message_id, extraction = pending.wa_id, pending.source_message_id, extraction_of(pending)
    index = choice_of(pending).action_index
    actions = list(extraction.actions)
    actions[index] = actions[index].model_copy(update={"product": option})
    chosen = extraction.model_copy(update={"actions": actions})
    result = ledger.apply_extraction(session, wa_id, chosen, message_id)
    _settle(session, wa_id, chosen, message_id, result)
    return result


def cancel(session: Session, pending: PendingAction) -> None:
    pending.status = PendingStatus.CANCELLED
    session.commit()
