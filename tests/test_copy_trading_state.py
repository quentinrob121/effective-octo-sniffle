import json

from copy_trading_bot import state as state_mod


def test_load_and_append_round_trip(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    monkeypatch.setattr(state_mod, "STATE_PATH", path)
    assert state_mod.load_processed() == set()

    state_mod.append_processed(
        [
            {"signature": "aaa", "ticker": "NVDA", "type": "buy"},
            {"signature": "bbb", "ticker": "AAPL", "type": "sell"},
        ]
    )
    assert state_mod.load_processed() == {"aaa", "bbb"}

    # Idempotent: appending the same signature is a no-op.
    state_mod.append_processed([{"signature": "aaa", "ticker": "NVDA", "type": "buy"}])
    data = json.loads(path.read_text())
    assert len(data["trades"]) == 2


def test_append_caps_history(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    monkeypatch.setattr(state_mod, "STATE_PATH", path)
    monkeypatch.setattr(state_mod, "MAX_ENTRIES", 3)
    state_mod.append_processed(
        [{"signature": f"sig{i}", "ticker": "X", "type": "buy"} for i in range(5)]
    )
    data = json.loads(path.read_text())
    assert [t["signature"] for t in data["trades"]] == ["sig2", "sig3", "sig4"]
