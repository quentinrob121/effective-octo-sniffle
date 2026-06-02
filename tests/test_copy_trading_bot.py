"""Tests for the run-once orchestrator. Mocked at the scraper + alpaca seams
so no network is touched."""

from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from copy_trading_bot import bot as bot_mod
from copy_trading_bot import state as state_mod
from copy_trading_bot.config import CopyTraderConfig
from copy_trading_bot.executor import ExecutionResult
from copy_trading_bot.models import PoliticianTrade


def _config(**overrides) -> CopyTraderConfig:
    base = dict(
        politician_id="M001236",
        politician_name="Tim Moore",
        trade_usd=500.0,
        max_trade_usd=2_000.0,
        page_size=24,
        dry_run=False,
        skip_older_than_days=90,
    )
    base.update(overrides)
    return CopyTraderConfig(**base)


def _trade(ticker: str, tx: str = "buy") -> PoliticianTrade:
    return PoliticianTrade(
        politician_id="M001236",
        politician_name="Tim Moore",
        ticker=ticker,
        issuer=f"{ticker} Inc",
        traded_date=date.today(),
        published_date=date.today(),
        tx_type=tx,
        size_range="15K-50K",
        price=100.0,
    )


def test_run_once_persists_each_trade_individually(tmp_path, monkeypatch):
    """C4 fix: if execute() raises mid-loop, trades already processed must
    survive in state. This is the file-write-after-each-success contract."""
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(state_mod, "STATE_PATH", state_path)

    trades = [_trade("AAA"), _trade("BBB"), _trade("CCC")]
    monkeypatch.setattr(bot_mod, "fetch_trades", lambda *a, **k: trades)
    monkeypatch.setattr(bot_mod, "build_trading_client", lambda: MagicMock())
    monkeypatch.setattr(bot_mod, "build_data_client", lambda: MagicMock())
    monkeypatch.setattr(bot_mod, "AlpacaQuoteSource", lambda _client: MagicMock())

    call_count = {"n": 0}

    def fake_execute(trade, *_a, **_k):
        call_count["n"] += 1
        if trade.ticker == "BBB":
            raise RuntimeError("alpaca exploded mid-run")
        return ExecutionResult(trade, "submitted", "ok", order_id="x")

    with patch.object(bot_mod, "execute", fake_execute):
        with pytest.raises(RuntimeError):
            bot_mod.run_once(_config())

    # AAA was processed before BBB crashed; must be persisted.
    assert state_mod.load_processed() == {trades[0].signature}
    # CCC was never attempted.
    assert call_count["n"] == 2


def test_run_once_skips_already_processed(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(state_mod, "STATE_PATH", state_path)

    trades = [_trade("AAA"), _trade("BBB")]
    state_mod.append_processed([{"signature": trades[0].signature}])

    monkeypatch.setattr(bot_mod, "fetch_trades", lambda *a, **k: trades)
    monkeypatch.setattr(bot_mod, "build_trading_client", lambda: MagicMock())
    monkeypatch.setattr(bot_mod, "build_data_client", lambda: MagicMock())
    monkeypatch.setattr(bot_mod, "AlpacaQuoteSource", lambda _client: MagicMock())

    seen = []

    def fake_execute(trade, *_a, **_k):
        seen.append(trade.ticker)
        return ExecutionResult(trade, "submitted", "ok", order_id="x")

    with patch.object(bot_mod, "execute", fake_execute):
        bot_mod.run_once(_config())

    assert seen == ["BBB"]


def test_run_once_does_not_record_errors(tmp_path, monkeypatch):
    """Errors must stay un-recorded so the next cron run retries them."""
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(state_mod, "STATE_PATH", state_path)

    trade = _trade("AAA")
    monkeypatch.setattr(bot_mod, "fetch_trades", lambda *a, **k: [trade])
    monkeypatch.setattr(bot_mod, "build_trading_client", lambda: MagicMock())
    monkeypatch.setattr(bot_mod, "build_data_client", lambda: MagicMock())
    monkeypatch.setattr(bot_mod, "AlpacaQuoteSource", lambda _client: MagicMock())

    def fake_execute(trade, *_a, **_k):
        return ExecutionResult(trade, "error", "alpaca rejected")

    with patch.object(bot_mod, "execute", fake_execute):
        bot_mod.run_once(_config())

    assert state_mod.load_processed() == set()
