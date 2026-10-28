"""Structured output the model must return when reading a merchant's message.

A single message can describe several operations ("j'ai vendu 3 savons et
Marie a pris 2 sucres à crédit"), so the model returns a list of actions.
"""

import copy
from decimal import Decimal
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema, field_validator


class Action(str, Enum):
    SALE = "sale"
    PURCHASE = "purchase"  # restock
    CREDIT_SALE = "credit_sale"  # sale on credit -> creates a debt
    DEBT_PAYMENT = "debt_payment"  # customer pays back part of a debt
    STOCK_CHECK = "stock_check"  # "combien de savons il me reste ?"
    UNKNOWN = "unknown"


# Decimal would otherwise be advertised as "number or numeric string".
Quantity = Annotated[
    Decimal | None,
    Field(default=None, gt=0),
    WithJsonSchema({"type": ["number", "null"], "description": "Number of units, as stated"}),
]


class ExtractedAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Action
    product: str | None = Field(default=None, description="Product name as the merchant said it")
    quantity: Quantity
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
    language: Literal["fr", "en", "pcm"] | None = Field(
        default=None, description="Language of the message: fr, en or pcm (Cameroonian Pidgin)"
    )


# Keywords the strict structured-output mode doesn't need; Pydantic enforces the constraints anyway.
_DROPPED_KEYS = {"title", "default", "exclusiveMinimum", "minimum"}


def strict_json_schema(model: type[BaseModel]) -> dict:
    """JSON schema for strict structured output.

    Strict mode wants every object closed (additionalProperties: false) with all
    of its properties listed in "required"; optional fields become nullable
    (type [X, "null"]). $refs are inlined so the schema is self-contained.
    """
    schema = copy.deepcopy(model.model_json_schema())
    defs = schema.pop("$defs", {})

    def convert(node):
        if isinstance(node, list):
            return [convert(item) for item in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            target = convert(defs[node["$ref"].rsplit("/", 1)[-1]])
            extra = {k: v for k, v in node.items() if k != "$ref" and k not in _DROPPED_KEYS}
            return {**target, **convert(extra)}

        node = {k: convert(v) for k, v in node.items() if k not in _DROPPED_KEYS}

        if "anyOf" in node:
            node = _collapse_nullable(node)
        if node.get("type") == "object" and "properties" in node:
            node["additionalProperties"] = False
            node["required"] = list(node["properties"])
        return node

    return convert(schema)


def _collapse_nullable(node: dict) -> dict:
    """anyOf [{type: X, enum?}, {type: null}] -> {type: [X, "null"], enum?: [..., None]}."""
    branches = node["anyOf"]
    if not all(set(b) <= {"type", "enum", "description"} for b in branches):
        return node
    types: list[str] = []
    enum: list | None = None
    for branch in branches:
        branch_types = branch["type"] if isinstance(branch["type"], list) else [branch["type"]]
        types += [t for t in branch_types if t not in types]
        if "enum" in branch:
            enum = (enum or []) + branch["enum"]
    collapsed = {k: v for k, v in node.items() if k != "anyOf"}
    collapsed["type"] = types[0] if len(types) == 1 else types
    if enum is not None:
        collapsed["enum"] = enum + ([None] if "null" in types else [])
    for branch in branches:
        if "description" in branch and "description" not in collapsed:
            collapsed["description"] = branch["description"]
    return collapsed
