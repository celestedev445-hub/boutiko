"""Public landing page: served at "/", static files, public config."""

from app.config import get_settings


def test_landing_page(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Boutiko" in response.text
    assert '/static/style.css' in response.text


def test_stylesheet(client):
    response = client.get("/static/style.css")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")


def test_public_config_is_empty_by_default(client, monkeypatch):
    monkeypatch.delenv("DEMO_WHATSAPP_NUMBER", raising=False)
    get_settings.cache_clear()
    assert client.get("/api/public-config").json() == {"demo_whatsapp_number": ""}


def test_public_config_returns_the_demo_number(client, monkeypatch):
    monkeypatch.setenv("DEMO_WHATSAPP_NUMBER", "+237 6 90 00 12 34")
    get_settings.cache_clear()
    assert client.get("/api/public-config").json() == {"demo_whatsapp_number": "237690001234"}


def test_public_config_exposes_nothing_else(client, monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "dummy-test-value")
    get_settings.cache_clear()
    assert "dummy-test-value" not in client.get("/api/public-config").text


def test_other_routes_unchanged(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "1"}).status_code == 403
