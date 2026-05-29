from __future__ import annotations

import logging
from dataclasses import dataclass

from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient

from alpaca_starter import list_tagged

from .config import WHEEL_PREFIX
from .state import Stage, TickerState

log = logging.getLogger("wheel_bot.reconciler")


@dataclass(frozen=True)
class ReconcileResult:
    """Outcome of comparing stored state against live Alpaca state for a ticker.

    ``transition`` describes what happened (assigned, expired, called_away,
    adopted, external_close, none). ``detail`` is a human-readable note.
    """

    ticker: str
    transition: str
    detail: str


def _safe_get_position(client: TradingClient, symbol: str):
    """Return the Position or None when Alpaca says we have no shares (404/422).

    Per the project-wide rule (mirrored from copy_trading_bot.executor):
    silently swallowing other APIErrors would mark the cycle "no position"
    and replay STOs we already executed, so re-raise anything that isn't a
    clear "no such position" signal.
    """
    try:
        return client.get_open_position(symbol)
    except APIError as exc:
        status = getattr(exc, "status_code", None)
        if status in (404, 422):
            return None
        raise


def _position_qty(position) -> int:
    if position is None:
        return 0
    try:
        return int(float(position.qty_available or position.qty))
    except (TypeError, ValueError):
        return 0


def _avg_entry(position) -> float:
    if position is None:
        return 0.0
    try:
        return float(position.avg_entry_price)
    except (TypeError, ValueError):
        return 0.0


def _open_wheel_options_for(client: TradingClient, ticker: str) -> list:
    """Open wheel-tagged orders whose underlying is ``ticker``.

    The tagged-orders helper filters by ``client_order_id`` prefix; we further
    filter to the ticker by matching the embedded uppercase symbol segment
    (e.g. ``wheel-PLTR-put-...``). The order's ``symbol`` field is the OCC
    option symbol, not the underlying, so we can't filter by it directly.
    """
    needle = f"{WHEEL_PREFIX}-{ticker.upper()}-"
    return [
        o
        for o in list_tagged(client, prefix=WHEEL_PREFIX)
        if (getattr(o, "client_order_id", "") or "").startswith(needle)
    ]


def reconcile_ticker(
    client: TradingClient,
    state: TickerState,
) -> ReconcileResult:
    """Compare stored state against Alpaca and mutate ``state`` in place.

    State machine transitions (each fires exactly when both the stored stage
    and the observed Alpaca state match):

    * PUT_OPEN  + option gone + equity exists  -> ASSIGNED   -> HOLDING
    * PUT_OPEN  + option gone + no equity      -> EXPIRED    -> IDLE
    * CALL_OPEN + option gone + equity exists  -> EXPIRED    -> HOLDING
    * CALL_OPEN + option gone + no equity      -> CALLED_AWAY-> IDLE
    * IDLE      + equity exists                -> ADOPT      -> HOLDING
    * HOLDING   + no equity                    -> EXT_CLOSE  -> IDLE
    """
    ticker = state.ticker.upper()
    open_options = _open_wheel_options_for(client, ticker)
    position = _safe_get_position(client, ticker)
    qty = _position_qty(position)

    if state.stage == Stage.PUT_OPEN and not open_options:
        if qty > 0:
            return _on_put_assigned(state, position)
        return _on_put_expired(state)

    if state.stage == Stage.CALL_OPEN and not open_options:
        if qty > 0:
            return _on_call_expired_worthless(state)
        return _on_called_away(state)

    if state.stage == Stage.IDLE and qty > 0:
        return _on_adopt(state, position)

    if state.stage == Stage.HOLDING and qty == 0:
        return _on_external_close(state)

    return ReconcileResult(ticker, "none", "no change")


def _on_put_assigned(state: TickerState, position) -> ReconcileResult:
    contract = state.active_contract or {}
    strike = float(contract.get("strike", 0.0))
    premium_per_share = float(contract.get("premium_received", 0.0))
    contracts = int(contract.get("contracts", 1))
    shares = contracts * 100
    # Effective basis after applying the put premium received. We reset the
    # per-lot call-premium counter — that bucket only tracks call premium
    # collected against this lot.
    if shares > 0:
        new_basis = strike - premium_per_share
    else:
        new_basis = _avg_entry(position)
    state.stage = Stage.HOLDING
    state.shares_held = shares
    state.avg_basis_per_share = round(new_basis, 4)
    state.cumulative_premium_this_lot = 0.0
    state.active_contract = None
    return ReconcileResult(
        state.ticker,
        "assigned",
        f"PUT assigned: {contracts}x100 @ {strike} - {premium_per_share}/sh prem"
        f" -> basis ${state.avg_basis_per_share}",
    )


