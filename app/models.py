"""Data model.

Stock is never stored directly: it is the sum of dated stock movements for a
product. Debts work the same way — a customer's balance is the sum of their
debt entries (credit given is positive, repayments are negative).
Money amounts are integers in the shop's currency (FCFA has no subunit).
"""

import enum
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Integer,
    Enum,
    ForeignKey,
    JSON,
    Numeric,
    String,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.db import Base

Quantity = Numeric(12, 3)


def _enum(cls: type[enum.Enum]) -> Enum:
    # Stored as VARCHAR + CHECK so adding a value doesn't need ALTER TYPE.
    return Enum(cls, native_enum=False, length=20, values_callable=lambda e: [m.value for m in e])


class Language(str, enum.Enum):
    FR = "fr"
    EN = "en"
    PIDGIN = "pcm"


class MovementKind(str, enum.Enum):
    PURCHASE = "purchase"  # restock from a supplier
    SALE = "sale"
    ADJUSTMENT = "adjustment"  # manual correction or shelf-count reconciliation
    LOSS = "loss"  # spoiled, broken, stolen


class SummaryChannel(str, enum.Enum):
    AUTO = "auto"  # sent by the scheduler in the evening
    MANUAL = "manual"  # the merchant asked for it


class PendingStatus(str, enum.Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class DebtKind(str, enum.Enum):
    CREDIT = "credit"  # customer took goods on credit
    REPAYMENT = "repayment"


class Shop(Base):
    __tablename__ = "shops"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    owner_phone: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    language: Mapped[Language] = mapped_column(_enum(Language), default=Language.FR)
    currency: Mapped[str] = mapped_column(String(3), default="XAF")
    summary_hour: Mapped[int | None] = mapped_column(Integer)  # None: settings.summary_hour
    # WhatsApp only allows free-form messages within 24 h of the merchant's last message.
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Day whose automatic summary couldn't be sent (24 h window closed), to offer it on the next message.
    summary_skipped_day: Mapped[date | None] = mapped_column(Date)
    # Day whose summary was offered ("tape RÉSUMÉ"): "résumé" shows that day, the scheduler leaves it alone.
    summary_offered_day: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Children are removed by the database (ON DELETE CASCADE), not by the ORM.
    products: Mapped[list["Product"]] = relationship(back_populates="shop", passive_deletes=True)
    customers: Mapped[list["Customer"]] = relationship(back_populates="shop", passive_deletes=True)


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("shop_id", "name_key", name="uq_products_shop_id_name_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))  # as first written by the merchant
    name_key: Mapped[str] = mapped_column(String(120))  # normalized, for lookups (see services.normalize)
    unit: Mapped[str] = mapped_column(String(20), default="piece")
    sale_price: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    shop: Mapped[Shop] = relationship(back_populates="products")
    movements: Mapped[list["StockMovement"]] = relationship(back_populates="product", passive_deletes=True)


class StockMovement(Base):
    __tablename__ = "stock_movements"

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    kind: Mapped[MovementKind] = mapped_column(_enum(MovementKind))
    # Signed: positive adds to stock, negative removes from it.
    quantity: Mapped[Decimal] = mapped_column(Quantity)
    unit_cost: Mapped[int | None] = mapped_column(BigInteger)
    total_cost: Mapped[int | None] = mapped_column(BigInteger)  # purchases: amount paid for the line, as stated
    sale_id: Mapped[int | None] = mapped_column(ForeignKey("sales.id", ondelete="SET NULL"))
    # WhatsApp message id that produced this movement, for traceability/idempotency.
    source_message_id: Mapped[str | None] = mapped_column(String(128))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    product: Mapped[Product] = relationship(back_populates="movements")


class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (UniqueConstraint("shop_id", "name_key", name="uq_customers_shop_id_name_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    name_key: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str | None] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    shop: Mapped[Shop] = relationship(back_populates="customers")
    debts: Mapped[list["Debt"]] = relationship(back_populates="customer", passive_deletes=True)


class Sale(Base):
    __tablename__ = "sales"

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"), index=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"))
    quantity: Mapped[Decimal] = mapped_column(Quantity)
    # None when the merchant gave no price and none is known for the product yet.
    unit_price: Mapped[int | None] = mapped_column(BigInteger)
    total: Mapped[int | None] = mapped_column(BigInteger)
    on_credit: Mapped[bool] = mapped_column(default=False)
    source_message_id: Mapped[str | None] = mapped_column(String(128))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class Debt(Base):
    __tablename__ = "debts"

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), index=True)
    kind: Mapped[DebtKind] = mapped_column(_enum(DebtKind))
    # Signed: positive for credit given, negative for repayment.
    amount: Mapped[int] = mapped_column(BigInteger)
    sale_id: Mapped[int | None] = mapped_column(ForeignKey("sales.id", ondelete="SET NULL"))
    note: Mapped[str | None] = mapped_column(String(255))
    source_message_id: Mapped[str | None] = mapped_column(String(128))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    customer: Mapped[Customer] = relationship(back_populates="debts")


class ProcessedMessage(Base):
    """One row per WhatsApp message applied to the ledger, so a redelivery is never applied twice."""

    __tablename__ = "processed_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    message_id: Mapped[str] = mapped_column(String(128), unique=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PendingAction(Base):
    """Actions read on a photo, waiting for the merchant to answer YES or NO before they're recorded."""

    __tablename__ = "pending_actions"

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    wa_id: Mapped[str] = mapped_column(String(20), index=True)  # WhatsApp id of the merchant who sent the photo
    source_message_id: Mapped[str] = mapped_column(String(128), unique=True)
    payload: Mapped[dict] = mapped_column(JSON)  # Extraction.model_dump(mode="json")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[PendingStatus] = mapped_column(_enum(PendingStatus), default=PendingStatus.PENDING)


class DailySummary(Base):
    """A summary sent to a merchant: at most one per day and channel."""

    __tablename__ = "daily_summaries"
    __table_args__ = (UniqueConstraint("shop_id", "day", "channel", name="uq_daily_summaries_shop_id_day_channel"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    day: Mapped[date] = mapped_column(Date)  # the shop's local day
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    channel: Mapped[SummaryChannel] = mapped_column(_enum(SummaryChannel))


def current_stock(session: Session, product_id: int, at: datetime | None = None) -> Decimal:
    """Stock of a product, optionally as of a given time."""
    query = select(func.coalesce(func.sum(StockMovement.quantity), 0)).where(
        StockMovement.product_id == product_id
    )
    if at is not None:
        query = query.where(StockMovement.occurred_at <= at)
    return Decimal(str(session.scalar(query)))


def customer_balance(session: Session, customer_id: int) -> int:
    """Amount the customer still owes the shop."""
    query = select(func.coalesce(func.sum(Debt.amount), 0)).where(Debt.customer_id == customer_id)
    return int(session.scalar(query))
