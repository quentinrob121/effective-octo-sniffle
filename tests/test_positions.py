from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from alpaca_starter import positions


def _fake_position(**overrides):
    base = dict(
        symbol="AAPL",
        side="long",
        qty="3",
        avg_entry_price="150",
        current_price="160",
        market_value="480",
        unrealized_pl="30",
        unrealized_plpc="0.0666",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_position_summary_maps_fields():
    client = MagicMock()
    client.get_all_positions.return_value = [_fake_position()]

    rows = positions.position_summary(client)

    assert rows == [
        {
            "symbol": "AAPL",
            "side": "long",
            "qty": "3",
            "avg_entry_price": "150",
            "current_price": "160",
            "market_value": "480",
            "unrealized_pl": "30",
            "unrealized_plpc": "0.0666",
        }
    ]


def test_close_position_rejects_qty_and_percentage():
    client = MagicMock()
    with pytest.raises(ValueError):
        positions.close_position(client, "AAPL", qty=1, percentage=50)
    client.close_position.assert_not_called()


def test_close_position_partial_by_percentage():
    client = MagicMock()
    positions.close_position(client, "AAPL", percentage=50)
    client.close_position.assert_called_once()
    _, kwargs = client.close_position.call_args
    assert kwargs["close_options"].percentage == "50"


def test_close_all_positions_cancels_orders():
    client = MagicMock()
    positions.close_all_positions(client)
    client.close_all_positions.assert_called_once_with(cancel_orders=True)
