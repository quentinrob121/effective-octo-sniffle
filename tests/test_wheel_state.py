from __future__ import annotations

import json
from pathlib import Path

import pytest

from wheel_bot.state import (
    Stage,
    TickerState,
    WheelState,
    append_audit,
    clear_intent,
    journal_intent,
    load_pending_intents,
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


def test_load_state_corrupt_file_backs_up_and_returns_empty(tmp_path: Path):
    """Silently dropping a corrupt state file is dangerous — we'd lose all
    record of open positions. The fix: rename the corrupt file aside with a
    timestamp so an operator can inspect it, then start fresh."""
    path = tmp_path / "state.json"
    path.write_text("{this is not json")
    state = load_state(path=path)
    assert state.tickers == {}
    # A backup file should now exist with a .corrupt-<ts> suffix.
    backups = list(tmp_path.glob("state.json.corrupt-*"))
    assert backups, "corrupt file must be preserved as a .corrupt-<ts> backup"
    # And the original path is gone (replaced).
    assert not path.exists()


def test_pending_intent_journal_roundtrip(tmp_path: Path):
    """The journal is the orphan-recovery contract: write before submit, read
    on startup, clear after persistence. The intent dict must round-trip
    intact so recovery can rebuild active_contract from it."""
    path = tmp_path / "pending_intents.json"
    assert load_pending_intents(path=path) == []
    intent = {
        "ticker": "PLTR",
        "intent": "sto_put",
        "client_order_id": "wheel-PLTR-put-deadbeef",
        "contract": {
            "symbol": "PLTR250620P00040000",
            "type": "put",
            "strike": 40.0,
            "expiration": "2025-06-20",
            "premium_received": 1.05,
            "contracts": 1,
        },
    }
    journal_intent(intent, path=path)
    loaded = load_pending_intents(path=path)
    assert len(loaded) == 1
    assert loaded[0]["client_order_id"] == "wheel-PLTR-put-deadbeef"
    assert loaded[0]["contract"]["strike"] == 40.0
    # Append a second intent, clear the first, verify only the second remains.
    journal_intent(
        {"ticker": "HOOD", "intent": "btc", "client_order_id": "wheel-HOOD-btc-cafebabe"},
        path=path,
    )
    assert len(load_pending_intents(path=path)) == 2
    clear_intent("wheel-PLTR-put-deadbeef", path=path)
    remaining = load_pending_intents(path=path)
    assert len(remaining) == 1
    assert remaining[0]["client_order_id"] == "wheel-HOOD-btc-cafebabe"
    # Clearing a missing coid is a no-op.
    clear_intent("does-not-exist", path=path)
    assert len(load_pending_intents(path=path)) == 1


def test_journal_intent_requires_client_order_id(tmp_path: Path):
    """Without a client_order_id, recovery can't match the intent to an
    Alpaca order — fail fast at write time."""
    path = tmp_path / "pending_intents.json"
    with pytest.raises(ValueError):
        journal_intent({"ticker": "PLTR", "intent": "sto_put"}, path=path)


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
