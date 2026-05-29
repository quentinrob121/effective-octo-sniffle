from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest

from alpaca_starter import make_coid
from alpaca_starter.orders import (
    submit_option_limit_order,
    submit_option_market_order,
)

from .config import WHEEL_PREFIX, WheelConfig
from .option_picker import OptionCandidate
from .state import Stage, TickerState

log = logging.getLogger("wheel_bot.executor")


@dataclass(frozen=True)
class ExecAction:
    ticker: str
    action: str  # "sto_put" | "sto_call" | "btc" | "skipped" | "error" | "halted"
    detail: str
    order_id: str | None = None
    client_order_id: str | None = None


# ---------------------------------------------------------------------------
# Cash-secured guard
# ---------------------------------------------------------------------------

def _is_put_sell(order) -> bool:
    """Heuristic: an option SELL order whose OCC symbol marks it as a put.

    Used so we can subtract collateral reserved by puts we didn't tag (e.g.
    legacy positions opened by hand or by another tool). The check is
    structural — we don't trust asset_class because some SDK responses omit
    it; the OCC symbol shape is the authoritative signal.
    """
    try:
        side = OrderSide(str(order.side).split(".")[-1].lower())
    except Exception:  # noqa: BLE001
        return False
    if side is not OrderSide.SELL:
        return False
    symbol = getattr(order, "symbol", "") or ""
    if len(symbol) < 15:
        return False
    # OCC format: ROOT(YYMMDD)(C|P)(STRIKE8). The C/P byte is at index -9 of
    # the full symbol. We check that exact byte (not "P anywhere in the tail")
    # so 'PLTR' calls aren't misread as puts.
    return symbol[-9] == "P"


def _put_collateral(order, fallback_strike: float) -> float:
    """Collateral reserved by an open put SELL: strike * 100 * qty.

    The strike comes from the OCC symbol's trailing 8-digit
    strike-in-thousandths field — that's the authoritative source.
    ``limit_price`` on a SELL is the *premium per share*, not the strike,
    so we deliberately don't fall back to it. Worst case (unparseable
    symbol) we use ``fallback_strike``, which over-estimates collateral —
    the safe direction.
    """
    sym = getattr(order, "symbol", "") or ""
    strike: float
    if len(sym) >= 15:
        try:
            strike = int(sym[-8:]) / 1000.0
        except ValueError:
            strike = fallback_strike
    else:
        strike = fallback_strike
    qty = 1
    try:
        qty = int(float(order.qty))
    except (TypeError, ValueError):
        qty = 1
    return strike * 100 * qty


def has_cash_for_put(
    client: TradingClient,
    strike: float,
    contracts: int,
) -> tuple[bool, float, float]:
    """Return ``(ok, available_after_reserves, needed)``.

    ``ok`` is True iff our options buying power minus collateral already
    reserved by every open put SELL (wheel-tagged OR foreign) covers the
    proposed put's collateral.
    """
    needed = strike * 100 * contracts
    try:
        account = client.get_account()
        options_bp = float(
            getattr(account, "options_buying_power", None)
            or getattr(account, "buying_power", 0.0)
        )
    except APIError as exc:
        log.warning("get_account failed: %s; refusing to STO put", exc)
        return False, 0.0, needed
    open_orders = client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
    reserved = sum(
        _put_collateral(o, fallback_strike=strike) for o in open_orders if _is_put_sell(o)
    )
    available = options_bp - reserved
    return available >= needed, available, needed


# ---------------------------------------------------------------------------
# STO put
# ---------------------------------------------------------------------------

