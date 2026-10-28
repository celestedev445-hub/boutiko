from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models import Customer, Debt, Language, MovementKind, Product, Sale, Shop, StockMovement
from app.schemas import Action, ExtractedAction, Extraction
from app.services.ledger import LedgerWarning, apply_extraction
from app.services.normalize import normalize_name

PHONE = "237690001234"


def extraction(*actions: dict, language: str | None = "fr") -> Extraction:
    return Extraction(actions=[ExtractedAction(**a) for a in actions], language=language)


_counter = iter(range(10_000))


def apply(session, *actions: dict, language: str | None = "fr", message_id: str | None = None):
    message_id = message_id or f"wamid.{next(_counter)}"
    return apply_extraction(session, PHONE, extraction(*actions, language=language), message_id)


def count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


# --- normalization ---


def test_normalize_name():
    assert normalize_name("  Savons   Azur ") == "savon azur"
    assert normalize_name("Gâteaux") == "gateau"
    assert normalize_name("riz") == "riz"
    assert normalize_name("Crème") == normalize_name("creme")


# --- shop ---


def test_first_message_creates_shop_with_detected_language(session):
    apply(session, {"action": "stock_check", "product": "savon"}, language="pcm")
    shop = session.scalar(select(Shop))
    assert shop.owner_phone == PHONE
    assert shop.name == PHONE
    assert shop.language == Language.PIDGIN


def test_shop_is_reused_and_follows_the_latest_language(session):
    apply(session, {"action": "purchase", "product": "savon", "quantity": 5})
    apply(session, {"action": "purchase", "product": "savon", "quantity": 5}, language="en")
    assert count(session, Shop) == 1
    assert session.scalar(select(Shop)).language == Language.EN
    apply(session, {"action": "purchase", "product": "savon", "quantity": 5}, language=None)
    assert session.scalar(select(Shop)).language == Language.EN  # nothing detected: unchanged


def test_generic_units_are_not_kept(session):
    apply(session, {"action": "purchase", "product": "savon Azur", "quantity": 12, "unit": "pièces"})
    assert session.scalar(select(Product)).unit == "piece"
    result = apply(session, {"action": "stock_check", "product": "savon azur"})
    assert result.actions[0].unit is None


def test_purchase_total_cost_is_the_stated_total(session):
    apply(session, {"action": "purchase", "product": "riz", "quantity": 3, "unit": "sac", "total_price": 25000})
    movement = session.scalar(select(StockMovement))
    assert (movement.unit_cost, movement.total_cost) == (8333, 25000)


# --- each action ---


def test_sale(session):
    apply(session, {"action": "purchase", "product": "savon", "quantity": 20})
    result = apply(session, {"action": "sale", "product": "Savons", "quantity": 3, "price": 250})

    action = result.actions[0]
    assert action.recorded
    assert action.total == 750
    assert action.stock == Decimal(17)
    assert action.warnings == []
    sale = session.scalar(select(Sale))
    assert (sale.quantity, sale.unit_price, sale.total, sale.on_credit) == (Decimal(3), 250, 750, False)
    movement = session.scalar(select(StockMovement).where(StockMovement.kind == MovementKind.SALE))
    assert movement.quantity == Decimal(-3)
    assert movement.sale_id == sale.id
    assert count(session, Product) == 1  # "Savons" matched "savon"


def test_credit_sale_creates_debt(session):
    result = apply(
        session,
        {"action": "credit_sale", "product": "sucre", "quantity": 2, "unit": "kg", "price": 750, "customer": "Marie"},
    )
    action = result.actions[0]
    assert action.recorded
    assert action.customer == "Marie"
    assert action.customer_balance == 1500
    sale = session.scalar(select(Sale))
    assert sale.on_credit
    debt = session.scalar(select(Debt))
    assert (debt.amount, debt.sale_id) == (1500, sale.id)


