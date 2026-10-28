"""Figures of a shop's day, computed from the ledger. No model, integers in FCFA."""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import clock
from app.config import get_settings
from app.models import Customer, Debt, DebtKind, MovementKind, Product, Sale, Shop, StockMovement
from app.services.normalize import is_generic_unit

TOP_PRODUCTS = 3


@dataclass
class ProductFigure:
    name: str
    unit: str | None  # None for plain units ("12 savons")
    quantity: Decimal
    amount: int  # FCFA, priced sales only


@dataclass
class StockFigure:
    name: str
    unit: str | None
    stock: Decimal


@dataclass
class DebtorFigure:
    name: str
    amount: int


@dataclass
class DayFigures:
    shop_id: int
    day: date  # the shop's local day
    sales_count: int = 0
    sales_total: int = 0  # credit sales included
    credit_sales_count: int = 0
    credit_sales_total: int = 0
    unpriced_sales_count: int = 0  # recorded without a known price
    purchases_count: int = 0
    purchases_total: int = 0
    debt_repaid_total: int = 0
    new_debt_total: int = 0
    debts_outstanding_total: int = 0  # owed to the shop at the end of the day
    debtors_count: int = 0
    top_debtor: DebtorFigure | None = None
    top_products: list[ProductFigure] = field(default_factory=list)
    low_stock: list[StockFigure] = field(default_factory=list)  # 0 < stock <= threshold
    out_of_stock: list[StockFigure] = field(default_factory=list)  # stock <= 0
    yesterday_sales_total: int = 0
    sales_change: int = 0  # today - yesterday
    sales_change_percent: int | None = None  # None when nothing was sold yesterday

    @property
    def is_empty(self) -> bool:
        """Nothing recorded that day."""
        return not (self.sales_count or self.purchases_count or self.debt_repaid_total or self.new_debt_total)

    def amounts(self) -> set[int]:
        """Every FCFA amount of the summary: the only ones a written summary may contain."""
        values = {
            self.sales_total, self.credit_sales_total, self.purchases_total, self.debt_repaid_total,
            self.new_debt_total, self.debts_outstanding_total, self.yesterday_sales_total, abs(self.sales_change),
        }
        values |= {p.amount for p in self.top_products}
        if self.top_debtor:
            values.add(self.top_debtor.amount)
        return values


def shop_timezone() -> ZoneInfo:
    return ZoneInfo(get_settings().shop_timezone)


def local_now(now: datetime | None = None) -> datetime:
    return (now or clock.now()).astimezone(shop_timezone())


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """[start, end) of a local day, in UTC."""
    zone = shop_timezone()
    start = datetime.combine(day, time(), tzinfo=zone)
    end = datetime.combine(day + timedelta(days=1), time(), tzinfo=zone)
    return start.astimezone(UTC), end.astimezone(UTC)


def _decimal(value) -> Decimal:
    return Decimal(str(value or 0))


def _sales_total(session: Session, shop_id: int, start: datetime, end: datetime) -> int:
    total = session.scalar(
        select(func.coalesce(func.sum(Sale.total), 0)).where(
            Sale.shop_id == shop_id, Sale.occurred_at >= start, Sale.occurred_at < end
        )
    )
    return int(total)


