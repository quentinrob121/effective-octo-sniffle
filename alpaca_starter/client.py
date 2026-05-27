from __future__ import annotations

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.trading.client import TradingClient

from .config import Settings, load_settings


def build_trading_client(settings: Settings | None = None) -> TradingClient:
    settings = settings or load_settings()
    return TradingClient(
        api_key=settings.api_key,
        secret_key=settings.api_secret,
        paper=settings.is_paper,
    )


def build_data_client(settings: Settings | None = None) -> StockHistoricalDataClient:
    settings = settings or load_settings()
    return StockHistoricalDataClient(
        api_key=settings.api_key,
        secret_key=settings.api_secret,
    )
