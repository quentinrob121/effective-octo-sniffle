from __future__ import annotations

import enum
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

STATE_DIR = Path(__file__).parent / "state"
STATE_PATH = STATE_DIR / "wheel_state.json"
AUDIT_PATH = STATE_DIR / "audit.jsonl"
SUMMARIES_DIR = STATE_DIR / "summaries"


class Stage(str, enum.Enum):
    """Per-ticker phase of the wheel.

    DISABLED = paused via CLI; reconciler still runs but no STO/BTC happens.
    IDLE     = no shares, no open option. Ready to STO a cash-secured put.
    PUT_OPEN = short put outstanding; waiting for assignment / expiry / close.
    HOLDING  = 100*N shares assigned (or adopted); no open call. Ready to STO a covered call.
    CALL_OPEN = short call outstanding; waiting for call-away / expiry / close.
    """

    DISABLED = "DISABLED"
    IDLE = "IDLE"
    PUT_OPEN = "PUT_OPEN"
    HOLDING = "HOLDING"
    CALL_OPEN = "CALL_OPEN"


@dataclass
class TickerState:
    ticker: str
    stage: Stage = Stage.IDLE
    # OCC symbol, strike, expiration ISO, premium_received (per-share),
    # order_id, client_order_id, opened_at ISO, contracts.
    active_contract: dict | None = None
    shares_held: int = 0
    avg_basis_per_share: float = 0.0
    # Per-share premium collected on calls in THIS lot. Resets to 0 when stage
    # returns to IDLE (i.e. lot closed). Put premium is NOT tracked here — it's
    # already folded into avg_basis_per_share when the put assigned.
    cumulative_premium_this_lot: float = 0.0
    cumulative_premium_lifetime: float = 0.0
    enabled: bool = True
    cycles: list[dict] = field(default_factory=list)  # completed-lot audit log

    @classmethod
    def from_dict(cls, data: dict) -> "TickerState":
        ts = cls(ticker=data["ticker"])
        ts.stage = Stage(data.get("stage", Stage.IDLE.value))
        ts.active_contract = data.get("active_contract")
        ts.shares_held = int(data.get("shares_held", 0))
        ts.avg_basis_per_share = float(data.get("avg_basis_per_share", 0.0))
        ts.cumulative_premium_this_lot = float(
            data.get("cumulative_premium_this_lot", 0.0)
        )
        ts.cumulative_premium_lifetime = float(
            data.get("cumulative_premium_lifetime", 0.0)
        )
        ts.enabled = bool(data.get("enabled", True))
        ts.cycles = list(data.get("cycles", []))
        return ts

    def to_dict(self) -> dict:
        d = asdict(self)
        d["stage"] = self.stage.value
        return d


@dataclass
class WheelState:
    tickers: dict[str, TickerState] = field(default_factory=dict)

    def get_or_create(self, ticker: str) -> TickerState:
        ticker = ticker.upper()
        if ticker not in self.tickers:
            self.tickers[ticker] = TickerState(ticker=ticker)
        return self.tickers[ticker]

    @classmethod
    def from_dict(cls, data: dict) -> "WheelState":
        return cls(
            tickers={
                k.upper(): TickerState.from_dict(v)
                for k, v in data.get("tickers", {}).items()
            }
        )

    def to_dict(self) -> dict:
        return {"tickers": {k: v.to_dict() for k, v in self.tickers.items()}}


def load_state(path: Path = STATE_PATH) -> WheelState:
    if not path.exists():
        return WheelState()
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        # A truncated/garbled state file would otherwise wedge the bot.
        # Start fresh — reconciler will adopt any real Alpaca positions on
        # the next run.
        return WheelState()
    return WheelState.from_dict(data)


def save_state(state: WheelState, path: Path = STATE_PATH) -> None:
    """Atomic write: temp file + os.replace so a mid-write crash leaves the
    old file intact rather than producing a half-written JSON we can't parse."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state.to_dict(), indent=2, sort_keys=True) + "\n"
    fd, tmp = tempfile.mkstemp(
        prefix=".wheel_state.", suffix=".json.tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(payload)
        os.replace(tmp, path)
    except Exception:
        # Best-effort cleanup; never let the cleanup mask the original error.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def append_audit(entry: dict, path: Path = AUDIT_PATH) -> None:
    """Append-only audit log of state transitions / orders. JSON Lines so we
    can grep without parsing the whole file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = dict(entry)
    entry.setdefault("ts", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    with path.open("a") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")
