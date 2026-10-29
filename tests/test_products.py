"""Product deduplication, ambiguity questions, forbidden names, merging, and migration 0005's data step."""

import importlib.util
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app import meta_client
from app.models import (
    MovementKind,
    PendingAction,
    PendingKind,
    PendingStatus,
    Product,
    Sale,
    Shop,
    StockMovement,
    current_stock,
)
from app.schemas import ExtractedAction, Extraction
from app.services import ledger, products
from app.services.ledger import LedgerWarning
from app.services.normalize import product_key
from app.services.products import ProductChoice
from tests.test_webhook import _message, _payload

PHONE = "237690000000"
_ids = iter(range(100_000))


def apply(session, *actions: dict, language: str = "fr", message_id: str | None = None):
    extraction = Extraction(actions=[ExtractedAction(**a) for a in actions], language=language)
    return ledger.apply_extraction(session, PHONE, extraction, message_id or f"wamid.{next(_ids)}")


def count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


def names(session) -> list[str]:
    return sorted(session.scalars(select(Product.name)))


# --- product key ---


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("savons", "savon"), ("Savon Azur", "savons azur"), ("3 pièces de savons Azur", "Savon Azur"),
        ("les savons", "savon"), ("l'huile", "huile"), ("du sucre", "sucre"), ("the soaps", "soap"),
        ("Tomatoes", "tomato"), ("boxes", "box"), ("Gâteaux", "gateau"), ("caisses", "caisse"),
        ("pcs de Maggi", "Maggi"), ("unités de lait", "lait"),
    ],
)
def test_same_key(a, b):
    assert product_key(a) == product_key(b)


@pytest.mark.parametrize(("a", "b"), [("savon azur", "savon palmida"), ("riz", "sac riz"), ("lait", "lait concentré")])
def test_different_key(a, b):
    assert product_key(a) != product_key(b)


# --- one product, not three ---


def test_savons_and_savon_azur_are_one_product(session):
    apply(session, {"action": "purchase", "product": "savons", "quantity": 10})
    apply(session, {"action": "sale", "product": "Savon Azur", "quantity": 2, "price": 250})
    apply(session, {"action": "sale", "product": "savons Azur", "quantity": 3, "unit": "pièces", "price": 250})

    assert names(session) == ["Savon Azur"]  # the more precise name is kept
    product = session.scalar(select(Product))
    assert product.name_key == "savon azur"
    assert current_stock(session, product.id) == Decimal(5)
    assert count(session, Sale) == 2


def test_more_precise_name_is_not_taken_when_another_product_has_it(session):
    apply(session, {"action": "purchase", "product": "savon", "quantity": 10})
    product = session.scalar(select(Product))
    other = Product(shop_id=product.shop_id, name="Savon Azur Extra", name_key="savon azur extra")
    session.add(other)
    session.commit()
    products.rename_if_free(session, product, "Savon Azur Extra")
    assert product.name == "savon"


# --- several candidates: ask ---


@pytest.fixture
def two_soaps(session):
    apply(session, {"action": "purchase", "product": "Savon Azur", "quantity": 10})
    apply(session, {"action": "purchase", "product": "Savon Palmida", "quantity": 10})
    return session


def test_ambiguous_name_records_nothing_and_asks(two_soaps):
    session = two_soaps
    before = count(session, StockMovement)
    result = apply(session, {"action": "sale", "product": "savon", "quantity": 3, "price": 250}, message_id="wamid.AMB")

    assert result.choice == ProductChoice(action_index=0, query="savon", options=["Savon Azur", "Savon Palmida"])
    assert result.actions == []
    assert count(session, Sale) == 0
    assert count(session, StockMovement) == before
    assert not ledger.is_processed(session, "wamid.AMB")


def test_option_parsing():
    choice = ProductChoice(0, "savon", ["Savon Azur", "Savon Palmida"])
    assert products.parse_choice("Azur", choice) == "Savon Azur"
    assert products.parse_choice("palmida !", choice) == "Savon Palmida"
    assert products.parse_choice("le savon azur", choice) == "Savon Azur"
    assert products.parse_choice("2", choice) == "Savon Palmida"
    assert products.parse_choice("savon", choice) is None  # still both
    assert products.parse_choice("Lux", choice) is None
    assert products.parse_choice("3", choice) is None


# --- the whole conversation, through the webhook ---


@pytest.fixture
def model(monkeypatch):
    state = SimpleNamespace(extraction=Extraction(), texts=[], known=[])

    def extract(text, known_products=None):
        state.texts.append(text)
        state.known.append(known_products)
        return state.extraction

    monkeypatch.setattr(meta_client, "extract_from_text", extract)
    return state


