import pytest

from app import meta_client
from app.config import get_settings


def test_empty_env_vars_keep_defaults(monkeypatch):
    monkeypatch.setenv("WHATSAPP_API_VERSION", "")
    monkeypatch.setenv("MODEL_API_BASE_URL", "")
    monkeypatch.setenv("DATABASE_URL", "")
    get_settings.cache_clear()

    settings = get_settings()
    assert settings.whatsapp_api_version == "v21.0"
    assert settings.model_api_base_url == "https://api.meta.ai/v1"
    assert settings.database_url.startswith("postgresql+psycopg://")


def test_meta_client_requires_api_key(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "")
    get_settings.cache_clear()
    meta_client.get_client.cache_clear()
    with pytest.raises(meta_client.MissingAPIKeyError):
        meta_client.get_client()


def test_meta_client_uses_settings(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "dummy-test-value")
    monkeypatch.setenv("MODEL_API_BASE_URL", "")
    get_settings.cache_clear()
    meta_client.get_client.cache_clear()
    try:
        client = meta_client.get_client()
        assert client.api_key == "dummy-test-value"
        assert str(client.base_url).rstrip("/") == "https://api.meta.ai/v1"
    finally:
        meta_client.get_client.cache_clear()
