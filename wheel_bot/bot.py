from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from alpaca.common.exceptions import APIError
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import (
    OptionChainRequest,
    OptionLatestQuoteRequest,
    StockLatestQuoteRequest,
)
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import ContractType

from alpaca_starter import build_data_client, build_trading_client, load_settings

from .config import WheelConfig, load_config
from .executor import (
    ExecAction,
    btc,
    effective_basis_per_share,
    should_close,
    sto_call,
    sto_put,
)
from .option_picker import OptionCandidate, pick_expiration, pick_strike
from .reconciler import ReconcileResult, recover_orphan_intents, reconcile_ticker
from .state import (
    STATE_PATH,
    SUMMARIES_DIR,
    Stage,
    TickerState,
    WheelState,
    append_audit,
    load_state,
    save_state,
)

log = logging.getLogger("wheel_bot.bot")


@dataclass
class RunResult:
    reconciliations: list[ReconcileResult]
    actions: list[ExecAction]
    holds: list[str]  # human-readable notes for held positions we didn't trade


# ---------------------------------------------------------------------------
# Chain + quote adapters (thin wrappers around alpaca-py)
# ---------------------------------------------------------------------------

def _build_option_data_client() -> OptionHistoricalDataClient:
    settings = load_settings()
    return OptionHistoricalDataClient(
        api_key=settings.api_key, secret_key=settings.api_secret
    )


def _spot_price(stock_client: StockHistoricalDataClient, ticker: str) -> float | None:
    """Best-effort: latest IEX quote midpoint, or None if Alpaca returns nothing."""
    try:
        req = StockLatestQuoteRequest(symbol_or_symbols=ticker)
        quotes = stock_client.get_stock_latest_quote(req)
    except APIError as exc:
        log.warning("spot quote for %s failed: %s", ticker, exc)
        return None
    quote = quotes.get(ticker) if isinstance(quotes, dict) else None
    if quote is None:
        return None
    try:
        bid = float(quote.bid_price or 0)
        ask = float(quote.ask_price or 0)
    except (TypeError, ValueError):
        return None
    if bid > 0 and ask > 0:
        return (bid + ask) / 2
    if ask > 0:
        return ask
    if bid > 0:
        return bid
    return None


def _fetch_chain(
    option_client: OptionHistoricalDataClient,
    ticker: str,
    option_type: ContractType,
    today: date,
    dte_max: int,
) -> list[OptionCandidate]:
    """Pull the live option chain for ``ticker`` and normalize to OptionCandidate.

    Filters by type and an expiration upper bound (lower bound is enforced by
    the picker via ``dte_min``). Returns ``[]`` on API errors so the bot can
    skip the cycle rather than crashing the run.
    """
    # Pull a slightly wider window than dte_max so the picker has room — the
    # picker enforces the strict [dte_min, dte_max] window itself.
    upper = date.fromordinal(today.toordinal() + dte_max + 7)
    req = OptionChainRequest(
        underlying_symbol=ticker,
        type=option_type,
        expiration_date_lte=upper,
    )
    try:
        snapshot = option_client.get_option_chain(req)
    except APIError as exc:
        log.warning("chain for %s failed: %s", ticker, exc)
        return []
    if not snapshot:
        return []
    out: list[OptionCandidate] = []
    for occ_symbol, snap in snapshot.items():
        parsed = _parse_occ(occ_symbol)
        if parsed is None:
            continue
        underlying, expiration, kind, strike = parsed
        if (option_type == ContractType.PUT and kind != "put") or (
            option_type == ContractType.CALL and kind != "call"
        ):
            continue
        quote = getattr(snap, "latest_quote", None)
        bid = float(getattr(quote, "bid_price", 0) or 0) if quote else 0.0
        ask = float(getattr(quote, "ask_price", 0) or 0) if quote else 0.0
        out.append(
            OptionCandidate(
                symbol=occ_symbol,
                underlying=underlying,
                expiration=expiration,
                strike=strike,
                type=kind,
                bid=bid,
                ask=ask,
            )
        )
    return out


