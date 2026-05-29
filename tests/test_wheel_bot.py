from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from alpaca.common.exceptions import APIError

import wheel_bot.bot as bot_mod
from wheel_bot.config import WheelConfig
from wheel_bot.option_picker import OptionCandidate
from wheel_bot.state import (
    Stage,
    TickerState,
    WheelState,
    load_state,
    save_state,
)


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


def _put_chain(today: date) -> list[OptionCandidate]:
    exp = today + timedelta(days=21)
    return [
        OptionCandidate(
            symbol=f"PLTR{exp.strftime('%y%m%d')}P{int(s*1000):08d}",
            underlying="PLTR",
            expiration=exp,
            strike=s,
            type="put",
            bid=1.00,
            ask=1.10,
        )
        for s in (35, 38, 40, 42, 45)
    ]


def _call_chain(today: date) -> list[OptionCandidate]:
    exp = today + timedelta(days=21)
    return [
        OptionCandidate(
            symbol=f"PLTR{exp.strftime('%y%m%d')}C{int(s*1000):08d}",
            underlying="PLTR",
            expiration=exp,
            strike=s,
            type="call",
            bid=0.80,
            ask=0.90,
        )
        for s in (40, 42, 44, 46, 48)
    ]


def _no_position_client(open_orders=None):
    client = MagicMock()
    http_error = MagicMock()
    http_error.response.status_code = 404
    client.get_open_position.side_effect = APIError(
        '{"message": "no position"}', http_error=http_error
    )
    client.get_orders.return_value = open_orders or []
    acct = MagicMock()
    acct.options_buying_power = "100000"
    acct.buying_power = "100000"
    client.get_account.return_value = acct
    return client


def _patched(monkeypatch, *, trading_client, stock_spot, put_chain, call_chain, option_asks=None):
    monkeypatch.setattr(bot_mod, "build_trading_client", lambda: trading_client)
    monkeypatch.setattr(bot_mod, "build_data_client", lambda: MagicMock())
    monkeypatch.setattr(bot_mod, "_build_option_data_client", lambda: MagicMock())
    monkeypatch.setattr(bot_mod, "_spot_price", lambda *_a, **_k: stock_spot)

    from alpaca.trading.enums import ContractType

    def fake_fetch(_client, ticker, option_type, today, dte_max):
        return put_chain if option_type == ContractType.PUT else call_chain

    monkeypatch.setattr(bot_mod, "_fetch_chain", fake_fetch)
    monkeypatch.setattr(
        bot_mod, "_option_ask", lambda _c, sym: (option_asks or {}).get(sym)
    )


def _isolate_state(monkeypatch, tmp_path: Path):
    """Redirect every state read/write to ``tmp_path``.

    load_state/save_state default their path arg at definition time, so
    patching ``wheel_bot.state.STATE_PATH`` alone wouldn't take effect;
    we wrap the functions themselves to inject the path.
    """
    state_path = tmp_path / "wheel_state.json"
    audit_path = tmp_path / "audit.jsonl"
    summaries_dir = tmp_path / "summaries"
    summaries_dir.mkdir()
    import wheel_bot.state as state_mod

    real_load = state_mod.load_state
    real_save = state_mod.save_state
    real_audit = state_mod.append_audit

    monkeypatch.setattr(bot_mod, "load_state", lambda path=state_path: real_load(path=path))
    monkeypatch.setattr(bot_mod, "save_state", lambda state, path=state_path: real_save(state, path=path))
    monkeypatch.setattr(bot_mod, "append_audit", lambda entry, path=audit_path: real_audit(entry, path=path))
    monkeypatch.setattr(bot_mod, "STATE_PATH", state_path)
    monkeypatch.setattr(bot_mod, "SUMMARIES_DIR", summaries_dir)
    return state_path


def test_full_machine_idle_to_put_open_to_assigned_to_call_open_to_called_away(
    monkeypatch, tmp_path: Path
):
    """End-to-end happy path:

    IDLE -> STO put -> reconciler sees assignment -> HOLDING -> STO call
    -> reconciler sees called-away -> back to IDLE with cycles logged.
    """
    state_path = _isolate_state(monkeypatch, tmp_path)
    today = date(2025, 1, 1)

    # --- Run 1: IDLE -> PUT_OPEN ---------------------------------------
    client = _no_position_client()
    submitted = MagicMock(id="ord-put")
    client.submit_order.return_value = submitted
    _patched(
        monkeypatch,
        trading_client=client,
        stock_spot=44.0,  # target_strike = 44 * 0.9 = 39.6 -> nearest is 40
        put_chain=_put_chain(today),
        call_chain=_call_chain(today),
    )
    with patch.object(bot_mod, "datetime") as dt:
        dt.now.return_value = MagicMock(date=lambda: today)
        result1 = bot_mod.run_once(_config())
    state = load_state(path=state_path)
    pltr = state.tickers["PLTR"]
    assert pltr.stage == Stage.PUT_OPEN
    assert pltr.active_contract["strike"] == 40
    assert any(a.action == "sto_put" for a in result1.actions)

    # --- Run 2: assignment -> HOLDING; then HOLDING -> CALL_OPEN -------
    assigned_client = MagicMock()
    pos = MagicMock()
    pos.symbol = "PLTR"
    pos.qty = "100"
    pos.qty_available = "100"
    pos.avg_entry_price = "40.0"
    assigned_client.get_open_position.return_value = pos
    assigned_client.get_orders.return_value = []  # put gone (assigned)
    assigned_client.get_account.return_value = client.get_account.return_value
    call_submitted = MagicMock(id="ord-call")
    assigned_client.submit_order.return_value = call_submitted
    _patched(
        monkeypatch,
        trading_client=assigned_client,
        stock_spot=44.0,  # call target = 44 * 1.1 = 48.4 -> nearest call strike 48
        put_chain=_put_chain(today),
        call_chain=_call_chain(today),
    )
    with patch.object(bot_mod, "datetime") as dt:
        dt.now.return_value = MagicMock(date=lambda: today)
        result2 = bot_mod.run_once(_config())
    state = load_state(path=state_path)
    pltr = state.tickers["PLTR"]
    assert any(r.transition == "assigned" for r in result2.reconciliations)
    assert pltr.stage == Stage.CALL_OPEN
    assert pltr.active_contract["type"] == "call"
    assert pltr.shares_held == 100

    # --- Run 3: called away -> IDLE -----------------------------------
    away_client = _no_position_client()
    _patched(
        monkeypatch,
        trading_client=away_client,
        stock_spot=44.0,
        put_chain=_put_chain(today),
        call_chain=_call_chain(today),
    )
    away_client.submit_order.return_value = MagicMock(id="ord-put-2")
    with patch.object(bot_mod, "datetime") as dt:
        dt.now.return_value = MagicMock(date=lambda: today)
        result3 = bot_mod.run_once(_config())
    state = load_state(path=state_path)
    pltr = state.tickers["PLTR"]
    assert any(r.transition == "called_away" for r in result3.reconciliations)
    # After reconcile -> IDLE, the bot then STOs a new put in the same run.
    assert pltr.stage == Stage.PUT_OPEN
    # Cycle log should have a called_away entry.
    assert any(c["kind"] == "called_away" for c in pltr.cycles)


