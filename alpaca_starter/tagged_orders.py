from __future__ import annotations

import logging
import uuid

from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest

log = logging.getLogger("alpaca_starter.tagged_orders")

# Alpaca caps client_order_id at 48 chars. We compose:
#   "<prefix>-<sym>-<kind>-<8 hex>"   e.g. "wheel-PLTR-put-3f1a9b8e"
# The kind segment is optional; we truncate symbol if necessary so the total
# never exceeds 48 characters even for long underliers.
MAX_COID_LEN = 48


def make_coid(prefix: str, symbol: str, kind: str | None = None) -> str:
    """Tag an order so we can later list/cancel only orders we own.

    Returns ``"{prefix}-{SYM}[-{kind}]-{8 hex random}"`` within Alpaca's
    48-char client_order_id limit.
    """
    suffix = uuid.uuid4().hex[:8]
    sym = (symbol or "").upper()
    parts = [prefix, sym]
    if kind:
        parts.append(kind)
    parts.append(suffix)
    coid = "-".join(parts)
    if len(coid) <= MAX_COID_LEN:
        return coid
    # Trim the symbol segment to fit. Suffix + prefix + kind are load-bearing
    # for filtering, so we sacrifice symbol length first.
    overflow = len(coid) - MAX_COID_LEN
    sym_trimmed = sym[: max(1, len(sym) - overflow)]
    parts = [prefix, sym_trimmed]
    if kind:
        parts.append(kind)
    parts.append(suffix)
    return "-".join(parts)


def _open_orders(client: TradingClient) -> list:
    return client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))


def list_tagged(
    client: TradingClient,
    prefix: str,
    symbol: str | None = None,
) -> list:
    """All open orders whose ``client_order_id`` starts with ``f"{prefix}-"``.

    The ``symbol`` filter compares against the order's ``symbol`` field — for
    options that's the OCC symbol (e.g. ``PLTR250620P00040000``), so passing
    ``symbol="PLTR"`` will NOT match the option leg. Use ``symbol=None`` to
    grab everything tagged with ``prefix`` and filter further at the caller.
    """
    out = []
    needle = f"{prefix}-"
    for o in _open_orders(client):
        coid = getattr(o, "client_order_id", "") or ""
        if not coid.startswith(needle):
            continue
        if symbol and (getattr(o, "symbol", "") or "").upper() != symbol.upper():
            continue
        out.append(o)
    return out


def cancel_tagged(
    client: TradingClient,
    prefix: str,
    symbol: str | None = None,
) -> int:
    """Cancel every open tagged order (optionally for a single symbol).

    Returns the count actually cancelled. Per-order cancel failures are
    logged but do not abort the loop — best-effort cleanup.
    """
    count = 0
    for o in list_tagged(client, prefix, symbol):
        try:
            client.cancel_order_by_id(o.id)
            count += 1
        except APIError as exc:
            log.warning("failed to cancel %s: %s", o.id, exc)
    return count
