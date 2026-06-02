from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# Trailing-stop distance below the high-water mark. Alpaca trails this
# server-side, so we don't have to keep "tightening" — placing the order once
# is enough.
DEFAULT_TRAIL_PCT = 10.0

# Tag for the SELL orders this module places, so list/cancel only touch ours.
MANAGED_STOP_PREFIX = "psmgr"

# If set, place a GTC re-entry limit BUY this fraction below the stop trigger
# whenever we attach a fresh stop. 0 disables re-entry. Default off (opt-in
# because it commits cash you may not want re-deployed).
DEFAULT_REENTRY_PCT = 0.0
DEFAULT_REENTRY_USD = 500.0


@dataclass(frozen=True)
class PositionManagerConfig:
    trail_pct: float
    reentry_pct: float  # 0 to disable; otherwise % below trigger to bid
    reentry_usd: float
    dry_run: bool


def load_config() -> PositionManagerConfig:
    load_dotenv()
    return PositionManagerConfig(
        trail_pct=float(os.getenv("PSMGR_TRAIL_PCT", DEFAULT_TRAIL_PCT)),
        reentry_pct=float(os.getenv("PSMGR_REENTRY_PCT", DEFAULT_REENTRY_PCT)),
        reentry_usd=float(os.getenv("PSMGR_REENTRY_USD", DEFAULT_REENTRY_USD)),
        dry_run=os.getenv("PSMGR_DRY_RUN", "false").lower() in ("1", "true", "yes"),
    )
