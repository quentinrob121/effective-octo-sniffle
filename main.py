"""Demo entry point for the Alpaca paper-trading starter.

Usage:
    python main.py account
    python main.py quote AAPL
    python main.py bars AAPL
    python main.py orders
    python main.py buy AAPL 1            # market order (paper)
    python main.py buy AAPL 1 --limit 150
    python main.py sell AAPL 1           # market sell (paper)
    python main.py sell AAPL 1 --limit 160
    python main.py bracket AAPL 1 --take-profit 200 --stop-loss 140
    python main.py stop AAPL 1 --stop-price 140
    python main.py positions
    python main.py close AAPL            # close one position
    python main.py close --all          # close every position
    python main.py stream AAPL MSFT     # live trade feed (Ctrl+C to stop)
"""

from __future__ import annotations

import argparse

from alpaca.trading.enums import OrderSide

from alpaca_starter import build_data_client, build_trading_client, load_settings
from alpaca_starter.account import account_summary
from alpaca_starter.market_data import latest_quote, recent_daily_bars
from alpaca_starter.orders import (
    list_open_orders,
    submit_bracket_order,
    submit_limit_order,
    submit_market_order,
    submit_stop_order,
)
from alpaca_starter.positions import (
    close_all_positions,
    close_position,
    position_summary,
)
from alpaca_starter.stream import stream_trades


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


def _submit_directional(args: argparse.Namespace, side: OrderSide) -> None:
    client = build_trading_client()
    if args.limit is not None:
        order = submit_limit_order(
            client, args.symbol, args.qty, args.limit, side=side
        )
    else:
        order = submit_market_order(client, args.symbol, args.qty, side=side)
    print(f"Submitted {order.side} {order.qty} {order.symbol} "
          f"(id={order.id}, status={order.status})")


def cmd_buy(args: argparse.Namespace) -> None:
    _submit_directional(args, OrderSide.BUY)


def cmd_sell(args: argparse.Namespace) -> None:
    _submit_directional(args, OrderSide.SELL)


def cmd_bracket(args: argparse.Namespace) -> None:
    client = build_trading_client()
    order = submit_bracket_order(
        client,
        args.symbol,
        args.qty,
        take_profit_price=args.take_profit,
        stop_loss_price=args.stop_loss,
        stop_loss_limit_price=args.stop_limit,
    )
    print(f"Submitted bracket {order.qty} {order.symbol} "
          f"(id={order.id}, status={order.status})")


def cmd_stop(args: argparse.Namespace) -> None:
    client = build_trading_client()
    order = submit_stop_order(client, args.symbol, args.qty, args.stop_price)
    print(f"Submitted stop {order.side} {order.qty} {order.symbol} "
          f"@ {args.stop_price} (id={order.id}, status={order.status})")


def cmd_positions(_: argparse.Namespace) -> None:
    client = build_trading_client()
    rows = position_summary(client)
    if not rows:
        print("No open positions.")
        return
    for row in rows:
        print(f"{row['symbol']:>6}  {row['side']:>5}  qty {row['qty']}  "
              f"@ {row['avg_entry_price']}  now {row['current_price']}  "
              f"P/L {row['unrealized_pl']} ({row['unrealized_plpc']})")


def cmd_close(args: argparse.Namespace) -> None:
    client = build_trading_client()
    if args.all:
        results = close_all_positions(client, cancel_orders=True)
        print(f"Submitted close for {len(results)} position(s).")
        return
    if not args.symbol:
        raise SystemExit("Provide a symbol or use --all.")
    order = close_position(client, args.symbol)
    print(f"Closing {args.symbol} (order id={order.id}, status={order.status})")


def cmd_stream(args: argparse.Namespace) -> None:
    print(f"Streaming trades for {', '.join(args.symbols)} (Ctrl+C to stop)...")
    stream_trades(args.symbols)


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

    p_sell = sub.add_parser("sell", help="Submit a sell order (paper)")
    p_sell.add_argument("symbol")
    p_sell.add_argument("qty", type=float)
    p_sell.add_argument("--limit", type=float, default=None,
                        help="Limit price (omit for a market order)")
    p_sell.set_defaults(func=cmd_sell)

    p_bracket = sub.add_parser("bracket", help="Bracket order: entry + TP + SL")
    p_bracket.add_argument("symbol")
    p_bracket.add_argument("qty", type=float)
    p_bracket.add_argument("--take-profit", type=float, required=True,
                           help="Take-profit limit price")
    p_bracket.add_argument("--stop-loss", type=float, required=True,
                           help="Stop-loss trigger price")
    p_bracket.add_argument("--stop-limit", type=float, default=None,
                           help="Optional stop-loss limit price (stop-limit leg)")
    p_bracket.set_defaults(func=cmd_bracket)

    p_stop = sub.add_parser("stop", help="Standalone protective stop (sell)")
    p_stop.add_argument("symbol")
    p_stop.add_argument("qty", type=float)
    p_stop.add_argument("--stop-price", type=float, required=True)
    p_stop.set_defaults(func=cmd_stop)

    sub.add_parser("positions", help="List open positions").set_defaults(
        func=cmd_positions
    )

    p_close = sub.add_parser("close", help="Close one position or all")
    p_close.add_argument("symbol", nargs="?", default=None)
    p_close.add_argument("--all", action="store_true", help="Close every position")
    p_close.set_defaults(func=cmd_close)

    p_stream = sub.add_parser("stream", help="Live trade feed (websocket)")
    p_stream.add_argument("symbols", nargs="+")
    p_stream.set_defaults(func=cmd_stream)

    return parser


def main() -> None:
    settings = load_settings()
    print(f"Connected to {'PAPER' if settings.is_paper else 'LIVE'} "
          f"endpoint: {settings.base_url}\n")
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
