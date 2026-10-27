"""Structured output the model must return when reading a merchant's message.

A single message can describe several operations ("j'ai vendu 3 savons et
Marie a pris 2 sucres à crédit"), so the model returns a list of actions.
"""

from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Action(str, Enum):
    SALE = "sale"
    PURCHASE = "purchase"  # restock
    CREDIT_SALE = "credit_sale"  # sale on credit -> creates a debt
    DEBT_PAYMENT = "debt_payment"  # customer pays back part of a debt
    STOCK_CHECK = "stock_check"  # "combien de savons il me reste ?"
    UNKNOWN = "unknown"


class ExtractedAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Action
    product: str | None = Field(default=None, description="Product name as the merchant said it")
    quantity: Decimal | None = Field(default=None, gt=0)
    unit: str | None = Field(default=None, description="e.g. piece, kg, litre, carton, sac")
    price: int | None = Field(default=None, ge=0, description="Unit price in FCFA")
    total_price: int | None = Field(
        default=None,
        ge=0,
        description="Total amount in FCFA for this line, when the merchant states a total, e.g. '3 sacs pour 25 000'",
    )
    customer: str | None = Field(default=None, description="Customer name, for credit sales and payments")

    @field_validator("product", "unit", "customer")
    @classmethod
    def _strip_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class Extraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actions: list[ExtractedAction] = Field(default_factory=list)
    language: str | None = Field(default=None, description="Detected language: fr, en or pcm (pidgin)")
