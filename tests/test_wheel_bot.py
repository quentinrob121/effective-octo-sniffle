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
    pending_path = tmp_path / "pending_intents.json"
    import wheel_bot.state as state_mod
    import wheel_bot.executor as exec_mod
    import wheel_bot.reconciler as recon_mod

    real_load = state_mod.load_state
    real_save = state_mod.save_state
    real_audit = state_mod.append_audit
    real_journal = state_mod.journal_intent
    real_clear = state_mod.clear_intent
    real_load_pend = state_mod.load_pending_intents

    monkeypatch.setattr(bot_mod, "load_state", lambda path=state_path: real_load(path=path))
    monkeypatch.setattr(bot_mod, "save_state", lambda state, path=state_path: real_save(state, path=path))
    monkeypatch.setattr(bot_mod, "append_audit", lambda entry, path=audit_path: real_audit(entry, path=path))
    monkeypatch.setattr(bot_mod, "STATE_PATH", state_path)
    monkeypatch.setattr(bot_mod, "SUMMARIES_DIR", summaries_dir)

    # Pending-intent journal isolation: executor writes via these helpers,
    # reconciler reads from them in recover_orphan_intents.
    monkeypatch.setattr(state_mod, "PENDING_INTENTS_PATH", pending_path)
    monkeypatch.setattr(
        state_mod,
        "journal_intent",
        lambda intent, path=pending_path: real_journal(intent, path=path),
    )
    monkeypatch.setattr(
        state_mod,
        "clear_intent",
        lambda coid, path=pending_path: real_clear(coid, path=path),
    )
    monkeypatch.setattr(
        state_mod,
        "load_pending_intents",
        lambda path=pending_path: real_load_pend(path=path),
    )
    monkeypatch.setattr(exec_mod, "journal_intent", state_mod.journal_intent)
    monkeypatch.setattr(exec_mod, "clear_intent", state_mod.clear_intent)
    monkeypatch.setattr(recon_mod, "load_pending_intents", state_mod.load_pending_intents)
    monkeypatch.setattr(recon_mod, "clear_intent", state_mod.clear_intent)
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
        stock_spot=44.0,  # call target now anchors on basis 39: 39*1.1 = 42.9 -> nearest 42
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


def test_call_strike_targets_basis_not_spot(monkeypatch, tmp_path: Path):
    """Spec rule: 'sell calls 10% above what I paid'. If spot is below basis,
    anchoring on spot would target a strike near or below basis — defeating
    the wheel. We anchor on effective_basis_per_share instead so the call
    is always premium-collecting above cost.

    basis=$50, spot=$40, offset=10% -> target=$55 (NOT $44).
    """
    state_path = _isolate_state(monkeypatch, tmp_path)
    initial = WheelState()
    ts = initial.get_or_create("PLTR")
    ts.stage = Stage.HOLDING
    ts.shares_held = 100
    ts.avg_basis_per_share = 50.0
    save_state(initial, path=state_path)

    client = MagicMock()
    pos = MagicMock()
    pos.symbol = "PLTR"
    pos.qty = "100"
    pos.qty_available = "100"
    pos.avg_entry_price = "50"
    client.get_open_position.return_value = pos
    client.get_orders.return_value = []
    client.get_all_positions.return_value = [pos]
    client.get_account.return_value = MagicMock(
        options_buying_power="100000", buying_power="100000"
    )
    client.submit_order.return_value = MagicMock(id="ord-call")

    today = date(2025, 1, 1)
    exp = today + timedelta(days=21)
    # Provide a chain that includes a strike at 55 (the basis-anchored target)
    # AND a strike at 44 (the spot-anchored target). The picker should choose
    # the closer one to the BASIS-anchored target.
    call_chain = [
        OptionCandidate(
            symbol=f"PLTR{exp.strftime('%y%m%d')}C{int(s*1000):08d}",
            underlying="PLTR",
            expiration=exp,
            strike=s,
            type="call",
            bid=0.80,
            ask=0.90,
        )
        for s in (44, 50, 55, 60)
    ]
    _patched(
        monkeypatch,
        trading_client=client,
        stock_spot=40.0,  # spot well below basis
        put_chain=_put_chain(today),
        call_chain=call_chain,
    )
    with patch.object(bot_mod, "datetime") as dt:
        dt.now.return_value = MagicMock(date=lambda: today)
        bot_mod.run_once(_config())

    pltr = load_state(path=state_path).tickers["PLTR"]
    assert pltr.stage == Stage.CALL_OPEN
    # basis-anchored target = 50 * 1.10 = 55. NOT spot-anchored 40*1.10 = 44.
    assert pltr.active_contract["strike"] == 55


