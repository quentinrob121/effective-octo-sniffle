from __future__ import annotations

from unittest.mock import MagicMock

from alpaca_starter.tagged_orders import (
    MAX_COID_LEN,
    cancel_tagged,
    list_tagged,
    make_coid,
)


def _order(symbol: str, coid: str = "", oid: str | None = None):
    o = MagicMock()
    o.symbol = symbol
    o.client_order_id = coid
    o.id = oid or f"id-{symbol}-{coid}"
    return o


def test_make_coid_basic_structure():
    coid = make_coid("wheel", "PLTR", "put")
    parts = coid.split("-")
    assert parts[0] == "wheel"
    assert parts[1] == "PLTR"
    assert parts[2] == "put"
    assert len(parts[3]) == 8  # hex suffix


def test_make_coid_uppercases_symbol():
    coid = make_coid("wheel", "pltr", "call")
    assert coid.split("-")[1] == "PLTR"


def test_make_coid_omits_kind_when_none():
    coid = make_coid("wheel", "PLTR")
    parts = coid.split("-")
    assert parts == ["wheel", "PLTR", parts[-1]]
    assert len(parts[-1]) == 8


def test_make_coid_unique_across_calls():
    a = make_coid("wheel", "PLTR", "put")
    b = make_coid("wheel", "PLTR", "put")
    assert a != b


def test_make_coid_respects_alpaca_length_limit():
    # Pathologically long symbol — the hex suffix and prefix must still fit.
    coid = make_coid("wheel", "VERYLONGUNDERLIER", "put")
    assert len(coid) <= MAX_COID_LEN


def test_list_tagged_filters_by_prefix():
    client = MagicMock()
    client.get_orders.return_value = [
        _order("PLTR250620P00040000", coid="wheel-PLTR-put-aaaaaaaa"),
        _order("NVDA", coid="ladder-NVDA-1-bbbbbbbb"),
        _order("PLTR", coid="wheel-PLTR-call-cccccccc"),
    ]
    out = list_tagged(client, prefix="wheel")
    coids = [o.client_order_id for o in out]
    assert any(c.startswith("wheel-PLTR-put-") for c in coids)
    assert any(c.startswith("wheel-PLTR-call-") for c in coids)
    assert not any("ladder" in c for c in coids)


def test_list_tagged_filters_by_symbol():
    client = MagicMock()
    client.get_orders.return_value = [
        _order("PLTR", coid="wheel-PLTR-call-aaaaaaaa"),
        _order("HOOD", coid="wheel-HOOD-call-bbbbbbbb"),
    ]
    out = list_tagged(client, prefix="wheel", symbol="PLTR")
    assert len(out) == 1
    assert out[0].symbol == "PLTR"


def test_list_tagged_ignores_empty_coid():
    client = MagicMock()
    client.get_orders.return_value = [
        _order("PLTR", coid=""),
        _order("HOOD", coid="wheel-HOOD-call-bbbbbbbb"),
    ]
    out = list_tagged(client, prefix="wheel")
    assert [o.symbol for o in out] == ["HOOD"]


def test_cancel_tagged_only_touches_matching():
    client = MagicMock()
    client.get_orders.return_value = [
        _order("PLTR", coid="wheel-PLTR-put-aaaaaaaa", oid="id1"),
        _order("NVDA", coid="ladder-NVDA-1-bbbbbbbb", oid="id2"),
        _order("HOOD", coid="wheel-HOOD-put-cccccccc", oid="id3"),
    ]
    n = cancel_tagged(client, prefix="wheel")
    assert n == 2
    cancelled_ids = [c.args[0] for c in client.cancel_order_by_id.call_args_list]
    assert "id1" in cancelled_ids
    assert "id3" in cancelled_ids
    assert "id2" not in cancelled_ids


def test_cancel_tagged_scoped_to_symbol():
    client = MagicMock()
    client.get_orders.return_value = [
        _order("PLTR", coid="wheel-PLTR-put-aaaaaaaa", oid="id1"),
        _order("HOOD", coid="wheel-HOOD-put-cccccccc", oid="id2"),
    ]
    n = cancel_tagged(client, prefix="wheel", symbol="PLTR")
    assert n == 1
    client.cancel_order_by_id.assert_called_once_with("id1")
