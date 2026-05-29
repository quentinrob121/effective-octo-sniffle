from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest
from alpaca.common.exceptions import APIError

from wheel_bot.reconciler import reconcile_ticker, recover_orphan_intents
from wheel_bot.state import Stage, TickerState, WheelState


def _api_error(status: int, message: str = "err") -> APIError:
    """Build an APIError whose ``status_code`` property reads ``status``.

    APIError reads ``status_code`` from ``http_error.response.status_code``,
    so we pass a small stand-in that exposes that attribute chain.
    """
    http_error = MagicMock()
    http_error.response.status_code = status
    return APIError(f'{{"message": "{message}"}}', http_error=http_error)


def _client_no_position_no_orders():
    """A client that has no open wheel orders, no positions of any kind, and
    404s on per-symbol position lookup."""
    client = MagicMock()
    client.get_orders.return_value = []  # no open orders at all
    client.get_open_position.side_effect = _api_error(404, "position not found")
    client.get_all_positions.return_value = []
    return client


def _client_with_position(symbol: str, qty: int, avg_entry: float):
    client = MagicMock()
    client.get_orders.return_value = []
    pos = MagicMock()
    pos.symbol = symbol
    pos.qty = str(qty)
    pos.qty_available = str(qty)
    pos.avg_entry_price = str(avg_entry)
    client.get_open_position.return_value = pos
    client.get_all_positions.return_value = [pos]
    return client


def _client_with_open_wheel_option(coid: str):
    client = MagicMock()
    o = MagicMock()
    o.client_order_id = coid
    o.symbol = "PLTR250620P00040000"
    client.get_orders.return_value = [o]
    client.get_open_position.side_effect = _api_error(404, "no position")
    client.get_all_positions.return_value = []
    return client


def _short_option_position(occ_symbol: str, qty: int = -1):
    """A short option position as returned by client.get_all_positions().

    Alpaca reports shorts with a NEGATIVE qty — that's the structural signal
    we use to distinguish "we sold this option" from "we own this option".
    """
    p = MagicMock()
    p.symbol = occ_symbol
    p.qty = str(qty)
    p.qty_available = str(qty)
    return p


def test_put_assigned_transitions_to_holding_with_correct_basis():
    state = TickerState(ticker="PLTR", stage=Stage.PUT_OPEN)
    state.active_contract = {
        "symbol": "PLTR250620P00040000",
        "strike": 40.0,
        "expiration": "2025-06-20",
        "premium_received": 1.05,
        "contracts": 1,
    }
    client = _client_with_position("PLTR", 100, 40.0)

    rec = reconcile_ticker(client, state)

    assert rec.transition == "assigned"
    assert state.stage == Stage.HOLDING
    assert state.shares_held == 100
    # basis = strike - premium = 40 - 1.05 = 38.95
    assert state.avg_basis_per_share == pytest.approx(38.95)
    assert state.cumulative_premium_this_lot == 0.0
    assert state.active_contract is None


def test_put_expired_transitions_to_idle_and_credits_lifetime():
    state = TickerState(ticker="PLTR", stage=Stage.PUT_OPEN)
    state.active_contract = {
        "symbol": "PLTR250620P00040000",
        "strike": 40.0,
        "expiration": "2025-06-20",
        "premium_received": 1.05,
        "contracts": 1,
    }
    client = _client_no_position_no_orders()
    rec = reconcile_ticker(client, state)
    assert rec.transition == "expired_put"
    assert state.stage == Stage.IDLE
    assert state.cumulative_premium_lifetime == pytest.approx(105.0)  # 1.05 * 100


def test_call_expired_worthless_returns_to_holding():
    state = TickerState(ticker="PLTR", stage=Stage.CALL_OPEN)
    state.shares_held = 100
    state.avg_basis_per_share = 38.95
    state.cumulative_premium_this_lot = 0.0
    state.active_contract = {
        "symbol": "PLTR250620C00045000",
        "strike": 45.0,
        "expiration": "2025-06-20",
        "premium_received": 0.60,
        "contracts": 1,
    }
    client = _client_with_position("PLTR", 100, 40.0)
    rec = reconcile_ticker(client, state)
    assert rec.transition == "expired_call"
    assert state.stage == Stage.HOLDING
    assert state.cumulative_premium_this_lot == pytest.approx(0.60)


