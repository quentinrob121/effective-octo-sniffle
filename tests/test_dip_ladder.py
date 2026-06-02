from unittest.mock import MagicMock

import pytest

from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import LimitOrderRequest

from dip_ladder import LadderConfig, place_ladder, plan_ladder
from dip_ladder.config import LADDER_CLIENT_ID_PREFIX
from dip_ladder.ladder import cancel_ladder, list_ladder_orders


def test_plan_matches_screenshot_geometry_for_360_ref():
    rungs = plan_ladder(ref_price=360.0, base_qty=10)
    # Levels and quantities should match the table in the screenshot.
    assert [round(r.limit_price, 2) for r in rungs] == [306.00, 270.00, 234.00, 180.00]
    assert [r.qty for r in rungs] == [10, 20, 30, 50]


def test_plan_with_dollar_base_scales_by_weight():
    rungs = plan_ladder(ref_price=100.0, base_usd=500.0)
    # Shallowest rung at $85, $500 / $85 = ~5 shares -> 5
    # Deeper rungs scale 2x, 3x, 5x in $.
    qtys = [r.qty for r in rungs]
    assert qtys[0] == 5  # 500 / 85
    # 2x dollars at $75 -> 13 ; 3x at $65 -> 23 ; 5x at $50 -> 50
    assert qtys[1] == 13
    assert qtys[2] == 23
    assert qtys[3] == 50


def test_plan_requires_exactly_one_sizing_arg():
    with pytest.raises(ValueError):
        plan_ladder(ref_price=100.0)
    with pytest.raises(ValueError):
        plan_ladder(ref_price=100.0, base_qty=10, base_usd=500.0)


def test_config_rejects_invalid_levels_and_weights():
    with pytest.raises(ValueError):
        LadderConfig(levels_pct=(0.1,), weights=(1.0, 2.0))
    with pytest.raises(ValueError):
        LadderConfig(levels_pct=(0.0,), weights=(1.0,))
    with pytest.raises(ValueError):
        LadderConfig(levels_pct=(1.0,), weights=(1.0,))
    with pytest.raises(ValueError):
        LadderConfig(levels_pct=(0.5,), weights=(0.0,))


def test_plan_rejects_total_above_cap():
    tight = LadderConfig(max_total_usd=1.0)
    with pytest.raises(ValueError):
        plan_ladder(ref_price=100.0, base_qty=10, config=tight)


def test_place_submits_one_gtc_limit_per_rung():
    client = MagicMock()
    client.submit_order.return_value = MagicMock(id="o1")
    placed = place_ladder(client, "NFLX", ref_price=360.0, base_qty=10)
    assert len(placed) == 4
    assert all(p.action == "submitted" for p in placed)
    assert client.submit_order.call_count == 4

    first_req = client.submit_order.call_args_list[0].args[0]
    assert isinstance(first_req, LimitOrderRequest)
    assert first_req.symbol == "NFLX"
    assert first_req.side == OrderSide.BUY
    assert first_req.time_in_force == TimeInForce.GTC
    assert first_req.qty == 10
    assert float(first_req.limit_price) == 306.0
    assert first_req.client_order_id.startswith(LADDER_CLIENT_ID_PREFIX + "-")


def test_place_dry_run_does_not_submit():
    client = MagicMock()
    placed = place_ladder(
        client, "NFLX", ref_price=360.0, base_qty=10, dry_run=True
    )
    assert all(p.action == "skipped" for p in placed)
    client.submit_order.assert_not_called()


def test_place_records_alpaca_errors_per_rung():
    client = MagicMock()
    client.submit_order.side_effect = [
        MagicMock(id="ok"),
        APIError("market closed"),
        MagicMock(id="ok"),
        MagicMock(id="ok"),
    ]
    placed = place_ladder(client, "NFLX", ref_price=360.0, base_qty=10)
    actions = [p.action for p in placed]
    assert actions == ["submitted", "error", "submitted", "submitted"]


def _fake_order(symbol, coid, status="OPEN"):
    o = MagicMock()
    o.symbol = symbol
    o.client_order_id = coid
    o.status = status
    o.id = f"id-{coid}"
    o.side = "buy"
    o.qty = 1
    o.limit_price = 100
    return o


def test_list_ladder_orders_filters_by_tag_and_symbol():
    client = MagicMock()
    client.get_orders.return_value = [
        _fake_order("NFLX", "ladder-NFLX-360-0-1"),
        _fake_order("NFLX", "not-a-ladder"),
        _fake_order("NVDA", "ladder-NVDA-100-0-1"),
    ]
    nflx_only = list_ladder_orders(client, "NFLX")
    assert [o.symbol for o in nflx_only] == ["NFLX"]
    all_ladders = list_ladder_orders(client)
    assert [o.symbol for o in all_ladders] == ["NFLX", "NVDA"]


def test_cancel_ladder_cancels_matched_orders():
    client = MagicMock()
    client.get_orders.return_value = [
        _fake_order("NFLX", "ladder-NFLX-360-0-1"),
        _fake_order("NFLX", "ladder-NFLX-360-1-1"),
    ]
    n = cancel_ladder(client, "NFLX")
    assert n == 2
    assert client.cancel_order_by_id.call_count == 2
