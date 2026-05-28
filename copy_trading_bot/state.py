from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

STATE_PATH = Path(__file__).parent / "state" / "processed_trades.json"
# Keep the file size sane in git: drop oldest entries past this cap.
MAX_ENTRIES = 1_000


def load_processed() -> set[str]:
    if not STATE_PATH.exists():
        return set()
    try:
        data = json.loads(STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return set()
    return {entry["signature"] for entry in data.get("trades", [])}


def append_processed(entries: list[dict]) -> None:
    """Persist newly processed trade signatures. ``entries`` is a list of dicts
    each containing at least ``signature``; extra fields are kept for audit."""
    if not entries:
        return
    existing: list[dict] = []
    if STATE_PATH.exists():
        try:
            existing = json.loads(STATE_PATH.read_text()).get("trades", [])
        except (json.JSONDecodeError, OSError):
            existing = []
    seen = {e["signature"] for e in existing}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for entry in entries:
        if entry["signature"] in seen:
            continue
        entry.setdefault("processed_at", now)
        existing.append(entry)
        seen.add(entry["signature"])
    if len(existing) > MAX_ENTRIES:
        existing = existing[-MAX_ENTRIES:]
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps({"trades": existing}, indent=2) + "\n")
