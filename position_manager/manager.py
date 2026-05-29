from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetOrdersRequest,
    LimitOrderRequest,
    TrailingStopOrderRequest,
)

from .config import MANAGED_STOP_PREFIX, PositionManagerConfig

log = logging.getLogger("position_manager")


@dataclass(frozen=True)
class ManagedAction:
    symbol: str
    action: str  # "stop-placed" | "stop-exists" | "reentry-placed" | "skipped" | "error"
    detail: str
    order_id: str | None = None


def _coid(kind: str, symbol: str) -> str:
    """Tag managed orders so we can list/cancel only our own."""
    return f"{MANAGED_STOP_PREFIX}-{kind}-{symbol[:6]}-{uuid.uuid4().hex[:8]}"


def _open_orders(client: TradingClient) -> list:
    return client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))


def _symbols_with_open_sell(orders: list, side: OrderSide) -> set[str]:
    """Which symbols already have an open order on the given side?
    Used so we don't stack duplicate stops or duplicate re-entries."""
    out: set[str] = set()
    for o in orders:
        try:
            o_side = OrderSide(str(o.side).split(".")[-1].lower())
        except Exception:  # noqa: BLE001
            continue
        if o_side is side and getattr(o, "symbol", None):
            out.add(o.symbol.upper())
    return out


def _position_qty(position) -> float:
    try:
        return float(position.qty_available or position.qty)
    except (TypeError, ValueError):
        return 0.0


def ensure_trailing_stops(
    client: TradingClient,
    config: PositionManagerConfig,
) -> list[ManagedAction]:
    """For every open long position that lacks an open SELL order, attach a
    trailing-stop SELL at ``config.trail_pct`` below the high-water mark.
    Alpaca trails the price server-side, so this is fire-and-forget."""
    actions: list[ManagedAction] = []
    try:
        positions = client.get_all_positions()
    except APIError as exc:
        log.warning("could not fetch positions: %s", exc)
        return [ManagedAction("-", "error", f"get_all_positions: {exc}")]
    open_orders = _open_orders(client)
    busy = _symbols_with_open_sell(open_orders, OrderSide.SELL)

    for pos in positions:
        symbol = (pos.symbol or "").upper()
        qty = _position_qty(pos)
        if qty <= 0 or not symbol:
            continue
        if symbol in busy:
            actions.append(
                ManagedAction(symbol, "stop-exists", "already has an open SELL")
            )
            continue
        if config.dry_run:
            actions.append(
                ManagedAction(
                    symbol,
                    "skipped",
                    f"dry-run trailing-stop {qty} {symbol} @ {config.trail_pct}%",
                )
            )
            continue
        req = TrailingStopOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC,
            trail_percent=config.trail_pct,
            client_order_id=_coid("stop", symbol),
        )
        try:
            order = client.submit_order(req)
        except APIError as exc:
            log.warning("trailing stop for %s rejected: %s", symbol, exc)
            actions.append(ManagedAction(symbol, "error", f"alpaca rejected: {exc}"))
            continue
        actions.append(
            ManagedAction(
                symbol,
                "stop-placed",
                f"trailing-stop {qty} {symbol} @ {config.trail_pct}%",
                order_id=str(order.id),
            )
        )
        # Optional re-entry: a GTC limit BUY parked some % below current price.
        if config.reentry_pct > 0:
            actions.append(_place_reentry(client, pos, open_orders, config))

    return actions


def _place_reentry(
    client: TradingClient,
    position,
    open_orders: list,
    config: PositionManagerConfig,
) -> ManagedAction:
    symbol = position.symbol.upper()
    busy_buys = _symbols_with_open_sell(open_orders, OrderSide.BUY)
    if symbol in busy_buys:
        return ManagedAction(symbol, "skipped", "open BUY already exists")
    try:
        current = float(position.current_price or position.avg_entry_price or 0)
    except (TypeError, ValueError):
        current = 0.0
    if current <= 0:
        return ManagedAction(symbol, "skipped", "no usable price for re-entry")
    bid_price = round(current * (1 - config.reentry_pct / 100.0), 2)
    qty = max(1, int(config.reentry_usd // bid_price))
    if config.dry_run:
        return ManagedAction(
            symbol,
            "skipped",
            f"dry-run reentry buy {qty} {symbol} @ ${bid_price}",
        )
    req = LimitOrderRequest(
        symbol=symbol,
        qty=qty,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.GTC,
        limit_price=bid_price,
        client_order_id=_coid("reentry", symbol),
    )
    try:
        order = client.submit_order(req)
    except APIError as exc:
        log.warning("re-entry for %s rejected: %s", symbol, exc)
        return ManagedAction(symbol, "error", f"alpaca rejected: {exc}")
    return ManagedAction(
        symbol,
        "reentry-placed",
        f"GTC buy {qty} {symbol} @ ${bid_price}",
        order_id=str(order.id),
    )


def list_managed_orders(client: TradingClient) -> list:
    """Open orders we placed (tagged via client_order_id)."""
    out = []
    for o in _open_orders(client):
        coid = getattr(o, "client_order_id", "") or ""
        if coid.startswith(MANAGED_STOP_PREFIX + "-"):
            out.append(o)
    return out


def cancel_managed_orders(
    client: TradingClient, symbol: str | None = None
) -> int:
    """Cancel managed orders, optionally for a single symbol."""
    count = 0
    for o in list_managed_orders(client):
        if symbol and (o.symbol or "").upper() != symbol.upper():
            continue
        try:
            client.cancel_order_by_id(o.id)
            count += 1
        except APIError as exc:
            log.warning("failed to cancel %s: %s", o.id, exc)
    return count