def _parse_occ(symbol: str) -> tuple[str, date, str, float] | None:
    """Parse an OCC option symbol into (underlying, expiration, type, strike).

    Format: ``ROOT YYMMDD C/P SSSSSSSS`` (no spaces) where the last 15 chars
    are ``YYMMDD(C|P)NNNNNNNN``. Returns ``None`` if the tail doesn't match.
    """
    if len(symbol) < 15:
        return None
    tail = symbol[-15:]
    root = symbol[:-15]
    yy, mm, dd = tail[0:2], tail[2:4], tail[4:6]
    kind_ch = tail[6]
    strike_raw = tail[7:]
    try:
        year = 2000 + int(yy)
        expiration = date(year, int(mm), int(dd))
        strike = int(strike_raw) / 1000.0
    except ValueError:
        return None
    if kind_ch == "C":
        kind = "call"
    elif kind_ch == "P":
        kind = "put"
    else:
        return None
    return root, expiration, kind, strike


def _option_ask(
    option_client: OptionHistoricalDataClient, occ_symbol: str
) -> float | None:
    """Latest ask for one OCC option symbol, or None on failure."""
    try:
        req = OptionLatestQuoteRequest(symbol_or_symbols=occ_symbol)
        quotes = option_client.get_option_latest_quote(req)
    except APIError as exc:
        log.warning("ask quote for %s failed: %s", occ_symbol, exc)
        return None
    q = quotes.get(occ_symbol) if isinstance(quotes, dict) else None
    if q is None:
        return None
    try:
        return float(q.ask_price or 0)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Per-ticker orchestration
# ---------------------------------------------------------------------------

def _handle_idle(
    client: TradingClient,
    option_client: OptionHistoricalDataClient,
    stock_client: StockHistoricalDataClient,
    state: TickerState,
    config: WheelConfig,
    today: date,
    save: callable,
) -> ExecAction:
    spot = _spot_price(stock_client, state.ticker)
    if spot is None or spot <= 0:
        return ExecAction(state.ticker, "skipped", "no spot quote")
    target_strike = spot * (1 - config.put_strike_offset_pct / 100.0)
    chain = _fetch_chain(option_client, state.ticker, ContractType.PUT, today, config.dte_max)
    if not chain:
        return ExecAction(state.ticker, "skipped", "empty put chain")
    expiration = pick_expiration(
        chain, today, config.target_dte, config.dte_min, config.dte_max
    )
    if expiration is None:
        return ExecAction(
            state.ticker,
            "skipped",
            f"no PUT expiration in [{config.dte_min},{config.dte_max}] DTE",
        )
    at_exp = [c for c in chain if c.expiration == expiration]
    pick = pick_strike(at_exp, target_strike, type="put")
    if pick is None:
        return ExecAction(state.ticker, "skipped", "no PUT strike candidate")
    return sto_put(client, state, pick, config, save=save)


def _handle_holding(
    client: TradingClient,
    option_client: OptionHistoricalDataClient,
    stock_client: StockHistoricalDataClient,
    state: TickerState,
    config: WheelConfig,
    today: date,
    save: callable,
) -> ExecAction:
    """Pick a covered-call strike ABOVE EFFECTIVE BASIS, not above spot.

    Spec rule: "sell calls 10% above what I paid". If we anchored to spot
    and spot has fallen 20% since assignment, the picker would target a
    strike right around our basis — i.e. a guaranteed flat lot if called
    away, defeating the wheel's premium-collection thesis. By anchoring on
    ``effective_basis_per_share`` (cost minus all call premium already
    collected against this lot) we honor the spec and protect the lot.

    Spot is still queried solely as a health check that we have a live
    quote; it does NOT feed the strike calculation. The executor's
    ``floor < basis`` guard remains as a second line of defense.
    """
    spot = _spot_price(stock_client, state.ticker)
    if spot is None or spot <= 0:
        return ExecAction(state.ticker, "skipped", "no spot quote")
    basis = effective_basis_per_share(state)
    target_strike = basis * (1 + config.call_strike_offset_pct / 100.0)
    chain = _fetch_chain(option_client, state.ticker, ContractType.CALL, today, config.dte_max)
    if not chain:
        return ExecAction(state.ticker, "skipped", "empty call chain")
    expiration = pick_expiration(
        chain, today, config.target_dte, config.dte_min, config.dte_max
    )
    if expiration is None:
        return ExecAction(state.ticker, "skipped", "no CALL expiration in DTE window")
    at_exp = [c for c in chain if c.expiration == expiration]
    pick = pick_strike(at_exp, target_strike, type="call")
    if pick is None:
        return ExecAction(state.ticker, "skipped", "no CALL strike candidate")
    return sto_call(client, state, pick, config, save=save)


