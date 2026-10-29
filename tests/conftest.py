import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import db, languages, meta_client
from app.config import Settings, get_settings
from app.services import translations
from app.whatsapp import router
from app.whatsapp.dedup import seen_messages

VERIFY_TOKEN = "test-verify-token"
APP_SECRET = "test-app-secret"


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    # Ignore any local .env so tests only see what they set themselves.
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", VERIFY_TOKEN)
    monkeypatch.delenv("WHATSAPP_APP_SECRET", raising=False)
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    monkeypatch.setenv("SUMMARY_ENABLED", "false")  # never start the scheduler loop in tests
    # No key: any Meta API call a test forgot to mock fails instead of reaching the network.
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.delenv("BASETEN_API_KEY", raising=False)
    meta_client.get_client.cache_clear()
    get_settings.cache_clear()
    seen_messages.clear()
    translations.forget_failures()
    languages.forget_names()
    yield get_settings()
    get_settings.cache_clear()
    seen_messages.clear()


@pytest.fixture
def sent(monkeypatch):
    """Capture outgoing WhatsApp messages instead of calling the Graph API."""
    messages: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "app.whatsapp.client.send_text", lambda to, body, phone_number_id=None: messages.append((to, body))
    )
    return messages


@pytest.fixture
def client():
    from app.main import app

    return TestClient(app)


@pytest.fixture
def session_factory(monkeypatch):
    """Fresh in-memory SQLite database, wired in place of the real one."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    db.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", factory)
    yield factory
    engine.dispose()


@pytest.fixture
def session(session_factory):
    with session_factory() as s:
        yield s


@pytest.fixture
def echo_handler(monkeypatch):
    """Webhook-level tests: answer text with an echo instead of running the AI pipeline."""
    monkeypatch.setitem(router.HANDLERS, "text", lambda message: f"Echo: {message.text.body}")
