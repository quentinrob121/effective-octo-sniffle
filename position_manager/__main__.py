from __future__ import annotations

import argparse
import dataclasses
import logging
import sys

from alpaca_starter import build_trading_client

from .config import load_config
from .manager import (
    cancel_managed_orders,
    ensure_trailing_stops,
    list_managed_orders,
)


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def cmd_run(args: argparse.Namespace) -> int:
    config = load_config()
    if args.dry_run:
        config = dataclasses.replace(config, dry_run=True)
    client = build_trading_client()
    actions = ensure_trailing_stops(client, config)
    if not actions:
        print("No open positions.")
        return 0
    for a in actions:
        print(f"{a.action:>15}  {a.symbol:<6}  {a.detail}")
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    client = build_trading_client()
    orders = list_managed_orders(client)
    if not orders:
        print("No managed orders open.")
        return 0
    for o in orders:
        side = str(o.side).split(".")[-1].lower()
        extra = (
            f" trail {o.trail_percent}%"
            if getattr(o, "trail_percent", None)
            else f" limit ${o.limit_price}"
            if getattr(o, "limit_price", None)
            else ""
        )
        print(
            f"{o.symbol:<6}  {side:<4}  qty {o.qty}{extra}  "
            f"id={o.id}  coid={o.client_order_id}"
        )
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    client = build_trading_client()
    n = cancel_managed_orders(client, args.symbol.upper() if args.symbol else None)
    scope = args.symbol.upper() if args.symbol else "all symbols"
    print(f"Cancelled {n} managed order(s) for {scope}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="position_manager")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser(
        "run",
        help="Ensure every long position has a trailing-stop SELL; "
        "optionally place a re-entry GTC limit BUY.",
    )
    p_run.add_argument("--dry-run", action="store_true")
    p_run.set_defaults(func=cmd_run)

    p_status = sub.add_parser("status", help="List managed open orders")
    p_status.set_defaults(func=cmd_status)

    p_cancel = sub.add_parser("cancel", help="Cancel managed orders")
    p_cancel.add_argument("symbol", nargs="?", default=None)
    p_cancel.set_defaults(func=cmd_cancel)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
