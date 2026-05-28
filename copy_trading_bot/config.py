from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# Tim Moore (R-NC, House). Picked because he is the highest-volume active trader
# on Capitol Trades (200+ disclosed trades, exclusively stocks, most recent
# activity within the last few weeks). Override via COPY_POLITICIAN_ID.
DEFAULT_POLITICIAN_ID = "M001236"
DEFAULT_POLITICIAN_NAME = "Tim Moore"

# Notional dollars per copied trade. Conservative default — Alpaca paper
# accounts start at $100K so $500/trade gives ~200 slots before exhausting cash.
DEFAULT_TRADE_USD = 500.0

# Hard cap: never spend more than this on a single trade, regardless of mode.
DEFAULT_MAX_TRADE_USD = 2_000.0

# Capitol Trades pageSize. 24 is plenty for catching new disclosures hourly.
DEFAULT_PAGE_SIZE = 24


@dataclass(frozen=True)
class CopyTraderConfig:
    politician_id: str
    politician_name: str
    trade_usd: float
    max_trade_usd: float
    page_size: int
    dry_run: bool
    skip_older_than_days: int  # ignore disclosures with a traded-date older than this

    @property
    def politician_url(self) -> str:
        return (
            "https://www.capitoltrades.com/trades"
            f"?politician={self.politician_id}"
            f"&pageSize={self.page_size}"
            "&sortBy=-pubDate"
        )


def load_config() -> CopyTraderConfig:
    load_dotenv()
    return CopyTraderConfig(
        politician_id=os.getenv("COPY_POLITICIAN_ID", DEFAULT_POLITICIAN_ID),
        politician_name=os.getenv("COPY_POLITICIAN_NAME", DEFAULT_POLITICIAN_NAME),
        trade_usd=float(os.getenv("COPY_TRADE_USD", DEFAULT_TRADE_USD)),
        max_trade_usd=float(os.getenv("COPY_MAX_TRADE_USD", DEFAULT_MAX_TRADE_USD)),
        page_size=int(os.getenv("COPY_PAGE_SIZE", DEFAULT_PAGE_SIZE)),
        dry_run=os.getenv("COPY_DRY_RUN", "false").lower() in ("1", "true", "yes"),
        skip_older_than_days=int(os.getenv("COPY_SKIP_OLDER_THAN_DAYS", "90")),
    )
