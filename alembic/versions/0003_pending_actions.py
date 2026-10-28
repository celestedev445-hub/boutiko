"""pending actions

Actions read on a photo, waiting for the merchant's YES or NO.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-28
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pending_actions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("shop_id", sa.Integer(), nullable=False),
        sa.Column("wa_id", sa.String(length=20), nullable=False),
        sa.Column("source_message_id", sa.String(length=128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status",
            sa.Enum("pending", "confirmed", "cancelled", "expired", name="pendingstatus", native_enum=False, length=20),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["shop_id"], ["shops.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_message_id"),
    )
    op.create_index(op.f("ix_pending_actions_shop_id"), "pending_actions", ["shop_id"], unique=False)
    op.create_index(op.f("ix_pending_actions_wa_id"), "pending_actions", ["wa_id"], unique=False)
    op.create_index(op.f("ix_pending_actions_expires_at"), "pending_actions", ["expires_at"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_pending_actions_expires_at"), table_name="pending_actions")
    op.drop_index(op.f("ix_pending_actions_wa_id"), table_name="pending_actions")
    op.drop_index(op.f("ix_pending_actions_shop_id"), table_name="pending_actions")
    op.drop_table("pending_actions")
