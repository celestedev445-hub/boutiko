"""daily summary

- shops: summary_hour (per-shop override), last_message_at (24 h window),
  summary_skipped_day / summary_offered_day (summary offered on the next message)
- stock_movements: total_cost (purchase amount as stated)
- daily_summaries: one row per shop, day and channel

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-28
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("shops", sa.Column("summary_hour", sa.Integer(), nullable=True))
    op.add_column("shops", sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("shops", sa.Column("summary_skipped_day", sa.Date(), nullable=True))
    op.add_column("shops", sa.Column("summary_offered_day", sa.Date(), nullable=True))

    op.add_column("stock_movements", sa.Column("total_cost", sa.BigInteger(), nullable=True))
    # Existing purchases: best estimate from the unit cost.
    op.execute(
        "UPDATE stock_movements SET total_cost = ROUND(unit_cost * quantity) "
        "WHERE kind = 'purchase' AND unit_cost IS NOT NULL"
    )

    op.create_table(
        "daily_summaries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("shop_id", sa.Integer(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "channel",
            sa.Enum("auto", "manual", name="summarychannel", native_enum=False, length=20),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["shop_id"], ["shops.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("shop_id", "day", "channel", name="uq_daily_summaries_shop_id_day_channel"),
    )
    op.create_index(op.f("ix_daily_summaries_shop_id"), "daily_summaries", ["shop_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_daily_summaries_shop_id"), table_name="daily_summaries")
    op.drop_table("daily_summaries")
    op.drop_column("stock_movements", "total_cost")
    op.drop_column("shops", "summary_offered_day")
    op.drop_column("shops", "summary_skipped_day")
    op.drop_column("shops", "last_message_at")
    op.drop_column("shops", "summary_hour")
