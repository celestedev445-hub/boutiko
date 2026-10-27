from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schemas import Action, ExtractedAction, Extraction


def test_parses_model_json_output():
    raw = """{
      "language": "fr",
      "actions": [
        {"action": "sale", "product": "savon Azur", "quantity": 3, "unit": "piece", "price": 250, "customer": null},
        {"action": "credit_sale", "product": "sucre", "quantity": 2, "unit": "kg", "price": 800, "customer": "Marie"}
      ]
    }"""
    extraction = Extraction.model_validate_json(raw)
    assert extraction.language == "fr"
    assert [a.action for a in extraction.actions] == [Action.SALE, Action.CREDIT_SALE]
    assert extraction.actions[0].quantity == Decimal("3")
    assert extraction.actions[1].customer == "Marie"


def test_optional_fields_default_to_none():
    action = ExtractedAction(action="stock_check", product="huile")
    assert action.quantity is None
    assert action.price is None
    assert action.customer is None


def test_fractional_quantity_is_allowed():
    assert ExtractedAction(action="sale", product="riz", quantity="1.5", unit="kg").quantity == Decimal("1.5")


def test_blank_strings_become_none():
    action = ExtractedAction(action="debt_payment", customer="  ", product="", unit=" kg ")
    assert action.customer is None
    assert action.product is None
    assert action.unit == "kg"


@pytest.mark.parametrize(
    "fields",
    [
        {"action": "steal"},  # unknown action
        {"action": "sale", "quantity": 0},
        {"action": "sale", "quantity": -2},
        {"action": "sale", "price": -100},
        {"action": "sale", "colour": "red"},  # extra field
    ],
)
def test_invalid_values_are_rejected(fields):
    with pytest.raises(ValidationError):
        ExtractedAction(**fields)


def test_total_price_without_unit_price():
    # "3 sacs pour 25 000": only the total is stated, nothing is derived
    action = ExtractedAction(action="purchase", product="riz", quantity=3, unit="sac", total_price=25000)
    assert action.total_price == 25000
    assert action.price is None


def test_unit_price_without_total_price():
    action = ExtractedAction(action="sale", product="savon", quantity=3, price=250)
    assert action.price == 250
    assert action.total_price is None


def test_total_price_parsed_from_model_json():
    raw = '{"actions": [{"action": "purchase", "product": "riz", "quantity": 3, "unit": "sac", "total_price": 25000}]}'
    assert Extraction.model_validate_json(raw).actions[0].total_price == 25000


def test_negative_total_price_is_rejected():
    with pytest.raises(ValidationError):
        ExtractedAction(action="sale", total_price=-1)


def test_total_price_is_in_json_schema():
    props = Extraction.model_json_schema()["$defs"]["ExtractedAction"]["properties"]
    assert "total_price" in props
    assert "price" in props


def test_empty_extraction():
    assert Extraction.model_validate_json("{}").actions == []


def test_json_schema_lists_all_actions():
    schema = Extraction.model_json_schema()
    action_enum = schema["$defs"]["Action"]["enum"]
    assert set(action_enum) == {a.value for a in Action}
