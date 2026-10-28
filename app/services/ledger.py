"""Turn an extraction into rows: sales, stock movements, debts.

apply_extraction is the only entry point. It runs in one transaction: either
every action of the message is recorded, or none is.
"""

import enum
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    Customer,
    Debt,
    DebtKind,
    Language,
    MovementKind,
    ProcessedMessage,
    Product,
    Sale,
    Shop,
    StockMovement,
    current_stock,
    customer_balance,
)
from app.schemas import Action, ExtractedAction, Extraction
from app.services.normalize import normalize_name


class LedgerWarning(str, enum.Enum):
    MISSING_PRICE = "missing_price"  # recorded without an amount
    MISSING_QUANTITY = "missing_quantity"  # not recorded
    MISSING_PRODUCT = "missing_product"  # not recorded
    MISSING_CUSTOMER = "missing_customer"  # credit sale recorded without a debt, or payment not recorded
    MISSING_AMOUNT = "missing_amount"  # debt payment without an amount, not recorded
    UNKNOWN_PRODUCT = "unknown_product"  # stock check for a product never seen
    NEGATIVE_STOCK = "negative_stock"  # recorded; stock went below zero
    NOT_UNDERSTOOD = "not_understood"


@dataclass
class ActionResult:
    action: Action
    recorded: bool = False
    product: str | None = None
    unit: str | None = None
    quantity: Decimal | None = None
    total: int | None = None  # sale/purchase amount, or amount paid back
    stock: Decimal | None = None  # stock after the action
    customer: str | None = None
    customer_balance: int | None = None  # what the customer owes after the action
    warnings: list[LedgerWarning] = field(default_factory=list)


@dataclass
class LedgerResult:
    shop_id: int
    language: Language
    actions: list[ActionResult] = field(default_factory=list)
    duplicate: bool = False  # message already applied earlier: nothing was done


def apply_extraction(session: Session, owner_phone: str, extraction: Extraction, message_id: str) -> LedgerResult:
    """Record every action of a message for the shop of `owner_phone`.

    The shop is created on its first message. Commits on success, rolls back
    on any error. A message id that was already applied is not applied again.
    """
    try:
        shop = get_or_create_shop(session, owner_phone, extraction.language)
        language = Language(extraction.language) if extraction.language else shop.language
        if _already_processed(session, message_id):
            return LedgerResult(shop_id=shop.id, language=language, duplicate=True)

        session.add(ProcessedMessage(shop_id=shop.id, message_id=message_id))
        result = LedgerResult(shop_id=shop.id, language=language)
        for extracted in extraction.actions:
            result.actions.append(_apply_action(session, shop, extracted, message_id))
        session.commit()
        return result
    except IntegrityError:
        # Most likely the same message processed concurrently: the other one won.
        session.rollback()
        shop = session.scalar(select(Shop).where(Shop.owner_phone == owner_phone))
        if shop is not None and _already_processed(session, message_id):
            return LedgerResult(shop_id=shop.id, language=shop.language, duplicate=True)
        raise
    except Exception:
        session.rollback()
        raise


def is_processed(session: Session, message_id: str) -> bool:
    """True if this WhatsApp message was already applied to the ledger."""
    return _already_processed(session, message_id)


def _already_processed(session: Session, message_id: str) -> bool:
    return session.scalar(select(ProcessedMessage.id).where(ProcessedMessage.message_id == message_id)) is not None


def get_or_create_shop(session: Session, owner_phone: str, language: str | None) -> Shop:
    """The merchant's shop, created on their first message."""
    shop = session.scalar(select(Shop).where(Shop.owner_phone == owner_phone))
    if shop is None:
        # Provisional name until the merchant tells us the shop's name.
        shop = Shop(name=owner_phone, owner_phone=owner_phone, language=Language(language or "fr"))
        session.add(shop)
        session.flush()
    return shop


def _find_product(session: Session, shop: Shop, name: str) -> Product | None:
    return session.scalar(
        select(Product).where(Product.shop_id == shop.id, Product.name_key == normalize_name(name))
    )


def _get_or_create_product(session: Session, shop: Shop, name: str, unit: str | None) -> Product:
    product = _find_product(session, shop, name)
    if product is None:
        product = Product(shop_id=shop.id, name=name, name_key=normalize_name(name), unit=unit or "piece")
        session.add(product)
        session.flush()
    return product


def _get_or_create_customer(session: Session, shop: Shop, name: str) -> Customer:
    key = normalize_name(name)
    customer = session.scalar(select(Customer).where(Customer.shop_id == shop.id, Customer.name_key == key))
    if customer is None:
        customer = Customer(shop_id=shop.id, name=name, name_key=key)
        session.add(customer)
        session.flush()
    return customer


def _line_prices(extracted: ExtractedAction, quantity: Decimal, known_unit_price: int | None) -> tuple[int | None, int | None]:
    """(unit_price, total) following the merchant's words first, then the known price."""
    if extracted.total_price is not None:
        unit_price = int((Decimal(extracted.total_price) / quantity).quantize(Decimal(1), ROUND_HALF_UP))
        return unit_price, extracted.total_price
    unit_price = extracted.price if extracted.price is not None else known_unit_price
    if unit_price is None:
        return None, None
    return unit_price, int((unit_price * quantity).quantize(Decimal(1), ROUND_HALF_UP))