def _handle_open_short(
    client: TradingClient,
    option_client: OptionHistoricalDataClient,
    state: TickerState,
    config: WheelConfig,
    today: date,
    save: callable,
) -> ExecAction:
    contract = state.active_contract or {}
    symbol = contract.get("symbol")
    if not symbol:
        return ExecAction(state.ticker, "skipped", "no active contract symbol")
    ask = _option_ask(option_client, symbol)
    if ask is None:
        return ExecAction(state.ticker, "skipped", "no current ask for active contract")
    do_close, why = should_close(state, ask, today, config)
    if not do_close:
        return ExecAction(state.ticker, "skipped", why)
    return btc(client, state, ask, config, save=save)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def run_once(config: WheelConfig | None = None) -> RunResult:
    """One full cycle: reconcile, then make a decision per ticker.

    State is saved IMMEDIATELY after any successful order, so a mid-loop
    crash leaves an accurate file that the next run can pick up from.
    """
    config = config or load_config()
    log.info(
        "Wheel run: tickers=%s target_dte=%s dry_run=%s",
        ",".join(config.tickers),
        config.target_dte,
        config.dry_run,
    )

    trading_client = build_trading_client()
    option_client = _build_option_data_client()
    stock_client = build_data_client()

    state = load_state()
    for t in config.tickers:
        state.get_or_create(t)
    # Persist any newly-seeded tickers so `status` shows them even if we crash
    # before any per-ticker action saves again.
    save_state(state)

    today = datetime.now(timezone.utc).date()
    reconciliations: list[ReconcileResult] = []
    actions: list[ExecAction] = []
    holds: list[str] = []

    # Orphan-intent recovery: if last run crashed between submit() and save(),
    # the journal has the order's client_order_id. Reconcile any such orders
    # into state BEFORE per-ticker reconcile, so the rest of this run sees the
    # truth (and we don't accidentally STO a duplicate). The journal is then
    # cleared whether the order existed or not.
    try:
        orphan_recs = recover_orphan_intents(trading_client, state)
    except APIError as exc:
        log.warning("orphan-intent recovery failed: %s", exc)
        orphan_recs = []
    if orphan_recs:
        save_state(state)
        for r in orphan_recs:
            append_audit({
                "ticker": r.ticker,
                "kind": "orphan_recovery",
                "transition": r.transition,
                "detail": r.detail,
            })
        reconciliations.extend(orphan_recs)

    for ticker in config.tickers:
        ts = state.get_or_create(ticker)
        # ---- reconcile first
        try:
            rec = reconcile_ticker(trading_client, ts)
        except APIError as exc:
            log.warning("reconcile %s failed: %s", ticker, exc)
            reconciliations.append(
                ReconcileResult(ticker, "error", f"reconcile failed: {exc}")
            )
            continue
        reconciliations.append(rec)
        if rec.transition != "none":
            save_state(state)
            append_audit({"ticker": ticker, "kind": "reconcile", "transition": rec.transition, "detail": rec.detail})

        if ts.stage == Stage.DISABLED or not ts.enabled:
            actions.append(ExecAction(ticker, "skipped", "ticker disabled"))
            continue

        # ---- decide
        save_fn = lambda s=state: save_state(s)  # noqa: E731 - one-liner is clearer
        try:
            if ts.stage == Stage.IDLE:
                act = _handle_idle(trading_client, option_client, stock_client, ts, config, today, save_fn)
            elif ts.stage == Stage.HOLDING:
                act = _handle_holding(trading_client, option_client, stock_client, ts, config, today, save_fn)
            elif ts.stage in (Stage.PUT_OPEN, Stage.CALL_OPEN):
                act = _handle_open_short(trading_client, option_client, ts, config, today, save_fn)
            else:
                act = ExecAction(ticker, "skipped", f"unhandled stage {ts.stage.value}")
        except APIError as exc:
            log.warning("decision step for %s failed: %s", ticker, exc)
            act = ExecAction(ticker, "error", f"alpaca error: {exc}")
        actions.append(act)
        if act.action in ("sto_put", "sto_call", "btc"):
            append_audit({
                "ticker": ticker,
                "kind": act.action,
                "detail": act.detail,
                "order_id": act.order_id,
                "client_order_id": act.client_order_id,
            })
        # Flag deeply-underwater HOLDING tickers (no eligible call strike above basis).
        if ts.stage == Stage.HOLDING and act.action == "skipped" and "below basis" in act.detail:
            holds.append(f"{ticker}: held below basis ${ts.avg_basis_per_share:.2f}")

    return RunResult(reconciliations=reconciliations, actions=actions, holds=holds)


