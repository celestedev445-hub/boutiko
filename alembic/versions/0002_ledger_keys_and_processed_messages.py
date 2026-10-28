"""ledger keys and processed messages

- products / customers: normalized name_key, unique per shop (replaces unique name)
- sales: unit_price and total nullable (sale recorded without a known price)
- debts: source_message_id
- processed_messages: one row per WhatsApp message applied to the ledger

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-28
"""
import re
import unicodedata
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Frozen copy of app.services.normalize.normalize_name at the time of this migration.
def _normalize_name(name: str) -> str:
    text = unicodedata.normalize("NFKD", name)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = re.sub(r"[^\w\s'-]", " ", text)
    words = []
    for word in text.split():
        if len(word) > 3 and word.endswith("eaux"):
            word = word[:-1]
        elif len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        words.append(word)
    return " ".join(words)


def _add_name_key(table: str) -> None:
    op.add_column(table, sa.Column("name_key", sa.String(length=120), nullable=True))
    bind = op.get_bind()
    rows = bind.execute(sa.text(f"SELECT id, name FROM {table}")).all()
    for row_id, name in rows:
        bind.execute(
            sa.text(f"UPDATE {table} SET name_key = :key WHERE id = :id"),
            {"key": _normalize_name(name), "id": row_id},
        )
    with op.batch_alter_table(table) as batch:
        batch.alter_column("name_key", existing_type=sa.String(length=120), nullable=False)
        # Name given by PostgreSQL to the unnamed UniqueConstraint("shop_id", "name") of 0001.
        batch.drop_constraint(f"{table}_shop_id_name_key", type_="unique")
        batch.create_unique_constraint(f"uq_{table}_shop_id_name_key", ["shop_id", "name_key"])


def upgrade() -> None:
    op.create_table(
        "processed_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("shop_id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.String(length=128), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["shop_id"], ["shops.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("message_id"),
    )
    op.create_index(op.f("ix_processed_messages_shop_id"), "processed_messages", ["shop_id"], unique=False)

    _add_name_key("products")
    _add_name_key("customers")

    op.add_column("debts", sa.Column("source_message_id", sa.String(length=128), nullable=True))
    with op.batch_alter_table("sales") as batch:
        batch.alter_column("unit_price", existing_type=sa.BigInteger(), nullable=True)
        batch.alter_column("total", existing_type=sa.BigInteger(), nullable=True)


def downgrade() -> None:
    op.execute("UPDATE sales SET unit_price = 0 WHERE unit_price IS NULL")
    op.execute("UPDATE sales SET total = 0 WHERE total IS NULL")
    with op.batch_alter_table("sales") as batch:
        batch.alter_column("total", existing_type=sa.BigInteger(), nullable=False)
        batch.alter_column("unit_price", existing_type=sa.BigInteger(), nullable=False)
    op.drop_column("debts", "source_message_id")

    for table in ("customers", "products"):
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(f"uq_{table}_shop_id_name_key", type_="unique")
            batch.create_unique_constraint(f"{table}_shop_id_name_key", ["shop_id", "name"])
            batch.drop_column("name_key")

    op.drop_index(op.f("ix_processed_messages_shop_id"), table_name="processed_messages")
    op.drop_table("processed_messages")