def send(client, body: str, message_id: str | None = None):
    message = _message("text", id_=message_id or f"wamid.{next(_ids)}", text={"body": body})
    return client.post("/webhook", json=_payload(message))


def returns(model, *actions: dict, language: str = "fr") -> None:
    model.extraction = Extraction(actions=[ExtractedAction(**a) for a in actions], language=language)


def test_ambiguity_question_then_answer(client, sent, session_factory, model):
    returns(model, {"action": "purchase", "product": "Savon Azur", "quantity": 10})
    send(client, "acheté 10 savons Azur")
    returns(model, {"action": "purchase", "product": "Savon Palmida", "quantity": 10})
    send(client, "acheté 10 savons Palmida")

    returns(model, {"action": "sale", "product": "savons", "quantity": 3, "price": 250})
    send(client, "vendu 3 savons à 250", message_id="wamid.SALE")
    assert sent[-1][1] == "🤔 Quel savon : Azur ou Palmida ?"
    with session_factory() as session:
        row = session.scalar(select(PendingAction))
        assert (row.kind, row.status, row.source_message_id) == (PendingKind.CHOOSE_PRODUCT, PendingStatus.PENDING, "wamid.SALE")
        assert count(session, Sale) == 0

    model.texts.clear()
    send(client, "Palmida")

    assert model.texts == []  # answered without the model
    assert sent[-1][1] == "✅ 3 Savons Palmida vendus, 750 FCFA. Reste : 7."
    with session_factory() as session:
        sale = session.scalar(select(Sale))
        assert session.get(Product, sale.product_id).name == "Savon Palmida"
        assert sale.source_message_id == "wamid.SALE"
        assert session.scalar(select(PendingAction.status).where(PendingAction.source_message_id == "wamid.SALE")) == PendingStatus.CONFIRMED

    # Answering again does nothing more.
    send(client, "Palmida")
    with session_factory() as session:
        assert count(session, Sale) == 1


def test_ambiguity_question_in_english_and_cancel(client, sent, session_factory, model):
    returns(model, {"action": "purchase", "product": "Savon Azur", "quantity": 10})
    send(client, "x")
    returns(model, {"action": "purchase", "product": "Savon Palmida", "quantity": 10})
    send(client, "y")
    returns(model, {"action": "sale", "product": "savon", "quantity": 1, "price": 250}, language="en")
    send(client, "sold 1 soap")
    assert sent[-1][1] == "🤔 Which savon: Azur or Palmida?"

    send(client, "cancel")
    assert sent[-1][1] == "❌ Cancelled, nothing was saved."
    with session_factory() as session:
        assert count(session, Sale) == 0


def test_unrelated_message_leaves_the_question_open(client, sent, session_factory, model):
    returns(model, {"action": "purchase", "product": "Savon Azur", "quantity": 10})
    send(client, "x")
    returns(model, {"action": "purchase", "product": "Savon Palmida", "quantity": 10})
    send(client, "y")
    returns(model, {"action": "sale", "product": "savon", "quantity": 1, "price": 250})
    send(client, "vendu 1 savon")

    returns(model, {"action": "purchase", "product": "riz", "quantity": 2, "unit": "sac"})
    send(client, "acheté 2 sacs de riz")
    assert sent[-1][1].startswith("📦 2 sacs de riz ajoutés au stock")
    send(client, "azur")
    assert sent[-1][1].startswith("✅ 1 Savon Azur vendu")


def test_known_products_are_given_to_the_model(client, sent, session_factory, model):
    returns(model, {"action": "purchase", "product": "Savon Azur", "quantity": 10})
    send(client, "x")
    returns(model, {"action": "sale", "product": "Savon Azur", "quantity": 1, "price": 250})
    send(client, "y")
    returns(model, {"action": "purchase", "product": "riz", "quantity": 1})
    send(client, "z")
    assert model.known == [[], ["Savon Azur"], ["Savon Azur"]]


def test_known_products_message_in_the_prompt(monkeypatch):
    calls = []
    monkeypatch.setattr(meta_client, "_meta_completion", lambda retryable, kw: calls.append(kw) or SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"actions": [], "language": "fr"}'))]
    ))
    meta_client.extract_from_text("vendu 3 savons", known_products=["Savon Azur", "Riz"])
    meta_client.extract_from_text("vendu 3 savons")
    assert calls[0]["messages"][1] == {
        "role": "system",
        "content": meta_client.KNOWN_PRODUCTS_PROMPT.format(names="- Savon Azur\n- Riz"),
    }
    assert [m["role"] for m in calls[1]["messages"]] == ["system", "user"]
    assert "shop, boutique, magasin, comptoir" in meta_client.EXTRACTION_PROMPT
    assert "set_stock" in meta_client.EXTRACTION_PROMPT


