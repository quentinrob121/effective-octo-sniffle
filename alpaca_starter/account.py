from __future__ import annotations

from alpaca.trading.client import TradingClient
from alpaca.trading.models import TradeAccount


def get_account(client: TradingClient) -> TradeAccount:
    return client.get_account()


def account_summary(client: TradingClient) -> dict[str, str]:
    """A compact, human-readable snapshot of the account."""
    account = client.get_account()
    return {
        "account_number": account.account_number,
        "status": str(account.status),
        "currency": account.currency,
        "cash": account.cash,
        "buying_power": account.buying_power,
        "portfolio_value": account.portfolio_value,
        "equity": account.equity,
        "trading_blocked": str(account.trading_blocked),
        "pattern_day_trader": str(account.pattern_day_trader),
    }
