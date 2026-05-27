from __future__ import annotations

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.models import Order
from alpaca.trading.requests import (
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
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


def list_open_orders(client: TradingClient) -> list[Order]:
    from alpaca.trading.enums import QueryOrderStatus

    request = GetOrdersRequest(status=QueryOrderStatus.OPEN)
    return client.get_orders(request)


def cancel_all_orders(client: TradingClient):
    return client.cancel_orders()