def summarize(result: RunResult) -> str:
    """One-line summary of a run, suitable for CI logs."""
    counts: dict[str, int] = {}
    for a in result.actions:
        counts[a.action] = counts.get(a.action, 0) + 1
    rec_counts: dict[str, int] = {}
    for r in result.reconciliations:
        rec_counts[r.transition] = rec_counts.get(r.transition, 0) + 1
    parts = [f"{k}={v}" for k, v in sorted(counts.items())]
    rec_parts = [f"{k}={v}" for k, v in sorted(rec_counts.items()) if k != "none"]
    msg = "; ".join(parts) if parts else "no actions"
    if rec_parts:
        msg += " | reconciles: " + ", ".join(rec_parts)
    return msg


def total_return(
    state: WheelState,
    spot_lookup: callable | None = None,
) -> dict:
    """Compute realized + unrealized P&L across all wheel tickers.

    Realized   = lifetime premium collected + sum of called-away lot P&L
                 from the cycles log (which already records `lot_pnl`).
    Unrealized = for each HOLDING ticker, (spot - effective_basis) * shares.
                 ``spot_lookup(ticker)`` should return current spot or None.

    Returns a dict with realized, unrealized, total, and a per-ticker breakdown
    so the summary can render the details inline.
    """
    realized = 0.0
    unrealized = 0.0
    per_ticker: dict[str, dict] = {}
    for ticker, ts in state.tickers.items():
        ticker_realized = float(ts.cumulative_premium_lifetime or 0.0)
        called_away_pnl = 0.0
        for cycle in ts.cycles or []:
            if cycle.get("kind") == "called_away":
                # ``lot_pnl`` already includes the (strike - basis + lot_prem) *
                # shares math; don't double-count the premium portion (it's not
                # in cumulative_premium_lifetime — that's only put premium and
                # call premium realized via BTC).
                called_away_pnl += float(cycle.get("lot_pnl", 0.0) or 0.0)
        realized_for_ticker = ticker_realized + called_away_pnl
        realized += realized_for_ticker

        ticker_unrealized = 0.0
        if ts.stage in (Stage.HOLDING, Stage.CALL_OPEN) and ts.shares_held > 0:
            spot = spot_lookup(ticker) if spot_lookup else None
            if spot is not None and spot > 0:
                eff_basis = ts.avg_basis_per_share - ts.cumulative_premium_this_lot
                ticker_unrealized = (float(spot) - eff_basis) * ts.shares_held
                unrealized += ticker_unrealized
        per_ticker[ticker] = {
            "realized": round(realized_for_ticker, 2),
            "unrealized": round(ticker_unrealized, 2),
            "stage": ts.stage.value,
            "shares": ts.shares_held,
            "basis": ts.avg_basis_per_share,
        }
    return {
        "realized": round(realized, 2),
        "unrealized": round(unrealized, 2),
        "total": round(realized + unrealized, 2),
        "per_ticker": per_ticker,
    }


