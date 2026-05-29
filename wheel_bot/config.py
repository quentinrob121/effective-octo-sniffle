from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# Tag for every order this module places. Also doubles as the state-file
# directory under wheel_bot/state/, and as the prefix for client_order_id
# so list_tagged/cancel_tagged can find our orders later.
WHEEL_PREFIX = "wheel"


@dataclass(frozen=True)
class WheelConfig:
    tickers: tuple[str, ...]
    put_strike_offset_pct: float  # e.g. 10 = sell puts 10% below spot
    call_strike_offset_pct: float  # e.g. 10 = sell calls 10% above spot
    target_dte: int
    dte_min: int
    dte_max: int
    close_profit_pct: float  # e.g. 50 = BTC when premium is 50% decayed
    close_min_dte: int  # don't BTC if days_to_expiration <= this
    contracts_per_cycle: int
    min_premium_usd: float  # skip if bid * 100 < this (avoid pennies)
    dry_run: bool


def _split_tickers(raw: str) -> tuple[str, ...]:
    return tuple(t.strip().upper() for t in raw.split(",") if t.strip())


def load_config() -> WheelConfig:
    load_dotenv()
    return WheelConfig(
        tickers=_split_tickers(os.getenv("WHEEL_TICKERS", "PLTR,NANC,HOOD")),
        put_strike_offset_pct=float(os.getenv("WHEEL_PUT_STRIKE_OFFSET_PCT", "10")),
        call_strike_offset_pct=float(os.getenv("WHEEL_CALL_STRIKE_OFFSET_PCT", "10")),
        target_dte=int(os.getenv("WHEEL_TARGET_DTE", "21")),
        dte_min=int(os.getenv("WHEEL_DTE_MIN", "14")),
        dte_max=int(os.getenv("WHEEL_DTE_MAX", "28")),
        close_profit_pct=float(os.getenv("WHEEL_CLOSE_PROFIT_PCT", "50")),
        close_min_dte=int(os.getenv("WHEEL_CLOSE_MIN_DTE", "2")),
        contracts_per_cycle=int(os.getenv("WHEEL_CONTRACTS_PER_CYCLE", "1")),
        min_premium_usd=float(os.getenv("WHEEL_MIN_PREMIUM_USD", "10")),
        dry_run=os.getenv("WHEEL_DRY_RUN", "false").lower() in ("1", "true", "yes"),
    )
