from __future__ import annotations

from alpaca.trading.client import TradingClient
from alpaca.trading.models import ClosePositionResponse, Order, Position
from alpaca.trading.requests import ClosePositionRequest


def list_positions(client: TradingClient) -> list[Position]:
    return client.get_all_positions()


def get_position(client: TradingClient, symbol: str) -> Position:
    return client.get_open_position(symbol)


def position_summary(client: TradingClient) -> list[dict[str, str]]:
    """A compact, human-readable view of all open positions."""
    rows = []
    for p in client.get_all_positions():
        rows.append(
            {
                "symbol": p.symbol,
                "side": str(p.side),
                "qty": p.qty,
                "avg_entry_price": p.avg_entry_price,
                "current_price": p.current_price,
                "market_value": p.market_value,
                "unrealized_pl": p.unrealized_pl,
                "unrealized_plpc": p.unrealized_plpc,
            }
        )
    return rows


def close_position(
    client: TradingClient,
    symbol: str,
    qty: float | None = None,
    percentage: float | None = None,
) -> Order:
    """Close a position fully, or partially by qty or percentage (not both)."""
    if qty is not None and percentage is not None:
        raise ValueError("Provide at most one of qty or percentage.")
    options = None
    if qty is not None:
        options = ClosePositionRequest(qty=str(qty))
    elif percentage is not None:
        options = ClosePositionRequest(percentage=str(percentage))
    return client.close_position(symbol, close_options=options)


def close_all_positions(
    client: TradingClient,
    cancel_orders: bool = True,
) -> list[ClosePositionResponse]:
    return client.close_all_positions(cancel_orders=cancel_orders)
