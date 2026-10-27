"""initial schema

Revision ID: 0001
Revises: 
Create Date: 2026-10-27 12:27:23.048241
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0001'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('shops',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('owner_phone', sa.String(length=20), nullable=False),
    sa.Column('language', sa.Enum('fr', 'en', 'pcm', name='language', native_enum=False, length=20), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_shops_owner_phone'), 'shops', ['owner_phone'], unique=True)
    op.create_table('customers',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('shop_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('phone', sa.String(length=20), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['shop_id'], ['shops.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('shop_id', 'name')
    )
    op.create_index(op.f('ix_customers_shop_id'), 'customers', ['shop_id'], unique=False)
    op.create_table('products',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('shop_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('unit', sa.String(length=20), nullable=False),
    sa.Column('sale_price', sa.BigInteger(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['shop_id'], ['shops.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('shop_id', 'name')
    )
    op.create_index(op.f('ix_products_shop_id'), 'products', ['shop_id'], unique=False)
    op.create_table('sales',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('shop_id', sa.Integer(), nullable=False),
    sa.Column('product_id', sa.Integer(), nullable=False),
    sa.Column('customer_id', sa.Integer(), nullable=True),
    sa.Column('quantity', sa.Numeric(precision=12, scale=3), nullable=False),
    sa.Column('unit_price', sa.BigInteger(), nullable=False),
    sa.Column('total', sa.BigInteger(), nullable=False),
    sa.Column('on_credit', sa.Boolean(), nullable=False),
    sa.Column('source_message_id', sa.String(length=128), nullable=True),
    sa.Column('occurred_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['product_id'], ['products.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['shop_id'], ['shops.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_sales_occurred_at'), 'sales', ['occurred_at'], unique=False)
    op.create_index(op.f('ix_sales_product_id'), 'sales', ['product_id'], unique=False)
    op.create_index(op.f('ix_sales_shop_id'), 'sales', ['shop_id'], unique=False)
    op.create_table('debts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('shop_id', sa.Integer(), nullable=False),
    sa.Column('customer_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.Enum('credit', 'repayment', name='debtkind', native_enum=False, length=20), nullable=False),
    sa.Column('amount', sa.BigInteger(), nullable=False),
    sa.Column('sale_id', sa.Integer(), nullable=True),
    sa.Column('note', sa.String(length=255), nullable=True),
    sa.Column('occurred_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['sale_id'], ['sales.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['shop_id'], ['shops.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_debts_customer_id'), 'debts', ['customer_id'], unique=False)
    op.create_index(op.f('ix_debts_occurred_at'), 'debts', ['occurred_at'], unique=False)
    op.create_index(op.f('ix_debts_shop_id'), 'debts', ['shop_id'], unique=False)
    op.create_table('stock_movements',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('shop_id', sa.Integer(), nullable=False),
    sa.Column('product_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.Enum('purchase', 'sale', 'adjustment', 'loss', name='movementkind', native_enum=False, length=20), nullable=False),
    sa.Column('quantity', sa.Numeric(precision=12, scale=3), nullable=False),
    sa.Column('unit_cost', sa.BigInteger(), nullable=True),
    sa.Column('sale_id', sa.Integer(), nullable=True),
    sa.Column('source_message_id', sa.String(length=128), nullable=True),
    sa.Column('occurred_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['product_id'], ['products.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['sale_id'], ['sales.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['shop_id'], ['shops.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_stock_movements_occurred_at'), 'stock_movements', ['occurred_at'], unique=False)
    op.create_index(op.f('ix_stock_movements_product_id'), 'stock_movements', ['product_id'], unique=False)
    op.create_index(op.f('ix_stock_movements_shop_id'), 'stock_movements', ['shop_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_stock_movements_shop_id'), table_name='stock_movements')
    op.drop_index(op.f('ix_stock_movements_product_id'), table_name='stock_movements')
    op.drop_index(op.f('ix_stock_movements_occurred_at'), table_name='stock_movements')
    op.drop_table('stock_movements')
    op.drop_index(op.f('ix_debts_shop_id'), table_name='debts')
    op.drop_index(op.f('ix_debts_occurred_at'), table_name='debts')
    op.drop_index(op.f('ix_debts_customer_id'), table_name='debts')
    op.drop_table('debts')
    op.drop_index(op.f('ix_sales_shop_id'), table_name='sales')
    op.drop_index(op.f('ix_sales_product_id'), table_name='sales')
    op.drop_index(op.f('ix_sales_occurred_at'), table_name='sales')
    op.drop_table('sales')
    op.drop_index(op.f('ix_products_shop_id'), table_name='products')
    op.drop_table('products')
    op.drop_index(op.f('ix_customers_shop_id'), table_name='customers')
    op.drop_table('customers')
    op.drop_index(op.f('ix_shops_owner_phone'), table_name='shops')
    op.drop_table('shops')