def test_call_strike_uses_effective_basis_with_premiums(monkeypatch, tmp_path: Path):
    """``effective_basis_per_share`` = avg_basis - cumulative_premium_this_lot.
    With basis=$50 and $2/share collected via earlier calls, effective is $48,
    so the 10% target is $48 * 1.10 = $52.80 -> closest strike in chain is 53.
    """
    state_path = _isolate_state(monkeypatch, tmp_path)
    initial = WheelState()
    ts = initial.get_or_create("PLTR")
    ts.stage = Stage.HOLDING
    ts.shares_held = 100
    ts.avg_basis_per_share = 50.0
    ts.cumulative_premium_this_lot = 2.0  # $200 over 100 shares
    save_state(initial, path=state_path)

    client = MagicMock()
    pos = MagicMock()
    pos.symbol = "PLTR"
    pos.qty = "100"
    pos.qty_available = "100"
    pos.avg_entry_price = "50"
    client.get_open_position.return_value = pos
    client.get_orders.return_value = []
    client.get_all_positions.return_value = [pos]
    client.get_account.return_value = MagicMock(
        options_buying_power="100000", buying_power="100000"
    )
    client.submit_order.return_value = MagicMock(id="ord-call")

    today = date(2025, 1, 1)
    exp = today + timedelta(days=21)
    # Strikes spanning the 52.80 target. 53 is the closest.
    call_chain = [
        OptionCandidate(
            symbol=f"PLTR{exp.strftime('%y%m%d')}C{int(s*1000):08d}",
            underlying="PLTR",
            expiration=exp,
            strike=s,
            type="call",
            bid=0.80,
            ask=0.90,
        )
        for s in (50, 51, 53, 55, 60)
    ]
    _patched(
        monkeypatch,
        trading_client=client,
        stock_spot=40.0,
        put_chain=_put_chain(today),
        call_chain=call_chain,
    )
    with patch.object(bot_mod, "datetime") as dt:
        dt.now.return_value = MagicMock(date=lambda: today)
        bot_mod.run_once(_config())

    pltr = load_state(path=state_path).tickers["PLTR"]
    assert pltr.stage == Stage.CALL_OPEN
    # effective basis = 50 - 2 = 48; target = 48 * 1.10 = 52.80; nearest = 53.
    assert pltr.active_contract["strike"] == 53