def daily_summary_markdown(
    state: WheelState,
    result: RunResult,
    today: date | None = None,
    spot_lookup: callable | None = None,
    positions: list | None = None,
) -> str:
    """Render the daily summary markdown body. Pure function so tests don't
    need to touch disk."""
    today = today or datetime.now(timezone.utc).date()
    ts_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    returns = total_return(state, spot_lookup=spot_lookup)

    lines: list[str] = [f"## Run at {ts_iso}", ""]

    lines.append("### Per-ticker state")
    if not state.tickers:
        lines.append("- (no tickers tracked yet)")
    else:
        lines.append("| Ticker | Stage | Shares | Basis | Lot call prem ($/sh) | Active contract |")
        lines.append("|---|---|---|---|---|---|")
        for ticker in sorted(state.tickers):
            t = state.tickers[ticker]
            active = "—"
            if t.active_contract:
                c = t.active_contract
                active = (
                    f"{c.get('symbol')} strike=${c.get('strike')} "
                    f"exp={c.get('expiration')} prem=${c.get('premium_received')}"
                )
            lines.append(
                f"| {ticker} | {t.stage.value} | {t.shares_held} | "
                f"${t.avg_basis_per_share:.4f} | "
                f"${t.cumulative_premium_this_lot:.4f} | {active} |"
            )
    lines.append("")

    lifetime = sum(
        float(t.cumulative_premium_lifetime or 0.0) for t in state.tickers.values()
    )
    lines.append(f"### Lifetime premium collected: ${lifetime:.2f}")
    lines.append("")

    lines.append("### Positions")
    if positions:
        lines.append("| Symbol | Side | Qty | Avg entry | Market value | Unrealized P/L |")
        lines.append("|---|---|---|---|---|---|")
        for p in positions:
            lines.append(
                f"| {getattr(p, 'symbol', '?')} | {getattr(p, 'side', '?')} | "
                f"{getattr(p, 'qty', '?')} | {getattr(p, 'avg_entry_price', '?')} | "
                f"{getattr(p, 'market_value', '?')} | {getattr(p, 'unrealized_pl', '?')} |"
            )
    else:
        lines.append("- (no live positions reported)")
    lines.append("")

    lines.append(
        f"### Total return: ${returns['total']:.2f} "
        f"(realized ${returns['realized']:.2f} + unrealized ${returns['unrealized']:.2f})"
    )
    lines.append("")

    lines.append("### Today's reconciliations")
    if not result.reconciliations:
        lines.append("- (none)")
    for r in result.reconciliations:
        lines.append(f"- {r.ticker}: {r.transition} — {r.detail}")
    lines.append("")

    lines.append("### Today's actions")
    if not result.actions:
        lines.append("- (none)")
    for a in result.actions:
        lines.append(f"- {a.ticker}: {a.action} — {a.detail}")
    if result.holds:
        lines.append("")
        lines.append("### Holds (no action)")
        for h in result.holds:
            lines.append(f"- {h}")
    lines.append("")
    return "\n".join(lines) + "\n"


def write_daily_summary(
    result: RunResult,
    today: date | None = None,
    state: WheelState | None = None,
    spot_lookup: callable | None = None,
    positions: list | None = None,
) -> Path:
    """Write a per-day markdown summary under wheel_bot/state/summaries/.

    Idempotent: appends a new section if today's file already exists, so
    multiple intraday runs accumulate into one daily report.

    Includes per-ticker state, lifetime premium, live positions, total
    return (realized + unrealized), and the run's reconciliations/actions
    — i.e. every field the spec calls for.
    """
    today = today or datetime.now(timezone.utc).date()
    SUMMARIES_DIR.mkdir(parents=True, exist_ok=True)
    out = SUMMARIES_DIR / f"{today.isoformat()}.md"
    if state is None:
        state = load_state()
    body = daily_summary_markdown(
        state, result, today=today, spot_lookup=spot_lookup, positions=positions
    )
    existing = out.read_text() if out.exists() else f"# Wheel summary {today.isoformat()}\n\n"
    out.write_text(existing + body)
    return out


def render_status(state: WheelState, ticker: str | None = None) -> str:
    """Human-readable status block."""
    if ticker:
        ticker = ticker.upper()
        if ticker not in state.tickers:
            return f"{ticker}: no state recorded"
        return _render_one(state.tickers[ticker])
    if not state.tickers:
        return "No tickers tracked yet. Configure WHEEL_TICKERS then run."
    return "\n".join(_render_one(t) for t in state.tickers.values())


def _render_one(ts: TickerState) -> str:
    parts = [
        f"{ts.ticker:<6} stage={ts.stage.value:<10}",
        f"enabled={ts.enabled}",
        f"shares={ts.shares_held}",
        f"basis=${ts.avg_basis_per_share:.4f}",
        f"lot_call_prem=${ts.cumulative_premium_this_lot:.4f}/sh",
        f"lifetime=${ts.cumulative_premium_lifetime:.2f}",
    ]
    if ts.active_contract:
        c = ts.active_contract
        parts.append(
            f"active={c.get('symbol')} strike={c.get('strike')} "
            f"exp={c.get('expiration')} prem=${c.get('premium_received')}"
        )
    return "  ".join(parts)


__all__ = [
    "RunResult",
    "run_once",
    "summarize",
    "write_daily_summary",
    "render_status",
    "STATE_PATH",
]
