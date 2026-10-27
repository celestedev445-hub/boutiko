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


class Message(_Model):
    id: str
    sender: str = Field(alias="from")
    timestamp: str
    type: str
    text: Text | None = None
    audio: Media | None = None
    image: Media | None = None


class Value(_Model):
    messaging_product: str | None = None
    messages: list[Message] = []


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
        return [m for e in self.entry for c in e.changes for m in c.value.messages]
