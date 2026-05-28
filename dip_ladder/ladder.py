from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, LimitOrderRequest

from .config import LADDER_CLIENT_ID_PREFIX, LadderConfig, PlannedRung, plan_ladder

log = logging.getLogger("dip_ladder")


@dataclass(frozen=True)
class PlacedRung:
    rung: PlannedRung
    action: str  # "submitted" | "skipped" | "error"
    detail: str
    order_id: str | None = None
    client_order_id: str | None = None


def _client_order_id(symbol: str, ref_price: float, idx: int) -> str:
    # Stays under Alpaca's 48-char client_order_id limit. Suffix with ms-time
    # so re-running on the same ref price doesn't collide.
    ts = int(time.time() * 1000) % 10_000_000
    return f"{LADDER_CLIENT_ID_PREFIX}-{symbol[:6]}-{int(ref_price)}-{idx}-{ts}"


def place_ladder(
    client: TradingClient,
    symbol: str,
    ref_price: float,
    base_qty: int | None = None,
    base_usd: float | None = None,
    config: LadderConfig | None = None,
    dry_run: bool = False,
) -> list[PlacedRung]:
    """Submit GTC limit buys at each rung. Idempotency is the caller's job:
    if you call this twice with the same ref_price, you get two ladders."""
    config = config or LadderConfig()
    plan = plan_ladder(
        ref_price=ref_price, base_qty=base_qty, base_usd=base_usd, config=config
    )
    placed: list[PlacedRung] = []
    for idx, rung in enumerate(plan):
        coid = _client_order_id(symbol, ref_price, idx)
        if dry_run:
            placed.append(
                PlacedRung(
                    rung=rung,
                    action="skipped",
                    detail=f"dry-run buy {rung.qty} {symbol} @ ${rung.limit_price}",
                    client_order_id=coid,
                )
            )
            continue
        req = LimitOrderRequest(
            symbol=symbol,
            qty=rung.qty,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.GTC,
            limit_price=rung.limit_price,
            client_order_id=coid,
        )
        try:
            order = client.submit_order(req)
        except APIError as exc:
            log.warning(
                "ladder rung -%.0f%% %s: alpaca rejected: %s",
                rung.level_pct * 100,
                symbol,
                exc,
            )
            placed.append(
                PlacedRung(
                    rung=rung,
                    action="error",
                    detail=f"alpaca rejected: {exc}",
                    client_order_id=coid,
                )
            )
            continue
        placed.append(
            PlacedRung(
                rung=rung,
                action="submitted",
                detail=f"GTC buy {rung.qty} {symbol} @ ${rung.limit_price}",
                order_id=str(order.id),
                client_order_id=coid,
            )
        )
    return placed


def list_ladder_orders(client: TradingClient, symbol: str | None = None) -> list:
    """All open orders whose client_order_id was tagged as a ladder rung,
    optionally filtered to one symbol."""
    req = GetOrdersRequest(status=QueryOrderStatus.OPEN)
    orders = client.get_orders(req)
    out = []
    for o in orders:
        coid = getattr(o, "client_order_id", "") or ""
        if not coid.startswith(LADDER_CLIENT_ID_PREFIX + "-"):
            continue
        if symbol and (o.symbol or "").upper() != symbol.upper():
            continue
        out.append(o)
    return out


def cancel_ladder(client: TradingClient, symbol: str | None = None) -> int:
    """Cancel every open ladder order (or just for one symbol). Returns count."""
    targets = list_ladder_orders(client, symbol)
    count = 0
    for o in targets:
        try:
            client.cancel_order_by_id(o.id)
            count += 1
        except APIError as exc:
            log.warning("failed to cancel %s: %s", o.id, exc)
    return count
