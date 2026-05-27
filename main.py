"""Demo entry point for the Alpaca paper-trading starter.

Usage:
    python main.py account
    python main.py quote AAPL
    python main.py bars AAPL
    python main.py orders
    python main.py buy AAPL 1            # market order (paper)
    python main.py buy AAPL 1 --limit 150
"""

from __future__ import annotations

import argparse

from alpaca.trading.enums import OrderSide

from alpaca_starter import build_data_client, build_trading_client, load_settings
from alpaca_starter.account import account_summary
from alpaca_starter.market_data import latest_quote, recent_daily_bars
from alpaca_starter.orders import (
    list_open_orders,
    submit_limit_order,
    submit_market_order,
)


def cmd_account(_: argparse.Namespace) -> None:
    client = build_trading_client()
    for key, value in account_summary(client).items():
        print(f"{key:>18}: {value}")


def cmd_quote(args: argparse.Namespace) -> None:
    client = build_data_client()
    quote = latest_quote(client, args.symbol)
    print(f"{args.symbol}  bid {quote.bid_price} x {quote.bid_size}  "
          f"ask {quote.ask_price} x {quote.ask_size}")


def cmd_bars(args: argparse.Namespace) -> None:
    client = build_data_client()
    for bar in recent_daily_bars(client, args.symbol):
        print(f"{bar.timestamp.date()}  O {bar.open}  H {bar.high}  "
              f"L {bar.low}  C {bar.close}  V {bar.volume}")


def cmd_orders(_: argparse.Namespace) -> None:
    client = build_trading_client()
    orders = list_open_orders(client)
    if not orders:
        print("No open orders.")
        return
    for order in orders:
        print(f"{order.id}  {order.side}  {order.qty} {order.symbol}  "
              f"{order.order_type}  status={order.status}")


def cmd_buy(args: argparse.Namespace) -> None:
    client = build_trading_client()
    if args.limit is not None:
        order = submit_limit_order(
            client, args.symbol, args.qty, args.limit, side=OrderSide.BUY
        )
    else:
        order = submit_market_order(
            client, args.symbol, args.qty, side=OrderSide.BUY
        )
    print(f"Submitted {order.side} {order.qty} {order.symbol} "
          f"(id={order.id}, status={order.status})")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Alpaca paper-trading starter")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("account", help="Show account summary").set_defaults(func=cmd_account)

    p_quote = sub.add_parser("quote", help="Latest quote for a symbol")
    p_quote.add_argument("symbol")
    p_quote.set_defaults(func=cmd_quote)

    p_bars = sub.add_parser("bars", help="Recent daily bars for a symbol")
    p_bars.add_argument("symbol")
    p_bars.set_defaults(func=cmd_bars)

    sub.add_parser("orders", help="List open orders").set_defaults(func=cmd_orders)

    p_buy = sub.add_parser("buy", help="Submit a buy order (paper)")
    p_buy.add_argument("symbol")
    p_buy.add_argument("qty", type=float)
    p_buy.add_argument("--limit", type=float, default=None,
                       help="Limit price (omit for a market order)")
    p_buy.set_defaults(func=cmd_buy)

    return parser


def main() -> None:
    settings = load_settings()
    print(f"Connected to {'PAPER' if settings.is_paper else 'LIVE'} "
          f"endpoint: {settings.base_url}\n")
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
