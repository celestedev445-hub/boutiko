"""Any language: free language codes, cached Meta translations of the fixed replies, intents, keywords."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import httpx
import openai
import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app import meta_client
from app.languages import Language, as_language, language_name
from app.models import DailySummary, PendingAction, PendingStatus, Sale, Shop, StockMovement, Translation
from app.schemas import ExtractedAction, Extraction
from app.services import pending, replies, summary_text, transcribe, translations, vision
from tests.test_webhook import _message, _payload

PHONE = "237690000000"  # sender of tests.test_webhook._message
KEYWORDS_ES = {"SUMMARY": "RESUMEN", "YES": "SÍ", "NO": "NO"}
_ids = iter(range(100_000))


def completion(text: str):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


@pytest.fixture
def translator(monkeypatch):
    """The translation model: "ES: <english>" by default, keywords in Spanish.

    state.reply(english) overrides the translation; state.error is raised instead.
    """
    state = SimpleNamespace(calls=[], reply=None, error=None)

    def create_completion(**kwargs):
        system, english = kwargs["messages"][0]["content"], kwargs["messages"][1]["content"]
        assert "You translate" in system
        state.calls.append(english)
        if state.error is not None:
            raise state.error
        if "a word that shopkeepers type" in system:
            return completion(KEYWORDS_ES.get(english, english))
        return completion(state.reply(english) if state.reply else f"ES: {english}")

    monkeypatch.setattr(meta_client, "create_completion", create_completion)
    return state


@pytest.fixture
def extractor(monkeypatch):
    """Text extraction: state.result is returned; texts records what was sent."""
    state = SimpleNamespace(texts=[], result=None)

    def extract(text, known_products=None):
        state.texts.append(text)
        return state.result

    monkeypatch.setattr(meta_client, "extract_from_text", extract)
    return state


def spanish(*actions: dict, intent: str | None = None) -> Extraction:
    actions = actions or ({"action": "unknown"},)
    return Extraction(
        actions=[ExtractedAction(**a) for a in actions], language="es", language_name="Spanish", intent=intent
    )


def send(client, body: str):
    return client.post("/webhook", json=_payload(_message("text", id_=f"wamid.L{next(_ids)}", text={"body": body})))


SALE = {"action": "sale", "product": "jabones", "quantity": 3, "price": 500}


# --- language codes ---


def test_language_codes_are_free_and_normalized():
    assert Language("ES") == "es"
    assert Language("pt_BR") == "pt-br"
    assert Language("zh-Hant") == "zh-hant"
    assert Language("fr-CM") == Language.FR  # hand-written base
    assert Language("sw").value == "sw"
    assert not Language("sw").hand_written and Language.PIDGIN.hand_written
    assert list(Language) == ["fr", "en", "pcm"]
    for bad in ("", "Spanish", "e", "es!", "x" * 20):
        with pytest.raises(ValueError):
            Language(bad)
    assert Extraction(language="Spanish").language is None
    assert Extraction(language="AR").language == "ar"
    assert as_language("not a code!") == Language.FR
    assert as_language(None, Language.EN) == Language.EN


def test_language_names_for_prompts():
    assert language_name(Language.PIDGIN) == "Cameroonian Pidgin English"
    assert language_name("sw", "Swahili") == "Swahili"
    assert language_name("ln") == 'the language with ISO 639 code "ln"'


def test_any_language_is_stored_on_the_shop(session):
    session.add_all([
        Shop(name="a", owner_phone="1", language=Language("sw"), language_name="Swahili"),
        Shop(name="b", owner_phone="2", language="zh-Hant"),
    ])
    session.commit()
    session.expire_all()
    shops = {s.owner_phone: s for s in session.scalars(select(Shop))}
    assert isinstance(shops["1"].language, Language)
    assert (shops["1"].language, shops["1"].language_name) == ("sw", "Swahili")
    assert shops["2"].language == "zh-hant"


# --- migration 0007 ---


def _migration_0007():
    path = Path(__file__).resolve().parent.parent / "alembic" / "versions" / "0007_any_language.py"
    spec = importlib.util.spec_from_file_location("migration_0007", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(connection, step):
    with Operations.context(MigrationContext.configure(connection)):
        step()


def test_migration_0007_keeps_languages_and_adds_translations():
    migration = _migration_0007()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        # shops as it was at revision 0006 (the columns that matter here)
        connection.execute(sa.text(
            "CREATE TABLE shops (id INTEGER PRIMARY KEY, name VARCHAR(120) NOT NULL, "
            "owner_phone VARCHAR(20) NOT NULL, language VARCHAR(20) NOT NULL)"
        ))
        connection.execute(sa.text(
            "INSERT INTO shops (name, owner_phone, language) VALUES ('a', '1', 'pcm'), ('b', '2', 'fr')"
        ))

        _run(connection, migration.upgrade)

        assert connection.execute(sa.text("SELECT language FROM shops ORDER BY id")).scalars().all() == ["pcm", "fr"]
        connection.execute(sa.text(
            "INSERT INTO shops (name, owner_phone, language, language_name) VALUES ('c', '3', 'sw', 'Swahili')"
        ))
        connection.execute(sa.text("INSERT INTO translations (key, language, text) VALUES ('reply.sale', 'sw', 't')"))
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(sa.text("INSERT INTO translations (key, language, text) VALUES ('reply.sale', 'sw', 'u')"))

        _run(connection, migration.downgrade)

        assert connection.execute(sa.text("SELECT language FROM shops ORDER BY id")).scalars().all() == ["pcm", "fr", "en"]
        assert "translations" not in sa.inspect(connection).get_table_names()
        assert "language_name" not in {c["name"] for c in sa.inspect(connection).get_columns("shops")}


# --- translations: cached, checked, never blocking ---


def test_spanish_reply_is_translated_once_then_read_from_the_database(
    client, sent, session_factory, translator, extractor
):
    extractor.result = spanish(SALE)
    send(client, "vendí 3 jabones a 500")
    first_calls = len(translator.calls)

    assert sent[-1][1].splitlines()[0] == "ES: ✅ 3 jabones sold, 1 500 FCFA."
    assert "✅ {item} sold, {total}." in translator.calls  # the English template, never the amounts

    send(client, "vendí 3 jabones a 500")  # same message again: no model call
    assert len(translator.calls) == first_calls
    assert sent[-1][1].splitlines()[0] == "ES: ✅ 3 jabones sold, 1 500 FCFA."
    with session_factory() as session:
        rows = session.scalars(select(Translation).where(Translation.key == "reply.sale")).all()
        assert [(r.language, r.text) for r in rows] == [("es", "ES: ✅ {item} sold, {total}.")]
        shop = session.scalar(select(Shop))
        assert (shop.language, shop.language_name) == ("es", "Spanish")
        assert session.scalar(select(func.count()).select_from(Sale)) == 2


@pytest.mark.parametrize(
    ("translation", "reason"),
    [
        ("✅ vendido, {total}.", "placeholders differ"),  # {item} lost
        ("vendido {item}, {total}.", "emojis differ"),
        ("✅ {item} vendido, {total} {extra}.", "placeholders differ"),
    ],
)
def test_translation_that_loses_a_placeholder_or_emoji_is_rejected(
    client, sent, session_factory, translator, extractor, caplog, translation, reason
):
    translator.reply = lambda english: translation if english == "✅ {item} sold, {total}." else f"ES: {english}"
    extractor.result = spanish(SALE)
    send(client, "vendí 3 jabones a 500")

    assert sent[-1][1].splitlines()[0] == "✅ 3 jabones sold, 1 500 FCFA."  # English
    assert f"Translation reply.sale/es rejected ({reason})" in caplog.text
    with session_factory() as session:
        assert session.scalar(select(Translation.id).where(Translation.key == "reply.sale")) is None


def test_translation_keeps_the_english_spacing(session_factory, translator):
    translator.reply = lambda english: english.strip().replace("Left", "Quedan")
    assert translations.translate("reply.remaining", " Left: {stock}.", Language("es")) == " Quedan: {stock}."


def test_translation_check():
    assert translations.problem("✅ {item} sold, {total}.", "✅ {total} : {item} vendu.") is None
    assert translations.problem("🎙️ 5 minutes max", "🎙️ cinco minutos") == "numbers differ"
    assert translations.problem("✅ {item}", "✅ {item} {") == "stray braces"
    assert translations.problem("✅ {item}", "") == "empty"


def test_model_failure_answers_in_english_and_still_records(client, sent, session_factory, translator, extractor):
    translator.error = openai.APIConnectionError(request=httpx.Request("POST", "https://api.meta.ai/v1"))
    extractor.result = spanish(SALE)
    send(client, "vendí 3 jabones a 500")

    assert sent[-1][1].splitlines()[0] == "✅ 3 jabones sold, 1 500 FCFA."
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Sale)) == 1
        assert session.scalar(select(func.count()).select_from(Translation)) == 0

    calls = len(translator.calls)
    send(client, "vendí 3 jabones a 500")  # failed recently: English again, without retrying the model
    assert len(translator.calls) == calls


def test_database_error_answers_in_english(monkeypatch, translator):
    def broken():
        raise RuntimeError("database down")

    monkeypatch.setattr("app.db.SessionLocal", broken)
    assert replies.cancelled(Language("es")) == "❌ Cancelled, nothing was saved."
    assert translator.calls == []


# --- fr, en, pcm: hand-written, unchanged, no model call ---


def test_hand_written_languages_never_call_the_model(translator):
    assert replies.welcome(Language.FR, "Bonjour") == (
        "👋 Bonjour ! Envoie-moi tes ventes, achats ou dettes, en texte, en vocal ou en photo, "
        "par exemple « vendu 3 savons à 250 ». Tape RÉSUMÉ pour le bilan du jour."
    )
    assert replies.welcome(Language.EN, "Hello").endswith("Type SUMMARY for today's summary.")
    assert replies.welcome(Language.PIDGIN, "How far").endswith("Type SUMMARY make you see how market go today.")
    extraction = Extraction(actions=[ExtractedAction(action="purchase", product="riz", quantity=2)])
    assert replies.pending_summary(extraction, Language.FR).endswith("Je l'enregistre ? Réponds OUI ou NON")
    assert replies.pending_summary(extraction, Language.PIDGIN).endswith("Make I save am? Answer YES or NO")
    assert summary_text.offer(Language.FR, yesterday=False) == "💡 Tape RÉSUMÉ pour voir le bilan du jour."
    assert replies.unsupported(Language.FR) == "Désolé, je comprends le texte, les notes vocales et les photos."
    assert translator.calls == []


def test_numbers_and_plurals_follow_english_rules_in_other_languages():
    from decimal import Decimal

    assert replies.format_quantity(Decimal("1.5"), Language("es")) == "1.5"
    assert replies.format_quantity(Decimal("1.5"), Language.FR) == "1,5"
    assert replies.quantity_with_unit(Decimal(3), "saco", Language("es")) == "3 saco"  # no English "s" added
    assert replies.quantity_with_unit(Decimal(3), "袋", Language("zh")) == "3 袋"
    assert replies.quantity_with_unit(Decimal(3), "bag", Language.EN) == "3 bags"


# --- intents and keywords ---


def test_greeting_in_any_language_shows_the_summary_word_in_that_language(
    client, sent, session_factory, translator, extractor
):
    extractor.result = spanish(intent="greeting")
    send(client, "hola")
    reply = sent[-1][1]
    assert reply.startswith("ES: 👋 ES: Good")
    assert "Type RESUMEN for today's summary." in reply

    # The word shown is then recognized, without calling the extraction model.
    texts = len(extractor.texts)
    send(client, "Resumen")
    assert len(extractor.texts) == texts
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(DailySummary)) == 1


def test_summary_request_intent(client, sent, session_factory, translator, extractor):
    extractor.result = spanish(intent="summary_request")
    send(client, "¿cómo me fue hoy?")
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(DailySummary)) == 1
        assert session.scalar(select(Shop.language)) == "es"


@pytest.fixture
def photo(session_factory, translator):
    """A photo read in Spanish, waiting for YES / NO."""
    with session_factory() as session:
        extraction = spanish({"action": "purchase", "product": "arroz", "quantity": 10})
        pending.create(session, PHONE, extraction, "wamid.PHOTO")
        return extraction


@pytest.mark.parametrize(
    ("intent", "status", "recorded"),
    [("confirm_yes", PendingStatus.CONFIRMED, 1), ("confirm_no", PendingStatus.CANCELLED, 0)],
)
def test_yes_or_no_understood_by_the_model(client, sent, session_factory, extractor, photo, intent, status, recorded):
    extractor.result = spanish(intent=intent)
    send(client, "claro que sí" if intent == "confirm_yes" else "mejor no")
    with session_factory() as session:
        assert session.scalar(select(PendingAction.status)) == status
        assert session.scalar(select(func.count()).select_from(StockMovement)) == recorded


def test_translated_yes_shown_in_the_question_is_recognized(client, sent, session_factory, extractor, photo):
    question = replies.pending_summary(photo, Language("es"))
    assert question.endswith("ES: Shall I save it? Reply SÍ or NO")

    send(client, "sí")
    assert extractor.texts == []  # recognized without the model
    with session_factory() as session:
        assert session.scalar(select(PendingAction.status)) == PendingStatus.CONFIRMED


def test_intent_is_ignored_when_the_message_records_something(client, sent, session_factory, translator, extractor):
    extractor.result = spanish(SALE, intent="greeting")
    send(client, "hola, vendí 3 jabones a 500")
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Sale)) == 1


# --- the language's name in the other prompts ---


def test_vision_and_summary_prompts_use_the_language_name(monkeypatch, translator):
    assert "The merchant usually writes in Swahili." in vision._instruction(None, Language("sw"), "Swahili")
    assert "Cameroonian Pidgin" in vision._instruction(None, Language.PIDGIN)

    prompts = []

    def create_completion(**kwargs):
        system = kwargs["messages"][0]["content"]
        if "You translate" in system:
            return completion("Habari za " + kwargs["messages"][1]["content"])
        prompts.append(system)
        return completion("")

    monkeypatch.setattr(meta_client, "create_completion", create_completion)
    monkeypatch.setattr(summary_text, "figures_for_model", lambda figures, language: {})
    summary_text._write_with_model(None, Language("sw"), "Swahili")
    assert "Write in Swahili." in prompts[0]


def test_transcription_never_forces_a_language(monkeypatch, settings):
    monkeypatch.setenv("MODEL_API_KEY", "dummy-test-value")
    from app.config import get_settings

    get_settings.cache_clear()
    requests = []

    class FakeHTTP:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, url, files, headers):
            requests.append(files["request"][1])
            return httpx.Response(200, json={"transcript": "nimeuza mikate mitano"})

    monkeypatch.setattr(transcribe, "_http_client", FakeHTTP)
    assert transcribe.transcribe_with_asr(b"RIFF").text == "nimeuza mikate mitano"
    assert "language" not in requests[0].lower()
    assert "any language" in transcribe.FALLBACK_PROMPT and "never translate" in transcribe.FALLBACK_PROMPT
