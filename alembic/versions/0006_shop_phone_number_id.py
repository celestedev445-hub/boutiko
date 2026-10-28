"""shop phone_number_id

- shops: phone_number_id, the business number the merchant last wrote to
  (messages without an incoming one, like the evening summary, are sent from it)

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-28
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("shops", sa.Column("phone_number_id", sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column("shops", "phone_number_id")
