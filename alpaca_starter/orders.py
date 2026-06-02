from __future__ import annotations

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
from alpaca.trading.models import Order
from alpaca.trading.requests import (
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    StopLossRequest,
    StopOrderRequest,
    TakeProfitRequest,
    TrailingStopOrderRequest,
)


def submit_market_order(
    client: TradingClient,
    symbol: str,
    qty: float,
    side: OrderSide = OrderSide.BUY,
    time_in_force: TimeInForce = TimeInForce.DAY,
) -> Order:
    request = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=side,
        time_in_force=time_in_force,
    )
    return client.submit_order(request)


def submit_limit_order(
    client: TradingClient,
    symbol: str,
    qty: float,
    limit_price: float,
    side: OrderSide = OrderSide.BUY,
    time_in_force: TimeInForce = TimeInForce.DAY,
) -> Order:
    request = LimitOrderRequest(
        symbol=symbol,
        qty=qty,
        side=side,
        time_in_force=time_in_force,
        limit_price=limit_price,
    )
    return client.submit_order(request)


def submit_stop_order(
    client: TradingClient,
    symbol: str,
    qty: float,
    stop_price: float,
    side: OrderSide = OrderSide.SELL,
    time_in_force: TimeInForce = TimeInForce.DAY,
) -> Order:
    """A standalone stop order — typically a protective sell on an open position."""
    request = StopOrderRequest(
        symbol=symbol,
        qty=qty,
        side=side,
        time_in_force=time_in_force,
        stop_price=stop_price,
    )
    return client.submit_order(request)


def submit_trailing_stop_order(
    client: TradingClient,
    symbol: str,
    qty: float,
    trail_percent: float | None = None,
    trail_price: float | None = None,
    side: OrderSide = OrderSide.SELL,
    time_in_force: TimeInForce = TimeInForce.DAY,
) -> Order:
    """Trailing stop. Provide exactly one of trail_percent or trail_price."""
    if (trail_percent is None) == (trail_price is None):
        raise ValueError("Provide exactly one of trail_percent or trail_price.")
    request = TrailingStopOrderRequest(
        symbol=symbol,
        qty=qty,
        side=side,
        time_in_force=time_in_force,
        trail_percent=trail_percent,
        trail_price=trail_price,
    )
    return client.submit_order(request)


def submit_bracket_order(
    client: TradingClient,
    symbol: str,
    qty: float,
    take_profit_price: float,
    stop_loss_price: float,
    stop_loss_limit_price: float | None = None,
    side: OrderSide = OrderSide.BUY,
    time_in_force: TimeInForce = TimeInForce.GTC,
) -> Order:
    """Entry market order bracketed by a take-profit and a stop-loss leg.

    If stop_loss_limit_price is given the protective leg is a stop-limit,
    otherwise a plain stop.
    """
    request = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=side,
        time_in_force=time_in_force,
        order_class=OrderClass.BRACKET,
        take_profit=TakeProfitRequest(limit_price=take_profit_price),
        stop_loss=StopLossRequest(
            stop_price=stop_loss_price,
            limit_price=stop_loss_limit_price,
        ),
    )
    return client.submit_order(request)


def submit_option_market_order(
    client: TradingClient,
    option_symbol: str,
    qty: float,
    side: OrderSide,
    time_in_force: TimeInForce = TimeInForce.DAY,
    client_order_id: str | None = None,
) -> Order:
    """Market order on an OCC option symbol (e.g. ``PLTR250620P00040000``).

    Defaults to ``DAY`` because Alpaca REJECTS option market orders with any
    ``time_in_force`` other than DAY. (This is an Alpaca-side rule, not a
    Pythonic preference — submitting GTC + market on options returns 422.)
    Use ``submit_option_limit_order`` (GTC-capable) for off-hours queueing.
    """
    request = MarketOrderRequest(
        symbol=option_symbol,
        qty=qty,
        side=side,
        time_in_force=time_in_force,
        client_order_id=client_order_id,
    )
    return client.submit_order(request)


def submit_option_limit_order(
    client: TradingClient,
    option_symbol: str,
    qty: float,
    side: OrderSide,
    limit_price: float,
    time_in_force: TimeInForce = TimeInForce.GTC,
    client_order_id: str | None = None,
) -> Order:
    """Limit order on an OCC option symbol. See ``submit_option_market_order``
    for the rationale on GTC being the default time-in-force."""
    request = LimitOrderRequest(
        symbol=option_symbol,
        qty=qty,
        side=side,
        time_in_force=time_in_force,
        limit_price=limit_price,
        client_order_id=client_order_id,
    )
    return client.submit_order(request)


def list_open_orders(client: TradingClient) -> list[Order]:
    from alpaca.trading.enums import QueryOrderStatus

    request = GetOrdersRequest(status=QueryOrderStatus.OPEN)
    return client.get_orders(request)


def cancel_all_orders(client: TradingClient):
    return client.cancel_orders()
