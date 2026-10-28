"""Turn an extraction into rows: sales, stock movements, debts.

apply_extraction is the only entry point. It runs in one transaction: either
every action of the message is recorded, or none is. When a product name could
mean several products, nothing is recorded and a ProductChoice is returned so
the merchant can be asked first.
"""

import enum
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import clock
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
from app.services import products
from app.services.normalize import is_generic_unit, normalize_name, product_key
from app.services.products import Match, ProductChoice, Resolution

# Actions about a product, whose name must be resolved before anything is recorded.
PRODUCT_ACTIONS = {Action.SALE, Action.CREDIT_SALE, Action.PURCHASE, Action.SET_STOCK, Action.STOCK_CHECK}


class LedgerWarning(str, enum.Enum):
    MISSING_PRICE = "missing_price"  # recorded without an amount
    MISSING_QUANTITY = "missing_quantity"  # not recorded
    MISSING_PRODUCT = "missing_product"  # not recorded
    MISSING_CUSTOMER = "missing_customer"  # credit sale recorded without a debt, or payment not recorded
    MISSING_AMOUNT = "missing_amount"  # debt payment without an amount, not recorded
    UNKNOWN_PRODUCT = "unknown_product"  # stock check for a product never seen
    FORBIDDEN_PRODUCT = "forbidden_product"  # "shop", "stock"...: not recorded
    NEGATIVE_STOCK = "negative_stock"  # recorded; stock went below zero
    STOCK_UNKNOWN = "stock_unknown"  # recorded; ask the merchant to count this product (once)
    NOT_UNDERSTOOD = "not_understood"


@dataclass
class ActionResult:
    action: Action
    recorded: bool = False
    product: str | None = None
    unit: str | None = None
    quantity: Decimal | None = None
    total: int | None = None  # sale/purchase amount, or amount paid back
    stock: Decimal | None = None  # stock after the action; None when not followed
    customer: str | None = None
    customer_balance: int | None = None  # what the customer owes after the action
    warnings: list[LedgerWarning] = field(default_factory=list)


@dataclass
class LedgerResult:
    shop_id: int
    language: Language
    actions: list[ActionResult] = field(default_factory=list)
    duplicate: bool = False  # message already applied earlier: nothing was done
    choice: ProductChoice | None = None  # nothing was recorded: ask which product first


def apply_extraction(session: Session, owner_phone: str, extraction: Extraction, message_id: str) -> LedgerResult:
    """Record every action of a message for the shop of `owner_phone`.

    The shop is created on its first message. Commits on success, rolls back
    on any error. A message id that was already applied is not applied again.
    """
    try:
        shop = get_or_create_shop(session, owner_phone, extraction.language)
        if extraction.language:
            shop.language = Language(extraction.language)  # replies and summaries follow the latest language
        language = shop.language
        if _already_processed(session, message_id):
            return LedgerResult(shop_id=shop.id, language=language, duplicate=True)

        resolutions: dict[int, Resolution] = {}
        for index, extracted in enumerate(extraction.actions):
            if extracted.action in PRODUCT_ACTIONS and extracted.product:
                resolution = products.resolve(session, shop, extracted.product)
                if resolution.match == Match.AMBIGUOUS:
                    shop_id = shop.id
                    session.rollback()  # record nothing until the merchant says which one
                    choice = ProductChoice(
                        action_index=index, query=extracted.product, options=[p.name for p in resolution.options]
                    )
                    return LedgerResult(shop_id=shop_id, language=language, choice=choice)
                resolutions[index] = resolution

        session.add(ProcessedMessage(shop_id=shop.id, message_id=message_id))
        result = LedgerResult(shop_id=shop.id, language=language)
        for index, extracted in enumerate(extraction.actions):
            result.actions.append(_apply_action(session, shop, extracted, resolutions.get(index), message_id))
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


def _product_for(
    session: Session, shop: Shop, extracted: ExtractedAction, resolution: Resolution, result: ActionResult
) -> Product | None:
    """The existing product, a new one, or None (with a warning) if the name is not a product."""
    if resolution.match == Match.FORBIDDEN:
        result.warnings.append(LedgerWarning.FORBIDDEN_PRODUCT)
        return None
    if resolution.match == Match.FOUND:
        product = resolution.product
        if resolution.more_precise_name:
            products.rename_if_free(session, product, resolution.more_precise_name)
        return product
    name = extracted.product.strip()
    unit = "piece" if is_generic_unit(extracted.unit) else extracted.unit
    product = Product(shop_id=shop.id, name=name, name_key=product_key(name), unit=unit)
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