def sto_put(
    client: TradingClient,
    state: TickerState,
    candidate: OptionCandidate,
    config: WheelConfig,
    save: callable | None = None,
) -> ExecAction:
    """Sell-to-open a cash-secured put. Persists state on success.

    Refuses if the bid is below the configured minimum premium, or if
    options buying-power minus already-reserved collateral can't cover this
    put's notional.
    """
    if state.stage != Stage.IDLE:
        return ExecAction(state.ticker, "skipped", f"stage={state.stage.value}, not IDLE")
    if not state.enabled:
        return ExecAction(state.ticker, "skipped", "ticker disabled")

    bid = candidate.bid
    # Order matters: halted (bid<=0) first so we don't mis-classify a frozen
    # chain as "low premium". Dry-run check goes before the cash guard so a
    # preview run without funded options BP still shows what would happen.
    if bid <= 0:
        return ExecAction(state.ticker, "halted", "PUT bid=0; chain likely halted/illiquid")
    if bid * 100 < config.min_premium_usd:
        return ExecAction(
            state.ticker,
            "skipped",
            f"PUT bid ${bid:.2f} below min premium ${config.min_premium_usd/100:.2f}/sh",
        )

    coid = make_coid(WHEEL_PREFIX, state.ticker, "put")
    if config.dry_run:
        log.info(
            "DRY-RUN STO put %s %s strike=%s prem=%.2f coid=%s",
            state.ticker,
            candidate.symbol,
            candidate.strike,
            bid,
            coid,
        )
        return ExecAction(
            state.ticker,
            "skipped",
            f"dry-run STO put {candidate.symbol} @ ${bid:.2f}",
            client_order_id=coid,
        )

    ok, available, needed = has_cash_for_put(
        client, candidate.strike, config.contracts_per_cycle
    )
    if not ok:
        return ExecAction(
            state.ticker,
            "skipped",
            f"insufficient options BP: need ${needed:.2f}, have ${available:.2f}",
        )

    try:
        order = submit_option_limit_order(
            client,
            option_symbol=candidate.symbol,
            qty=config.contracts_per_cycle,
            side=OrderSide.SELL,
            limit_price=round(bid, 2),
            time_in_force=TimeInForce.GTC,
            client_order_id=coid,
        )
    except APIError as exc:
        log.warning("STO put rejected for %s: %s", state.ticker, exc)
        return ExecAction(state.ticker, "error", f"alpaca rejected: {exc}")

    state.stage = Stage.PUT_OPEN
    state.active_contract = {
        "symbol": candidate.symbol,
        "type": "put",
        "strike": candidate.strike,
        "expiration": candidate.expiration.isoformat(),
        "premium_received": round(bid, 4),
        "contracts": config.contracts_per_cycle,
        "order_id": str(order.id),
        "client_order_id": coid,
    }
    if save is not None:
        save()
    return ExecAction(
        state.ticker,
        "sto_put",
        f"STO PUT {candidate.symbol} @ ${bid:.2f}",
        order_id=str(order.id),
        client_order_id=coid,
    )


# ---------------------------------------------------------------------------
# STO call (covered)
# ---------------------------------------------------------------------------

def effective_basis_per_share(state: TickerState) -> float:
    """Basis with all call premium collected against this lot already netted.

    This is the *floor* for any covered-call strike: selling a call at or
    above this number guarantees the lot can't be called away for less than
    the realized cost.
    """
    if state.shares_held <= 0:
        return state.avg_basis_per_share
    return state.avg_basis_per_share - state.cumulative_premium_this_lot


