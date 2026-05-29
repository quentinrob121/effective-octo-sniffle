from __future__ import annotations

import argparse
import dataclasses
import sys

from alpaca_starter import build_trading_client, setup_logging, warn_if_live

from .bot import (
    render_status,
    run_once,
    summarize,
    write_daily_summary,
)
from .config import WHEEL_PREFIX, load_config
from .executor import btc as exec_btc
from .option_picker import OptionCandidate
from .state import Stage, load_state, save_state


def cmd_status(args: argparse.Namespace) -> int:
    state = load_state()
    print(render_status(state, args.ticker))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    config = load_config()
    if args.dry_run:
        config = dataclasses.replace(config, dry_run=True)
    result = run_once(config)
    for r in result.reconciliations:
        if r.transition != "none":
            print(f"RECONCILE  {r.ticker:<6}  {r.transition:<14}  {r.detail}")
    for a in result.actions:
        print(f"ACTION     {a.ticker:<6}  {a.action:<10}  {a.detail}")
    print(f"SUMMARY: {summarize(result)}")
    return 0


def cmd_summary(_: argparse.Namespace) -> int:
    # Re-run with no orders just to capture the current snapshot for the
    # markdown summary; flag dry-run to be safe.
    from datetime import datetime, timezone

    state = load_state()
    today = datetime.now(timezone.utc).date()
    # If nothing happened yet today, write a stub so the workflow can still
    # always commit something predictable.
    from .bot import RunResult

    result = RunResult(reconciliations=[], actions=[], holds=[])
    path = write_daily_summary(result, today)
    print(f"summary written to {path}")
    print()
    print(render_status(state))
    return 0


def cmd_close(args: argparse.Namespace) -> int:
    """Force-BTC any open wheel contract on TICKER. Use when you want out now."""
    state = load_state()
    ts = state.tickers.get(args.ticker.upper())
    if ts is None or ts.active_contract is None or ts.stage not in (Stage.PUT_OPEN, Stage.CALL_OPEN):
        print(f"{args.ticker.upper()}: no open wheel contract to close")
        return 0
    config = load_config()
    client = build_trading_client()
    # Force-close ignores the "should_close" gate. Bid sentinel is the recorded
    # premium so the audit log still has a reference; the actual fill price
    # will be whatever the market gives us (market order).
    ask = float(ts.active_contract.get("premium_received", 0.0))
    action = exec_btc(client, ts, ask, config, save=lambda: save_state(state))
    print(f"{action.action:<10}  {action.detail}")
    return 0


def cmd_pause(args: argparse.Namespace) -> int:
    return _set_enabled(args.ticker, False)


def cmd_resume(args: argparse.Namespace) -> int:
    return _set_enabled(args.ticker, True)


def _set_enabled(ticker: str, enabled: bool) -> int:
    state = load_state()
    ts = state.get_or_create(ticker)
    ts.enabled = enabled
    save_state(state)
    print(f"{ticker.upper()} enabled={enabled}")
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    """Wipe local state for one ticker. Does NOT cancel open orders at Alpaca."""
    state = load_state()
    if args.ticker.upper() in state.tickers:
        del state.tickers[args.ticker.upper()]
        save_state(state)
        print(f"{args.ticker.upper()}: state cleared")
    else:
        print(f"{args.ticker.upper()}: no state to clear")
    return 0


def cmd_adopt(args: argparse.Namespace) -> int:
    """Mark a ticker as HOLDING with a manually-specified basis. Useful when
    auto-adopt (using Alpaca's avg_entry_price) doesn't reflect what you
    actually paid (e.g. an in-kind transfer)."""
    state = load_state()
    ts = state.get_or_create(args.ticker)
    ts.stage = Stage.HOLDING
    ts.avg_basis_per_share = float(args.basis)
    ts.cumulative_premium_this_lot = 0.0
    if args.shares is not None:
        ts.shares_held = int(args.shares)
    ts.enabled = True
    save_state(state)
    print(f"{ts.ticker}: HOLDING shares={ts.shares_held} basis=${ts.avg_basis_per_share:.4f}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wheel_bot")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="Show per-ticker state")
    p_status.add_argument("ticker", nargs="?", default=None)
    p_status.set_defaults(func=cmd_status)

    p_run = sub.add_parser("run", help="Run one wheel cycle")
    p_run.add_argument("--dry-run", action="store_true")
    p_run.set_defaults(func=cmd_run)

    p_sum = sub.add_parser("summary", help="Write today's markdown summary")
    p_sum.set_defaults(func=cmd_summary)

    p_close = sub.add_parser("close", help="BTC the open wheel contract on TICKER")
    p_close.add_argument("ticker")
    p_close.set_defaults(func=cmd_close)

    p_pause = sub.add_parser("pause", help="Disable a ticker (no new STO/BTC)")
    p_pause.add_argument("ticker")
    p_pause.set_defaults(func=cmd_pause)

    p_resume = sub.add_parser("resume", help="Re-enable a ticker")
    p_resume.add_argument("ticker")
    p_resume.set_defaults(func=cmd_resume)

    p_reset = sub.add_parser("reset", help="Wipe local state for TICKER (admin)")
    p_reset.add_argument("ticker")
    p_reset.set_defaults(func=cmd_reset)

    p_adopt = sub.add_parser(
        "adopt", help="Set TICKER to HOLDING with --basis (and optional --shares)"
    )
    p_adopt.add_argument("ticker")
    p_adopt.add_argument("--basis", type=float, required=True)
    p_adopt.add_argument("--shares", type=int, default=None)
    p_adopt.set_defaults(func=cmd_adopt)

    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    warn_if_live("wheel_bot")
    # WHEEL_PREFIX is imported just so this CLI module fails fast if the
    # tag constant is missing — orders are placed via the executor.
    assert WHEEL_PREFIX
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
