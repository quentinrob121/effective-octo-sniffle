from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest
from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderSide
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

import wheel_bot.state as state_mod
from wheel_bot.config import WheelConfig
from wheel_bot.executor import (
    btc,
    effective_basis_per_share,
    has_cash_for_put,
    should_close,
    sto_call,
    sto_put,
)
from wheel_bot.option_picker import OptionCandidate
from wheel_bot.state import Stage, TickerState


@pytest.fixture(autouse=True)
def _isolate_pending_intents(tmp_path, monkeypatch):
    """Redirect the executor's pending-intent journal to a per-test temp file
    so tests don't leak state to the repo's real wheel_bot/state/ dir."""
    monkeypatch.setattr(
        state_mod, "PENDING_INTENTS_PATH", tmp_path / "pending_intents.json"
    )
    # The journal helpers default their `path` arg at definition time, so we
    # also rebind them to versions that look at the patched path.
    real_journal = state_mod.journal_intent
    real_clear = state_mod.load_pending_intents.__wrapped__ if False else None
    # Simpler: patch the helpers to use the new default path explicitly.
    monkeypatch.setattr(
        state_mod,
        "journal_intent",
        lambda intent, path=tmp_path / "pending_intents.json": real_journal(intent, path=path),
    )
    real_clear_fn = state_mod.clear_intent
    monkeypatch.setattr(
        state_mod,
        "clear_intent",
        lambda coid, path=tmp_path / "pending_intents.json": real_clear_fn(coid, path=path),
    )
    # Executor binds these at import time, so re-bind there too.
    import wheel_bot.executor as exec_mod

    monkeypatch.setattr(exec_mod, "journal_intent", state_mod.journal_intent)
    monkeypatch.setattr(exec_mod, "clear_intent", state_mod.clear_intent)
    yield


def _config(**overrides) -> WheelConfig:
    base = dict(
        tickers=("PLTR",),
        put_strike_offset_pct=10.0,
        call_strike_offset_pct=10.0,
        target_dte=21,
        dte_min=14,
        dte_max=28,
        close_profit_pct=50.0,
        close_min_dte=2,
        contracts_per_cycle=1,
        min_premium_usd=10.0,
        dry_run=False,
    )
    base.update(overrides)
    return WheelConfig(**base)


def _candidate(strike: float, exp: date, type: str = "put", bid: float = 1.0):
    return OptionCandidate(
        symbol=f"PLTR{exp.strftime('%y%m%d')}{'P' if type == 'put' else 'C'}{int(strike*1000):08d}",
        underlying="PLTR",
        expiration=exp,
        strike=strike,
        type=type,
        bid=bid,
        ask=bid + 0.05,
    )


def _account(options_bp: float = 100_000):
    a = MagicMock()
    a.options_buying_power = str(options_bp)
    a.buying_power = str(options_bp)
    return a


# ---------------------------------------------------------------------------
# Cash guard
# ---------------------------------------------------------------------------

def test_has_cash_for_put_simple_pass():
    client = MagicMock()
    client.get_account.return_value = _account(options_bp=10_000)
    client.get_orders.return_value = []
    ok, avail, needed = has_cash_for_put(client, strike=40, contracts=1)
    assert ok
    assert needed == 4000
    assert avail == 10_000


def test_has_cash_for_put_uses_options_buying_power_not_open_orders_sum():
    """The previous impl summed up open put-SELL collateral and subtracted
    that from options_buying_power. That was double-protective (Alpaca's BP
    figure already nets working orders) AND double-broken (Alpaca's BP also
    nets just-filled puts that have LEFT the OPEN queue). We trust
    options_buying_power directly now; open orders don't enter the math."""
    client = MagicMock()
    client.get_account.return_value = _account(options_bp=10_000)
    # The open-orders list now plays no role; supply something nonzero to
    # confirm we're NOT subtracting from it.
    foreign = MagicMock()
    foreign.symbol = "FOOX250620P00060000"
    foreign.side = "OrderSide.SELL"
    foreign.qty = "1"
    client.get_orders.return_value = [foreign]
    ok, avail, needed = has_cash_for_put(client, strike=40, contracts=1)
    assert ok
    assert avail == 10_000  # NOT 10000 - 6000
    assert needed == 4000


def test_has_cash_for_put_refuses_when_underwater():
    client = MagicMock()
    # options_buying_power below the put's notional -> refuse.
    client.get_account.return_value = _account(options_bp=3_000)
    client.get_orders.return_value = []
    ok, avail, needed = has_cash_for_put(client, strike=40, contracts=1)
    assert not ok
    assert avail == 3_000
    assert needed == 4_000


# ---------------------------------------------------------------------------
# STO put
# ---------------------------------------------------------------------------

