"""List (and merge) duplicate products of a shop.

Two products are duplicates when they have the same product key: same name
once case, accents, simple plurals, articles and generic units are ignored
("savons" and "Savon", "les savons Azur" and "Savon Azur"). The most used one
is kept; the others' sales and stock movements move to it (debts follow their
sales), their stocks add up, and they are deleted. All in one transaction.

Nothing is changed without --apply. Not run by pytest: it uses DATABASE_URL.

    python scripts/merge_products.py 237690001234            # list duplicates
    python scripts/merge_products.py 237690001234 --apply    # merge them

With Docker:

    docker compose exec api python scripts/merge_products.py 237690001234
"""

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("wa_id", help="the merchant's WhatsApp number, e.g. 237690001234")
    parser.add_argument("--apply", action="store_true", help="merge the duplicates (default: only list them)")
    args = parser.parse_args()

    os.chdir(ROOT)  # so Settings finds .env
    sys.path.insert(0, str(ROOT))
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.logging_config import mask_phone
    from app.models import Language, Shop, current_stock
    from app.services import products
    from app.services.replies import format_quantity

    with SessionLocal() as session:
        shop = session.scalar(select(Shop).where(Shop.owner_phone == args.wa_id))
        if shop is None:
            print(f"No shop for {mask_phone(args.wa_id)}.")
            return 1
        groups = products.find_duplicates(session, shop.id)
        if not groups:
            print(f"Shop {shop.id}: no duplicate products.")
            return 0

        print(f"Shop {shop.id}: {len(groups)} group(s) of duplicates")
        for group in groups:
            def describe(product) -> str:
                return f"{product.name!r} (id {product.id}, stock {format_quantity(current_stock(session, product.id), Language.EN)})"

            print(f"  key {group.key!r}: keep {describe(group.keep)}")
            for duplicate in group.duplicates:
                print(f"      merge {describe(duplicate)}")

        if not args.apply:
            print("\nNothing changed. Run again with --apply to merge.")
            return 0
        try:
            for group in groups:
                products.merge(session, group)
            session.commit()
        except Exception:
            session.rollback()
            print("Merge failed, nothing was changed.")
            raise
        for group in groups:
            print(f"  {group.keep.name!r}: stock now {format_quantity(current_stock(session, group.keep.id), Language.EN)}")
        print("Merged.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
