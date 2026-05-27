from unittest.mock import MagicMock

import pytest

from alpaca.trading.enums import OrderClass, OrderSide
from alpaca.trading.requests import (
    MarketOrderRequest,
    StopOrderRequest,
)

from alpaca_starter import orders


def _captured_request(submit_fn, *args, **kwargs):
    client = MagicMock()
    submit_fn(client, *args, **kwargs)
    client.submit_order.assert_called_once()
    return client.submit_order.call_args.args[0]


def test_market_order_request_built():
    req = _captured_request(orders.submit_market_order, "AAPL", 2)
    assert isinstance(req, MarketOrderRequest)
    assert req.symbol == "AAPL"
    assert req.qty == 2
    assert req.side == OrderSide.BUY


def test_stop_order_defaults_to_sell():
    req = _captured_request(orders.submit_stop_order, "AAPL", 1, 140)
    assert isinstance(req, StopOrderRequest)
    assert req.side == OrderSide.SELL
    assert float(req.stop_price) == 140


def test_bracket_order_has_tp_and_sl_legs():
    req = _captured_request(
        orders.submit_bracket_order,
        "AAPL",
        1,
        take_profit_price=200,
        stop_loss_price=140,
    )
    assert req.order_class == OrderClass.BRACKET
    assert float(req.take_profit.limit_price) == 200
    assert float(req.stop_loss.stop_price) == 140


def test_bracket_order_stop_limit_leg():
    req = _captured_request(
        orders.submit_bracket_order,
        "AAPL",
        1,
        take_profit_price=200,
        stop_loss_price=140,
        stop_loss_limit_price=139,
    )
    assert float(req.stop_loss.limit_price) == 139


def test_trailing_stop_requires_exactly_one_trail_arg():
    client = MagicMock()
    with pytest.raises(ValueError):
        orders.submit_trailing_stop_order(client, "AAPL", 1)
    with pytest.raises(ValueError):
        orders.submit_trailing_stop_order(
            client, "AAPL", 1, trail_percent=5, trail_price=2
        )
    client.submit_order.assert_not_called()
