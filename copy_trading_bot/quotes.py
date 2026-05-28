from __future__ import annotations

import logging
from dataclasses import dataclass

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest

log = logging.getLogger("copy_trading_bot.quotes")


@dataclass
class AlpacaQuoteSource:
    """Latest-quote lookup that returns None on any failure rather than raising.

    Alpaca's free IEX feed returns empty quotes outside market hours or for
    thinly traded names — callers fall back to the disclosed reference price."""

    client: StockHistoricalDataClient

    def latest_price(self, symbol: str) -> float | None:
        try:
            req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
            data = self.client.get_stock_latest_quote(req)
        except Exception as exc:  # noqa: BLE001 - never propagate quote errors
            log.warning("quote lookup failed for %s: %s", symbol, exc)
            return None
        quote = data.get(symbol) if hasattr(data, "get") else None
        if quote is None:
            return None
        for attr in ("ask_price", "bid_price"):
            value = getattr(quote, attr, None)
            if value:
                return float(value)
        return None
