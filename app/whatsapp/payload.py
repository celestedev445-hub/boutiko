"""Minimal models for the WhatsApp Cloud API webhook payload.

Only the fields Boutiko uses are declared; everything else is ignored.
"""

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Text(_Model):
    body: str


class Media(_Model):
    id: str
    mime_type: str | None = None
    caption: str | None = None
    voice: bool | None = None  # audio recorded in WhatsApp (voice note) rather than a forwarded file


class Message(_Model):
    id: str
    sender: str = Field(alias="from")
    timestamp: str
    type: str
    text: Text | None = None
    audio: Media | None = None
    image: Media | None = None
    # Not in Meta's message object: copied from the change's metadata by WebhookPayload.messages().
    phone_number_id: str | None = None


class Status(_Model):
    """Delivery receipt for a message we sent: sent, delivered, read or failed."""

    id: str
    status: str


class Metadata(_Model):
    """The business number that received the change."""

    display_phone_number: str | None = None
    phone_number_id: str | None = None


class Value(_Model):
    messaging_product: str | None = None
    metadata: Metadata | None = None
    messages: list[Message] = []
    statuses: list[Status] = []


class Change(_Model):
    field: str
    value: Value


class Entry(_Model):
    id: str
    changes: list[Change] = []


class WebhookPayload(_Model):
    object: str
    entry: list[Entry] = []

    def messages(self) -> list[Message]:
        """Incoming messages, each tagged with the phone_number_id of the number that received it."""
        result = []
        for entry in self.entry:
            for change in entry.changes:
                receiver = change.value.metadata.phone_number_id if change.value.metadata else None
                result += [m.model_copy(update={"phone_number_id": receiver}) for m in change.value.messages]
        return result

    def statuses(self) -> list[Status]:
        return [s for e in self.entry for c in e.changes for s in c.value.statuses]
