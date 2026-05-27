import pytest

from alpaca_starter.config import Settings, load_settings


@pytest.fixture(autouse=True)
def clear_alpaca_env(monkeypatch):
    for var in ("ALPACA_API_KEY", "ALPACA_API_SECRET", "ALPACA_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    # Stop load_settings from reading a developer's local .env during tests.
    monkeypatch.setattr("alpaca_starter.config.load_dotenv", lambda *a, **k: False)


def test_load_settings_reads_env(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "key123")
    monkeypatch.setenv("ALPACA_API_SECRET", "secret456")

    settings = load_settings()

    assert settings.api_key == "key123"
    assert settings.api_secret == "secret456"
    assert settings.base_url == "https://paper-api.alpaca.markets/v2"
    assert settings.is_paper is True


def test_load_settings_missing_raises(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "key123")
    # secret intentionally absent
    with pytest.raises(RuntimeError) as exc:
        load_settings()
    assert "ALPACA_API_SECRET" in str(exc.value)


def test_is_paper_false_for_live_url():
    settings = Settings(
        api_key="k",
        api_secret="s",
        base_url="https://api.alpaca.markets/v2",
    )
    assert settings.is_paper is False