def test_called_away_transitions_to_idle_and_records_pnl():
    state = TickerState(ticker="PLTR", stage=Stage.CALL_OPEN)
    state.shares_held = 100
    state.avg_basis_per_share = 38.95
    state.cumulative_premium_this_lot = 0.60  # previous call already counted
    state.active_contract = {
        "symbol": "PLTR250620C00045000",
        "strike": 45.0,
        "expiration": "2025-06-20",
        "premium_received": 0.40,
        "contracts": 1,
    }
    client = _client_no_position_no_orders()
    rec = reconcile_ticker(client, state)
    assert rec.transition == "called_away"
    assert state.stage == Stage.IDLE
    assert state.shares_held == 0
    # pnl = (strike - basis + lot_prem_total) * shares
    # = (45 - 38.95 + 0.60 + 0.40) * 100 = 7.05 * 100 = 705
    cycle = state.cycles[-1]
    assert cycle["kind"] == "called_away"
    assert cycle["lot_pnl"] == pytest.approx(705.0)


def test_idle_with_position_adopts_basis_from_alpaca(caplog):
    state = TickerState(ticker="PLTR", stage=Stage.IDLE)
    client = _client_with_position("PLTR", 100, 37.25)
    rec = reconcile_ticker(client, state)
    assert rec.transition == "adopted"
    assert state.stage == Stage.HOLDING
    assert state.shares_held == 100
    assert state.avg_basis_per_share == pytest.approx(37.25)


def test_holding_without_position_resets_to_idle():
    state = TickerState(ticker="PLTR", stage=Stage.HOLDING)
    state.shares_held = 100
    state.avg_basis_per_share = 40.0
    client = _client_no_position_no_orders()
    rec = reconcile_ticker(client, state)
    assert rec.transition == "external_close"
    assert state.stage == Stage.IDLE
    assert state.shares_held == 0


def test_put_still_open_no_transition():
    state = TickerState(ticker="PLTR", stage=Stage.PUT_OPEN)
    state.active_contract = {
        "symbol": "PLTR250620P00040000",
        "strike": 40.0,
        "expiration": "2025-06-20",
        "premium_received": 1.0,
        "contracts": 1,
    }
    # Open wheel-tagged option, no position: put is still alive.
    client = _client_with_open_wheel_option("wheel-PLTR-put-aaaaaaaa")
    rec = reconcile_ticker(client, state)
    assert rec.transition == "none"
    assert state.stage == Stage.PUT_OPEN


def test_non_404_api_error_propagates():
    state = TickerState(ticker="PLTR", stage=Stage.IDLE)
    client = MagicMock()
    client.get_orders.return_value = []
    client.get_open_position.side_effect = _api_error(429, "rate limited")
    with pytest.raises(APIError):
        reconcile_ticker(client, state)


def test_partial_assignment_edge_case_records_actual_qty():
    """If only 1 of 2 contracts assigned we MUST adopt the actual share count
    (100), not the contracts * 100 = 200 the executor optimistically recorded.

    Previously this test asserted the buggy behavior (basis only). Now we
    assert the FIX: state.shares_held must match Alpaca's reality so the
    next covered-call attempt isn't naked on the un-assigned half."""
    state = TickerState(ticker="PLTR", stage=Stage.PUT_OPEN)
    state.active_contract = {
        "symbol": "PLTR250620P00040000",
        "strike": 40.0,
        "expiration": "2025-06-20",
        "premium_received": 1.05,
        "contracts": 2,  # expected 200 shares
    }
    client = _client_with_position("PLTR", 100, 40.0)  # only 100 actually assigned
    rec = reconcile_ticker(client, state)
    assert rec.transition == "assigned"
    assert state.stage == Stage.HOLDING
    # AUTHORITATIVE: shares_held tracks Alpaca's actual fill, not the
    # contract metadata.
    assert state.shares_held == 100
    assert state.avg_basis_per_share == pytest.approx(38.95)


def test_full_assignment_updates_shares_held_correctly():
    """A clean 2-contract -> 200-share assignment populates shares_held from
    the position qty (which equals contracts*100 in the happy case)."""
    state = TickerState(ticker="PLTR", stage=Stage.PUT_OPEN)
    state.active_contract = {
        "symbol": "PLTR250620P00040000",
        "strike": 40.0,
        "expiration": "2025-06-20",
        "premium_received": 1.05,
        "contracts": 2,
    }
    client = _client_with_position("PLTR", 200, 40.0)
    rec = reconcile_ticker(client, state)
    assert rec.transition == "assigned"
    assert state.shares_held == 200
    assert state.avg_basis_per_share == pytest.approx(38.95)