def test_sto_put_submits_correct_limit_order_and_persists():
    client = MagicMock()
    client.get_account.return_value = _account(50_000)
    client.get_orders.return_value = []
    client.submit_order.return_value = MagicMock(id="ord-1")

    state = TickerState(ticker="PLTR", stage=Stage.IDLE)
    cand = _candidate(40, date.today() + timedelta(days=21), bid=1.10)
    saved = MagicMock()
    action = sto_put(client, state, cand, _config(), save=saved)

    assert action.action == "sto_put"
    assert state.stage == Stage.PUT_OPEN
    assert state.active_contract["strike"] == 40
    assert state.active_contract["premium_received"] == 1.10
    assert state.active_contract["client_order_id"].startswith("wheel-PLTR-put-")
    req = client.submit_order.call_args.args[0]
    assert isinstance(req, LimitOrderRequest)
    assert req.side == OrderSide.SELL
    assert float(req.limit_price) == 1.10
    saved.assert_called_once()


def test_sto_put_refuses_when_options_bp_too_low():
    """options_buying_power below the put's notional -> skip with reason."""
    client = MagicMock()
    client.get_account.return_value = _account(3_000)
    client.get_orders.return_value = []
    state = TickerState(ticker="PLTR", stage=Stage.IDLE)
    cand = _candidate(40, date.today() + timedelta(days=21), bid=1.10)
    action = sto_put(client, state, cand, _config())
    assert action.action == "skipped"
    assert "insufficient" in action.detail
    client.submit_order.assert_not_called()
    assert state.stage == Stage.IDLE


def test_sto_put_skipped_below_min_premium():
    client = MagicMock()
    state = TickerState(ticker="PLTR", stage=Stage.IDLE)
    cand = _candidate(40, date.today() + timedelta(days=21), bid=0.05)
    action = sto_put(client, state, cand, _config(min_premium_usd=10.0))
    assert action.action == "skipped"
    assert "below min premium" in action.detail


def test_sto_put_halts_on_zero_bid():
    client = MagicMock()
    state = TickerState(ticker="PLTR", stage=Stage.IDLE)
    cand = _candidate(40, date.today() + timedelta(days=21), bid=0.0)
    action = sto_put(client, state, cand, _config())
    assert action.action == "halted"


def test_sto_put_dry_run_does_not_submit():
    client = MagicMock()
    state = TickerState(ticker="PLTR", stage=Stage.IDLE)
    cand = _candidate(40, date.today() + timedelta(days=21), bid=1.10)
    action = sto_put(client, state, cand, _config(dry_run=True))
    assert action.action == "skipped"
    assert "dry-run" in action.detail
    client.submit_order.assert_not_called()
    assert state.stage == Stage.IDLE  # nothing persisted


# ---------------------------------------------------------------------------
# STO call
# ---------------------------------------------------------------------------

def test_effective_basis_subtracts_lot_premium():
    state = TickerState(ticker="PLTR", stage=Stage.HOLDING)
    state.shares_held = 100
    state.avg_basis_per_share = 40.0
    state.cumulative_premium_this_lot = 1.50
    assert effective_basis_per_share(state) == pytest.approx(38.50)


def test_sto_call_refuses_below_effective_basis():
    client = MagicMock()
    state = TickerState(ticker="PLTR", stage=Stage.HOLDING)
    state.shares_held = 100
    state.avg_basis_per_share = 40.0
    state.cumulative_premium_this_lot = 0.0
    cand = _candidate(35, date.today() + timedelta(days=21), type="call", bid=1.50)
    action = sto_call(client, state, cand, _config())
    assert action.action == "skipped"
    assert "below basis" in action.detail
    client.submit_order.assert_not_called()


def test_sto_call_submits_when_strike_above_basis():
    client = MagicMock()
    client.submit_order.return_value = MagicMock(id="ord-call")
    state = TickerState(ticker="PLTR", stage=Stage.HOLDING)
    state.shares_held = 100
    state.avg_basis_per_share = 40.0
    cand = _candidate(45, date.today() + timedelta(days=21), type="call", bid=0.80)
    saved = MagicMock()
    action = sto_call(client, state, cand, _config(), save=saved)
    assert action.action == "sto_call"
    assert state.stage == Stage.CALL_OPEN
    assert state.active_contract["strike"] == 45
    saved.assert_called_once()
    req = client.submit_order.call_args.args[0]
    assert isinstance(req, LimitOrderRequest)
    assert req.side == OrderSide.SELL


def test_sto_call_skipped_for_disabled_ticker():
    client = MagicMock()
    state = TickerState(ticker="PLTR", stage=Stage.HOLDING, enabled=False)
    state.shares_held = 100
    state.avg_basis_per_share = 40.0
    cand = _candidate(45, date.today() + timedelta(days=21), type="call", bid=0.80)
    action = sto_call(client, state, cand, _config())
    assert action.action == "skipped"
    client.submit_order.assert_not_called()


