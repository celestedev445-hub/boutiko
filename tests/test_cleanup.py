"""Product cleanup: contained names merged into the more precise product, blocked names removed."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models import Customer, Debt, DebtKind, MovementKind, Product, Sale, Shop, StockMovement, current_stock
from app.services import products
from scripts import merge_products

PHONE = "237690001234"
NOW = datetime.now(UTC)


@pytest.fixture
def shop(session):
    shop = Shop(name="test", owner_phone=PHONE)
    session.add(shop)
    session.commit()
    return shop


def product(session, shop, name, *, bought=0, sold=0, stock_known=None) -> Product:
    p = Product(shop_id=shop.id, name=name, name_key=name.lower(), stock_known=bool(bought) if stock_known is None else stock_known)
    session.add(p)
    session.flush()
    if bought:
        session.add(StockMovement(shop_id=shop.id, product_id=p.id, kind=MovementKind.PURCHASE, quantity=bought, occurred_at=NOW))
    for _ in range(sold):
        sale = Sale(shop_id=shop.id, product_id=p.id, quantity=1, unit_price=100, total=100, occurred_at=NOW)
        session.add(sale)
        session.flush()
        if p.stock_known:
            session.add(StockMovement(shop_id=shop.id, product_id=p.id, kind=MovementKind.SALE, quantity=-1,
                                      sale_id=sale.id, occurred_at=NOW))
    session.commit()
    return p


def names(session) -> list[str]:
    return sorted(session.scalars(select(Product.name)))


# --- (b) contained names ---


def test_contained_name_with_one_candidate_is_merged_into_the_precise_one(session, shop):
    savon = product(session, shop, "savon", sold=2)
    azur = product(session, shop, "Savon Azur", bought=10, sold=1)
    product(session, shop, "riz", bought=5)

    plan = products.plan_cleanup(session, shop.id)
    assert [(g.keep.name, [d.name for d in g.duplicates]) for g in plan.contained] == [("Savon Azur", ["savon"])]
    assert plan.ambiguous == []

    products.apply_cleanup(session, plan)
    session.commit()

    assert names(session) == ["Savon Azur", "riz"]
    assert session.scalar(select(func.count()).select_from(Sale).where(Sale.product_id == azur.id)) == 3
    assert current_stock(session, azur.id) == Decimal(9)  # savon's sales had no stock movement (stock unknown)
    assert session.get(Product, azur.id).name_key == "savon azur"
    assert session.get(Product, savon.id) is None


def test_contained_name_with_several_candidates_is_only_listed(session, shop):
    savon = product(session, shop, "savon", sold=1)
    product(session, shop, "Savon Azur", bought=10)
    product(session, shop, "Savon Palmida", bought=10)

    plan = products.plan_cleanup(session, shop.id)
    assert plan.contained == []
    assert [(a.product.name, sorted(c.name for c in a.candidates)) for a in plan.ambiguous] == [
        ("savon", ["Savon Azur", "Savon Palmida"])
    ]

    products.apply_cleanup(session, plan)
    session.commit()
    assert names(session) == ["Savon Azur", "Savon Palmida", "savon"]
    assert session.get(Product, savon.id) is not None


def test_same_name_then_contained(session, shop):
    product(session, shop, "savons", sold=1)
    product(session, shop, "Savon", sold=1)
    azur = product(session, shop, "les savons Azur", bought=4)

    plan = products.plan_cleanup(session, shop.id)
    assert len(plan.same_key) == 1
    assert [(g.keep.id, len(g.duplicates)) for g in plan.contained] == [(azur.id, 1)]

    products.apply_cleanup(session, plan)
    session.commit()
    assert names(session) == ["les savons Azur"]
    assert session.scalar(select(func.count()).select_from(Sale)) == 2


# --- blocked names ---


def test_unused_blocked_products_are_deleted_used_ones_kept(session, shop):
    unused = product(session, shop, "shops", stock_known=False)
    session.add(StockMovement(shop_id=shop.id, product_id=unused.id, kind=MovementKind.ADJUSTMENT, quantity=3, occurred_at=NOW))
    sold = product(session, shop, "boutique", sold=1)
    bought = product(session, shop, "Store", bought=2)
    owed = product(session, shop, "stock", stock_known=False)
    sale = Sale(shop_id=shop.id, product_id=owed.id, quantity=1, unit_price=500, total=500, on_credit=True, occurred_at=NOW)
    session.add(sale)
    session.flush()
    customer = Customer(shop_id=shop.id, name="Marie", name_key="marie")
    session.add(customer)
    session.flush()
    session.add(Debt(shop_id=shop.id, customer_id=customer.id, kind=DebtKind.CREDIT, amount=500, sale_id=sale.id, occurred_at=NOW))
    product(session, shop, "riz", bought=1)
    session.commit()

    plan = products.plan_cleanup(session, shop.id)
    found = {b.product.name: (b.sales, b.purchases, b.debts, b.in_use) for b in plan.blocked}
    assert found == {
        "shops": (0, 0, 0, False),
        "boutique": (1, 0, 0, True),
        "Store": (0, 1, 0, True),
        "stock": (1, 0, 1, True),
    }
    assert plan.same_key == plan.contained == []  # blocked names never take part in merges

    deleted = products.apply_cleanup(session, plan)
    session.commit()

    assert [p.name for p in deleted] == ["shops"]
    assert names(session) == ["Store", "boutique", "riz", "stock"]
    assert session.scalar(select(func.count()).select_from(StockMovement).where(StockMovement.product_id == unused.id)) == 0
    assert session.get(Product, sold.id) is not None and session.get(Product, bought.id) is not None


# --- the script: before / plan / after ---


def test_script_shows_the_plan_and_changes_nothing_without_apply(session, shop, session_factory, capsys):
    product(session, shop, "savon", sold=1)
    product(session, shop, "Savon Azur", bought=10)
    product(session, shop, "shop", stock_known=False)

    assert merge_products.main([PHONE]) == 0

    out = capsys.readouterr().out
    assert "— before:" in out
    assert "merge (contained name) 'savon' -> 'Savon Azur'" in out
    assert "delete                 'shop': not a product name, unused" in out
    assert "Nothing changed. Run again with --apply" in out
    assert names(session) == ["Savon Azur", "savon", "shop"]


def test_script_apply_shows_after(session, shop, session_factory, capsys):
    product(session, shop, "savon", sold=1)
    product(session, shop, "Savon Azur", bought=10, sold=2)
    product(session, shop, "Savon Palmida", bought=5)
    product(session, shop, "magasin", sold=1)

    assert merge_products.main([PHONE, "--apply"]) == 0

    out = capsys.readouterr().out
    before, after = out.split("— after")
    assert "not merged             'savon': could be 'Savon Azur', 'Savon Palmida'" in before
    assert "to check               'magasin': not a product name, but 1 sale(s), 0 purchase(s), 0 debt(s)" in before
    assert "(0 product(s) removed)" in after
    assert "'Savon Azur' (id" in after and "stock 8)" in after
    session.expire_all()
    assert names(session) == ["Savon Azur", "Savon Palmida", "magasin", "savon"]


def test_script_nothing_to_do(session, shop, session_factory, capsys):
    product(session, shop, "riz", bought=1)
    assert merge_products.main([PHONE]) == 0
    assert "Nothing to clean up." in capsys.readouterr().out


def test_script_unknown_shop(session_factory, capsys):
    assert merge_products.main(["237600000000"]) == 1
    assert "No shop for ********0000." in capsys.readouterr().out
