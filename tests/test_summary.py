"""Evening summary: figures, writing (model mocked), commands and scheduler, with a frozen clock."""

import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import openai
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import clock, meta_client
from app.models import DailySummary, Language, MovementKind, Product, Shop, StockMovement, SummaryChannel
from app.schemas import ExtractedAction, Extraction
from app.services import daily, ledger
from app.services.summary import build_daily_summary
from app.services.summary_text import template, unknown_amounts, write_summary
from tests.test_webhook import _message, _payload

PHONE = "237690000000"  # sender of tests.test_webhook._message
DOUALA = ZoneInfo("Africa/Douala")
TODAY = date(2026, 10, 27)  # a Tuesday
YESTERDAY = date(2026, 10, 26)
_ids = iter(range(100_000))


def local(day: date, hour: int, minute: int = 0) -> datetime:
    """A Douala local time, as the UTC datetime the app works with."""
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=DOUALA).astimezone(UTC)


@pytest.fixture
def now(monkeypatch):
    """Frozen clock: set now.value to move it."""
    state = SimpleNamespace(value=local(TODAY, 10))
    monkeypatch.setattr(clock, "now", lambda: state.value)
    return state


@pytest.fixture
def writer(monkeypatch):
    """The summary-writing model. state.text: what it returns, an exception, or {language name: text}."""
    state = SimpleNamespace(text="", calls=[])

    def create_completion(**kwargs):
        state.calls.append(kwargs)
        if isinstance(state.text, Exception):
            raise state.text
        text = state.text
        if isinstance(text, dict):
            prompt = kwargs["messages"][0]["content"]
            text = next(t for name, t in text.items() if f"Write in {name}." in prompt)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])

    monkeypatch.setattr(meta_client, "create_completion", create_completion)
    return state


@pytest.fixture
def extractor(monkeypatch):
    """Text extraction: records texts; returns an 'unknown' action."""
    state = SimpleNamespace(texts=[])

    def extract(text, known_products=None):
        state.texts.append(text)
        return Extraction(actions=[ExtractedAction(action="unknown")], language="fr")

    monkeypatch.setattr(meta_client, "extract_from_text", extract)
    return state


def record(session, *actions: dict, language: str = "fr", phone: str = PHONE) -> None:
    extraction = Extraction(actions=[ExtractedAction(**a) for a in actions], language=language)
    ledger.apply_extraction(session, phone, extraction, f"wamid.{next(_ids)}")


def shop_of(session, phone: str = PHONE) -> Shop:
    return session.scalar(select(Shop).where(Shop.owner_phone == phone))


@pytest.fixture
def busy_day(session, now):
    """Yesterday 20 000 FCFA of sales; today a full day of purchases, sales, credit and payments."""
    now.value = local(YESTERDAY, 18)
    record(session, {"action": "sale", "product": "pagne", "quantity": 1, "total_price": 20000})

    now.value = local(TODAY, 8)
    record(
        session,
        {"action": "purchase", "product": "riz", "quantity": 10, "unit": "sac", "total_price": 230000},
        {"action": "purchase", "product": "savon Azur", "quantity": 20, "unit": "pièces", "price": 100},
        {"action": "purchase", "product": "huile", "quantity": 3, "unit": "bidon"},
        {"action": "purchase", "product": "Maggi", "quantity": 5, "price": 100},
    )
    now.value = local(TODAY, 12)
    record(
        session,
        {"action": "sale", "product": "savons Azur", "quantity": 6, "price": 250},
        {"action": "sale", "product": "riz", "quantity": 2, "unit": "sac", "total_price": 46000},
        {"action": "sale", "product": "Maggi", "quantity": 3, "price": 100},
        {"action": "sale", "product": "huile", "quantity": 3, "unit": "bidon"},  # no price known
    )
    now.value = local(TODAY, 17)
    record(
        session,
        {"action": "credit_sale", "product": "sucre", "quantity": 2, "unit": "kg", "price": 750, "customer": "Marie"},
        {"action": "credit_sale", "product": "lait", "quantity": 1, "price": 3000, "customer": "Paul"},
        {"action": "debt_payment", "customer": "Marie", "total_price": 500},
    )
    now.value = local(TODAY, 20, 30)
    return shop_of(session)


# --- figures ---


