from __future__ import annotations

from datetime import date, timedelta

from wheel_bot.option_picker import (
    OptionCandidate,
    filter_chain,
    pick_expiration,
    pick_strike,
)


def _candidate(strike: float, exp: date, type: str = "put") -> OptionCandidate:
    return OptionCandidate(
        symbol=f"X{exp.strftime('%y%m%d')}{'P' if type == 'put' else 'C'}{int(strike*1000):08d}",
        underlying="X",
        expiration=exp,
        strike=strike,
        type=type,
    )


def test_pick_strike_returns_nearest_strike():
    today = date(2025, 1, 1)
    exp = today + timedelta(days=21)
    chain = [_candidate(s, exp) for s in (35, 38, 40, 42, 45)]
    pick = pick_strike(chain, target_price=39.4, type="put")
    assert pick is not None
    assert pick.strike == 40


def test_pick_strike_breaks_ties_arbitrarily_but_returns_one():
    today = date(2025, 1, 1)
    exp = today + timedelta(days=21)
    chain = [_candidate(s, exp) for s in (38, 40)]
    # 39 is equidistant from 38 and 40; min() returns first by stability.
    pick = pick_strike(chain, target_price=39.0, type="put")
    assert pick is not None
    assert pick.strike in (38, 40)


def test_pick_strike_returns_none_when_no_candidates():
    today = date(2025, 1, 1)
    exp = today + timedelta(days=21)
    chain = [_candidate(s, exp, type="call") for s in (35, 40)]
    assert pick_strike(chain, target_price=37, type="put") is None


def test_pick_strike_exact_match_wins():
    today = date(2025, 1, 1)
    exp = today + timedelta(days=21)
    chain = [_candidate(s, exp) for s in (35, 40, 42)]
    pick = pick_strike(chain, target_price=40.0, type="put")
    assert pick.strike == 40


def test_pick_expiration_in_window():
    today = date(2025, 1, 1)
    chain = [
        _candidate(40, today + timedelta(days=d))
        for d in (5, 10, 14, 21, 28, 35)
    ]
    exp = pick_expiration(chain, today, target_dte=21, dte_min=14, dte_max=28)
    assert exp == today + timedelta(days=21)


def test_pick_expiration_returns_nearest_in_window_when_target_absent():
    today = date(2025, 1, 1)
    # No 21-DTE option exists; nearest in window is 18 vs 25 -> 18 wins (|18-21|=3 < |25-21|=4).
    chain = [
        _candidate(40, today + timedelta(days=d))
        for d in (10, 18, 25, 35)
    ]
    exp = pick_expiration(chain, today, target_dte=21, dte_min=14, dte_max=28)
    assert exp == today + timedelta(days=18)


def test_pick_expiration_none_when_no_candidate_in_window():
    """NANC monthly: if the only expiration is 40 DTE we must return None
    rather than picking it — that's the bot's signal to skip the cycle."""
    today = date(2025, 1, 1)
    chain = [_candidate(40, today + timedelta(days=40))]
    exp = pick_expiration(chain, today, target_dte=21, dte_min=14, dte_max=28)
    assert exp is None


def test_pick_expiration_empty_chain():
    assert pick_expiration([], date(2025, 1, 1)) is None


def test_filter_chain_by_type_and_expiration():
    today = date(2025, 1, 1)
    exp1 = today + timedelta(days=14)
    exp2 = today + timedelta(days=21)
    chain = [
        _candidate(40, exp1, "put"),
        _candidate(40, exp2, "put"),
        _candidate(40, exp1, "call"),
    ]
    puts = filter_chain(chain, type="put")
    assert len(puts) == 2
    puts_exp1 = filter_chain(chain, type="put", expiration=exp1)
    assert len(puts_exp1) == 1
    assert puts_exp1[0].expiration == exp1