def test_put_stays_open_when_short_position_exists():
    """The bug we're guarding against: STO fills, the order leaves the OPEN
    queue immediately, but the short put lives on as a position with qty<0.
    The reconciler must see that short position and NOT fire expired/assigned
    on it for weeks."""
    state = TickerState(ticker="PLTR", stage=Stage.PUT_OPEN)
    state.active_contract = {
        "symbol": "PLTR250620P00040000",
        "strike": 40.0,
        "expiration": "2025-06-20",
        "premium_received": 1.05,
        "contracts": 1,
    }
    client = MagicMock()
    client.get_orders.return_value = []
    client.get_open_position.side_effect = _api_error(404, "no equity")
    client.get_all_positions.return_value = [
        _short_option_position("PLTR250620P00040000", qty=-1),
    ]
    rec = reconcile_ticker(client, state)
    assert rec.transition == "none"
    assert state.stage == Stage.PUT_OPEN
    assert state.active_contract is not None


def test_put_expired_only_fires_after_expiration_date():
    """The date-based safeguard: if the option has disappeared from positions
    AND from open orders BUT we're still before the recorded expiration, that's
    a transient anomaly (Alpaca cache lag, manual cancel, etc.) — log + wait.
    Only after the expiration date passes do we credit it as expired."""
    state = TickerState(ticker="PLTR", stage=Stage.PUT_OPEN)
    state.active_contract = {
        "symbol": "PLTR250620P00040000",
        "strike": 40.0,
        "expiration": "2025-06-20",
        "premium_received": 1.05,
        "contracts": 1,
    }
    client = _client_no_position_no_orders()

    # Pre-expiration: no transition, premium NOT credited.
    rec = reconcile_ticker(client, state, today=date(2025, 6, 19))
    assert rec.transition == "none"
    assert state.stage == Stage.PUT_OPEN
    assert state.cumulative_premium_lifetime == 0

    # On/after expiration: fires the expired path.
    rec = reconcile_ticker(client, state, today=date(2025, 6, 20))
    assert rec.transition == "expired_put"
    assert state.stage == Stage.IDLE
    assert state.cumulative_premium_lifetime == pytest.approx(105.0)


def test_call_stays_open_when_short_position_exists():
    """Equivalent of the put-still-alive test, for calls."""
    state = TickerState(ticker="PLTR", stage=Stage.CALL_OPEN)
    state.shares_held = 100
    state.avg_basis_per_share = 40.0
    state.active_contract = {
        "symbol": "PLTR250620C00045000",
        "strike": 45.0,
        "expiration": "2025-06-20",
        "premium_received": 0.60,
        "contracts": 1,
    }
    client = MagicMock()
    client.get_orders.return_value = []
    # equity at 100 + short call alive.
    pos_equity = MagicMock()
    pos_equity.symbol = "PLTR"
    pos_equity.qty = "100"
    pos_equity.qty_available = "100"
    pos_equity.avg_entry_price = "40"
    client.get_open_position.return_value = pos_equity
    client.get_all_positions.return_value = [
        pos_equity,
        _short_option_position("PLTR250620C00045000", qty=-1),
    ]
    rec = reconcile_ticker(client, state)
    assert rec.transition == "none"
    assert state.stage == Stage.CALL_OPEN


def test_call_expired_only_fires_after_expiration_date():
    """A call missing from positions/orders before its expiration date is an
    anomaly, not a worthless expiration."""
    state = TickerState(ticker="PLTR", stage=Stage.CALL_OPEN)
    state.shares_held = 100
    state.avg_basis_per_share = 40.0
    state.active_contract = {
        "symbol": "PLTR250620C00045000",
        "strike": 45.0,
        "expiration": "2025-06-20",
        "premium_received": 0.60,
        "contracts": 1,
    }
    # equity exists; call missing from positions/orders.
    client = _client_with_position("PLTR", 100, 40.0)
    # Pre-expiration -> no transition.
    rec = reconcile_ticker(client, state, today=date(2025, 6, 19))
    assert rec.transition == "none"
    assert state.stage == Stage.CALL_OPEN
    # On expiration -> expired call -> back to HOLDING.
    rec = reconcile_ticker(client, state, today=date(2025, 6, 20))
    assert rec.transition == "expired_call"
    assert state.stage == Stage.HOLDING
    assert state.cumulative_premium_this_lot == pytest.approx(0.60)