def test_sto_call_returns_halted_when_bid_zero():
    """Halt detection (bid<=0) MUST run before min-premium check, otherwise
    a halted chain reports 'skipped: below min premium' and we miss the
    operationally-meaningful 'halted' classification."""
    client = MagicMock()
    state = TickerState(ticker="PLTR", stage=Stage.HOLDING)
    state.shares_held = 100
    state.avg_basis_per_share = 40.0
    cand = _candidate(45, date.today() + timedelta(days=21), type="call", bid=0.0)
    action = sto_call(client, state, cand, _config())
    assert action.action == "halted"
    client.submit_order.assert_not_called()


# ---------------------------------------------------------------------------
# BTC
# ---------------------------------------------------------------------------

def test_should_close_fires_at_50pct_decay_with_enough_dte():
    state = TickerState(ticker="PLTR", stage=Stage.PUT_OPEN)
    state.active_contract = {
        "symbol": "PLTR",
        "expiration": (date(2025, 1, 1) + timedelta(days=10)).isoformat(),
        "premium_received": 1.00,
    }
    do, why = should_close(state, current_ask=0.40, today=date(2025, 1, 1), config=_config())
    assert do
    assert "BTC" in why


def test_should_close_skipped_near_expiry():
    state = TickerState(ticker="PLTR", stage=Stage.PUT_OPEN)
    state.active_contract = {
        "symbol": "PLTR",
        "expiration": (date(2025, 1, 1) + timedelta(days=1)).isoformat(),
        "premium_received": 1.00,
    }
    do, why = should_close(state, current_ask=0.05, today=date(2025, 1, 1), config=_config(close_min_dte=2))
    assert not do
    assert "DTE" in why


def test_should_close_skipped_when_not_decayed_enough():
    state = TickerState(ticker="PLTR", stage=Stage.PUT_OPEN)
    state.active_contract = {
        "symbol": "PLTR",
        "expiration": (date(2025, 1, 1) + timedelta(days=10)).isoformat(),
        "premium_received": 1.00,
    }
    do, _ = should_close(state, current_ask=0.80, today=date(2025, 1, 1), config=_config())
    assert not do


def test_btc_submits_limit_at_ask_plus_5c_gtc():
    """BTC must use a GTC LIMIT priced ask+0.05, NOT a market order.

    Alpaca rejects option market+GTC (422). Limit+GTC gives us off-hours
    queueing; the +5c buffer crosses the spread so the order fills near
    immediately when the market opens.
    """
    from alpaca.trading.enums import TimeInForce

    client = MagicMock()
    client.submit_order.return_value = MagicMock(id="ord-btc")
    state = TickerState(ticker="PLTR", stage=Stage.PUT_OPEN)
    state.active_contract = {
        "symbol": "PLTR250620P00040000",
        "premium_received": 1.00,
        "contracts": 1,
    }
    saved = MagicMock()
    action = btc(client, state, current_ask=0.40, config=_config(), save=saved)
    assert action.action == "btc"
    assert state.stage == Stage.IDLE
    assert state.active_contract is None
    saved.assert_called_once()
    req = client.submit_order.call_args.args[0]
    assert isinstance(req, LimitOrderRequest)
    assert req.side == OrderSide.BUY
    assert req.time_in_force == TimeInForce.GTC
    assert float(req.limit_price) == pytest.approx(0.45)  # ask + 0.05


def test_btc_call_transitions_to_holding_and_credits_lot_premium():
    client = MagicMock()
    client.submit_order.return_value = MagicMock(id="ord-btc")
    state = TickerState(ticker="PLTR", stage=Stage.CALL_OPEN)
    state.shares_held = 100
    state.avg_basis_per_share = 40
    state.active_contract = {
        "symbol": "PLTR250620C00045000",
        "premium_received": 1.00,
        "contracts": 1,
    }
    action = btc(client, state, current_ask=0.40, config=_config())
    assert action.action == "btc"
    assert state.stage == Stage.HOLDING
    assert state.cumulative_premium_this_lot == pytest.approx(0.60)


def test_btc_rejection_leaves_state_intact():
    client = MagicMock()
    http_error = MagicMock()
    http_error.response.status_code = 422
    err = APIError('{"message": "no liquidity"}', http_error=http_error)
    client.submit_order.side_effect = err
    state = TickerState(ticker="PLTR", stage=Stage.PUT_OPEN)
    state.active_contract = {
        "symbol": "PLTR250620P00040000",
        "premium_received": 1.00,
        "contracts": 1,
    }
    action = btc(client, state, current_ask=0.40, config=_config())
    assert action.action == "error"
    # State must not roll forward — next cron tick retries from PUT_OPEN.
    assert state.stage == Stage.PUT_OPEN
    assert state.active_contract is not None
