import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
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
    get_settings.cache_clear()
    seen_messages.clear()
    yield get_settings()
    get_settings.cache_clear()
    seen_messages.clear()


@pytest.fixture
def sent(monkeypatch):
    """Capture outgoing WhatsApp messages instead of calling the Graph API."""
    messages: list[tuple[str, str]] = []
    monkeypatch.setattr("app.whatsapp.client.send_text", lambda to, body: messages.append((to, body)))
    return messages


@pytest.fixture
def client():
    from app.main import app

    return TestClient(app)