def test_daily_figures(session, busy_day):
    figures = build_daily_summary(session, busy_day, TODAY)

    assert figures.sales_count == 6
    assert figures.sales_total == 1500 + 46000 + 300 + 1500 + 3000
    assert (figures.credit_sales_count, figures.credit_sales_total) == (2, 4500)
    assert figures.unpriced_sales_count == 1
    assert (figures.purchases_count, figures.purchases_total) == (4, 230000 + 2000 + 500)
    assert (figures.new_debt_total, figures.debt_repaid_total) == (4500, 500)
    assert (figures.debts_outstanding_total, figures.debtors_count) == (4000, 2)
    assert (figures.top_debtor.name, figures.top_debtor.amount) == ("Paul", 3000)
    assert [(p.name, p.unit, p.quantity, p.amount) for p in figures.top_products] == [
        ("savon Azur", None, Decimal(6), 1500),
        ("Maggi", None, Decimal(3), 300),  # same quantity as huile, more money
        ("huile", "bidon", Decimal(3), 0),
    ]
    assert [(s.name, s.stock) for s in figures.low_stock] == [("Maggi", Decimal(2))]
    # sucre, lait and pagne were sold without any purchase or count: their stock isn't
    # followed, so they are never reported as out of stock. huile was bought, then sold out.
    assert [(s.name, s.stock) for s in figures.out_of_stock] == [("huile", Decimal(0))]
    assert figures.yesterday_sales_total == 20000
    assert figures.sales_change == 32300
    assert figures.sales_change_percent == 162  # 161.5 rounded half up, no float
    assert all(isinstance(v, int) for v in figures.amounts())


def test_local_day_boundaries(session, now):
    now.value = local(YESTERDAY, 23, 59)  # 22:59 UTC
    record(session, {"action": "sale", "product": "pain", "quantity": 1, "price": 100})
    now.value = local(TODAY, 0, 30)  # 23:30 UTC the day before, but already today in Douala
    record(session, {"action": "sale", "product": "pain", "quantity": 1, "price": 150})
    shop = shop_of(session)
    assert build_daily_summary(session, shop, TODAY).sales_total == 150
    assert build_daily_summary(session, shop, YESTERDAY).sales_total == 100


def test_low_stock_threshold_is_configurable(session, now, monkeypatch):
    from app.config import get_settings

    record(session, {"action": "purchase", "product": "savon", "quantity": 5})
    shop = shop_of(session)
    assert build_daily_summary(session, shop, TODAY).low_stock == []
    monkeypatch.setenv("LOW_STOCK_THRESHOLD", "5")
    get_settings.cache_clear()
    assert [s.name for s in build_daily_summary(session, shop, TODAY).low_stock] == ["savon"]


def test_no_previous_day_means_no_percentage(session, now):
    record(session, {"action": "sale", "product": "pain", "quantity": 2, "price": 100})
    figures = build_daily_summary(session, shop_of(session), TODAY)
    assert (figures.yesterday_sales_total, figures.sales_change, figures.sales_change_percent) == (0, 200, None)


# --- writing ---


MODEL_TEXT = (
    "🌙 Bonsoir ! Belle journée aujourd'hui.\n"
    "🛒 6 ventes pour 52 300 FCFA, dont 4 500 FCFA à crédit.\n"
    "📈 Hier : 20 000 FCFA, soit 32 300 FCFA de plus.\n"
    "📝 Paul te doit 3 000 FCFA.\n"
    "💡 Pense à racheter de l'huile demain."
)
MODEL_TEXTS = {
    "French": MODEL_TEXT,
    "English": "🌙 Good evening! 6 sales for 52 300 FCFA today.\n💡 Restock the oil tomorrow.",
    "Cameroonian Pidgin English": "🌙 Good evening! You sell 6 times, 52 300 FCFA.\n💡 Buy oil tomorrow.",
}


def test_model_text_is_used_when_amounts_check_out(session, busy_day, writer):
    writer.text = MODEL_TEXT
    figures = build_daily_summary(session, busy_day, TODAY)

    assert write_summary(figures, Language.FR) == MODEL_TEXT

    call = writer.calls[0]
    assert call["model"] == "muse-spark-1.3-contributor"
    assert call["reasoning_effort"] == "low"
    assert "Write in French." in call["messages"][0]["content"]
    assert 'Start with exactly this greeting: "Bonsoir".' in call["messages"][0]["content"]
    payload = call["messages"][1]["content"]
    assert '"greeting": "Bonsoir"' in payload
    assert '"total_text": "52 300 FCFA"' in payload
    assert '"name": "Paul"' in payload
    assert '"out_of_stock": ["huile"]' in payload