def _on_put_expired(state: TickerState) -> ReconcileResult:
    contract = state.active_contract or {}
    premium_per_share = float(contract.get("premium_received", 0.0))
    contracts = int(contract.get("contracts", 1))
    state.cumulative_premium_lifetime += premium_per_share * 100 * contracts
    state.cycles.append(
        {
            "kind": "put_expired",
            "contract": contract,
            "premium_kept": round(premium_per_share * 100 * contracts, 4),
        }
    )
    state.stage = Stage.IDLE
    state.active_contract = None
    return ReconcileResult(
        state.ticker,
        "expired_put",
        f"PUT expired worthless, kept ${premium_per_share * 100 * contracts:.2f}",
    )


def _on_call_expired_worthless(state: TickerState) -> ReconcileResult:
    contract = state.active_contract or {}
    premium_per_share = float(contract.get("premium_received", 0.0))
    state.cumulative_premium_this_lot += premium_per_share
    state.cumulative_premium_lifetime += premium_per_share * state.shares_held
    state.active_contract = None
    state.stage = Stage.HOLDING
    return ReconcileResult(
        state.ticker,
        "expired_call",
        f"CALL expired worthless, kept ${premium_per_share * state.shares_held:.2f}"
        f" (lot prem so far ${state.cumulative_premium_this_lot * state.shares_held:.2f})",
    )


def _on_called_away(state: TickerState) -> ReconcileResult:
    contract = state.active_contract or {}
    strike = float(contract.get("strike", 0.0))
    premium_per_share = float(contract.get("premium_received", 0.0))
    state.cumulative_premium_this_lot += premium_per_share
    state.cumulative_premium_lifetime += premium_per_share * state.shares_held
    # Realized P&L on the lot: (strike - basis + lot_premium) * shares
    pnl = (
        strike
        - state.avg_basis_per_share
        + state.cumulative_premium_this_lot
    ) * state.shares_held
    state.cycles.append(
        {
            "kind": "called_away",
            "contract": contract,
            "basis": state.avg_basis_per_share,
            "strike": strike,
            "shares": state.shares_held,
            "lot_pnl": round(pnl, 4),
            "lot_call_premium": round(
                state.cumulative_premium_this_lot * state.shares_held, 4
            ),
        }
    )
    state.stage = Stage.IDLE
    state.active_contract = None
    state.shares_held = 0
    state.avg_basis_per_share = 0.0
    state.cumulative_premium_this_lot = 0.0
    return ReconcileResult(
        state.ticker,
        "called_away",
        f"CALL assigned at {strike}, lot P&L ${pnl:.2f}",
    )


def _on_adopt(state: TickerState, position) -> ReconcileResult:
    qty = _position_qty(position)
    basis = _avg_entry(position)
    state.stage = Stage.HOLDING
    state.shares_held = qty
    state.avg_basis_per_share = round(basis, 4)
    state.cumulative_premium_this_lot = 0.0
    log.warning(
        "adopting pre-existing %s position: %d shares @ basis $%.4f. "
        "Use `wheel_bot adopt %s --basis X` to correct if this is wrong.",
        state.ticker,
        qty,
        basis,
        state.ticker,
    )
    return ReconcileResult(
        state.ticker,
        "adopted",
        f"adopted {qty} shares @ ${basis:.4f}",
    )


def _on_external_close(state: TickerState) -> ReconcileResult:
    log.warning(
        "%s shows HOLDING in state but no equity at Alpaca; resetting to IDLE",
        state.ticker,
    )
    state.cycles.append(
        {
            "kind": "external_close",
            "basis": state.avg_basis_per_share,
            "shares_at_state": state.shares_held,
        }
    )
    state.stage = Stage.IDLE
    state.shares_held = 0
    state.avg_basis_per_share = 0.0
    state.cumulative_premium_this_lot = 0.0
    state.active_contract = None
    return ReconcileResult(state.ticker, "external_close", "external close detected")
