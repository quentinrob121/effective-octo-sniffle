from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone

from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient

from alpaca_starter import list_tagged

from .config import WHEEL_PREFIX
from .state import (
    Stage,
    TickerState,
    WheelState,
    clear_intent,
    load_pending_intents,
)

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

    NOTE: this only returns OPEN orders. A filled STO leaves the OPEN queue
    immediately but the short option lives on in get_all_positions until
    expiration/assignment. Use ``_short_option_alive`` for that check.
    """
    needle = f"{WHEEL_PREFIX}-{ticker.upper()}-"
    return [
        o
        for o in list_tagged(client, prefix=WHEEL_PREFIX)
        if (getattr(o, "client_order_id", "") or "").startswith(needle)
    ]


def _short_option_alive(client: TradingClient, occ_symbol: str) -> bool:
    """True iff Alpaca currently shows a short (negative qty) position in
    ``occ_symbol``.

    A short option that has been STO'd and filled won't appear in OPEN orders
    but WILL appear in positions with a negative quantity. We rely on
    ``get_all_positions`` since per-symbol option lookup via
    ``get_open_position`` is unreliable for some OCC symbols.
    """
    try:
        positions = client.get_all_positions()
    except APIError as exc:
        # If we can't see positions, fail closed: pretend the option is alive
        # so we don't fire a bogus expired/assigned transition. The next run
        # will reconcile when the API is back.
        log.warning("get_all_positions failed: %s; treating short option as alive", exc)
        return True
    for p in positions or []:
        if (getattr(p, "symbol", "") or "") != occ_symbol:
            continue
        try:
            q = float(getattr(p, "qty", 0) or 0)
        except (TypeError, ValueError):
            q = 0.0
        if q < 0:
            return True
    return False


def _parse_contract_expiration(contract: dict | None) -> date | None:
    """Read ``expiration`` (ISO string) out of an ``active_contract`` blob."""
    if not contract:
        return None
    exp_str = contract.get("expiration")
    if not exp_str:
        return None
    try:
        return date.fromisoformat(exp_str)
    except (TypeError, ValueError):
        return None


def reconcile_ticker(
    client: TradingClient,
    state: TickerState,
    today: date | None = None,
) -> ReconcileResult:
    """Compare stored state against Alpaca and mutate ``state`` in place.

    State machine transitions (each fires exactly when both the stored stage
    and the observed Alpaca state match):

    * PUT_OPEN  + short option still alive     -> NONE       (waiting)
    * PUT_OPEN  + option gone + equity exists  -> ASSIGNED   -> HOLDING
    * PUT_OPEN  + option gone + no equity      -> EXPIRED    -> IDLE (only after expiry date)
    * CALL_OPEN + short option still alive     -> NONE       (waiting)
    * CALL_OPEN + option gone + equity exists  -> EXPIRED    -> HOLDING (only after expiry date)
    * CALL_OPEN + option gone + no equity      -> CALLED_AWAY-> IDLE
    * IDLE      + equity exists                -> ADOPT      -> HOLDING
    * HOLDING   + no equity                    -> EXT_CLOSE  -> IDLE

    The "short option alive" check uses ``get_all_positions`` (not just open
    orders) because a freshly filled STO leaves the OPEN-orders queue
    immediately but the position lives on for weeks. Falsely treating it as
    gone was the bug that produced rapid-fire "expired worthless" + stacked
    re-STOs every cycle.
    """
    if today is None:
        today = datetime.now(timezone.utc).date()
    ticker = state.ticker.upper()
    open_options = _open_wheel_options_for(client, ticker)
    position = _safe_get_position(client, ticker)
    qty = _position_qty(position)

    if state.stage in (Stage.PUT_OPEN, Stage.CALL_OPEN):
        contract = state.active_contract or {}
        occ_symbol = contract.get("symbol")
        expiration = _parse_contract_expiration(contract)

        # If the short option is still alive at Alpaca, we're just waiting.
        if occ_symbol and _short_option_alive(client, occ_symbol):
            return ReconcileResult(ticker, "none", "short option still alive")
        # If there's an open wheel-tagged order for this ticker (working STO
        # not yet filled), nothing has happened yet either.
        if open_options:
            return ReconcileResult(ticker, "none", "open wheel order in progress")

        # The short option has disappeared from both OPEN orders AND positions.
        # Date-based safeguard: only treat that as expired/assigned/called-away
        # AFTER the contract's expiration date. Before that, this is more
        # likely an Alpaca-side anomaly (or a manual cancel that left state
        # stale). Log a warning and wait — don't credit ghost premium.
        if state.stage == Stage.PUT_OPEN:
            if qty > 0:
                # Equity exists where there was none before. Treat as
                # assignment regardless of expiration — the shares are here.
                return _on_put_assigned(state, position)
            if expiration is None or today >= expiration:
                return _on_put_expired(state)
            log.warning(
                "%s PUT %s missing from positions before expiration %s (today=%s); "
                "leaving state untouched, will recheck next cycle",
                ticker,
                occ_symbol,
                expiration,
                today,
            )
            return ReconcileResult(
                ticker, "none", "short put missing pre-expiration; awaiting next cycle"
            )

        # state.stage == Stage.CALL_OPEN
        if qty > 0:
            if expiration is None or today >= expiration:
                return _on_call_expired_worthless(state)
            log.warning(
                "%s CALL %s missing from positions before expiration %s (today=%s); "
                "leaving state untouched, will recheck next cycle",
                ticker,
                occ_symbol,
                expiration,
                today,
            )
            return ReconcileResult(
                ticker, "none", "short call missing pre-expiration; awaiting next cycle"
            )
        return _on_called_away(state)

    if state.stage == Stage.IDLE and qty > 0:
        return _on_adopt(state, position)

    if state.stage == Stage.HOLDING and qty == 0:
        return _on_external_close(state)

    return ReconcileResult(ticker, "none", "no change")


def _on_put_assigned(state: TickerState, position) -> ReconcileResult:
    """Transition PUT_OPEN -> HOLDING using ACTUAL assigned share count.

    Previously this trusted ``contract.contracts * 100`` for shares_held,
    which silently desynced state from reality on partial assignment (e.g. 1
    of 2 contracts filled): state said 200, Alpaca said 100, the next cycle
    would try a covered call against the over-stated lot and Alpaca would
    reject it as naked. We now use ``position.qty`` as the authoritative
    source. The recorded ``contracts`` still informs the *premium* math (we
    received premium on every contract we sold), but only the shares Alpaca
    actually delivered count toward our lot.
    """
    contract = state.active_contract or {}
    strike = float(contract.get("strike", 0.0))
    premium_per_share = float(contract.get("premium_received", 0.0))
    contracts = int(contract.get("contracts", 1))

    actual_shares = _position_qty(position)
    pre_existing_shares = max(state.shares_held, 0)
    new_shares_from_assignment = max(actual_shares - pre_existing_shares, 0)
    # If reconciler is racing the assignment notification, fall back to
    # contracts * 100 so we don't mis-report zero. This is a soft fallback —
    # the next cycle will correct via position.qty if it ever diverges.
    if new_shares_from_assignment <= 0 and actual_shares <= 0:
        new_shares_from_assignment = contracts * 100
        actual_shares = pre_existing_shares + new_shares_from_assignment

    # Per-share basis on the newly-assigned slice = strike - premium received.
    # (We received premium on every contract sold, even if only some assigned;
    # the per-share figure is unchanged.)
    if new_shares_from_assignment > 0:
        assigned_basis_per_share = strike - premium_per_share
    else:
        assigned_basis_per_share = _avg_entry(position) or strike

    # If there's a pre-existing lot, weighted-average the basis.
    if pre_existing_shares > 0 and state.avg_basis_per_share > 0:
        total_basis = (
            state.avg_basis_per_share * pre_existing_shares
            + assigned_basis_per_share * new_shares_from_assignment
        )
        new_basis = total_basis / max(actual_shares, 1)
    else:
        new_basis = assigned_basis_per_share

    state.stage = Stage.HOLDING
    state.shares_held = actual_shares
    state.avg_basis_per_share = round(new_basis, 4)
    state.cumulative_premium_this_lot = 0.0
    state.active_contract = None
    return ReconcileResult(
        state.ticker,
        "assigned",
        f"PUT assigned: {actual_shares} shares @ strike {strike} - {premium_per_share}/sh prem"
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


# ---------------------------------------------------------------------------
# Pending-intent recovery
# ---------------------------------------------------------------------------

def _lookup_order_by_coid(client: TradingClient, client_order_id: str):
    """Return the Alpaca Order with the given ``client_order_id``, or None.

    Uses ``get_order_by_client_id`` (recent alpaca-py releases) when present;
    falls back to scanning recent orders if not. Returns None for 404s
    (order never reached Alpaca) and re-raises anything else so a transient
    blip doesn't drop a pending intent.
    """
    fn = getattr(client, "get_order_by_client_id", None)
    if fn is not None:
        try:
            return fn(client_order_id)
        except APIError as exc:
            status = getattr(exc, "status_code", None)
            if status in (404, 422):
                return None
            raise
    # Fallback: scan recent (all-status) orders.
    try:
        recent = client.get_orders()
    except APIError:
        return None
    for o in recent or []:
        if (getattr(o, "client_order_id", "") or "") == client_order_id:
            return o
    return None


def recover_orphan_intents(client: TradingClient, state: WheelState) -> list[ReconcileResult]:
    """Reconcile pending-intent journal entries into ``state``.

    Called at the TOP of each ``run_once`` cycle, before per-ticker reconcile.
    For each journaled intent we look up the matching order at Alpaca:
      - Found AND state doesn't already reflect it -> apply the intent into
        state (set stage/active_contract) so the next decision sees the truth.
      - Not found -> the submit never made it; drop the intent.
    Either way the intent is cleared from the journal. This is the safety
    net for the submit-then-save crash window.
    """
    results: list[ReconcileResult] = []
    for intent in load_pending_intents():
        coid = intent.get("client_order_id")
        ticker = (intent.get("ticker") or "").upper()
        kind = intent.get("intent")
        if not coid or not ticker or not kind:
            # Malformed entry — drop it so we don't loop on it forever.
            if coid:
                clear_intent(coid)
            continue
        try:
            order = _lookup_order_by_coid(client, coid)
        except APIError as exc:
            log.warning(
                "orphan recovery: lookup for %s failed (%s); leaving in journal",
                coid,
                exc,
            )
            continue
        ts = state.get_or_create(ticker)
        if order is None:
            # Submission never succeeded. Drop the intent — nothing to recover.
            log.warning(
                "orphan recovery: %s/%s not found at Alpaca, dropping intent",
                ticker,
                coid,
            )
            clear_intent(coid)
            results.append(
                ReconcileResult(
                    ticker, "orphan_dropped", f"{kind} {coid} never reached Alpaca"
                )
            )
            continue
        # If state already reflects this order (active_contract.client_order_id
        # matches), the save() must have raced ahead but the intent didn't get
        # cleared. Just clear it now.
        already = (ts.active_contract or {}).get("client_order_id")
        if already == coid:
            clear_intent(coid)
            continue
        # Apply the intent into state.
        if kind == "sto_put":
            ts.stage = Stage.PUT_OPEN
            ts.active_contract = {
                **(intent.get("contract") or {}),
                "order_id": str(getattr(order, "id", "")),
                "client_order_id": coid,
            }
            results.append(
                ReconcileResult(ticker, "orphan_recovered", f"recovered STO put {coid}")
            )
        elif kind == "sto_call":
            ts.stage = Stage.CALL_OPEN
            ts.active_contract = {
                **(intent.get("contract") or {}),
                "order_id": str(getattr(order, "id", "")),
                "client_order_id": coid,
            }
            results.append(
                ReconcileResult(ticker, "orphan_recovered", f"recovered STO call {coid}")
            )
        elif kind == "btc":
            # The BTC was submitted; the per-ticker reconciler will see the
            # short option disappear and route to the proper transition. Just
            # log it.
            results.append(
                ReconcileResult(ticker, "orphan_recovered", f"BTC {coid} order seen at Alpaca")
            )
        clear_intent(coid)
    return results
