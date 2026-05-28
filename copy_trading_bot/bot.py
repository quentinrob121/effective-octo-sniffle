from __future__ import annotations

import dataclasses
import logging
from datetime import date, timedelta

from alpaca_starter import build_data_client, build_trading_client

from .config import CopyTraderConfig, load_config
from .executor import ExecutionResult, execute
from .quotes import AlpacaQuoteSource
from .scraper import chronological, fetch_trades
from .state import append_processed, load_processed

log = logging.getLogger("copy_trading_bot")


def run_once(config: CopyTraderConfig | None = None) -> list[ExecutionResult]:
    config = config or load_config()
    log.info(
        "Copy-trade run: politician=%s id=%s usd/trade=%.0f dry_run=%s",
        config.politician_name,
        config.politician_id,
        config.trade_usd,
        config.dry_run,
    )

    trades = fetch_trades(
        config.politician_url, config.politician_id, config.politician_name
    )
    log.info("Scraped %d trades from Capitol Trades", len(trades))

    cutoff = date.today() - timedelta(days=config.skip_older_than_days)
    fresh = [t for t in trades if t.traded_date >= cutoff]
    skipped_stale = len(trades) - len(fresh)
    if skipped_stale:
        log.info("Skipped %d trades older than %s", skipped_stale, cutoff)

    processed = load_processed()
    new_trades = [t for t in chronological(fresh) if t.signature not in processed]
    log.info("%d new trade(s) to mirror", len(new_trades))
    if not new_trades:
        return []

    trading_client = build_trading_client()
    quote_source = AlpacaQuoteSource(build_data_client())

    results: list[ExecutionResult] = []
    audit_entries: list[dict] = []
    for trade in new_trades:
        result = execute(trade, trading_client, quote_source, config)
        log.info(
            "%-9s %s %s [%s] -> %s",
            result.action.upper(),
            trade.tx_type,
            trade.ticker,
            trade.size_range,
            result.detail,
        )
        results.append(result)
        if result.action != "error":
            # Record skipped + submitted so we don't keep retrying.
            entry = {
                "signature": trade.signature,
                "ticker": trade.ticker,
                "type": trade.tx_type,
                "traded_date": trade.traded_date.isoformat(),
                "size_range": trade.size_range,
                "action": result.action,
                "detail": result.detail,
                "order_id": result.order_id,
            }
            audit_entries.append(entry)

    append_processed(audit_entries)
    return results


def summarize(results: list[ExecutionResult]) -> str:
    counts: dict[str, int] = {}
    for r in results:
        counts[r.action] = counts.get(r.action, 0) + 1
    parts = [f"{k}={v}" for k, v in sorted(counts.items())]
    return "; ".join(parts) if parts else "no new trades"


# Re-export for tests
__all__ = ["run_once", "summarize", "ExecutionResult"]

_ = dataclasses  # silence linters that flag unused-import patterns
