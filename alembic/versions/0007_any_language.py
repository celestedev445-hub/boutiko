"""any language

- shops.language: free language code (VARCHAR(16)) instead of the fr / en / pcm enum,
  existing values kept; shops.language_name: its English name, for prompts
- translations: fixed replies translated once per (key, language) by a Meta model

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-28
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_LANGUAGE = sa.Enum("fr", "en", "pcm", name="language", native_enum=False, length=20)


def upgrade() -> None:
    # Batch mode: SQLite (tests) can't alter a column type in place.
    with op.batch_alter_table("shops") as batch:
        batch.alter_column("language", existing_type=_OLD_LANGUAGE, type_=sa.String(16), existing_nullable=False)
        batch.add_column(sa.Column("language_name", sa.String(64), nullable=True))

    op.create_table(
        "translations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("language", sa.String(16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("key", "language", name="uq_translations_key_language"),
    )


def downgrade() -> None:
    op.drop_table("translations")
    # The old enum only knows fr / en / pcm: other shops fall back to English.
    op.execute("UPDATE shops SET language = 'en' WHERE language NOT IN ('fr', 'en', 'pcm')")
    with op.batch_alter_table("shops") as batch:
        batch.drop_column("language_name")
        batch.alter_column("language", existing_type=sa.String(16), type_=_OLD_LANGUAGE, existing_nullable=False)
