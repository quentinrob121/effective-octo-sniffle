from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class PoliticianTrade:
    politician_id: str
    politician_name: str
    ticker: str
    issuer: str
    traded_date: date
    published_date: date
    tx_type: str  # "buy" | "sell" | "exchange" | ...
    size_range: str  # e.g. "15K–50K"
    price: float | None  # disclosed reference price, may be None

    @property
    def signature(self) -> str:
        # Intentionally excludes price: Capitol Trades occasionally amends a
        # disclosed price (corrections, post-hoc adjustments) and we don't
        # want such an amendment to look like a brand-new trade and re-fire.
        raw = "|".join(
            [
                self.politician_id,
                self.ticker,
                self.traded_date.isoformat(),
                self.tx_type,
                self.size_range,
            ]
        )
        return hashlib.sha1(raw.encode()).hexdigest()[:16]