def test_invented_amount_falls_back_to_template(session, busy_day, writer, caplog):
    writer.text = MODEL_TEXT.replace("32 300 FCFA de plus", "un bénéfice de 18 700 FCFA")
    figures = build_daily_summary(session, busy_day, TODAY)

    text = write_summary(figures, Language.FR)

    assert text == template(figures, Language.FR)
    assert "18 700" not in text
    assert "1 amount(s) not in the figures" in caplog.text


@pytest.mark.parametrize("bad", ["Tu as gagné 52k aujourd'hui", "Total 52 301 FCFA", "Ventes : 52.300 F et 99 F"])
def test_guard_catches_other_forms(session, busy_day, bad):
    figures = build_daily_summary(session, busy_day, TODAY)
    assert unknown_amounts(bad, figures)


def test_guard_accepts_other_spellings_of_real_amounts(session, busy_day):
    figures = build_daily_summary(session, busy_day, TODAY)
    assert unknown_amounts("52.300 F, 52 300 francs, 4500 FCFA, 6 ventes, +162 %", figures) == []


def test_model_failure_falls_back_to_template(session, busy_day, writer):
    request = httpx.Request("POST", "https://api.meta.ai/v1/chat/completions")
    writer.text = openai.APIConnectionError(request=request)
    figures = build_daily_summary(session, busy_day, TODAY)
    assert write_summary(figures, Language.EN) == template(figures, Language.EN)


@pytest.mark.parametrize("language", list(Language))
def test_template_has_every_figure_and_no_advice(session, busy_day, language):
    text = template(build_daily_summary(session, busy_day, TODAY), language)
    for amount in ["52 300 FCFA", "4 500 FCFA", "232 500 FCFA", "500 FCFA", "4 000 FCFA", "20 000 FCFA", "1 500 FCFA"]:
        assert amount in text
    assert "+162 %" in text
    assert "savon Azur (6 · 1 500 FCFA)" in text
    assert "huile (3 bidons)" in text or "huile (3 bidon)" in text
    assert "Maggi (2)" in text
    assert "💡" not in text
    assert 8 <= len(text.splitlines()) <= 12


def test_template_french(session, busy_day):
    assert template(build_daily_summary(session, busy_day, TODAY), Language.FR) == (
        "🌙 Bonsoir ! Voici le bilan du mardi 27 octobre\n"
        "🛒 Ventes : 6 pour 52 300 FCFA (dont 2 à crédit : 4 500 FCFA) · 1 sans prix\n"
        "📈 Hier : 20 000 FCFA (+162 %)\n"
        "🏆 Top : savon Azur (6 · 1 500 FCFA), Maggi (3 · 300 FCFA), huile (3 bidons)\n"
        "📦 Achats : 232 500 FCFA\n"
        "💵 Dettes encaissées : 500 FCFA · nouvelles dettes : 4 500 FCFA\n"
        "📝 On te doit en tout : 4 000 FCFA (2 clients)\n"
        "⚠️ Stock bas : Maggi (2)\n"
        "❌ Épuisé : huile\n"
        "Bonne soirée ! 🙏"
    )


def test_unknown_stock_is_never_out_of_stock(session, now):
    record(session, {"action": "sale", "product": "savon", "quantity": 5, "price": 250})
    record(session, {"action": "purchase", "product": "riz", "quantity": 2, "unit": "sac"})
    record(session, {"action": "sale", "product": "riz", "quantity": 2, "unit": "sac", "price": 23000})
    figures = build_daily_summary(session, shop_of(session), TODAY)
    assert [s.name for s in figures.out_of_stock] == ["riz"]
    assert figures.low_stock == []

    # Older data: movements on a product whose stock was never counted.
    savon = session.scalar(select(Product).where(Product.name == "savon"))
    session.add(StockMovement(shop_id=savon.shop_id, product_id=savon.id, kind=MovementKind.SALE, quantity=-5,
                              occurred_at=local(TODAY, 11)))
    session.commit()
    assert [s.name for s in build_daily_summary(session, shop_of(session), TODAY).out_of_stock] == ["riz"]