def sto_call(
    client: TradingClient,
    state: TickerState,
    candidate: OptionCandidate,
    config: WheelConfig,
    save: callable | None = None,
) -> ExecAction:
    """Sell-to-open a covered call against an existing lot. Persists state on success.

    Refuses if the strike is below the effective basis per share (cost minus
    cumulative call premium against this lot) — selling below basis locks
    in a loss the wheel is specifically designed to avoid.
    """
    if state.stage != Stage.HOLDING:
        return ExecAction(state.ticker, "skipped", f"stage={state.stage.value}, not HOLDING")
    if not state.enabled:
        return ExecAction(state.ticker, "skipped", "ticker disabled")
    if state.shares_held < 100:
        return ExecAction(
            state.ticker,
            "skipped",
            f"only {state.shares_held} shares; need 100 to cover a call",
        )

    floor = effective_basis_per_share(state)
    if candidate.strike < floor:
        return ExecAction(
            state.ticker,
            "skipped",
            f"CALL strike ${candidate.strike} < effective basis ${floor:.2f}; will not sell below basis",
        )
    bid = candidate.bid
    if bid * 100 < config.min_premium_usd:
        return ExecAction(
            state.ticker,
            "skipped",
            f"CALL bid ${bid:.2f} below min premium",
        )
    if bid <= 0:
        return ExecAction(state.ticker, "halted", "CALL bid=0; chain halted/illiquid")

    coid = make_coid(WHEEL_PREFIX, state.ticker, "call")
    contracts = min(state.shares_held // 100, config.contracts_per_cycle)
    if config.dry_run:
        return ExecAction(
            state.ticker,
            "skipped",
            f"dry-run STO call {candidate.symbol} x{contracts} @ ${bid:.2f}",
            client_order_id=coid,
        )

    try:
        order = submit_option_limit_order(
            client,
            option_symbol=candidate.symbol,
            qty=contracts,
            side=OrderSide.SELL,
            limit_price=round(bid, 2),
            time_in_force=TimeInForce.GTC,
            client_order_id=coid,
        )
    except APIError as exc:
        log.warning("STO call rejected for %s: %s", state.ticker, exc)
        return ExecAction(state.ticker, "error", f"alpaca rejected: {exc}")

    state.stage = Stage.CALL_OPEN
    state.active_contract = {
        "symbol": candidate.symbol,
        "type": "call",
        "strike": candidate.strike,
        "expiration": candidate.expiration.isoformat(),
        "premium_received": round(bid, 4),
        "contracts": contracts,
        "order_id": str(order.id),
        "client_order_id": coid,
    }
    if save is not None:
        save()
    return ExecAction(
        state.ticker,
        "sto_call",
        f"STO CALL {candidate.symbol} @ ${bid:.2f}",
        order_id=str(order.id),
        client_order_id=coid,
    )


# ---------------------------------------------------------------------------
# BTC (close to capture profit)
# ---------------------------------------------------------------------------

def should_close(
    state: TickerState,
    current_ask: float,
    today: date,
    config: WheelConfig,
) -> tuple[bool, str]:
    """Decide whether to BTC the open short option to lock in profit.

    Closes when the contract has decayed to ``close_profit_pct``% (so we buy
    back for that fraction of the original premium received). Skips when
    days-to-expiration is at-or-below ``close_min_dte`` — at that point
    pin-risk dominates and we'd rather just let it expire.
    """
    contract = state.active_contract or {}
    premium = float(contract.get("premium_received", 0.0))
    if premium <= 0:
        return False, "no premium on record"
    exp_str = contract.get("expiration")
    if not exp_str:
        return False, "no expiration on record"
    try:
        exp = date.fromisoformat(exp_str)
    except ValueError:
        return False, f"unparseable expiration {exp_str!r}"
    dte = (exp - today).days
    if dte <= config.close_min_dte:
        return False, f"DTE={dte} <= close_min_dte={config.close_min_dte}, let it ride"
    # Close when current ask <= premium * (1 - close_profit_pct/100). i.e. we
    # can buy it back for the configured remaining fraction.
    target_buyback = premium * (1 - config.close_profit_pct / 100.0)
    if current_ask <= target_buyback and current_ask >= 0:
        return True, (
            f"BTC: ask ${current_ask:.2f} <= target ${target_buyback:.2f} "
            f"({config.close_profit_pct:.0f}% decayed), DTE={dte}"
        )
    return False, (
        f"hold: ask ${current_ask:.2f} > target ${target_buyback:.2f}, DTE={dte}"
    )


def btc(
    client: TradingClient,
    state: TickerState,
    current_ask: float,
    config: WheelConfig,
    save: callable | None = None,
) -> ExecAction:
    """Buy-to-close the currently open short option for ``state``.

    Updates state to the next legal stage on success (PUT_OPEN -> IDLE,
    CALL_OPEN -> HOLDING). On Alpaca rejection, state is left untouched so
    the next cron tick can retry.
    """
    if state.stage not in (Stage.PUT_OPEN, Stage.CALL_OPEN):
        return ExecAction(state.ticker, "skipped", f"stage={state.stage.value}, nothing to close")
    contract = state.active_contract or {}
    symbol = contract.get("symbol")
    if not symbol:
        return ExecAction(state.ticker, "skipped", "no active contract symbol on record")
    contracts = int(contract.get("contracts", 1))
    premium_per_share = float(contract.get("premium_received", 0.0))

    coid = make_coid(WHEEL_PREFIX, state.ticker, "btc")
    if config.dry_run:
        return ExecAction(
            state.ticker,
            "skipped",
            f"dry-run BTC {symbol} @ ask ${current_ask:.2f}",
            client_order_id=coid,
        )

    try:
        # Use market: at this point we've already decided closing is profitable,
        # and parking a GTC limit at ask risks the option expiring unfilled.
        order = submit_option_market_order(
            client,
            option_symbol=symbol,
            qty=contracts,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.GTC,
            client_order_id=coid,
        )
    except APIError as exc:
        log.warning("BTC rejected for %s: %s", state.ticker, exc)
        # CRITICAL: leave state untouched so the next run retries from the
        # same stage. The active_contract is still valid.
        return ExecAction(state.ticker, "error", f"alpaca rejected: {exc}")

    realized_premium = (premium_per_share - current_ask) * 100 * contracts
    state.cumulative_premium_lifetime += realized_premium
    if state.stage == Stage.CALL_OPEN:
        # Call premium net of buy-back ticks the lot premium counter up.
        state.cumulative_premium_this_lot += (premium_per_share - current_ask)
        state.stage = Stage.HOLDING
    else:  # PUT_OPEN
        state.cycles.append(
            {
                "kind": "put_closed_early",
                "contract": contract,
                "btc_ask": current_ask,
                "premium_net": round(realized_premium, 4),
            }
        )
        state.stage = Stage.IDLE
    state.active_contract = None
    if save is not None:
        save()
    return ExecAction(
        state.ticker,
        "btc",
        f"BTC {symbol} @ ${current_ask:.2f}; lot premium realized ${realized_premium:.2f}",
        order_id=str(order.id),
        client_order_id=coid,
    )