def build_daily_summary(session: Session, shop: Shop, day: date) -> DayFigures:
    settings = get_settings()
    start, end = day_bounds(day)
    figures = DayFigures(shop_id=shop.id, day=day)
    products = {p.id: p for p in session.scalars(select(Product).where(Product.shop_id == shop.id))}

    # Sales of the day
    sales = session.scalars(
        select(Sale).where(Sale.shop_id == shop.id, Sale.occurred_at >= start, Sale.occurred_at < end)
    ).all()
    sold_quantity: dict[int, Decimal] = defaultdict(Decimal)
    sold_amount: dict[int, int] = defaultdict(int)
    for sale in sales:
        figures.sales_count += 1
        sold_quantity[sale.product_id] += _decimal(sale.quantity)
        if sale.total is None:
            figures.unpriced_sales_count += 1
        else:
            figures.sales_total += sale.total
            sold_amount[sale.product_id] += sale.total
        if sale.on_credit:
            figures.credit_sales_count += 1
            figures.credit_sales_total += sale.total or 0
    ranking = sorted(sold_quantity, key=lambda pid: (-sold_quantity[pid], -sold_amount[pid], products[pid].name))
    figures.top_products = [
        ProductFigure(
            name=products[pid].name,
            unit=None if is_generic_unit(products[pid].unit) else products[pid].unit,
            quantity=sold_quantity[pid],
            amount=sold_amount[pid],
        )
        for pid in ranking[:TOP_PRODUCTS]
    ]

    # Purchases of the day
    purchases = session.scalars(
        select(StockMovement).where(
            StockMovement.shop_id == shop.id,
            StockMovement.kind == MovementKind.PURCHASE,
            StockMovement.occurred_at >= start,
            StockMovement.occurred_at < end,
        )
    ).all()
    figures.purchases_count = len(purchases)
    for movement in purchases:
        if movement.total_cost is not None:
            figures.purchases_total += movement.total_cost
        elif movement.unit_cost is not None:  # rows recorded before total_cost existed
            figures.purchases_total += int((movement.unit_cost * _decimal(movement.quantity)).quantize(Decimal(1), ROUND_HALF_UP))

    # Debts of the day
    debts = session.scalars(
        select(Debt).where(Debt.shop_id == shop.id, Debt.occurred_at >= start, Debt.occurred_at < end)
    ).all()
    figures.new_debt_total = sum(d.amount for d in debts if d.kind == DebtKind.CREDIT)
    figures.debt_repaid_total = -sum(d.amount for d in debts if d.kind == DebtKind.REPAYMENT)

    # Debts still owed at the end of the day
    balances = session.execute(
        select(Customer.name, func.sum(Debt.amount))
        .join(Customer, Customer.id == Debt.customer_id)
        .where(Debt.shop_id == shop.id, Debt.occurred_at < end)
        .group_by(Customer.id, Customer.name)
    ).all()
    owing = [(name, int(balance)) for name, balance in balances if balance and int(balance) > 0]
    figures.debts_outstanding_total = sum(amount for _, amount in owing)
    figures.debtors_count = len(owing)
    if owing:
        name, amount = max(owing, key=lambda item: (item[1], item[0]))
        figures.top_debtor = DebtorFigure(name=name, amount=amount)

    # Stock at the end of the day
    stock_rows = session.execute(
        select(StockMovement.product_id, func.sum(StockMovement.quantity))
        .where(StockMovement.shop_id == shop.id, StockMovement.occurred_at < end)
        .group_by(StockMovement.product_id)
    ).all()
    threshold = Decimal(settings.low_stock_threshold)
    for product_id, total in sorted(stock_rows, key=lambda row: (_decimal(row[1]), products[row[0]].name)):
        product = products[product_id]
        figure = StockFigure(
            name=product.name, unit=None if is_generic_unit(product.unit) else product.unit, stock=_decimal(total)
        )
        if figure.stock <= 0:
            figures.out_of_stock.append(figure)
        elif figure.stock <= threshold:
            figures.low_stock.append(figure)

    # Compared with the day before
    yesterday_start, yesterday_end = day_bounds(day - timedelta(days=1))
    figures.yesterday_sales_total = _sales_total(session, shop.id, yesterday_start, yesterday_end)
    figures.sales_change = figures.sales_total - figures.yesterday_sales_total
    if figures.yesterday_sales_total:
        percent = Decimal(figures.sales_change * 100) / Decimal(figures.yesterday_sales_total)
        figures.sales_change_percent = int(percent.quantize(Decimal(1), ROUND_HALF_UP))
    return figures