# --- greeting by the time of day ---


@pytest.mark.parametrize(
    ("hour", "fr", "en", "emoji", "closing"),
    [
        (9, "Bonjour", "Good morning", "☀️", "Bonne journée ! 🙏"),
        (14, "Bon après-midi", "Good afternoon", "🌤️", "Bonne fin de journée ! 🙏"),
        (20, "Bonsoir", "Good evening", "🌙", "Bonne soirée ! 🙏"),
    ],
)
def test_greeting_follows_the_shop_time(session, busy_day, now, hour, fr, en, emoji, closing):
    now.value = local(TODAY, hour)
    figures = build_daily_summary(session, busy_day, TODAY)
    text = template(figures, Language.FR)
    assert text.startswith(f"{emoji} {fr} ! Voici le bilan du mardi 27 octobre\n")
    assert text.endswith(closing)
    assert template(figures, Language.EN).startswith(f"{emoji} {en}! Here's your summary")
    assert template(figures, Language.PIDGIN).startswith(f"{emoji} {en}! See how market go")


def test_greeting_boundaries_in_shop_time_zone(now):
    from app.services.greetings import Period, period

    for (hour, minute), expected in {
        (11, 59): Period.MORNING, (12, 0): Period.AFTERNOON, (17, 59): Period.AFTERNOON, (18, 0): Period.EVENING,
    }.items():
        now.value = local(TODAY, hour, minute)
        assert period() == expected


def test_model_with_another_greeting_falls_back_to_template(session, busy_day, writer, now, caplog):
    now.value = local(TODAY, 20, 30)
    writer.text = MODEL_TEXT.replace("Bonsoir", "Bonjour")
    figures = build_daily_summary(session, busy_day, TODAY)
    assert write_summary(figures, Language.FR) == template(figures, Language.FR)
    assert "wrong or missing greeting" in caplog.text

    writer.text = MODEL_TEXT + "\nBonjour à demain !"
    assert write_summary(figures, Language.FR) == template(figures, Language.FR)


@pytest.mark.parametrize(
    ("hour", "text", "start"),
    [
        (9, "bonjour", "👋 Bonjour ! Envoie-moi tes ventes"),
        (14, "Salut !", "👋 Bon après-midi ! Envoie-moi tes ventes"),
        (20, "hello", "👋 Good evening! Send me your sales"),
        (20, "how far", "👋 Good evening! Send me your sales, wetin you buy"),
    ],
)
def test_welcome_message(client, sent, session_factory, now, extractor, writer, hour, text, start):
    now.value = local(TODAY, hour)
    send(client, text)
    assert sent[-1][1].startswith(start)
    assert extractor.texts == []
    assert writer.calls == []


@pytest.mark.parametrize(
    ("language", "start"),
    [
        (Language.FR, "☀️ Bonjour ! Bilan du mardi 27 octobre : aucune opération enregistrée."),
        (Language.EN, "☀️ Good morning! Summary for Tuesday 27 October: nothing recorded."),
        (Language.PIDGIN, "☀️ Good morning! How market go for Tuesday 27 October: nothing recorded."),
    ],
)
def test_empty_day_is_short_and_needs_no_model(session, now, writer, language, start):
    record(session, {"action": "stock_check", "product": "savon"})  # creates the shop, records nothing
    text = write_summary(build_daily_summary(session, shop_of(session), TODAY), language)
    assert text.startswith(start)
    assert writer.calls == []


# --- asking for it ---


def send(client, body: str, message_id: str | None = None):
    message_id = message_id or f"wamid.{next(_ids)}"
    return client.post("/webhook", json=_payload(_message("text", id_=message_id, text={"body": body})))


@pytest.mark.parametrize(
    ("text", "language"),
    [("résumé", "French"), ("RÉSUMÉ !", "French"), ("bilan", "French"), ("point du jour", "French"),
     ("summary", "English"), ("Today summary please", "English"), ("how market go today", "Cameroonian Pidgin English")],
)
def test_summary_request_sends_summary_without_extraction(client, sent, session_factory, busy_day, writer, extractor, text, language):
    writer.text = MODEL_TEXTS
    send(client, text)

    assert extractor.texts == []
    assert sent[-1][1] == MODEL_TEXTS[language]
    assert f"Write in {language}." in writer.calls[0]["messages"][0]["content"]
    with session_factory() as session:
        row = session.scalar(select(DailySummary))
        assert (row.day, row.channel) == (TODAY, SummaryChannel.MANUAL)


