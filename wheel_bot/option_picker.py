from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class OptionCandidate:
    """A normalized view of one contract from an option chain.

    Kept as a plain dataclass (rather than the SDK type) so the picker is a
    pure function that's trivial to test without touching Alpaca.
    """

    symbol: str  # OCC symbol
    underlying: str
    expiration: date
    strike: float
    type: str  # "call" or "put"
    bid: float = 0.0
    ask: float = 0.0


def filter_chain(
    chain: list[OptionCandidate],
    type: str,
    expiration: date | None = None,
) -> list[OptionCandidate]:
    """Filter contracts by type and (optionally) exact expiration date."""
    out = [c for c in chain if c.type == type]
    if expiration is not None:
        out = [c for c in out if c.expiration == expiration]
    return out


def pick_expiration(
    chain: list[OptionCandidate],
    today: date,
    target_dte: int = 21,
    dte_min: int = 14,
    dte_max: int = 28,
) -> date | None:
    """Pick the expiration in ``chain`` nearest to ``target_dte`` days from
    ``today``, restricted to the ``[dte_min, dte_max]`` window.

    Returns ``None`` if no contract in the chain has an expiration that falls
    inside the window — this matters for monthly-only chains (NANC) where no
    weekly may exist in the target band.
    """
    candidates: set[date] = set()
    for c in chain:
        dte = (c.expiration - today).days
        if dte_min <= dte <= dte_max:
            candidates.add(c.expiration)
    if not candidates:
        return None
    return min(candidates, key=lambda d: abs((d - today).days - target_dte))


def pick_strike(
    chain_at_expiration: list[OptionCandidate],
    target_price: float,
    type: str,
) -> OptionCandidate | None:
    """Pick the contract whose strike is closest to ``target_price``.

    Direction is symmetric (nearest in either direction). Callers that need a
    one-sided pick should pre-filter the chain. Returns ``None`` if no contract
    of the requested type is present.
    """
    typed = [c for c in chain_at_expiration if c.type == type]
    if not typed:
        return None
    return min(typed, key=lambda c: abs(c.strike - target_price))