# ---------------------------------------------------------------------------
# Orphan intent recovery (FIX #7)
# ---------------------------------------------------------------------------

def test_recover_orphan_intent_with_matching_alpaca_order_applies_to_state(
    monkeypatch, tmp_path
):
    """If a sto_put intent is journaled and the matching order exists at
    Alpaca, recovery must populate state.active_contract so the next decision
    sees the active short option (and does NOT submit a duplicate)."""
    import wheel_bot.state as state_mod
    import wheel_bot.reconciler as recon_mod

    pending_path = tmp_path / "pending_intents.json"
    real_load = state_mod.load_pending_intents
    real_clear = state_mod.clear_intent
    monkeypatch.setattr(state_mod, "PENDING_INTENTS_PATH", pending_path)
    monkeypatch.setattr(
        state_mod, "load_pending_intents", lambda path=pending_path: real_load(path=path)
    )
    monkeypatch.setattr(
        state_mod, "clear_intent", lambda coid, path=pending_path: real_clear(coid, path=path)
    )
    monkeypatch.setattr(recon_mod, "load_pending_intents", state_mod.load_pending_intents)
    monkeypatch.setattr(recon_mod, "clear_intent", state_mod.clear_intent)
    # Seed a pending intent.
    state_mod.journal_intent(
        {
            "ticker": "PLTR",
            "intent": "sto_put",
            "client_order_id": "wheel-PLTR-put-abc12345",
            "contract": {
                "symbol": "PLTR250620P00040000",
                "type": "put",
                "strike": 40.0,
                "expiration": "2025-06-20",
                "premium_received": 1.05,
                "contracts": 1,
            },
        },
        path=pending_path,
    )

    # Alpaca returns the matching order.
    alpaca_order = MagicMock(id="ord-recovered")
    alpaca_order.client_order_id = "wheel-PLTR-put-abc12345"
    client = MagicMock()
    client.get_order_by_client_id.return_value = alpaca_order

    state = WheelState()
    results = recover_orphan_intents(client, state)
    assert any(r.transition == "orphan_recovered" for r in results)
    pltr = state.tickers["PLTR"]
    assert pltr.stage == Stage.PUT_OPEN
    assert pltr.active_contract["symbol"] == "PLTR250620P00040000"
    assert pltr.active_contract["client_order_id"] == "wheel-PLTR-put-abc12345"
    # Journal must be cleared so we don't re-recover next run.
    assert state_mod.load_pending_intents(path=pending_path) == []


def test_recover_orphan_intent_drops_intent_when_alpaca_has_no_order(
    monkeypatch, tmp_path
):
    """If the journaled order isn't at Alpaca, submission never succeeded;
    drop the intent so we don't loop on it forever."""
    import wheel_bot.state as state_mod
    import wheel_bot.reconciler as recon_mod

    pending_path = tmp_path / "pending_intents.json"
    real_load = state_mod.load_pending_intents
    real_clear = state_mod.clear_intent
    monkeypatch.setattr(state_mod, "PENDING_INTENTS_PATH", pending_path)
    monkeypatch.setattr(
        state_mod, "load_pending_intents", lambda path=pending_path: real_load(path=path)
    )
    monkeypatch.setattr(
        state_mod, "clear_intent", lambda coid, path=pending_path: real_clear(coid, path=path)
    )
    monkeypatch.setattr(recon_mod, "load_pending_intents", state_mod.load_pending_intents)
    monkeypatch.setattr(recon_mod, "clear_intent", state_mod.clear_intent)
    state_mod.journal_intent(
        {
            "ticker": "PLTR",
            "intent": "sto_put",
            "client_order_id": "wheel-PLTR-put-missing1",
            "contract": {"symbol": "PLTR250620P00040000"},
        },
        path=pending_path,
    )

    client = MagicMock()
    client.get_order_by_client_id.side_effect = _api_error(404, "no such order")

    state = WheelState()
    results = recover_orphan_intents(client, state)
    assert any(r.transition == "orphan_dropped" for r in results)
    assert state_mod.load_pending_intents(path=pending_path) == []
    assert state.tickers.get("PLTR") is None or state.tickers["PLTR"].stage == Stage.IDLE