def _apply_action(session: Session, shop: Shop, extracted: ExtractedAction, message_id: str) -> ActionResult:
    result = ActionResult(
        action=extracted.action,
        product=extracted.product,
        unit=extracted.unit,
        quantity=extracted.quantity,
        customer=extracted.customer,
    )
    match extracted.action:
        case Action.SALE | Action.CREDIT_SALE:
            _apply_sale(session, shop, extracted, message_id, result)
        case Action.PURCHASE:
            _apply_purchase(session, shop, extracted, message_id, result)
        case Action.DEBT_PAYMENT:
            _apply_debt_payment(session, shop, extracted, message_id, result)
        case Action.STOCK_CHECK:
            _check_stock(session, shop, extracted, result)
        case _:
            result.warnings.append(LedgerWarning.NOT_UNDERSTOOD)
    return result


def _require_product_and_quantity(extracted: ExtractedAction, result: ActionResult) -> bool:
    if not extracted.product:
        result.warnings.append(LedgerWarning.MISSING_PRODUCT)
    elif extracted.quantity is None:
        result.warnings.append(LedgerWarning.MISSING_QUANTITY)
    else:
        return True
    return False


def _record_stock(session: Session, product: Product, result: ActionResult) -> None:
    session.flush()
    result.stock = current_stock(session, product.id)
    if result.stock < 0:
        result.warnings.append(LedgerWarning.NEGATIVE_STOCK)


def _apply_sale(session: Session, shop: Shop, extracted: ExtractedAction, message_id: str, result: ActionResult) -> None:
    if not _require_product_and_quantity(extracted, result):
        return
    product = _get_or_create_product(session, shop, extracted.product, extracted.unit)
    quantity = extracted.quantity
    unit_price, total = _line_prices(extracted, quantity, product.sale_price)
    if total is None:
        result.warnings.append(LedgerWarning.MISSING_PRICE)
    elif extracted.price is not None or extracted.total_price is not None:
        product.sale_price = unit_price  # the merchant just told us the current price

    on_credit = extracted.action == Action.CREDIT_SALE
    customer = None
    if extracted.customer:
        customer = _get_or_create_customer(session, shop, extracted.customer)
    elif on_credit:
        result.warnings.append(LedgerWarning.MISSING_CUSTOMER)

    sale = Sale(
        shop_id=shop.id,
        product_id=product.id,
        customer_id=customer.id if customer else None,
        quantity=quantity,
        unit_price=unit_price,
        total=total,
        on_credit=on_credit,
        source_message_id=message_id,
    )
    session.add(sale)
    session.flush()
    session.add(
        StockMovement(
            shop_id=shop.id,
            product_id=product.id,
            kind=MovementKind.SALE,
            quantity=-quantity,
            sale_id=sale.id,
            source_message_id=message_id,
        )
    )
    # A debt needs both someone who owes and an amount owed.
    if on_credit and customer is not None and total is not None:
        session.add(
            Debt(
                shop_id=shop.id,
                customer_id=customer.id,
                kind=DebtKind.CREDIT,
                amount=total,
                sale_id=sale.id,
                source_message_id=message_id,
            )
        )
    result.recorded = True
    result.product = product.name
    result.total = total
    _record_stock(session, product, result)
    if customer is not None:
        result.customer = customer.name
        result.customer_balance = customer_balance(session, customer.id)


def _apply_purchase(session: Session, shop: Shop, extracted: ExtractedAction, message_id: str, result: ActionResult) -> None:
    if not _require_product_and_quantity(extracted, result):
        return
    product = _get_or_create_product(session, shop, extracted.product, extracted.unit)
    # Purchase prices are costs: they never update the product's sale price.
    unit_cost, total = _line_prices(extracted, extracted.quantity, None)
    session.add(
        StockMovement(
            shop_id=shop.id,
            product_id=product.id,
            kind=MovementKind.PURCHASE,
            quantity=extracted.quantity,
            unit_cost=unit_cost,
            source_message_id=message_id,
        )
    )
    result.recorded = True
    result.product = product.name
    result.total = total
    _record_stock(session, product, result)


def _apply_debt_payment(session: Session, shop: Shop, extracted: ExtractedAction, message_id: str, result: ActionResult) -> None:
    amount = extracted.total_price if extracted.total_price is not None else extracted.price
    if not extracted.customer:
        result.warnings.append(LedgerWarning.MISSING_CUSTOMER)
        return
    if amount is None:
        result.warnings.append(LedgerWarning.MISSING_AMOUNT)
        return
    customer = _get_or_create_customer(session, shop, extracted.customer)
    session.add(
        Debt(
            shop_id=shop.id,
            customer_id=customer.id,
            kind=DebtKind.REPAYMENT,
            amount=-amount,
            source_message_id=message_id,
        )
    )
    session.flush()
    result.recorded = True
    result.total = amount
    result.customer = customer.name
    result.customer_balance = customer_balance(session, customer.id)


def _check_stock(session: Session, shop: Shop, extracted: ExtractedAction, result: ActionResult) -> None:
    if not extracted.product:
        result.warnings.append(LedgerWarning.MISSING_PRODUCT)
        return
    product = _find_product(session, shop, extracted.product)
    if product is None:
        result.warnings.append(LedgerWarning.UNKNOWN_PRODUCT)
        return
    result.product = product.name
    result.unit = product.unit if product.unit != "piece" else None
    result.stock = current_stock(session, product.id)