def test_purchase_adds_stock_and_keeps_cost(session):
    result = apply(session, {"action": "purchase", "product": "riz", "quantity": 10, "unit": "sac", "total_price": 230000})
    action = result.actions[0]
    assert action.stock == Decimal(10)
    assert action.total == 230000
    movement = session.scalar(select(StockMovement))
    assert (movement.kind, movement.quantity, movement.unit_cost) == (MovementKind.PURCHASE, Decimal(10), 23000)
    product = session.scalar(select(Product))
    assert product.unit == "sac"
    assert product.sale_price is None  # a cost is not a sale price
    assert count(session, Sale) == 0


def test_debt_payment(session):
    apply(session, {"action": "credit_sale", "product": "sucre", "quantity": 3, "price": 1500, "customer": "Marie"})
    result = apply(session, {"action": "debt_payment", "customer": "marie", "total_price": 2000})
    action = result.actions[0]
    assert action.recorded
    assert action.total == 2000
    assert action.customer_balance == 2500
    assert session.scalar(select(Debt).where(Debt.amount < 0)).amount == -2000
    assert count(session, Customer) == 1


def test_debt_payment_amount_can_come_as_price(session):
    result = apply(session, {"action": "debt_payment", "customer": "Ekane", "price": 1000})
    assert result.actions[0].customer_balance == -1000


def test_stock_check_writes_nothing(session):
    apply(session, {"action": "purchase", "product": "Maggi", "quantity": 50})
    movements_before = count(session, StockMovement)
    result = apply(session, {"action": "stock_check", "product": "maggi"})
    action = result.actions[0]
    assert not action.recorded
    assert action.stock == Decimal(50)
    assert action.product == "Maggi"
    assert count(session, StockMovement) == movements_before


def test_stock_check_unknown_product(session):
    result = apply(session, {"action": "stock_check", "product": "huile"})
    assert result.actions[0].warnings == [LedgerWarning.UNKNOWN_PRODUCT]
    assert count(session, Product) == 0


def test_unknown_action(session):
    result = apply(session, {"action": "unknown"})
    assert result.actions[0].warnings == [LedgerWarning.NOT_UNDERSTOOD]


def test_multiple_actions_in_one_message(session):
    result = apply(
        session,
        {"action": "sale", "product": "savon", "quantity": 2, "price": 250},
        {"action": "credit_sale", "product": "riz", "quantity": 1, "unit": "sac", "total_price": 23000, "customer": "Ngozi"},
    )
    assert [a.recorded for a in result.actions] == [True, True]
    assert count(session, Sale) == 2
    assert count(session, Debt) == 1


# --- prices ---


def test_total_price_gives_rounded_unit_price(session):
    result = apply(session, {"action": "sale", "product": "riz", "quantity": 3, "unit": "sac", "total_price": 25000})
    sale = session.scalar(select(Sale))
    assert sale.total == 25000
    assert sale.unit_price == 8333
    assert result.actions[0].total == 25000
    assert session.scalar(select(Product)).sale_price == 8333


def test_price_times_quantity(session):
    apply(session, {"action": "sale", "product": "riz", "quantity": "1.5", "unit": "kg", "price": 700})
    sale = session.scalar(select(Sale))
    assert (sale.unit_price, sale.total) == (700, 1050)


def test_total_price_wins_over_price(session):
    apply(session, {"action": "sale", "product": "savon", "quantity": 3, "price": 250, "total_price": 700})
    assert session.scalar(select(Sale)).total == 700


def test_known_sale_price_is_reused(session):
    apply(session, {"action": "sale", "product": "savon", "quantity": 1, "price": 250})
    result = apply(session, {"action": "sale", "product": "savon", "quantity": 4})
    assert result.actions[0].total == 1000
    assert result.actions[0].warnings == [LedgerWarning.NEGATIVE_STOCK]


def test_new_price_updates_sale_price(session):
    apply(session, {"action": "sale", "product": "savon", "quantity": 1, "price": 250})
    apply(session, {"action": "sale", "product": "savon", "quantity": 1, "price": 300})
    assert session.scalar(select(Product)).sale_price == 300


