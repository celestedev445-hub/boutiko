"""Clean up a shop's products: merge duplicates, remove names that aren't products.

1. Same name: same product key once case, accents, simple plurals, articles
   and generic units are ignored ("savons" and "Savon").
2. Contained name: a name whose words are all in exactly ONE other product's
   name ("savon" -> "Savon Azur") is merged into that more precise product.
   With several candidates ("savon": Savon Azur, Savon Palmida) it is only
   listed: the merchant will be asked when they use it.
3. Blocked names (shop, boutique, store, stock...): deleted if they have no
   sale, no purchase and no debt; otherwise listed "to check".

A merge moves sales and stock movements to the kept product (debts follow
their sales) and adds the stocks up. Everything runs in one transaction, and
nothing is changed without --apply. Not run by pytest: it uses DATABASE_URL.

    python scripts/merge_products.py 237690001234            # show the plan
    python scripts/merge_products.py 237690001234 --apply    # apply it

With Docker:

    docker compose exec api python scripts/merge_products.py 237690001234
"""

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("wa_id", help="the merchant's WhatsApp number, e.g. 237690001234")
    parser.add_argument("--apply", action="store_true", help="apply the plan (default: only show it)")
    args = parser.parse_args(argv)

    os.chdir(ROOT)  # so Settings finds .env
    sys.path.insert(0, str(ROOT))
    from sqlalchemy import select

    from app import db
    from app.logging_config import mask_phone
    from app.models import Language, Product, Shop, current_stock
    from app.services import products
    from app.services.replies import format_quantity

    def stock(session, product) -> str:
        if not product.stock_known:
            return "stock not followed"
        return f"stock {format_quantity(current_stock(session, product.id), Language.EN)}"

    def show(session, product) -> str:
        return f"{product.name!r} (id {product.id}, {stock(session, product)})"

    with db.SessionLocal() as session:
        shop = session.scalar(select(Shop).where(Shop.owner_phone == args.wa_id))
        if shop is None:
            print(f"No shop for {mask_phone(args.wa_id)}.")
            return 1

        print(f"Shop {shop.id} — before:")
        for product in session.scalars(select(Product).where(Product.shop_id == shop.id).order_by(Product.name)):
            print(f"  {show(session, product)}")

        plan = products.plan_cleanup(session, shop.id)
        if plan.empty:
            print("\nNothing to clean up.")
            return 0

        print("\nPlan:")
        for group in plan.same_key:
            names = ", ".join(repr(p.name) for p in group.duplicates)
            print(f"  merge (same name)      {names} -> {group.keep.name!r}")
        for group in plan.contained:
            names = ", ".join(repr(p.name) for p in group.duplicates)
            print(f"  merge (contained name) {names} -> {group.keep.name!r}")
        for item in plan.ambiguous:
            names = ", ".join(repr(p.name) for p in item.candidates)
            print(f"  not merged             {item.product.name!r}: could be {names}")
        for blocked in plan.blocked:
            if blocked.in_use:
                print(f"  to check               {blocked.product.name!r}: not a product name, but "
                      f"{blocked.sales} sale(s), {blocked.purchases} purchase(s), {blocked.debts} debt(s)")
            else:
                print(f"  delete                 {blocked.product.name!r}: not a product name, unused")

        if not args.apply:
            print("\nNothing changed. Run again with --apply to apply the plan.")
            return 0
        try:
            deleted = products.apply_cleanup(session, plan)
            session.commit()
        except Exception:
            session.rollback()
            print("\nCleanup failed, nothing was changed.")
            raise

        print(f"\nShop {shop.id} — after ({len(deleted)} product(s) removed):")
        for product in session.scalars(select(Product).where(Product.shop_id == shop.id).order_by(Product.name)):
            print(f"  {show(session, product)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
