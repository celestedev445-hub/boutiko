from decimal import Decimal

import pytest

from app.models import Language
from app.schemas import Action
from app.services.ledger import ActionResult, LedgerResult, LedgerWarning
from app.services.replies import apology, confirmation, format_amount, format_quantity

FR, EN, PCM = Language.FR, Language.EN, Language.PIDGIN


def reply(language: Language, *actions: ActionResult) -> str:
    return confirmation(LedgerResult(shop_id=1, language=language, actions=list(actions)))


def sale(**overrides) -> ActionResult:
    values = dict(
        action=Action.SALE, recorded=True, product="riz", unit="sac", quantity=Decimal(3), total=25000, stock=Decimal(12)
    )
    return ActionResult(**{**values, **overrides})


def test_format_amount():
    assert format_amount(25000) == "25 000 FCFA"
    assert format_amount(4500) == "4 500 FCFA"
    assert format_amount(250) == "250 FCFA"
    assert format_amount(1250000) == "1 250 000 FCFA"


def test_format_quantity():
    assert format_quantity(Decimal("3.000"), FR) == "3"
    assert format_quantity(Decimal("1.500"), FR) == "1,5"
    assert format_quantity(Decimal("1.500"), EN) == "1.5"
    assert format_quantity(Decimal("20"), EN) == "20"


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        (FR, "✅ 3 sacs de riz vendus, 25 000 FCFA. Reste : 12."),
        (EN, "✅ 3 sacs of riz sold, 25 000 FCFA. Left: 12."),
        (PCM, "✅ 3 sac riz don sell, 25 000 FCFA. Remain: 12."),
    ],
)
def test_sale_reply_in_each_language(language, expected):
    assert reply(language, sale()) == expected


def test_sale_without_unit_pluralizes_product():
    assert reply(FR, sale(product="savon", unit=None, total=750)).startswith("✅ 3 savons vendus, 750 FCFA.")


def test_units_and_plurals():
    half = dict(product="sucre", unit="kg", quantity=Decimal("1.5"), total=1500)
    assert reply(FR, sale(**half)).startswith("✅ 1,5 kg de sucre vendu, 1 500 FCFA.")
    assert reply(EN, sale(**half)).startswith("✅ 1.5 kg of sucre sold")
    assert reply(EN, sale(unit="bag", quantity=Decimal("1.5"))).startswith("✅ 1.5 bags of riz")
    assert reply(FR, sale(unit="sac", quantity=Decimal(1))).startswith("✅ 1 sac de riz vendu,")
    assert reply(FR, sale(product="savon", unit=None, quantity=Decimal(2), total=500)).startswith("✅ 2 savons vendus")


def test_credit_sale_reply_shows_balance():
    text = reply(
        FR,
        ActionResult(
            action=Action.CREDIT_SALE, recorded=True, product="sucre", unit="kg", quantity=Decimal(2),
            total=1500, stock=Decimal(8), customer="Marie", customer_balance=4500,
        ),
    )
    assert "à crédit pour Marie, 1 500 FCFA" in text
    assert "📝 Marie doit maintenant 4 500 FCFA." in text


def test_payment_reply():
    payment = ActionResult(action=Action.DEBT_PAYMENT, recorded=True, total=2000, customer="Marie", customer_balance=2500)
    assert reply(FR, payment) == "💵 Marie a payé 2 000 FCFA.\n📝 Marie doit maintenant 2 500 FCFA."
    assert reply(EN, payment) == "💵 Marie paid 2 000 FCFA.\n📝 Marie now owes 2 500 FCFA."
    assert reply(PCM, payment) == "💵 Marie don pay 2 000 FCFA.\n📝 Marie dey owe 2 500 FCFA now."


def test_payment_clearing_debt():
    payment = ActionResult(action=Action.DEBT_PAYMENT, recorded=True, total=2000, customer="Marie", customer_balance=0)
    assert "Marie ne doit plus rien" in reply(FR, payment)


def test_stock_check_reply():
    check = ActionResult(action=Action.STOCK_CHECK, product="savon", stock=Decimal(18))
    assert reply(FR, check) == "📦 Savon : 18 en stock."
    assert reply(EN, check) == "📦 Savon: 18 in stock."
    assert reply(PCM, check) == "📦 Savon: 18 dey for stock."


def test_purchase_reply():
    purchase = ActionResult(action=Action.PURCHASE, recorded=True, product="riz", unit="sac", quantity=Decimal(10), stock=Decimal(30))
    assert reply(FR, purchase) == "📦 10 sacs de riz ajoutés au stock. Stock : 30."


def test_negative_stock_is_mentioned_kindly():
    text = reply(FR, sale(stock=Decimal(-2), warnings=[LedgerWarning.NEGATIVE_STOCK]))
    assert text.startswith("✅ 3 sacs de riz vendus")
    assert "Le stock de riz est à -2. Pas grave" in text


def test_missing_price_is_mentioned():
    text = reply(FR, sale(total=None, warnings=[LedgerWarning.MISSING_PRICE]))
    assert text.startswith("✅ 3 sacs de riz vendus. Reste : 12.")
    assert "Je n'ai pas le prix de riz" in text


@pytest.mark.parametrize("language", [FR, EN, PCM])
def test_empty_or_unknown_asks_to_rephrase_with_example(language):
    for result in ([], [ActionResult(action=Action.UNKNOWN, warnings=[LedgerWarning.NOT_UNDERSTOOD])]):
        text = reply(language, *result)
        assert text.startswith("🤔")
        assert "3" in text  # the example


def test_partly_understood():
    text = reply(FR, sale(), ActionResult(action=Action.UNKNOWN, warnings=[LedgerWarning.NOT_UNDERSTOOD]))
    assert text.startswith("✅")
    assert text.endswith("Je n'ai pas compris une partie du message.")


def test_apology_in_each_language():
    assert apology() == "Je n'ai pas pu lire ton message, réessaie dans un instant."
    assert apology(EN).startswith("I couldn't read your message")
    assert apology(PCM).startswith("I no fit read your message")
