"""Text pipeline end to end: webhook -> extraction (mocked) -> ledger (SQLite) -> reply."""

import pytest
from sqlalchemy import func, select

from app import meta_client
from app.models import Language, Sale, Shop
from app.schemas import ExtractedAction, Extraction
from app.whatsapp.dedup import seen_messages
from tests.test_webhook import _message, _payload

PHONE = "237690000000"  # sender used by tests.test_webhook._message


@pytest.fixture
def model(monkeypatch):
    """Set what the (mocked) model returns: an Extraction, or an exception to raise."""
    state = {"outcome": Extraction(), "texts": []}

    def fake_extract(text):
        state["texts"].append(text)
        if isinstance(state["outcome"], Exception):
            raise state["outcome"]
        return state["outcome"]

    monkeypatch.setattr(meta_client, "extract_from_text", fake_extract)

    def returns(*actions, language="fr"):
        state["outcome"] = Extraction(actions=[ExtractedAction(**a) for a in actions], language=language)

    def raises(error):
        state["outcome"] = error

    return returns, raises, state


def send(client, body: str, message_id: str = "wamid.TEST"):
    return client.post("/webhook", json=_payload(_message("text", id_=message_id, text={"body": body})))


def test_sale_message_is_recorded_and_confirmed(client, sent, session_factory, model):
    returns, _, state = model
    returns({"action": "sale", "product": "riz", "quantity": 3, "unit": "sac", "total_price": 25000})

    assert send(client, "j'ai vendu 3 sacs de riz pour 25 000").status_code == 200

    assert state["texts"] == ["j'ai vendu 3 sacs de riz pour 25 000"]
    assert sent == [(PHONE, "✅ 3 sacs de riz vendus, 25 000 FCFA. Reste : -3.\n"
                            "⚠️ Le stock de riz est à -3. Pas grave : pense à noter tes achats pour le corriger.")]
    with session_factory() as session:
        assert session.scalar(select(Sale)).total == 25000


def test_reply_language_follows_the_message(client, sent, session_factory, model):
    returns, _, _ = model
    returns({"action": "purchase", "product": "milk", "quantity": 4, "unit": "tin"}, language="pcm")
    send(client, "I buy 4 tin milk")
    assert sent[-1][1] == "📦 4 tin milk don enter stock. Stock: 4."

    returns({"action": "stock_check", "product": "milk"}, language="en")
    send(client, "how many milk remain?", message_id="wamid.2")
    assert sent[-1][1] == "📦 Milk: 4 tins in stock."


def test_unknown_message_asks_to_rephrase(client, sent, session_factory, model):
    returns, _, _ = model
    returns({"action": "unknown"})
    send(client, "bonjour")
    assert sent[0][1].startswith("🤔 Je n'ai pas compris. Écris par exemple")


def test_model_error_sends_apology(client, sent, session_factory, model, caplog):
    _, raises, _ = model
    raises(meta_client.ExtractionError("InternalServerError: unavailable"))

    assert send(client, "vendu 3 savons").status_code == 200

    assert sent == [(PHONE, "Je n'ai pas pu lire ton message, réessaie dans un instant.")]
    assert "Extraction failed" in caplog.text


def test_apology_uses_known_shop_language(client, sent, session_factory, model):
    returns, raises, _ = model
    returns({"action": "stock_check", "product": "soap"}, language="en")
    send(client, "how many soap?", message_id="wamid.1")
    raises(meta_client.ExtractionError("timeout"))
    send(client, "sold 2 soap", message_id="wamid.2")
    assert sent[-1][1] == "I couldn't read your message, please try again in a moment."


def test_database_error_sends_apology(client, sent, session_factory, model, monkeypatch):
    returns, _, _ = model
    returns({"action": "sale", "product": "savon", "quantity": 1, "price": 250})

    def boom(*args, **kwargs):
        raise RuntimeError("database is down")

    monkeypatch.setattr("app.services.ledger.apply_extraction", boom)
    send(client, "vendu 1 savon")
    assert sent == [(PHONE, "Je n'ai pas pu lire ton message, réessaie dans un instant.")]


def test_redelivered_message_is_not_recorded_or_answered_twice(client, sent, session_factory, model):
    returns, _, _ = model
    returns({"action": "sale", "product": "savon", "quantity": 1, "price": 250})
    send(client, "vendu 1 savon", message_id="wamid.SAME")
    seen_messages.clear()  # e.g. the API restarted between the two deliveries
    send(client, "vendu 1 savon", message_id="wamid.SAME")

    assert len(sent) == 1
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Sale)) == 1


def test_first_message_creates_the_shop(client, sent, session_factory, model):
    returns, _, _ = model
    returns({"action": "stock_check", "product": "savon"}, language="pcm")
    send(client, "how many soap remain?")
    with session_factory() as session:
        shop = session.scalar(select(Shop))
    assert shop.owner_phone == PHONE
    assert shop.language == Language.PIDGIN