def test_missing_price_is_recorded_with_warning(session):
    result = apply(session, {"action": "sale", "product": "huile", "quantity": 2})
    action = result.actions[0]
    assert action.recorded
    assert action.total is None
    assert LedgerWarning.MISSING_PRICE in action.warnings
    sale = session.scalar(select(Sale))
    assert (sale.unit_price, sale.total) == (None, None)
    assert count(session, StockMovement) == 1


def test_credit_sale_without_price_has_no_debt(session):
    result = apply(session, {"action": "credit_sale", "product": "huile", "quantity": 1, "customer": "Paul"})
    assert LedgerWarning.MISSING_PRICE in result.actions[0].warnings
    assert count(session, Debt) == 0
    assert result.actions[0].customer_balance == 0


# --- customers ---


def test_customer_created_then_balance_accumulates(session):
    apply(session, {"action": "credit_sale", "product": "sucre", "quantity": 1, "price": 1500, "customer": "Mama Brigitte"})
    result = apply(session, {"action": "credit_sale", "product": "lait", "quantity": 2, "price": 600, "customer": "mama  brigitte"})
    assert count(session, Customer) == 1
    assert session.scalar(select(Customer)).name == "Mama Brigitte"
    assert result.actions[0].customer_balance == 2700


def test_credit_sale_without_customer(session):
    result = apply(session, {"action": "credit_sale", "product": "sucre", "quantity": 1, "price": 1500})
    assert result.actions[0].recorded
    assert LedgerWarning.MISSING_CUSTOMER in result.actions[0].warnings
    assert count(session, Debt) == 0


def test_payment_without_customer_or_amount_is_not_recorded(session):
    result = apply(
        session,
        {"action": "debt_payment", "total_price": 2000},
        {"action": "debt_payment", "customer": "Marie"},
    )
    assert [a.warnings for a in result.actions] == [[LedgerWarning.MISSING_CUSTOMER], [LedgerWarning.MISSING_AMOUNT]]
    assert count(session, Debt) == 0


def test_missing_quantity_or_product_is_not_recorded(session):
    result = apply(session, {"action": "sale", "product": "savon", "price": 250}, {"action": "purchase", "quantity": 3})
    assert [a.warnings for a in result.actions] == [[LedgerWarning.MISSING_QUANTITY], [LedgerWarning.MISSING_PRODUCT]]
    assert count(session, Sale) == 0
    assert count(session, StockMovement) == 0


# --- stock ---


def test_negative_stock_is_recorded_with_warning(session):
    result = apply(session, {"action": "sale", "product": "savon", "quantity": 2, "price": 250})
    action = result.actions[0]
    assert action.recorded
    assert action.stock == Decimal(-2)
    assert action.warnings == [LedgerWarning.NEGATIVE_STOCK]


# --- idempotence ---


def test_same_message_is_applied_once(session):
    first = apply(session, {"action": "sale", "product": "savon", "quantity": 2, "price": 250}, message_id="wamid.SAME")
    second = apply(session, {"action": "sale", "product": "savon", "quantity": 2, "price": 250}, message_id="wamid.SAME")
    assert not first.duplicate
    assert second.duplicate
    assert second.actions == []
    assert count(session, Sale) == 1
    assert count(session, StockMovement) == 1


def test_failed_message_is_rolled_back(session, monkeypatch):
    from app.services import ledger

    def boom(*args, **kwargs):
        raise RuntimeError("db exploded")

    with monkeypatch.context() as m:
        m.setattr(ledger, "_apply_purchase", boom)
        with pytest.raises(RuntimeError):
            apply(
                session,
                {"action": "sale", "product": "savon", "quantity": 1, "price": 250},
                {"action": "purchase", "product": "riz", "quantity": 1},
                message_id="wamid.FAIL",
            )
    assert count(session, Sale) == 0
    assert count(session, Shop) == 0

    retry = apply(session, {"action": "sale", "product": "savon", "quantity": 1, "price": 250}, message_id="wamid.FAIL")
    assert not retry.duplicate
    assert count(session, Sale) == 1
