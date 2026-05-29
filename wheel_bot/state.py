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
PENDING_INTENTS_PATH = STATE_DIR / "pending_intents.json"


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
    """Load state, backing up corrupt files instead of silently dropping data.

    A corrupted JSON file is a real operational signal — silently returning
    an empty state would let the reconciler re-STO on top of forgotten open
    positions. We rename the bad file to ``<name>.corrupt-<utc-ts>`` so an
    operator can inspect it, and return empty so the reconciler can adopt
    any real Alpaca positions on the next run.
    """
    if not path.exists():
        return WheelState()
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        try:
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = path.with_suffix(path.suffix + f".corrupt-{ts}")
            os.replace(path, backup)
        except OSError:
            pass  # best-effort; the load still has to succeed
        return WheelState()
    return WheelState.from_dict(data)


def _atomic_write_json(path: Path, payload: str) -> None:
    """Temp-file + fsync + os.replace. The fsync is necessary because
    ``os.replace`` only guarantees atomicity at the directory-entry level —
    on a power loss between write() and rename() the new file could exist
    with zero bytes. fsync forces the new contents to disk first."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_state(state: WheelState, path: Path = STATE_PATH) -> None:
    """Atomic write of state JSON. See ``_atomic_write_json`` for the
    durability guarantees."""
    payload = json.dumps(state.to_dict(), indent=2, sort_keys=True) + "\n"
    _atomic_write_json(path, payload)


def append_audit(entry: dict, path: Path = AUDIT_PATH) -> None:
    """Append-only audit log of state transitions / orders. JSON Lines so we
    can grep without parsing the whole file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = dict(entry)
    entry.setdefault("ts", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    with path.open("a") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")


# ---------------------------------------------------------------------------
# Pending-intent journal (orphan-order recovery)
# ---------------------------------------------------------------------------
#
# The wheel executor's failure mode we're guarding against:
#   1. We submit STO put to Alpaca -> succeeds, order_id assigned.
#   2. We mutate state in memory.
#   3. We call save_state(...) -> raises (disk full, etc).
# At this point the order EXISTS at Alpaca but state still shows IDLE. The
# next cron tick would re-STO and we'd have TWO short puts on the same name.
#
# The journal pattern:
#   - Before submitting, write {ticker, intent, contract, client_order_id} to
#     pending_intents.json.
#   - Submit. Persist state. Then clear the intent.
#   - On startup, ``recover_orphan_intents`` (in reconciler.py) inspects the
#     journal, queries Alpaca for each client_order_id, and reconciles any
#     orders that exist but aren't reflected in state.

def load_pending_intents(path: Path = PENDING_INTENTS_PATH) -> list[dict]:
    """Return the list of in-flight intents (empty if file missing/corrupt)."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, list):
        return []
    return data


def _write_pending_intents(intents: list[dict], path: Path = PENDING_INTENTS_PATH) -> None:
    payload = json.dumps(intents, indent=2, sort_keys=True) + "\n"
    _atomic_write_json(path, payload)


def journal_intent(intent: dict, path: Path = PENDING_INTENTS_PATH) -> None:
    """Append ``intent`` to the journal BEFORE submitting an order. Must
    include at minimum a ``client_order_id`` so recovery can match it."""
    if "client_order_id" not in intent:
        raise ValueError("intent must include 'client_order_id'")
    intents = load_pending_intents(path)
    intents.append(dict(intent))
    _write_pending_intents(intents, path)


def clear_intent(client_order_id: str, path: Path = PENDING_INTENTS_PATH) -> None:
    """Remove the intent matching ``client_order_id`` from the journal.

    Called after state has been persisted; a no-op if the intent is missing
    (which can happen if recovery already cleaned it up)."""
    intents = load_pending_intents(path)
    remaining = [i for i in intents if i.get("client_order_id") != client_order_id]
    if len(remaining) == len(intents):
        return  # nothing to remove
    _write_pending_intents(remaining, path)