def test_disabled_ticker_skipped(monkeypatch, tmp_path: Path):
    state_path = _isolate_state(monkeypatch, tmp_path)
    initial = WheelState()
    ts = initial.get_or_create("PLTR")
    ts.enabled = False
    save_state(initial, path=state_path)

    client = _no_position_client()
    _patched(
        monkeypatch,
        trading_client=client,
        stock_spot=44.0,
        put_chain=_put_chain(date.today()),
        call_chain=_call_chain(date.today()),
    )
    result = bot_mod.run_once(_config())
    assert all(a.action == "skipped" for a in result.actions)
    client.submit_order.assert_not_called()


def test_put_skipped_when_chain_has_no_expiration_in_window(
    monkeypatch, tmp_path: Path
):
    """NANC monthly: if no expiration in [14,28] DTE, skip the cycle."""
    _isolate_state(monkeypatch, tmp_path)
    client = _no_position_client()
    today = date(2025, 1, 1)
    far_exp = today + timedelta(days=40)
    far_chain = [
        OptionCandidate(
            symbol=f"NANC{far_exp.strftime('%y%m%d')}P{int(40*1000):08d}",
            underlying="NANC",
            expiration=far_exp,
            strike=40,
            type="put",
            bid=1.00,
            ask=1.10,
        )
    ]
    _patched(
        monkeypatch,
        trading_client=client,
        stock_spot=44.0,
        put_chain=far_chain,
        call_chain=[],
    )
    with patch.object(bot_mod, "datetime") as dt:
        dt.now.return_value = MagicMock(date=lambda: today)
        result = bot_mod.run_once(_config(tickers=("NANC",)))
    assert any("no PUT expiration" in a.detail for a in result.actions)
    client.submit_order.assert_not_called()


def test_deep_underwater_lot_stays_holding(monkeypatch, tmp_path: Path):
    """If every available call strike is below basis, do nothing — never sell
    below cost."""
    state_path = _isolate_state(monkeypatch, tmp_path)
    initial = WheelState()
    ts = initial.get_or_create("PLTR")
    ts.stage = Stage.HOLDING
    ts.shares_held = 100
    ts.avg_basis_per_share = 80.0  # way above any available call strike
    save_state(initial, path=state_path)

    client = MagicMock()
    pos = MagicMock()
    pos.symbol = "PLTR"
    pos.qty = "100"
    pos.qty_available = "100"
    pos.avg_entry_price = "80"
    client.get_open_position.return_value = pos
    client.get_orders.return_value = []
    client.get_account.return_value = MagicMock(
        options_buying_power="100000", buying_power="100000"
    )
    today = date(2025, 1, 1)
    _patched(
        monkeypatch,
        trading_client=client,
        stock_spot=44.0,
        put_chain=_put_chain(today),
        call_chain=_call_chain(today),  # max strike 48 << basis 80
    )
    with patch.object(bot_mod, "datetime") as dt:
        dt.now.return_value = MagicMock(date=lambda: today)
        result = bot_mod.run_once(_config())
    assert any("below basis" in a.detail for a in result.actions)
    state = load_state(path=state_path)
    assert state.tickers["PLTR"].stage == Stage.HOLDING
    client.submit_order.assert_not_called()


def test_pause_blocks_new_sto_but_close_still_runs(monkeypatch, tmp_path: Path):
    """A paused (disabled) ticker should not STO. The `wheel_bot close` CLI
    bypasses the enabled gate by calling the executor directly — verified
    by the executor's own test that BTC succeeds; here we just confirm the
    paused state blocks the bot loop."""
    state_path = _isolate_state(monkeypatch, tmp_path)
    initial = WheelState()
    ts = initial.get_or_create("PLTR")
    ts.enabled = False
    save_state(initial, path=state_path)

    client = _no_position_client()
    today = date(2025, 1, 1)
    _patched(
        monkeypatch,
        trading_client=client,
        stock_spot=44.0,
        put_chain=_put_chain(today),
        call_chain=_call_chain(today),
    )
    with patch.object(bot_mod, "datetime") as dt:
        dt.now.return_value = MagicMock(date=lambda: today)
        result = bot_mod.run_once(_config())
    assert all(a.action == "skipped" for a in result.actions)
    client.submit_order.assert_not_called()
