"""Finding the product a merchant means, without creating duplicates.

"savons", "Savon Azur" and "3 pièces de savons Azur" are one product. When a
name could be several products ("savon" with both "Savon Azur" and "Savon
Palmida"), Boutiko asks instead of guessing.
"""

import enum
import logging
from dataclasses import dataclass, field

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models import Product, Sale, Shop, StockMovement
from app.services.normalize import is_forbidden_product_key, product_key

logger = logging.getLogger(__name__)

MAX_KNOWN_NAMES = 50


class Match(str, enum.Enum):
    FOUND = "found"
    NEW = "new"  # no such product yet
    AMBIGUOUS = "ambiguous"  # several products could be meant
    FORBIDDEN = "forbidden"  # "shop", "stock"...: not a product


@dataclass
class Resolution:
    match: Match
    product: Product | None = None
    options: list[Product] = field(default_factory=list)  # when AMBIGUOUS
    more_precise_name: str | None = None  # "Savon Azur" said for the product "savon"


@dataclass
class ProductChoice:
    """Question to ask before recording: which product did the merchant mean?"""

    action_index: int  # in the extraction's actions
    query: str  # what the merchant wrote, e.g. "savon"
    options: list[str]  # product names, e.g. ["Savon Azur", "Savon Palmida"]


def resolve(session: Session, shop: Shop, name: str) -> Resolution:
    key = product_key(name)
    if is_forbidden_product_key(key):
        return Resolution(Match.FORBIDDEN)
    products = list(session.scalars(select(Product).where(Product.shop_id == shop.id).order_by(Product.id)))
    keys = {p.id: product_key(p.name) for p in products}  # recomputed: older rows may have older keys

    exact = [p for p in products if keys[p.id] == key]
    if exact:
        return Resolution(Match.FOUND, product=exact[0])

    words = set(key.split())
    candidates = [p for p in products if (other := set(keys[p.id].split())) and (other <= words or words <= other)]
    if len(candidates) == 1:
        product = candidates[0]
        more_precise = name.strip() if len(words) > len(keys[product.id].split()) else None
        return Resolution(Match.FOUND, product=product, more_precise_name=more_precise)
    if len(candidates) > 1:
        return Resolution(Match.AMBIGUOUS, options=sorted(candidates, key=lambda p: (keys[p.id], p.id)))
    return Resolution(Match.NEW)


def rename_if_free(session: Session, product: Product, name: str) -> None:
    """Show the more precise name ("Savon Azur" rather than "savon"), unless another product has it."""
    key = product_key(name)
    taken = session.scalar(
        select(Product.id).where(Product.shop_id == product.shop_id, Product.name_key == key, Product.id != product.id)
    )
    if taken is None:
        product.name = name
        product.name_key = key


def option_labels(query: str, options: list[str]) -> list[str]:
    """What tells the options apart: "savon" with ["Savon Azur", "Savon Palmida"] -> ["Azur", "Palmida"]."""
    query_words = set(product_key(query).split())
    labels = []
    for option in options:
        rest = [word for word in option.split() if product_key(word) not in query_words]
        labels.append(" ".join(rest) if rest else option)
    if len(set(labels)) < len(labels):  # can't tell them apart by the extra words
        return list(options)
    return labels


def query_label(query: str, options: list[str]) -> str:
    """The word to ask about, as the products spell it: "savons" -> "savon"."""
    query_words = set(product_key(query).split())
    common = [word for word in options[0].split() if product_key(word) in query_words]
    return " ".join(common).lower() if common else query.strip()


def parse_choice(text: str, choice: ProductChoice) -> str | None:
    """The option the merchant picked ("Azur", "palmida", "2", "le savon azur"), or None."""
    key = product_key(text)
    if key.isdigit() or text.strip().isdigit():
        index = int(text.strip()) - 1
        return choice.options[index] if 0 <= index < len(choice.options) else None
    words = set(key.split())
    if not words:
        return None
    labels = option_labels(choice.query, choice.options)
    matches = [
        option for option, label in zip(choice.options, labels)
        if words <= set(product_key(option).split()) and words & set(product_key(label).split())
    ]
    return matches[0] if len(matches) == 1 else None


def known_names(session: Session, shop_id: int, limit: int = MAX_KNOWN_NAMES) -> list[str]:
    """The shop's product names, most used first, for the extraction prompt."""
    movements = (
        select(StockMovement.product_id, func.count().label("n")).group_by(StockMovement.product_id).subquery()
    )
    sales = select(Sale.product_id, func.count().label("n")).group_by(Sale.product_id).subquery()
    usage = func.coalesce(movements.c.n, 0) + func.coalesce(sales.c.n, 0)
    rows = session.execute(
        select(Product.name)
        .outerjoin(movements, movements.c.product_id == Product.id)
        .outerjoin(sales, sales.c.product_id == Product.id)
        .where(Product.shop_id == shop_id)
        .order_by(usage.desc(), Product.name)
        .limit(limit)
    )
    return [name for (name,) in rows]


@dataclass
class DuplicateGroup:
    key: str
    keep: Product
    duplicates: list[Product]


def find_duplicates(session: Session, shop_id: int) -> list[DuplicateGroup]:
    """Products of the shop that share the same product_key."""
    groups: dict[str, list[Product]] = {}
    for product in session.scalars(select(Product).where(Product.shop_id == shop_id).order_by(Product.id)):
        groups.setdefault(product_key(product.name), []).append(product)

    def usage(product: Product) -> int:
        return (
            session.scalar(select(func.count()).select_from(StockMovement).where(StockMovement.product_id == product.id))
            + session.scalar(select(func.count()).select_from(Sale).where(Sale.product_id == product.id))
        )

    result = []
    for key, products in sorted(groups.items()):
        if len(products) > 1:
            ranked = sorted(products, key=lambda p: (-usage(p), p.id))
            result.append(DuplicateGroup(key=key, keep=ranked[0], duplicates=ranked[1:]))
    return result


def merge(session: Session, group: DuplicateGroup) -> None:
    """Move the duplicates' sales and stock movements to the kept product, then delete them.

    Debts point to sales, so they follow. Stock is the sum of movements, so it adds up.
    The caller commits (or rolls back).
    """
    keep = group.keep
    ids = [p.id for p in group.duplicates]
    session.execute(update(Sale).where(Sale.product_id.in_(ids)).values(product_id=keep.id))
    session.execute(update(StockMovement).where(StockMovement.product_id.in_(ids)).values(product_id=keep.id))
    for duplicate in group.duplicates:
        keep.stock_known = keep.stock_known or duplicate.stock_known
        keep.stock_hint_sent = keep.stock_hint_sent or duplicate.stock_hint_sent
        if keep.sale_price is None:
            keep.sale_price = duplicate.sale_price
        session.delete(duplicate)
    session.flush()
    keep.name_key = group.key
    session.flush()
    logger.info("Product %s: merged %d duplicate(s)", keep.id, len(ids))
