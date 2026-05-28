from __future__ import annotations

import argparse
import logging
import sys

from alpaca_starter import build_data_client, build_trading_client

from copy_trading_bot.quotes import AlpacaQuoteSource

from .config import LadderConfig, plan_ladder
from .ladder import cancel_ladder, list_ladder_orders, place_ladder


def _build_config(args: argparse.Namespace) -> LadderConfig:
    levels = (
        tuple(float(p) / 100.0 for p in args.levels.split(","))
        if args.levels
        else LadderConfig().levels_pct
    )
    weights = (
        tuple(float(w) for w in args.weights.split(","))
        if args.weights
        else LadderConfig().weights
    )
    return LadderConfig(
        levels_pct=levels, weights=weights, max_total_usd=args.max_total_usd
    )


def _resolve_ref_price(args: argparse.Namespace) -> float:
    if args.ref_price is not None:
        return float(args.ref_price)
    quotes = AlpacaQuoteSource(build_data_client())
    price = quotes.latest_price(args.symbol.upper())
    if not price:
        raise SystemExit(
            f"Could not fetch a quote for {args.symbol}. "
            "Pass --ref-price explicitly."
        )
    return price


def cmd_plan(args: argparse.Namespace) -> int:
    """Print what would be placed; never touches Alpaca for trading."""
    config = _build_config(args)
    ref_price = _resolve_ref_price(args)
    rungs = plan_ladder(
        ref_price=ref_price,
        base_qty=args.base_shares,
        base_usd=args.base_usd,
        config=config,
    )
    print(f"{args.symbol.upper()}  ref ${ref_price:.2f}")
    print(f"{'Level':>6}  {'Price':>9}  {'Qty':>5}  {'Notional':>10}")
    total = 0.0
    for r in rungs:
        notional = r.limit_price * r.qty
        total += notional
        print(
            f"{-r.level_pct*100:>5.0f}%  ${r.limit_price:>8.2f}  "
            f"{r.qty:>5}  ${notional:>9,.0f}"
        )
    print(f"{'TOTAL':>6}                       ${total:>9,.0f}")
    return 0


def cmd_place(args: argparse.Namespace) -> int:
    config = _build_config(args)
    ref_price = _resolve_ref_price(args)
    client = build_trading_client()
    placed = place_ladder(
        client=client,
        symbol=args.symbol.upper(),
        ref_price=ref_price,
        base_qty=args.base_shares,
        base_usd=args.base_usd,
        config=config,
        dry_run=args.dry_run,
    )
    for p in placed:
        print(f"{p.action:>9}  {p.detail}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    client = build_trading_client()
    orders = list_ladder_orders(client, args.symbol.upper() if args.symbol else None)
    if not orders:
        print("No open ladder orders.")
        return 0
    for o in orders:
        print(
            f"{o.symbol:<6}  {o.side}  qty {o.qty}  limit ${o.limit_price}  "
            f"id={o.id}  coid={o.client_order_id}"
        )
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    client = build_trading_client()
    n = cancel_ladder(client, args.symbol.upper() if args.symbol else None)
    scope = args.symbol.upper() if args.symbol else "all symbols"
    print(f"Cancelled {n} ladder order(s) for {scope}.")
    return 0


def _add_common_ladder_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("symbol", help="Ticker, e.g. NFLX")
    p.add_argument(
        "--ref-price",
        type=float,
        default=None,
        help="Reference price the rungs hang off of. Defaults to the live quote.",
    )
    sizing = p.add_mutually_exclusive_group(required=True)
    sizing.add_argument(
        "--base-shares",
        type=int,
        help="Share count for the shallowest rung; deeper rungs scale by weight.",
    )
    sizing.add_argument(
        "--base-usd",
        type=float,
        help="Dollar budget for the shallowest rung; deeper rungs scale by weight.",
    )
    p.add_argument(
        "--levels",
        type=str,
        default="",
        help="Comma-separated drops in percent. Default: 15,25,35,50",
    )
    p.add_argument(
        "--weights",
        type=str,
        default="",
        help="Comma-separated rung weights. Default: 1,2,3,5",
    )
    p.add_argument(
        "--max-total-usd",
        type=float,
        default=30_000.0,
        help="Refuse to place a ladder whose total notional exceeds this.",
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    parser = argparse.ArgumentParser(prog="dip_ladder")
    sub = parser.add_subparsers(dest="command", required=True)

    p_plan = sub.add_parser("plan", help="Print rungs; do not place orders")
    _add_common_ladder_args(p_plan)
    p_plan.set_defaults(func=cmd_plan)

    p_place = sub.add_parser("place", help="Submit GTC limit buys for the rungs")
    _add_common_ladder_args(p_place)
    p_place.add_argument("--dry-run", action="store_true")
    p_place.set_defaults(func=cmd_place)

    p_status = sub.add_parser("status", help="Show open ladder orders")
    p_status.add_argument("symbol", nargs="?", default=None)
    p_status.set_defaults(func=cmd_status)

    p_cancel = sub.add_parser("cancel", help="Cancel ladder orders")
    p_cancel.add_argument("symbol", nargs="?", default=None)
    p_cancel.set_defaults(func=cmd_cancel)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
