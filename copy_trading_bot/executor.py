from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Protocol

from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

from dip_ladder import LadderConfig, place_ladder

from .config import CopyTraderConfig
from .models import PoliticianTrade

log = logging.getLogger("copy_trading_bot.executor")


class QuoteSource(Protocol):
    def latest_price(self, symbol: str) -> float | None:  # pragma: no cover - Protocol
        ...


@dataclass(frozen=True)
class ExecutionResult:
    trade: PoliticianTrade
    action: str  # "submitted" | "skipped" | "error"
    detail: str
    order_id: str | None = None


def _safe_get_position_qty(client: TradingClient, symbol: str) -> float:
    """Return 0 only when Alpaca explicitly says we have no position (404).
    Any other error (rate limit, 5xx, timeout, auth) must propagate — silently
    treating those as "no position" would mark the sell processed and never
    retry, leaving us long a stock the politician already exited."""
    try:
        position = client.get_open_position(symbol)
    except APIError as exc:
        if getattr(exc, "status_code", None) == 404:
            return 0.0
        raise
    try:
        return float(position.qty_available or position.qty)
    except (TypeError, ValueError):
        return 0.0


def _shares_for_dollars(price: float, dollars: float) -> int:
    if price <= 0 or dollars <= 0:
        return 0
    return int(math.floor(dollars / price))


def execute(
    trade: PoliticianTrade,
    client: TradingClient,
    quotes: QuoteSource,
    config: CopyTraderConfig,
) -> ExecutionResult:
    """Translate a single disclosed politician trade into an Alpaca order.

    Stocks only — non-US tickers are filtered upstream by the scraper.
    Buys size by ``config.trade_usd`` (capped at ``max_trade_usd``).
    Sells close any existing long position; if we hold nothing we skip
    rather than short.
    """
    symbol = trade.ticker
    side = OrderSide.BUY if trade.tx_type == "buy" else OrderSide.SELL

    if side is OrderSide.SELL:
        qty = _safe_get_position_qty(client, symbol)
        if qty <= 0:
            return ExecutionResult(trade, "skipped", f"no open position in {symbol}")
        return _submit(client, symbol, qty, side, trade, config, reason="mirror-sell")

    # BUY: size by config dollars at the current quoted price.
    price = quotes.latest_price(symbol)
    if price is None or price <= 0:
        # Fall back to the disclosed reference price if no quote (e.g. after hours).
        price = trade.price or 0.0
    if price <= 0:
        return ExecutionResult(trade, "skipped", f"no usable price for {symbol}")

    budget = min(config.trade_usd, config.max_trade_usd)
    qty = _shares_for_dollars(price, budget)
    if qty <= 0:
        return ExecutionResult(
            trade, "skipped", f"{symbol} @ ${price:.2f} > budget ${budget:.0f}"
        )
    result = _submit(client, symbol, qty, side, trade, config, reason="mirror-buy")
    if result.action == "submitted" and config.enable_ladder:
        _drop_ladder_under(client, symbol, ref_price=price, config=config)
    return result


def _drop_ladder_under(
    client: TradingClient,
    symbol: str,
    ref_price: float,
    config: CopyTraderConfig,
) -> None:
    """Drop a dip-ladder under a fresh mirror-buy. Must not propagate: the
    mirror-buy already succeeded, and if we raise the caller never records
    the trade in state — the next cron run would re-buy."""
    ladder_cfg = LadderConfig(max_total_usd=config.ladder_max_total_usd)
    try:
        placed = place_ladder(
            client=client,
            symbol=symbol,
            ref_price=ref_price,
            base_usd=config.trade_usd,
            config=ladder_cfg,
            dry_run=config.dry_run,
        )
    except Exception as exc:  # noqa: BLE001 - intentionally broad; see docstring
        log.warning("ladder failed for %s: %s", symbol, exc)
        return
    for p in placed:
        log.info(
            "LADDER %-9s -%.0f%% %s",
            p.action.upper(),
            p.rung.level_pct * 100,
            p.detail,
        )


def _submit(
    client: TradingClient,
    symbol: str,
    qty: float,
    side: OrderSide,
    trade: PoliticianTrade,
    config: CopyTraderConfig,
    reason: str,
) -> ExecutionResult:
    if config.dry_run:
        return ExecutionResult(
            trade, "skipped", f"dry-run {side.value} {qty} {symbol} ({reason})"
        )
    request = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=side,
        time_in_force=TimeInForce.DAY,
    )
    try:
        order = client.submit_order(request)
    except APIError as exc:
        log.warning("alpaca rejected %s %s %s: %s", side.value, qty, symbol, exc)
        return ExecutionResult(trade, "error", f"alpaca rejected: {exc}")
    return ExecutionResult(
        trade,
        "submitted",
        f"{side.value} {qty} {symbol} ({reason})",
        order_id=str(order.id),
    )
