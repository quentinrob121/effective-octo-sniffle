from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest
from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderSide
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

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


def _foreign_put_order(strike: float, qty: int = 1):
    """An open put SELL we didn't tag (e.g. opened by hand)."""
    o = MagicMock()
    o.symbol = f"FOOX250620P{int(strike*1000):08d}"
    o.side = "OrderSide.SELL"
    o.qty = str(qty)
    o.limit_price = str(strike)
    o.client_order_id = "manual-12345"
    return o


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


def test_has_cash_for_put_subtracts_foreign_put_collateral():
    client = MagicMock()
    client.get_account.return_value = _account(options_bp=10_000)
    # An open put on FOOX at strike 60 reserves 60*100*1 = 6000.
    client.get_orders.return_value = [_foreign_put_order(strike=60)]
    ok, avail, needed = has_cash_for_put(client, strike=40, contracts=1)
    assert avail == 10_000 - 6000
    assert needed == 4000
    assert ok  # 4000 still fits


def test_has_cash_for_put_refuses_when_underwater():
    client = MagicMock()
    client.get_account.return_value = _account(options_bp=5_000)
    client.get_orders.return_value = [_foreign_put_order(strike=60)]  # reserves 6000
    ok, _avail, _needed = has_cash_for_put(client, strike=40, contracts=1)
    assert not ok


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


def test_sto_put_refuses_when_cash_short_due_to_foreign_put():
    client = MagicMock()
    client.get_account.return_value = _account(5_000)
    client.get_orders.return_value = [_foreign_put_order(strike=60)]  # eats 6000
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


def test_btc_market_orders_and_updates_state():
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
    assert isinstance(req, MarketOrderRequest)
    assert req.side == OrderSide.BUY


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
