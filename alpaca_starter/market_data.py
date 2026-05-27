from __future__ import annotations

from datetime import datetime, timedelta, timezone

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame


def latest_quote(client: StockHistoricalDataClient, symbol: str):
    request = StockLatestQuoteRequest(symbol_or_symbols=symbol)
    return client.get_stock_latest_quote(request)[symbol]


def recent_daily_bars(
    client: StockHistoricalDataClient,
    symbol: str,
    days: int = 5,
):
    request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Day,
        start=datetime.now(timezone.utc) - timedelta(days=days * 2),
    )
    bars = client.get_stock_bars(request).data.get(symbol, [])
    return bars[-days:]