def test_summary_request_by_voice_note(session_factory, busy_day, writer, extractor):
    from app.whatsapp import router
    from app.whatsapp.payload import Message

    writer.text = MODEL_TEXT
    voice = Message.model_validate(_message("audio", id_="wamid.V", audio={"id": "M"}))
    reply = router._process_text(voice, "Résumé.", transcript="Résumé.")
    assert reply == "🎙️ J'ai compris : \"Résumé.\"\n" + MODEL_TEXT
    assert extractor.texts == []


def test_other_messages_still_go_to_extraction(client, sent, session_factory, now, writer, extractor):
    send(client, "résumé des ventes de savon hier")
    assert extractor.texts == ["résumé des ventes de savon hier"]
    assert writer.calls == []


@pytest.mark.parametrize(
    ("text", "hour", "reply"),
    [
        ("résumé à 21h", 21, "⏰ C'est noté : ton résumé arrivera chaque soir à 21h."),
        ("Bilan à 19h00", 19, "⏰ C'est noté : ton résumé arrivera chaque soir à 19h."),
        ("summary at 9pm", 21, "⏰ Done: your summary will arrive every day at 21:00."),
        ("summary at 22:00", 22, "⏰ Done: your summary will arrive every day at 22:00."),
    ],
)
def test_summary_hour_command(client, sent, session_factory, now, extractor, text, hour, reply):
    send(client, text)
    assert sent[-1][1] == reply
    assert extractor.texts == []
    with session_factory() as session:
        assert shop_of(session).summary_hour == hour


def test_impossible_hour_is_not_a_command():
    assert daily.parse_summary_hour("résumé à 25h") is None
    assert daily.parse_summary_hour("summary at 13pm") is None
    assert daily.parse_summary_hour("vendu 3 savons à 21h") is None


# --- scheduler ---


@pytest.fixture
def evening(session, busy_day, writer, sent, now):
    """busy_day's shop, whose merchant last wrote at 18:00 today."""
    writer.text = MODEL_TEXT
    busy_day.last_message_at = local(TODAY, 18)
    session.commit()
    return busy_day


def auto_rows(session_factory) -> list[DailySummary]:
    with session_factory() as session:
        return list(session.scalars(select(DailySummary).where(DailySummary.channel == SummaryChannel.AUTO)))


def test_not_sent_before_summary_hour(session_factory, evening, sent, now):
    now.value = local(TODAY, 19, 45)
    assert daily.run_due_summaries() == 0
    assert sent == []


def test_sent_once_per_day(session_factory, evening, sent, now):
    now.value = local(TODAY, 20, 0)
    assert daily.run_due_summaries() == 1
    assert sent == [(PHONE, MODEL_TEXT)]

    now.value = local(TODAY, 20, 15)
    assert daily.run_due_summaries() == 0
    now.value = local(TODAY, 23, 45)
    assert daily.run_due_summaries() == 0
    assert len(sent) == 1
    rows = auto_rows(session_factory)
    assert [(r.day, r.channel) for r in rows] == [(TODAY, SummaryChannel.AUTO)]


def test_per_shop_hour(session_factory, session, evening, sent, now):
    evening.summary_hour = 21
    session.commit()
    now.value = local(TODAY, 20, 30)
    assert daily.run_due_summaries() == 0
    now.value = local(TODAY, 21, 5)
    assert daily.run_due_summaries() == 1


def test_not_sent_after_manual_request(client, session_factory, evening, sent, now, extractor):
    now.value = local(TODAY, 19, 0)
    send(client, "résumé")
    now.value = local(TODAY, 20, 0)
    assert daily.run_due_summaries() == 0
    assert len(sent) == 1


def test_send_failure_is_retried(session_factory, evening, now, monkeypatch):
    attempts = []

    def flaky(to, body):
        attempts.append(body)
        if len(attempts) == 1:
            raise RuntimeError("Graph API down")

    monkeypatch.setattr("app.whatsapp.client.send_text", flaky)
    now.value = local(TODAY, 20, 0)
    assert daily.run_due_summaries() == 0
    assert auto_rows(session_factory) == []
    now.value = local(TODAY, 20, 15)
    assert daily.run_due_summaries() == 1
    assert len(attempts) == 2


