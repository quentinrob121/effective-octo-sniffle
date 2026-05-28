from __future__ import annotations

import argparse
import dataclasses
import logging
import os
import sys

from dotenv import load_dotenv

from .bot import run_once, summarize
from .config import load_config
from .scraper import fetch_trades


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def cmd_run(args: argparse.Namespace) -> int:
    config = load_config()
    if args.dry_run:
        config = dataclasses.replace(config, dry_run=True)
    results = run_once(config)
    print(summarize(results))
    return 0


def cmd_preview(_: argparse.Namespace) -> int:
    """Scrape the politician page and print what we'd consider mirroring,
    without touching Alpaca. Useful for sanity-checking config."""
    config = load_config()
    trades = fetch_trades(
        config.politician_url, config.politician_id, config.politician_name
    )
    print(f"{config.politician_name} ({config.politician_id}) -- {len(trades)} trade(s)")
    for t in trades:
        price = f"${t.price:.2f}" if t.price else "n/a"
        print(
            f"  {t.published_date}  traded {t.traded_date}  {t.tx_type:>4} "
            f"{t.ticker:<6} {t.size_range:>10}  ref {price}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="copy_trading_bot")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Scrape + mirror new trades")
    p_run.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would happen; do not place orders",
    )
    p_run.set_defaults(func=cmd_run)

    p_prev = sub.add_parser("preview", help="Show scraped trades without trading")
    p_prev.set_defaults(func=cmd_preview)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    # Resolve .env first so a .env-supplied paper URL isn't flagged as live.
    # The underlying alpaca-py client routes by the `paper` boolean
    # (alpaca_starter.client.build_trading_client), which is derived from
    # ALPACA_BASE_URL — so this *is* the source of truth.
    load_dotenv()
    base_url = os.getenv("ALPACA_BASE_URL")
    if base_url and "paper-api" not in base_url:
        logging.getLogger("copy_trading_bot").warning(
            "ALPACA_BASE_URL=%s is not the paper endpoint — orders will be LIVE",
            base_url,
        )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