def _apply_action(
    session: Session, shop: Shop, extracted: ExtractedAction, resolution: Resolution | None, message_id: str
) -> ActionResult:
    result = ActionResult(
        action=extracted.action,
        product=extracted.product,
        unit=extracted.unit,
        quantity=extracted.quantity,
        customer=extracted.customer,
    )
    match extracted.action:
        case Action.SALE | Action.CREDIT_SALE:
            _apply_sale(session, shop, extracted, resolution, message_id, result)
        case Action.PURCHASE:
            _apply_purchase(session, shop, extracted, resolution, message_id, result)
        case Action.SET_STOCK:
            _apply_set_stock(session, shop, extracted, resolution, message_id, result)
        case Action.DEBT_PAYMENT:
            _apply_debt_payment(session, shop, extracted, message_id, result)
        case Action.STOCK_CHECK:
            _check_stock(session, extracted, resolution, result)
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


def _apply_sale(
    session: Session, shop: Shop, extracted: ExtractedAction, resolution: Resolution | None, message_id: str,
    result: ActionResult,
) -> None:
    if not _require_product_and_quantity(extracted, result):
        return
    product = _product_for(session, shop, extracted, resolution, result)
    if product is None:
        return
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
        occurred_at=clock.now(),
    )
    session.add(sale)
    session.flush()
    if product.stock_known:
        session.add(
            StockMovement(
                shop_id=shop.id,
                product_id=product.id,
                kind=MovementKind.SALE,
                quantity=-quantity,
                sale_id=sale.id,
                source_message_id=message_id,
                occurred_at=clock.now(),
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
                occurred_at=clock.now(),
            )
        )
    result.recorded = True
    result.product = product.name
    result.total = total
    if product.stock_known:
        _record_stock(session, product, result)
    elif not product.stock_hint_sent:
        # Without a starting count, a sale can't tell what's left: ask once, never go negative.
        result.warnings.append(LedgerWarning.STOCK_UNKNOWN)
        product.stock_hint_sent = True
    if customer is not None:
        session.flush()
        result.customer = customer.name
        result.customer_balance = customer_balance(session, customer.id)


def _apply_purchase(
    session: Session, shop: Shop, extracted: ExtractedAction, resolution: Resolution | None, message_id: str,
    result: ActionResult,
) -> None:
    if not _require_product_and_quantity(extracted, result):
        return
    product = _product_for(session, shop, extracted, resolution, result)
    if product is None:
        return
    # Purchase prices are costs: they never update the product's sale price.
    unit_cost, total = _line_prices(extracted, extracted.quantity, None)
    session.add(
        StockMovement(
            shop_id=shop.id,
            product_id=product.id,
            kind=MovementKind.PURCHASE,
            quantity=extracted.quantity,
            unit_cost=unit_cost,
            total_cost=total,
            source_message_id=message_id,
            occurred_at=clock.now(),
        )
    )
    product.stock_known = True
    result.recorded = True
    result.product = product.name
    result.total = total
    _record_stock(session, product, result)


def _apply_set_stock(
    session: Session, shop: Shop, extracted: ExtractedAction, resolution: Resolution | None, message_id: str,
    result: ActionResult,
) -> None:
    """"J'ai 20 savons en stock": an adjustment brings the stock to what the merchant counted."""
    if not _require_product_and_quantity(extracted, result):
        return
    product = _product_for(session, shop, extracted, resolution, result)
    if product is None:
        return
    session.flush()
    difference = extracted.quantity - current_stock(session, product.id)
    if difference:
        session.add(
            StockMovement(
                shop_id=shop.id,
                product_id=product.id,
                kind=MovementKind.ADJUSTMENT,
                quantity=difference,
                source_message_id=message_id,
                occurred_at=clock.now(),
            )
        )
    product.stock_known = True
    result.recorded = True
    result.product = product.name
    result.unit = None if is_generic_unit(product.unit) else product.unit
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
            occurred_at=clock.now(),
        )
    )
    session.flush()
    result.recorded = True
    result.total = amount
    result.customer = customer.name
    result.customer_balance = customer_balance(session, customer.id)


def _check_stock(session: Session, extracted: ExtractedAction, resolution: Resolution | None, result: ActionResult) -> None:
    """Read only: never creates a product."""
    if not extracted.product:
        result.warnings.append(LedgerWarning.MISSING_PRODUCT)
        return
    if resolution.match == Match.FORBIDDEN:
        result.warnings.append(LedgerWarning.FORBIDDEN_PRODUCT)
        return
    if resolution.match == Match.NEW:
        result.warnings.append(LedgerWarning.UNKNOWN_PRODUCT)
        return
    product = resolution.product
    result.product = product.name
    result.unit = None if is_generic_unit(product.unit) else product.unit
    if not product.stock_known:
        result.warnings.append(LedgerWarning.STOCK_UNKNOWN)
        return
    result.stock = current_stock(session, product.id)