def test_outside_24h_window_is_skipped_and_logged_once(session_factory, session, evening, sent, now, caplog):
    caplog.set_level(logging.INFO)
    evening.last_message_at = local(TODAY, 20, 0) - timedelta(hours=23, minutes=31)
    session.commit()

    now.value = local(TODAY, 20, 0)
    assert daily.run_due_summaries() == 0
    now.value = local(TODAY, 20, 15)
    assert daily.run_due_summaries() == 0

    assert sent == []
    assert caplog.text.count("summary skipped: outside 24h window") == 1
    with session_factory() as s:
        assert shop_of(s).summary_skipped_day == TODAY


def test_window_closing_soon_still_sends(session_factory, session, evening, sent, now):
    evening.last_message_at = local(TODAY, 20, 0) - timedelta(hours=23, minutes=29)
    session.commit()
    now.value = local(TODAY, 20, 0)
    assert daily.run_due_summaries() == 1


@pytest.fixture
def skipped(session_factory, session, evening, now):
    """Today's summary was skipped because the window was closed."""
    evening.last_message_at = local(YESTERDAY, 9)
    session.commit()
    now.value = local(TODAY, 20, 0)
    daily.run_due_summaries()
    return evening


def test_skipped_summary_offered_next_morning(client, sent, session_factory, skipped, now, extractor, writer):
    now.value = local(date(2026, 10, 28), 8, 30)
    send(client, "le marché est calme")

    reply = sent[-1][1]
    assert reply.startswith("🤔")  # the normal reply first
    assert reply.endswith("\n\n💡 Tape RÉSUMÉ pour voir le bilan d'hier.")

    # The offer is made once.
    send(client, "toujours calme")
    assert "RÉSUMÉ" not in sent[-1][1]

    # RÉSUMÉ before noon shows yesterday's day.
    # model down: the template shows which day is summarized
    writer.text = openai.APIConnectionError(request=httpx.Request("POST", "https://api.meta.ai/v1/chat/completions"))
    send(client, "RÉSUMÉ")
    assert sent[-1][1].startswith("☀️ Bonjour ! Voici le bilan du mardi 27 octobre\n🛒 Ventes : 6 pour 52 300 FCFA")
    with session_factory() as session:
        row = session.scalar(select(DailySummary))
        assert (row.day, row.channel) == (TODAY, SummaryChannel.MANUAL)


def test_skipped_summary_offered_same_evening_and_not_auto_sent(client, sent, session_factory, skipped, now, extractor):
    now.value = local(TODAY, 21, 0)
    send(client, "bonsoir")  # the welcome message, then the offer
    assert sent[-1][1].startswith("👋 Bonsoir !")
    assert sent[-1][1].endswith("\n\n💡 Tape RÉSUMÉ pour voir le bilan du jour.")

    # The window is open again, but the offer stands in for the automatic summary.
    now.value = local(TODAY, 21, 15)
    assert daily.run_due_summaries() == 0


def test_skipped_summary_not_offered_after_noon_next_day(client, sent, session_factory, skipped, now, extractor):
    now.value = local(date(2026, 10, 28), 12, 30)
    send(client, "le marché est calme")
    assert "RÉSUMÉ" not in sent[-1][1]
    with session_factory() as session:
        assert shop_of(session).summary_skipped_day is None


def test_inbound_message_updates_the_window(client, sent, session_factory, now, extractor):
    now.value = local(TODAY, 9, 15)
    send(client, "le marché est calme")
    with session_factory() as session:
        assert clock.as_utc(shop_of(session).last_message_at) == local(TODAY, 9, 15)


def test_scheduler_starts_with_the_app_only_when_enabled(monkeypatch):
    from app import main
    from app.config import get_settings

    started = []

    async def fake_loop():
        started.append(True)

    monkeypatch.setattr(main, "summary_loop", fake_loop)
    with TestClient(main.app):
        pass
    assert started == []  # SUMMARY_ENABLED=false in conftest

    monkeypatch.setenv("SUMMARY_ENABLED", "true")
    get_settings.cache_clear()
    with TestClient(main.app):
        pass
    assert started == [True]