def test_daily_summary_contains_all_required_fields(monkeypatch, tmp_path: Path):
    """Spec: 'stage, premium collected, positions, total return'. The markdown
    file must surface ALL of those, not just reconciliations + actions."""
    state_path = _isolate_state(monkeypatch, tmp_path)
    state = WheelState()
    # Mix of stages so the per-ticker table has variety.
    pltr = state.get_or_create("PLTR")
    pltr.stage = Stage.HOLDING
    pltr.shares_held = 100
    pltr.avg_basis_per_share = 38.0
    pltr.cumulative_premium_this_lot = 0.60
    pltr.cumulative_premium_lifetime = 105.0
    pltr.cycles = [{"kind": "put_expired", "premium_kept": 105.0}]
    nanc = state.get_or_create("NANC")
    nanc.stage = Stage.IDLE
    nanc.cumulative_premium_lifetime = 50.0
    save_state(state, path=state_path)

    result = bot_mod.RunResult(
        reconciliations=[], actions=[], holds=[]
    )
    # Fake position so the Positions section has content.
    fake_pos = MagicMock()
    fake_pos.symbol = "PLTR"
    fake_pos.side = "long"
    fake_pos.qty = "100"
    fake_pos.avg_entry_price = "38.00"
    fake_pos.market_value = "4200.00"
    fake_pos.unrealized_pl = "400.00"

    path = bot_mod.write_daily_summary(
        result,
        today=date(2025, 6, 1),
        state=state,
        spot_lookup=lambda t: 42.0 if t == "PLTR" else None,
        positions=[fake_pos],
    )
    content = path.read_text()
    # Required fields per spec.
    assert "Stage" in content
    assert "Premium collected" in content or "premium collected" in content.lower()
    assert "Positions" in content
    assert "Total return" in content
    assert "PLTR" in content and "HOOD" not in content  # state contents
    # Per-ticker premium and basis surfaced.
    assert "$38" in content
    # Total return is realized + unrealized; sanity that it's there as a $.
    assert "$" in content


def test_total_return_includes_realized_and_unrealized():
    """Realized = cumulative_premium_lifetime + sum(called_away lot_pnl).
    Unrealized = (spot - effective_basis) * shares for HOLDING tickers."""
    state = WheelState()
    pltr = state.get_or_create("PLTR")
    pltr.stage = Stage.HOLDING
    pltr.shares_held = 100
    pltr.avg_basis_per_share = 38.0
    pltr.cumulative_premium_this_lot = 0.60  # eff basis = 37.40
    pltr.cumulative_premium_lifetime = 105.0
    pltr.cycles = [{"kind": "called_away", "lot_pnl": 200.0}]

    returns = bot_mod.total_return(state, spot_lookup=lambda t: 42.0)
    # realized = 105 (lifetime prem) + 200 (called_away lot pnl) = 305
    assert returns["realized"] == pytest.approx(305.0)
    # unrealized = (42 - 37.40) * 100 = 460
    assert returns["unrealized"] == pytest.approx(460.0)
    assert returns["total"] == pytest.approx(765.0)


def test_run_once_recovers_orphan_intent_without_duplicating(monkeypatch, tmp_path: Path):
    """A pending intent + an existing matching Alpaca order must NOT result
    in a second STO during the next cycle — this is the whole point of the
    journal."""
    state_path = _isolate_state(monkeypatch, tmp_path)
    # Seed a pending intent (the executor would have done this).
    import wheel_bot.state as state_mod
    state_mod.journal_intent(
        {
            "ticker": "PLTR",
            "intent": "sto_put",
            "client_order_id": "wheel-PLTR-put-orphan01",
            "contract": {
                "symbol": "PLTR250620P00040000",
                "type": "put",
                "strike": 40.0,
                "expiration": "2025-06-20",
                "premium_received": 1.05,
                "contracts": 1,
            },
        }
    )

    today = date(2025, 1, 1)
    client = _no_position_client()
    # Alpaca knows about the order (the journaled one) but it's not in
    # OPEN orders. get_order_by_client_id returns the matching order.
    recovered = MagicMock(id="ord-recovered")
    recovered.client_order_id = "wheel-PLTR-put-orphan01"
    client.get_order_by_client_id.return_value = recovered
    # AND the short put position is live, so per-ticker reconcile stays put.
    short_pos = MagicMock()
    short_pos.symbol = "PLTR250620P00040000"
    short_pos.qty = "-1"
    short_pos.qty_available = "-1"
    client.get_all_positions.return_value = [short_pos]

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

    pltr = load_state(path=state_path).tickers["PLTR"]
    assert pltr.stage == Stage.PUT_OPEN
    assert pltr.active_contract["client_order_id"] == "wheel-PLTR-put-orphan01"
    # No duplicate STO submitted in this cycle.
    assert all(a.action != "sto_put" for a in result.actions)
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