def test_photo_confirmed_then_ambiguous_asks(session, two_soaps):
    from app.services import pending

    extraction = Extraction(actions=[ExtractedAction(action="sale", product="savon", quantity=2, price=250)])
    row = pending.create(session, PHONE, extraction, "wamid.PHOTO")
    result = pending.confirm(session, row)
    assert result.choice is not None
    row = session.scalar(select(PendingAction))
    assert (row.kind, row.status) == (PendingKind.CHOOSE_PRODUCT, PendingStatus.PENDING)
    result = pending.choose(session, row, "Savon Azur")
    assert result.actions[0].recorded
    assert session.scalar(select(PendingAction.status)) == PendingStatus.CONFIRMED


# --- not products ---


@pytest.mark.parametrize("name", ["shop", "shops", "the shop", "boutique", "Magasins", "store", "stock", "produits", "articles", "choses", "x"])
def test_forbidden_names_create_nothing(session, name):
    result = apply(session, {"action": "sale", "product": name, "quantity": 2, "price": 250})
    assert result.actions[0].warnings == [LedgerWarning.FORBIDDEN_PRODUCT]
    assert not result.actions[0].recorded
    assert count(session, Product) == 0
    assert count(session, Sale) == 0


def test_how_many_soap_remain_for_shop(client, sent, session_factory, model):
    # Even if the model got it wrong, "shop" never becomes a product.
    returns(model, {"action": "stock_check", "product": "shops"}, language="en")
    send(client, "how many soap remain for shop?")
    assert sent[-1][1] == "🤔 \"shops\" isn't a product. Which product do you mean?"
    returns(model, {"action": "stock_check", "product": "soap"}, language="en")
    send(client, "how many soap remain for shop?")
    assert sent[-1][1] == "📦 I don't know \"soap\" yet. Send a purchase to add it."
    with session_factory() as session:
        assert count(session, Product) == 0


def test_stock_check_never_creates_a_product(session):
    result = apply(session, {"action": "stock_check", "product": "huile"})
    assert result.actions[0].warnings == [LedgerWarning.UNKNOWN_PRODUCT]
    assert count(session, Product) == 0


# --- stock unknown, in replies ---


def test_sale_without_known_stock_reply_and_hint_once(client, sent, session_factory, model):
    returns(model, {"action": "sale", "product": "savons", "quantity": 3, "price": 250})
    send(client, "vendu 3 savons")
    assert sent[-1][1] == "✅ 3 savons vendus, 750 FCFA.\n💡 Dis-moi combien tu as de savons en stock pour que je le suive."
    send(client, "vendu 3 savons")
    assert sent[-1][1] == "✅ 3 savons vendus, 750 FCFA."

    returns(model, {"action": "set_stock", "product": "savons", "quantity": 20})
    send(client, "j'ai 20 savons en stock")
    assert sent[-1][1] == "📦 Savons : 20 en stock, c'est noté."
    returns(model, {"action": "sale", "product": "savon", "quantity": 2, "price": 250})
    send(client, "vendu 2 savons")
    assert sent[-1][1] == "✅ 2 savons vendus, 500 FCFA. Reste : 18."
    with session_factory() as session:
        assert session.scalar(select(Product)).stock_known


def test_hint_uses_elision():
    from app.models import Language
    from app.schemas import Action
    from app.services.ledger import ActionResult, LedgerResult
    from app.services.replies import confirmation

    result = ActionResult(action=Action.SALE, recorded=True, product="huile", unit="bidon", quantity=Decimal(1), total=1500,
                          warnings=[LedgerWarning.STOCK_UNKNOWN])
    text = confirmation(LedgerResult(shop_id=1, language=Language.FR, actions=[result]))
    assert text.endswith("Dis-moi combien tu as d'huile en stock pour que je le suive.")


# --- merging duplicates ---


