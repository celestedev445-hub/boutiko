"""product keys and known stock

- products.stock_known: true once the stock has a starting point (a purchase or a count)
- products.stock_hint_sent: the merchant was asked once to give the stock
- pending_actions.kind: confirm (photo, YES/NO) or choose_product ("Quel savon ?")
- data: stock_known for products with a purchase; stock brought back to 0 for the
  others when negative; name_key recomputed with the new product key

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-28
"""
import logging
import re
import unicodedata
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

# Frozen copy of app.services.normalize.product_key at the time of this migration.
_ARTICLES = {"le", "la", "les", "l", "de", "du", "des", "d", "the", "of", "un", "une", "a", "an"}
_GENERIC_UNIT_WORDS = {"piece", "pc", "pcs", "pce", "unite", "unit"}


def _singular(word: str) -> str:
    if len(word) <= 3:
        return word
    if word.endswith(("eaux", "oux", "eux")):
        return word[:-1]
    if word.endswith(("xes", "oes")):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _product_key(name: str) -> str:
    text = unicodedata.normalize("NFKD", name)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = text.replace("'", " ").replace("’", " ")
    words = [_singular(word) for word in re.findall(r"[a-z0-9]+", text)]
    return " ".join(w for w in words if w not in _ARTICLES and w not in _GENERIC_UNIT_WORDS and not w.isdigit())


def migrate_data(bind) -> None:
    """The data part of the upgrade (also run by the tests on SQLite)."""
    # 1. A product that was ever bought has a known stock.
    bind.execute(
        sa.text(
            "UPDATE products SET stock_known = :yes WHERE id IN "
            "(SELECT DISTINCT product_id FROM stock_movements WHERE kind = 'purchase')"
        ),
        {"yes": True},
    )

    # 2. The others were only ever sold: a negative stock means nothing. Bring it back to 0
    #    with an adjustment (movements are never edited or deleted).
    negatives = bind.execute(
        sa.text(
            "SELECT p.id, p.shop_id, SUM(m.quantity) FROM products p "
            "JOIN stock_movements m ON m.product_id = p.id "
            "WHERE p.stock_known = :no GROUP BY p.id, p.shop_id HAVING SUM(m.quantity) < 0"
        ),
        {"no": False},
    ).all()
    now = datetime.now(UTC)
    for product_id, shop_id, stock in negatives:
        bind.execute(
            sa.text(
                "INSERT INTO stock_movements (shop_id, product_id, kind, quantity, occurred_at) "
                "VALUES (:shop_id, :product_id, 'adjustment', :quantity, :now)"
            ).bindparams(sa.bindparam("now", type_=sa.DateTime(timezone=True))),
            {"shop_id": shop_id, "product_id": product_id, "quantity": -stock, "now": now},
        )

    # 3. New lookup keys. A key already taken in the shop keeps the old one: those are
    #    duplicates for scripts/merge_products.py.
    taken: dict[int, set[str]] = {}
    rows = bind.execute(sa.text("SELECT id, shop_id, name, name_key FROM products ORDER BY id")).all()
    for _, shop_id, _, name_key in rows:
        taken.setdefault(shop_id, set()).add(name_key)
    duplicates = 0
    for product_id, shop_id, name, name_key in rows:
        key = _product_key(name)
        if key == name_key:
            continue
        if not key or key in taken[shop_id]:
            duplicates += 1
            continue
        bind.execute(sa.text("UPDATE products SET name_key = :key WHERE id = :id"), {"key": key, "id": product_id})
        taken[shop_id].discard(name_key)
        taken[shop_id].add(key)
    if duplicates:
        logger.info("%d product(s) look like duplicates: run scripts/merge_products.py", duplicates)


def upgrade() -> None:
    op.add_column("products", sa.Column("stock_known", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column("products", sa.Column("stock_hint_sent", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column(
        "pending_actions",
        sa.Column(
            "kind",
            sa.Enum("confirm", "choose_product", name="pendingkind", native_enum=False, length=20),
            server_default="confirm",
            nullable=False,
        ),
    )
    migrate_data(op.get_bind())


def downgrade() -> None:
    op.drop_column("pending_actions", "kind")
    op.drop_column("products", "stock_hint_sent")
    op.drop_column("products", "stock_known")
