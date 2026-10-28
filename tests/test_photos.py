"""Photos end to end, all mocked: Graph media download (httpx MockTransport), the
vision model (fake OpenAI client), text extraction (fake), ledger on SQLite."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import openai
import pytest
from sqlalchemy import func, select, update

from app import meta_client
from app.config import get_settings
from app.models import PendingAction, PendingStatus, ProcessedMessage, Product, Sale, StockMovement, current_stock
from app.schemas import Extraction, VisionExtraction, strict_json_schema
from app.services import pending
from app.services.pending import Decision, parse_decision
from app.whatsapp import media
from app.whatsapp.dedup import seen_messages
from tests.test_webhook import _message, _payload

PHONE = "237690000000"
OTHER_PHONE = "237677000000"
MEDIA_ID = "MEDIA_PHOTO_1"
MEDIA_URL = "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=MEDIA_PHOTO_1"
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 200
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200

INVOICE = [
    {"action": "purchase", "product": "lait concentré", "quantity": 10, "unit": "carton", "price": 18500},
    {"action": "purchase", "product": "riz", "quantity": 5, "unit": "sac", "total_price": 115000},
]
INVOICE_SUMMARY = (
    "🧾 Voici ce que j'ai lu :\n"
    "• Achat : 10 cartons de lait concentré — 185 000 FCFA\n"
    "• Achat : 5 sacs de riz — 115 000 FCFA\n"
    "Total : 300 000 FCFA\n"
    "Je l'enregistre ? Réponds OUI ou NON"
)


def reading(kind: str, actions: list[dict], clarification: str = "", language: str | None = None) -> str:
    """Model output in strict mode: every field present."""
    full = [
        {"product": None, "quantity": None, "unit": None, "price": None, "total_price": None, "customer": None, **a}
        for a in actions
    ]
    return json.dumps({"actions": full, "language": language, "image_kind": kind, "needs_clarification": clarification})


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "test-wa-token")
    monkeypatch.setenv("MODEL_API_KEY", "test-model-key")
    get_settings.cache_clear()


@pytest.fixture
def graph(monkeypatch, env):
    """Graph media endpoints. state["file_size"] / state["content"] can be changed by a test."""
    state = {"file_size": len(JPEG), "content": JPEG, "requests": []}

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        if request.url.host == "graph.facebook.com":
            return httpx.Response(200, json={"url": MEDIA_URL, "mime_type": "image/jpeg", "file_size": state["file_size"], "id": MEDIA_ID})
        return httpx.Response(200, content=state["content"])

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(media, "_http_client", lambda: httpx.Client(transport=transport))
    return state


@pytest.fixture
def vision_model(monkeypatch):
    """Fake Meta client for the vision call. state["outcomes"]: model outputs (JSON strings) or exceptions."""
    state = {"outcomes": [], "calls": []}

    def create(**kwargs):
        state["calls"].append(kwargs)
        outcome = state["outcomes"].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=outcome))])

    fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(meta_client, "get_client", lambda: fake)
    return state


@pytest.fixture
def text_model(monkeypatch):
    """Text extraction: records the texts it was asked about; returns an 'unknown' action."""
    state = {"texts": [], "extraction": Extraction.model_validate({"actions": [{"action": "unknown"}], "language": "fr"})}

    def fake_extract(text):
        state["texts"].append(text)
        return state["extraction"]

    monkeypatch.setattr(meta_client, "extract_from_text", fake_extract)
    return state


@pytest.fixture
def setup(client, sent, session_factory, graph, vision_model, text_model):
    return SimpleNamespace(client=client, sent=sent, db=session_factory, graph=graph, vision=vision_model, text=text_model)


def send_photo(t, *, caption: str | None = None, mime: str = "image/jpeg", message_id: str = "wamid.PHOTO", sender: str = PHONE):
    image = {"id": MEDIA_ID, "mime_type": mime, "sha256": "abc"}
    if caption is not None:
        image["caption"] = caption
    message = _message("image", id_=message_id, image=image)
    message["from"] = sender
    return t.client.post("/webhook", json=_payload(message))


def send_text(t, body: str, message_id: str, sender: str = PHONE):
    message = _message("text", id_=message_id, text={"body": body})
    message["from"] = sender
    return t.client.post("/webhook", json=_payload(message))


def count(t, model) -> int:
    with t.db() as session:
        return session.scalar(select(func.count()).select_from(model))


def pending_rows(t) -> list[PendingAction]:
    with t.db() as session:
        return list(session.scalars(select(PendingAction).order_by(PendingAction.id)))


def last_reply(t) -> str:
    return t.sent[-1][1]


# --- invoice -> pending -> YES / NO ---


def test_invoice_creates_pending_and_writes_nothing_to_the_ledger(setup):
    t = setup
    t.vision["outcomes"] = [reading("invoice", INVOICE)]

    assert send_photo(t).status_code == 200

    assert t.sent == [(PHONE, INVOICE_SUMMARY)]
    rows = pending_rows(t)
    assert len(rows) == 1
    row = rows[0]
    assert (row.wa_id, row.source_message_id, row.status) == (PHONE, "wamid.PHOTO", PendingStatus.PENDING)
    assert row.expires_at - row.created_at == timedelta(minutes=30)
    assert [a["product"] for a in row.payload["actions"]] == ["lait concentré", "riz"]
    assert count(t, StockMovement) == 0
    assert count(t, Sale) == 0
    assert count(t, ProcessedMessage) == 0
    assert count(t, Product) == 0


def test_vision_request(setup):
    t = setup
    t.vision["outcomes"] = [reading("product", [], clarification="Tu as vendu ou acheté ce produit ?")]
    send_photo(t, caption="vendu 3 à 500", mime="image/jpeg")

    call = t.vision["calls"][0]
    assert call["model"] == "muse-spark-1.3-contributor"
    assert call["reasoning_effort"] == "low"
    schema = call["response_format"]["json_schema"]
    assert call["response_format"]["type"] == "json_schema"
    assert schema["strict"] is True
    assert schema["schema"] == strict_json_schema(VisionExtraction)
    assert schema["schema"]["required"] == ["actions", "language", "image_kind", "needs_clarification"]
    assert schema["schema"]["additionalProperties"] is False
    assert schema["schema"]["properties"]["image_kind"]["enum"] == ["invoice", "receipt", "product", "handwritten_note", "other"]
    text, image = call["messages"][1]["content"]
    assert text["type"] == "text"
    assert "Caption: vendu 3 à 500" in text["text"]
    assert image["type"] == "image_url"
    assert image["image_url"]["url"].startswith("data:image/jpeg;base64,/9j/")


def test_model_vision_is_configurable(setup, monkeypatch):
    t = setup
    monkeypatch.setenv("MODEL_VISION", "muse-spark-vision-test")
    get_settings.cache_clear()
    t.vision["outcomes"] = [reading("other", [])]
    send_photo(t)
    assert t.vision["calls"][0]["model"] == "muse-spark-vision-test"


def test_yes_applies_the_actions_once(setup):
    t = setup
    t.vision["outcomes"] = [reading("invoice", INVOICE)]
    send_photo(t)

    send_text(t, "OUI", message_id="wamid.YES1")

    assert t.text["texts"] == []  # no model call for a YES
    assert last_reply(t) == (
        "📦 10 cartons de lait concentré ajoutés au stock. Stock : 10.\n"
        "📦 5 sacs de riz ajoutés au stock. Stock : 5."
    )
    assert pending_rows(t)[0].status == PendingStatus.CONFIRMED
    with t.db() as session:
        products = {p.name: p for p in session.scalars(select(Product))}
        assert current_stock(session, products["riz"].id) == Decimal(5)
        assert current_stock(session, products["lait concentré"].id) == Decimal(10)
        costs = sorted(session.scalars(select(StockMovement.unit_cost)))
        assert costs == [18500, 23000]
        # recorded under the photo's message id
        assert set(session.scalars(select(StockMovement.source_message_id))) == {"wamid.PHOTO"}

    # A second YES finds nothing pending: it is a normal message, nothing more is recorded.
    send_text(t, "oui", message_id="wamid.YES2")
    assert t.text["texts"] == ["oui"]
    assert count(t, StockMovement) == 2


def test_yes_is_never_applied_twice_even_if_reopened(setup):
    t = setup
    t.vision["outcomes"] = [reading("invoice", INVOICE)]
    send_photo(t)
    send_text(t, "yes", message_id="wamid.YES1")
    with t.db() as session:  # simulate a bug or race putting it back to pending
        session.execute(update(PendingAction).values(status=PendingStatus.PENDING))
        session.commit()

    send_text(t, "yes", message_id="wamid.YES2")

    assert last_reply(t) == "✅ C'est déjà enregistré."
    assert count(t, StockMovement) == 2
    assert pending_rows(t)[0].status == PendingStatus.CONFIRMED


def test_no_cancels_and_writes_nothing(setup):
    t = setup
    t.vision["outcomes"] = [reading("invoice", INVOICE)]
    send_photo(t)

    send_text(t, "Non", message_id="wamid.NO")

    assert last_reply(t) == "❌ Annulé, je n'ai rien enregistré."
    assert t.text["texts"] == []
    assert pending_rows(t)[0].status == PendingStatus.CANCELLED
    assert count(t, StockMovement) == 0
    # and a YES afterwards doesn't bring it back
    send_text(t, "oui", message_id="wamid.YES")
    assert count(t, StockMovement) == 0


def test_expired_pending_and_yes_is_a_normal_message(setup):
    t = setup
    t.vision["outcomes"] = [reading("invoice", INVOICE)]
    send_photo(t)
    with t.db() as session:
        past = datetime.now(UTC) - timedelta(minutes=1)
        session.execute(update(PendingAction).values(created_at=past - timedelta(minutes=30), expires_at=past))
        session.commit()

    send_text(t, "oui", message_id="wamid.YES")

    assert t.text["texts"] == ["oui"]  # went through the model like any message
    assert last_reply(t).startswith("🤔")
    assert pending_rows(t)[0].status == PendingStatus.EXPIRED
    assert count(t, StockMovement) == 0


def test_latest_pending_is_the_one_answered(setup):
    t = setup
    t.vision["outcomes"] = [
        reading("invoice", INVOICE),
        reading("product", [{"action": "sale", "product": "Savon Azur", "quantity": 3, "price": 500}], language="fr"),
    ]
    send_photo(t, message_id="wamid.P1")
    send_photo(t, caption="vendu 3 à 500", message_id="wamid.P2")

    send_text(t, "ok", message_id="wamid.YES")

    assert [r.status for r in pending_rows(t)] == [PendingStatus.PENDING, PendingStatus.CONFIRMED]
    assert count(t, Sale) == 1
    assert count(t, StockMovement) == 1


def test_other_merchants_yes_does_not_touch_my_pending(setup):
    t = setup
    t.vision["outcomes"] = [reading("invoice", INVOICE)]
    send_photo(t)
    send_text(t, "oui", message_id="wamid.OTHER", sender=OTHER_PHONE)
    assert t.text["texts"] == ["oui"]
    assert pending_rows(t)[0].status == PendingStatus.PENDING


def test_yes_by_voice_note_confirms_too(setup):
    from app.whatsapp import router
    from app.whatsapp.payload import Message

    t = setup
    t.vision["outcomes"] = [reading("invoice", INVOICE)]
    send_photo(t)
    voice = Message.model_validate(_message("audio", id_="wamid.VOICE", audio={"id": "M", "voice": True}))

    reply = router._process_text(voice, "Oui, merci.", transcript="Oui, merci.")

    assert reply.startswith("🎙️ J'ai compris : \"Oui, merci.\"\n📦 10 cartons de lait concentré ajoutés au stock.")
    assert count(t, StockMovement) == 2


# --- product photos ---


def test_product_photo_with_caption_creates_pending_sale(setup):
    t = setup
    t.vision["outcomes"] = [
        reading("product", [{"action": "sale", "product": "Savon Azur", "quantity": 3, "price": 500}], language="fr")
    ]
    send_photo(t, caption="vendu 3 à 500")

    assert last_reply(t) == (
        "🧾 Voici ce que j'ai lu :\n"
        "• Vente : 3 Savons Azur — 1 500 FCFA\n"
        "Total : 1 500 FCFA\n"
        "Je l'enregistre ? Réponds OUI ou NON"
    )
    row = pending_rows(t)[0]
    assert [a["action"] for a in row.payload["actions"]] == ["sale"]
    assert count(t, Sale) == 0


def test_product_photo_without_caption_asks_a_question(setup):
    t = setup
    t.vision["outcomes"] = [reading("product", [], clarification="Tu as vendu ou acheté ce savon ?")]
    send_photo(t)
    assert t.sent == [(PHONE, "❓ Tu as vendu ou acheté ce savon ?")]
    assert pending_rows(t) == []
    assert count(t, StockMovement) == 0
    assert count(t, ProcessedMessage) == 0


def test_product_photo_stock_question_is_answered_directly(setup):
    t = setup
    t.vision["outcomes"] = [reading("product", [{"action": "stock_check", "product": "Savon Azur"}], language="fr")]
    send_photo(t, caption="il me reste combien ?")
    assert pending_rows(t) == []
    assert last_reply(t) == "📦 Je ne connais pas encore « Savon Azur ». Note un achat pour l'ajouter au stock."


def test_unreadable_price_is_flagged_in_the_summary(setup):
    t = setup
    t.vision["outcomes"] = [
        reading(
            "invoice",
            [*INVOICE, {"action": "purchase", "product": "huile", "quantity": 3, "unit": "bidon"}],
            clarification="Le prix de l'huile est illisible, tu peux me le donner ?",
        )
    ]
    send_photo(t)
    assert last_reply(t) == (
        "🧾 Voici ce que j'ai lu :\n"
        "• Achat : 10 cartons de lait concentré — 185 000 FCFA\n"
        "• Achat : 5 sacs de riz — 115 000 FCFA\n"
        "• Achat : 3 bidons d'huile — prix ?\n"
        "Total : 300 000 FCFA (sans les lignes sans prix)\n"
        "⚠️ Le prix de l'huile est illisible, tu peux me le donner ?\n"
        "Je l'enregistre ? Réponds OUI ou NON"
    )


def test_nothing_found(setup):
    t = setup
    t.vision["outcomes"] = [reading("other", [])]
    send_photo(t)
    assert last_reply(t).startswith("🖼️ Je n'ai rien trouvé à enregistrer sur cette photo.")


def test_summary_in_english_and_pidgin(setup):
    t = setup
    sale = [{"action": "credit_sale", "product": "rice", "quantity": 1, "unit": "bag", "total_price": 23000, "customer": "Mama Ngozi"}]
    t.vision["outcomes"] = [reading("receipt", sale, language="en"), reading("receipt", sale, language="pcm")]
    send_photo(t, caption="credit", message_id="wamid.EN")
    assert last_reply(t) == (
        "🧾 Here's what I read:\n• Credit for Mama Ngozi: 1 bag of rice — 23 000 FCFA\n"
        "Total: 23 000 FCFA\nShall I save it? Reply YES or NO"
    )
    send_photo(t, caption="credit", message_id="wamid.PCM")
    assert last_reply(t).endswith("Make I save am? Answer YES or NO")
    assert last_reply(t).startswith("🧾 See wetin I read:")


# --- refusals and errors ---


def test_too_large_image(setup):
    t = setup
    t.graph["file_size"] = 6 * 1024 * 1024
    send_photo(t)
    assert last_reply(t) == "🖼️ Cette photo est trop lourde pour moi (5 Mo maximum). Envoie-la en qualité normale."
    assert len(t.graph["requests"]) == 1  # the image itself was never downloaded
    assert t.vision["calls"] == []
    assert pending_rows(t) == []


@pytest.mark.parametrize("mime", ["image/gif", "application/pdf", ""])
def test_unsupported_type_is_refused_before_download(setup, mime):
    t = setup
    send_photo(t, mime=mime)
    assert last_reply(t).startswith("🖼️ Je lis seulement les photos JPEG, PNG ou WebP.")
    assert t.graph["requests"] == []
    assert t.vision["calls"] == []


def test_file_that_is_not_really_an_image_is_refused(setup):
    t = setup
    t.graph["content"] = b"GIF89a" + b"\x00" * 50  # labelled image/jpeg
    send_photo(t)
    assert last_reply(t).startswith("🖼️ Je lis seulement les photos JPEG, PNG ou WebP.")
    assert t.vision["calls"] == []


def test_png_is_sent_with_its_real_type(setup):
    t = setup
    t.graph["content"] = PNG
    t.vision["outcomes"] = [reading("other", [])]
    send_photo(t, mime="image/png")
    url = t.vision["calls"][0]["messages"][1]["content"][1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")


def test_vision_retried_once_then_polite_error(setup):
    t = setup
    request = httpx.Request("POST", "https://api.meta.ai/v1/chat/completions")
    busy = openai.RateLimitError("slow down", response=httpx.Response(429, request=request), body=None)
    t.vision["outcomes"] = [busy, reading("invoice", INVOICE)]
    send_photo(t, message_id="wamid.A")
    assert len(t.vision["calls"]) == 2
    assert last_reply(t) == INVOICE_SUMMARY

    t.vision["outcomes"] = [busy, busy]
    send_photo(t, message_id="wamid.B")
    assert last_reply(t).startswith("🖼️ Je n'ai pas pu lire ta photo.")


def test_invalid_model_output_is_a_polite_error(setup):
    t = setup
    t.vision["outcomes"] = ['{"actions": []}']  # image_kind missing
    send_photo(t)
    assert last_reply(t).startswith("🖼️ Je n'ai pas pu lire ta photo.")
    assert pending_rows(t) == []


# --- idempotence ---


def test_same_photo_twice_gives_one_pending(setup):
    t = setup
    t.vision["outcomes"] = [reading("invoice", INVOICE)]
    send_photo(t, message_id="wamid.SAME")
    send_photo(t, message_id="wamid.SAME")  # in-memory dedup
    seen_messages.clear()  # as if the API restarted before Meta redelivered
    send_photo(t, message_id="wamid.SAME")

    assert len(pending_rows(t)) == 1
    assert len(t.sent) == 1
    assert len(t.vision["calls"]) == 1


def test_confirmed_photo_redelivered_is_not_read_again(setup):
    t = setup
    t.vision["outcomes"] = [reading("invoice", INVOICE)]
    send_photo(t, message_id="wamid.SAME")
    send_text(t, "oui", message_id="wamid.YES")
    seen_messages.clear()
    send_photo(t, message_id="wamid.SAME")
    assert len(t.vision["calls"]) == 1
    assert count(t, StockMovement) == 2


# --- logs ---


def test_logs_have_no_phone_or_content(setup, caplog):
    t = setup
    caplog.set_level("INFO")
    t.vision["outcomes"] = [reading("product", [{"action": "sale", "product": "Savon Azur", "quantity": 3, "price": 500}])]
    send_photo(t, caption="vendu 3 à Marie")
    assert PHONE not in caplog.text
    assert "Marie" not in caplog.text
    assert "Savon Azur" not in caplog.text
    assert "Photo wamid.PHOTO: product, 1 action(s): sale" in caplog.text


# --- YES / NO parsing ---


@pytest.mark.parametrize(
    "text",
    ["oui", "OUI", "Oui !", "oui merci", "Ok", "d'accord", "D’accord", "yes", "Yes please", "yeah", "yep",
     "na so", "I gree", "👍", "✅", "ouais", "c'est bon"],
)
def test_yes(text):
    assert parse_decision(text) == Decision.YES


@pytest.mark.parametrize("text", ["non", "NON.", "no", "No o", "cancel", "annule", "Annulé", "no be so", "👎", "❌"])
def test_no(text):
    assert parse_decision(text) == Decision.NO


@pytest.mark.parametrize("text", ["oui vendu 3 savons", "non je veux dire 4", "okay so I sell 2", "", "merci", "noel"])
def test_not_a_decision(text):
    assert parse_decision(text) is None


def test_pending_ttl_is_30_minutes():
    assert pending.TTL == timedelta(minutes=30)