def test_find_and_merge_duplicates(session):
    shop = ledger.get_or_create_shop(session, PHONE, "fr")
    a = Product(shop_id=shop.id, name="savons", name_key="savon", stock_known=True)
    b = Product(shop_id=shop.id, name="Savon", name_key="old-key-savon", sale_price=250)
    c = Product(shop_id=shop.id, name="riz", name_key="riz")
    session.add_all([a, b, c])
    session.flush()
    now = datetime.now(UTC)
    session.add_all([
        StockMovement(shop_id=shop.id, product_id=a.id, kind=MovementKind.PURCHASE, quantity=10, occurred_at=now),
        StockMovement(shop_id=shop.id, product_id=b.id, kind=MovementKind.PURCHASE, quantity=5, occurred_at=now),
        StockMovement(shop_id=shop.id, product_id=b.id, kind=MovementKind.ADJUSTMENT, quantity=-1, occurred_at=now),
        Sale(shop_id=shop.id, product_id=b.id, quantity=2, unit_price=250, total=500, occurred_at=now),
    ])
    session.commit()

    groups = products.find_duplicates(session, shop.id)
    assert [(g.key, g.keep.name, [d.name for d in g.duplicates]) for g in groups] == [("savon", "Savon", ["savons"])]

    for group in groups:
        products.merge(session, group)
    session.commit()

    assert names(session) == ["Savon", "riz"]
    kept = session.scalar(select(Product).where(Product.name == "Savon"))
    assert (kept.name_key, kept.stock_known, kept.sale_price) == ("savon", True, 250)
    assert current_stock(session, kept.id) == Decimal(14)
    assert session.scalar(select(Sale.product_id)) == kept.id


# --- migration 0005, data step ---


def _migration_0005():
    path = Path(__file__).resolve().parent.parent / "alembic" / "versions" / "0005_product_keys_and_known_stock.py"
    spec = importlib.util.spec_from_file_location("migration_0005", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_0005_on_existing_data(session):
    shop = Shop(name="x", owner_phone=PHONE)
    session.add(shop)
    session.flush()
    now = datetime.now(UTC)
    bought = Product(shop_id=shop.id, name="riz", name_key="riz")
    only_sold = Product(shop_id=shop.id, name="Savons Azur", name_key="savon azur")
    old_key = Product(shop_id=shop.id, name="3 pièces de Maggi", name_key="3 piece de maggi")
    clash = Product(shop_id=shop.id, name="les savons Azur", name_key="le savon azur")  # same new key as only_sold
    session.add_all([bought, only_sold, old_key, clash])
    session.flush()
    session.add_all([
        StockMovement(shop_id=shop.id, product_id=bought.id, kind=MovementKind.PURCHASE, quantity=5, occurred_at=now),
        StockMovement(shop_id=shop.id, product_id=bought.id, kind=MovementKind.SALE, quantity=-7, occurred_at=now),
        StockMovement(shop_id=shop.id, product_id=only_sold.id, kind=MovementKind.SALE, quantity=-3, occurred_at=now),
        StockMovement(shop_id=shop.id, product_id=old_key.id, kind=MovementKind.SALE, quantity=-2, occurred_at=now),
    ])
    session.commit()

    _migration_0005().migrate_data(session.connection())
    session.commit()
    session.expire_all()

    assert session.get(Product, bought.id).stock_known
    assert current_stock(session, bought.id) == Decimal(-2)  # known stock: left as is
    for product in (only_sold, old_key):
        assert not session.get(Product, product.id).stock_known
        assert current_stock(session, product.id) == 0  # was negative, brought back to 0
    assert session.get(Product, old_key.id).name_key == "maggi"
    assert session.get(Product, clash.id).name_key == "le savon azur"  # key taken: left for merge_products
    adjustments = session.scalars(select(StockMovement).where(StockMovement.kind == MovementKind.ADJUSTMENT)).all()
    assert sorted(m.quantity for m in adjustments) == [Decimal(2), Decimal(3)]


def test_photo_recap_with_a_stock_count():
    from app.models import Language
    from app.services.replies import pending_summary

    extraction = Extraction(actions=[ExtractedAction(action="set_stock", product="Savon Azur", quantity=20)])
    assert pending_summary(extraction, Language.FR) == (
        "🧾 Voici ce que j'ai lu :\n• En stock : Savon Azur, 20\nJe l'enregistre ? Réponds OUI ou NON"
    )


def test_options_are_listed_by_product_key(session):
    apply(session, {"action": "purchase", "product": "Savon Palmida", "quantity": 1})
    apply(session, {"action": "purchase", "product": "Savons Azur", "quantity": 1})
    result = apply(session, {"action": "sale", "product": "savon", "quantity": 1})
    assert result.choice.options == ["Savons Azur", "Savon Palmida"]
