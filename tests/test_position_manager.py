from unittest.mock import MagicMock

from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import LimitOrderRequest, TrailingStopOrderRequest

from position_manager.config import (
    MANAGED_STOP_PREFIX,
    PositionManagerConfig,
)
from position_manager.manager import (
    cancel_managed_orders,
    ensure_trailing_stops,
    list_managed_orders,
)


def _config(**overrides) -> PositionManagerConfig:
    base = dict(trail_pct=10.0, reentry_pct=0.0, reentry_usd=500.0, dry_run=False)
    base.update(overrides)
    return PositionManagerConfig(**base)


def _position(symbol: str, qty: float = 10, current: float = 100.0):
    p = MagicMock()
    p.symbol = symbol
    p.qty = str(qty)
    p.qty_available = str(qty)
    p.current_price = str(current)
    p.avg_entry_price = "90"
    return p


def _order(symbol: str, side: str, coid: str = ""):
    o = MagicMock()
    o.symbol = symbol
    o.side = f"OrderSide.{side.upper()}"
    o.client_order_id = coid
    o.id = f"id-{symbol}-{side}"
    return o


def test_places_trailing_stop_for_unprotected_position():
    client = MagicMock()
    client.get_all_positions.return_value = [_position("AAPL")]
    client.get_orders.return_value = []  # nothing already open
    client.submit_order.return_value = MagicMock(id="o1")

    actions = ensure_trailing_stops(client, _config())
    assert [a.action for a in actions] == ["stop-placed"]

    req = client.submit_order.call_args.args[0]
    assert isinstance(req, TrailingStopOrderRequest)
    assert req.symbol == "AAPL"
    assert req.side == OrderSide.SELL
    assert req.time_in_force == TimeInForce.GTC
    assert float(req.trail_percent) == 10.0
    assert req.client_order_id.startswith(MANAGED_STOP_PREFIX + "-stop-")


def test_does_not_stack_when_sell_already_open():
    client = MagicMock()
    client.get_all_positions.return_value = [_position("AAPL")]
    client.get_orders.return_value = [_order("AAPL", "SELL")]

    actions = ensure_trailing_stops(client, _config())
    assert [a.action for a in actions] == ["stop-exists"]
    client.submit_order.assert_not_called()


def test_dry_run_does_not_submit():
    client = MagicMock()
    client.get_all_positions.return_value = [_position("AAPL")]
    client.get_orders.return_value = []
    ensure_trailing_stops(client, _config(dry_run=True))
    client.submit_order.assert_not_called()


def test_reentry_places_limit_buy_when_enabled():
    client = MagicMock()
    client.get_all_positions.return_value = [_position("AAPL", current=100.0)]
    client.get_orders.return_value = []
    client.submit_order.return_value = MagicMock(id="o2")

    actions = ensure_trailing_stops(
        client, _config(reentry_pct=5.0, reentry_usd=500.0)
    )
    kinds = [a.action for a in actions]
    assert "stop-placed" in kinds
    assert "reentry-placed" in kinds

    submitted = [c.args[0] for c in client.submit_order.call_args_list]
    limit_reqs = [r for r in submitted if isinstance(r, LimitOrderRequest)]
    assert len(limit_reqs) == 1
    assert limit_reqs[0].side == OrderSide.BUY
    assert float(limit_reqs[0].limit_price) == 95.00  # 5% below $100
    assert limit_reqs[0].qty == 5  # 500 // 95


def test_reentry_off_by_default():
    client = MagicMock()
    client.get_all_positions.return_value = [_position("AAPL")]
    client.get_orders.return_value = []
    client.submit_order.return_value = MagicMock(id="o1")
    actions = ensure_trailing_stops(client, _config())
    assert [a.action for a in actions] == ["stop-placed"]
    assert client.submit_order.call_count == 1


def test_list_managed_orders_filters_by_tag():
    client = MagicMock()
    client.get_orders.return_value = [
        _order("AAPL", "SELL", coid=f"{MANAGED_STOP_PREFIX}-stop-AAPL-abc"),
        _order("NVDA", "BUY", coid="not-managed"),
        _order("TSLA", "SELL", coid=f"{MANAGED_STOP_PREFIX}-stop-TSLA-def"),
    ]
    out = list_managed_orders(client)
    assert [o.symbol for o in out] == ["AAPL", "TSLA"]


def test_cancel_managed_orders_only_touches_tagged():
    client = MagicMock()
    client.get_orders.return_value = [
        _order("AAPL", "SELL", coid=f"{MANAGED_STOP_PREFIX}-stop-AAPL-abc"),
        _order("NVDA", "BUY", coid="not-managed"),
    ]
    n = cancel_managed_orders(client)
    assert n == 1
    client.cancel_order_by_id.assert_called_once()


def test_no_positions_returns_empty():
    client = MagicMock()
    client.get_all_positions.return_value = []
    client.get_orders.return_value = []
    assert ensure_trailing_stops(client, _config()) == []
