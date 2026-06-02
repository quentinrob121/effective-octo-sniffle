from datetime import date
from unittest.mock import MagicMock

import pytest

from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderSide
from alpaca.trading.requests import MarketOrderRequest

from copy_trading_bot.config import CopyTraderConfig
from copy_trading_bot.executor import execute
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


def _trade(tx="buy", ticker="NVDA", price=100.0) -> PoliticianTrade:
    return PoliticianTrade(
        politician_id="M001236",
        politician_name="Tim Moore",
        ticker=ticker,
        issuer=f"{ticker} Inc",
        traded_date=date(2026, 5, 1),
        published_date=date(2026, 5, 10),
        tx_type=tx,
        size_range="15K–50K",
        price=price,
    )


class _Quotes:
    def __init__(self, price):
        self._price = price

    def latest_price(self, _symbol):
        return self._price


def test_buy_sizes_by_usd_budget():
    client = MagicMock()
    submitted = MagicMock(id="abc")
    client.submit_order.return_value = submitted
    res = execute(_trade(tx="buy", price=100.0), client, _Quotes(50.0), _config())
    assert res.action == "submitted"
    req = client.submit_order.call_args.args[0]
    assert isinstance(req, MarketOrderRequest)
    assert req.symbol == "NVDA"
    assert req.side == OrderSide.BUY
    # $500 budget / $50 quote = 10 shares
    assert req.qty == 10


def test_buy_falls_back_to_reference_price_when_quote_missing():
    client = MagicMock()
    client.submit_order.return_value = MagicMock(id="abc")
    execute(_trade(tx="buy", price=20.0), client, _Quotes(None), _config())
    req = client.submit_order.call_args.args[0]
    assert req.qty == 25  # 500 / 20


def test_buy_skips_when_share_unaffordable():
    client = MagicMock()
    res = execute(_trade(tx="buy", price=10_000.0), client, _Quotes(10_000.0), _config())
    assert res.action == "skipped"
    client.submit_order.assert_not_called()


def _api_error(status: int, message: str = "boom") -> APIError:
    """APIError's status_code is derived from http_error.response.status_code,
    so we have to fabricate that chain to test status-based branching."""
    fake_http = MagicMock()
    fake_http.response.status_code = status
    return APIError(message, http_error=fake_http)


def test_sell_skipped_when_no_position():
    client = MagicMock()
    client.get_open_position.side_effect = _api_error(404, "position not found")
    res = execute(_trade(tx="sell"), client, _Quotes(50.0), _config())
    assert res.action == "skipped"
    assert "no open position" in res.detail
    client.submit_order.assert_not_called()


def test_sell_closes_full_held_position():
    client = MagicMock()
    client.get_open_position.return_value = MagicMock(qty="7", qty_available="7")
    client.submit_order.return_value = MagicMock(id="xyz")
    res = execute(_trade(tx="sell"), client, _Quotes(50.0), _config())
    assert res.action == "submitted"
    req = client.submit_order.call_args.args[0]
    assert req.side == OrderSide.SELL
    assert req.qty == 7.0


def test_dry_run_never_submits():
    client = MagicMock()
    res = execute(
        _trade(tx="buy", price=50.0), client, _Quotes(50.0), _config(dry_run=True)
    )
    assert res.action == "skipped"
    assert "dry-run" in res.detail
    client.submit_order.assert_not_called()


def test_alpaca_error_returns_error_result():
    client = MagicMock()
    client.submit_order.side_effect = APIError("market closed")
    res = execute(_trade(tx="buy", price=50.0), client, _Quotes(50.0), _config())
    assert res.action == "error"
    assert "rejected" in res.detail


def test_buy_drops_ladder_when_enabled():
    from alpaca.trading.enums import TimeInForce
    from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

    client = MagicMock()
    client.submit_order.return_value = MagicMock(id="abc")
    config = _config(enable_ladder=True, trade_usd=500.0)
    res = execute(_trade(tx="buy", price=100.0), client, _Quotes(100.0), config)
    assert res.action == "submitted"
    # 1 mirror MARKET buy + 4 ladder GTC LIMIT buys = 5 orders.
    assert client.submit_order.call_count == 5

    calls = [c.args[0] for c in client.submit_order.call_args_list]
    assert isinstance(calls[0], MarketOrderRequest)
    for ladder_req in calls[1:]:
        assert isinstance(ladder_req, LimitOrderRequest)
        assert ladder_req.time_in_force == TimeInForce.GTC
        assert ladder_req.side == OrderSide.BUY


def test_buy_does_not_drop_ladder_when_disabled():
    client = MagicMock()
    client.submit_order.return_value = MagicMock(id="abc")
    config = _config(enable_ladder=False)
    execute(_trade(tx="buy", price=100.0), client, _Quotes(100.0), config)
    assert client.submit_order.call_count == 1


def test_sell_propagates_non_404_api_errors():
    """Rate limits / 5xx must not be silently swallowed — that would mark
    the sell processed and we'd hold a stock the politician already exited."""
    client = MagicMock()
    client.get_open_position.side_effect = _api_error(429, "rate limited")
    with pytest.raises(APIError):
        execute(_trade(tx="sell"), client, _Quotes(50.0), _config())


def test_ladder_exception_does_not_break_mirror_buy_recording():
    """If the ladder raises an unexpected exception, the mirror-buy has
    already gone through and must NOT cause `execute` to propagate — bot.py
    relies on `execute` returning a result so the trade gets persisted."""
    client = MagicMock()
    # First call (mirror buy) succeeds; subsequent ladder placements blow up.
    client.submit_order.side_effect = [MagicMock(id="ok")] + [
        RuntimeError("transport blew up") for _ in range(4)
    ]
    config = _config(enable_ladder=True, trade_usd=500.0)
    res = execute(_trade(tx="buy", price=100.0), client, _Quotes(100.0), config)
    assert res.action == "submitted"
