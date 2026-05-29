from __future__ import annotations

import json
from pathlib import Path

import pytest

from wheel_bot.state import (
    Stage,
    TickerState,
    WheelState,
    append_audit,
    load_state,
    save_state,
)


def test_ticker_state_roundtrips(tmp_path: Path):
    state = WheelState()
    ts = state.get_or_create("PLTR")
    ts.stage = Stage.PUT_OPEN
    ts.shares_held = 0
    ts.avg_basis_per_share = 38.50
    ts.cumulative_premium_this_lot = 0.42
    ts.cumulative_premium_lifetime = 12.75
    ts.active_contract = {
        "symbol": "PLTR250620P00040000",
        "strike": 40,
        "expiration": "2025-06-20",
        "premium_received": 1.05,
        "contracts": 1,
        "order_id": "abc",
        "client_order_id": "wheel-PLTR-put-deadbeef",
    }
    path = tmp_path / "state.json"
    save_state(state, path=path)

    loaded = load_state(path=path)
    assert "PLTR" in loaded.tickers
    pltr = loaded.tickers["PLTR"]
    assert pltr.stage == Stage.PUT_OPEN
    assert pltr.avg_basis_per_share == 38.50
    assert pltr.active_contract["symbol"] == "PLTR250620P00040000"
    assert pltr.cumulative_premium_lifetime == 12.75


def test_load_state_missing_file_returns_empty(tmp_path: Path):
    state = load_state(path=tmp_path / "absent.json")
    assert state.tickers == {}


def test_load_state_corrupt_file_returns_empty(tmp_path: Path):
    path = tmp_path / "garbled.json"
    path.write_text("{not json")
    # Corrupt state must not wedge the bot; reconciler picks up real
    # positions on the next run.
    assert load_state(path=path).tickers == {}


def test_save_state_is_atomic(tmp_path: Path):
    path = tmp_path / "state.json"
    state = WheelState()
    state.get_or_create("HOOD").avg_basis_per_share = 25.5
    save_state(state, path=path)
    # No leftover tmp files in the directory.
    tmps = [p for p in path.parent.iterdir() if p.name.startswith(".wheel_state.")]
    assert tmps == []
    payload = json.loads(path.read_text())
    assert payload["tickers"]["HOOD"]["avg_basis_per_share"] == 25.5


def test_save_state_overwrites_existing(tmp_path: Path):
    path = tmp_path / "state.json"
    s1 = WheelState()
    s1.get_or_create("AAA").shares_held = 100
    save_state(s1, path=path)
    s2 = WheelState()
    s2.get_or_create("AAA").shares_held = 200
    save_state(s2, path=path)
    loaded = load_state(path=path)
    assert loaded.tickers["AAA"].shares_held == 200


def test_append_audit_creates_and_appends(tmp_path: Path):
    apath = tmp_path / "audit.jsonl"
    append_audit({"kind": "reconcile", "ticker": "PLTR"}, path=apath)
    append_audit({"kind": "sto_put", "ticker": "PLTR"}, path=apath)
    lines = apath.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["kind"] == "reconcile"
    assert "ts" in json.loads(lines[1])


def test_get_or_create_is_idempotent():
    state = WheelState()
    a = state.get_or_create("PLTR")
    b = state.get_or_create("pltr")  # case-insensitive
    assert a is b


def test_premium_accumulation_pattern():
    """Simulate the basis ledger lifecycle: put assigned, calls collect premium,
    called away. Verifies the per-share bookkeeping the executor relies on."""
    state = TickerState(ticker="PLTR")
    # Put assigned at 40 for 1.00/sh premium.
    state.shares_held = 100
    state.avg_basis_per_share = 40 - 1.00  # 39.00 effective
    state.cumulative_premium_this_lot = 0.0
    assert state.avg_basis_per_share == pytest.approx(39.0)

    # Sell a call, premium 0.60/sh.
    state.cumulative_premium_this_lot += 0.60
    floor = state.avg_basis_per_share - state.cumulative_premium_this_lot
    assert floor == pytest.approx(38.40)

    # Another call cycle, 0.40/sh.
    state.cumulative_premium_this_lot += 0.40
    floor = state.avg_basis_per_share - state.cumulative_premium_this_lot
    assert floor == pytest.approx(38.0)
