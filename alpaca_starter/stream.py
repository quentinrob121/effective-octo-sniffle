from __future__ import annotations

from collections.abc import Sequence

from alpaca.data.live import StockDataStream

from .config import Settings, load_settings


def build_stock_stream(settings: Settings | None = None) -> StockDataStream:
    settings = settings or load_settings()
    return StockDataStream(
        api_key=settings.api_key,
        secret_key=settings.api_secret,
    )


def stream_trades(symbols: Sequence[str], settings: Settings | None = None) -> None:
    """Subscribe to live trade updates and print them until interrupted.

    Blocks the calling thread. Press Ctrl+C to stop.
    """
    stream = build_stock_stream(settings)

    async def on_trade(trade) -> None:
        print(f"{trade.timestamp}  {trade.symbol}  ${trade.price}  x{trade.size}")

    stream.subscribe_trades(on_trade, *symbols)
    try:
        stream.run()
    except KeyboardInterrupt:
        print("\nStopping stream...")
        stream.stop()
