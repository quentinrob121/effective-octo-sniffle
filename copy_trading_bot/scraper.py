from __future__ import annotations

from datetime import date, datetime
from typing import Iterable

import requests
from bs4 import BeautifulSoup, Tag

from .models import PoliticianTrade

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def fetch_trades_html(url: str, timeout: float = 20.0) -> str:
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def _parse_date(text: str) -> date | None:
    """Capitol Trades renders dates as '20 May 2026' (whitespace-collapsed)."""
    text = " ".join(text.split())
    for fmt in ("%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _parse_price(text: str) -> float | None:
    text = text.strip().lstrip("$").replace(",", "")
    if not text or text in ("N/A", "-"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _extract_ticker(cell: Tag) -> tuple[str, str] | None:
    """Returns (us_ticker, issuer_name) or None if non-US / unparseable."""
    raw = cell.get_text(" | ", strip=True)
    # Format: "Issuer Name | TICKER:COUNTRY"
    if "|" not in raw:
        return None
    issuer, tail = raw.rsplit("|", 1)
    ticker_part = tail.strip()
    if ":" not in ticker_part:
        return None
    symbol, country = ticker_part.split(":", 1)
    symbol = symbol.strip().upper()
    if country.strip().upper() != "US":
        return None
    if not symbol or not symbol.replace(".", "").replace("-", "").isalnum():
        return None
    return symbol, issuer.strip()


def parse_trades(
    html: str, politician_id: str, politician_name: str
) -> list[PoliticianTrade]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if table is None:
        return []
    trades: list[PoliticianTrade] = []
    for row in table.find_all("tr")[1:]:  # skip header
        cells = row.find_all("td")
        if len(cells) < 9:
            continue
        ticker_info = _extract_ticker(cells[1])
        if ticker_info is None:
            continue
        ticker, issuer = ticker_info
        published = _parse_date(cells[2].get_text(" ", strip=True))
        traded = _parse_date(cells[3].get_text(" ", strip=True))
        tx_type = cells[6].get_text(strip=True).lower()
        size_range = cells[7].get_text(strip=True)
        price = _parse_price(cells[8].get_text(strip=True))
        if not published or not traded or tx_type not in ("buy", "sell"):
            continue
        trades.append(
            PoliticianTrade(
                politician_id=politician_id,
                politician_name=politician_name,
                ticker=ticker,
                issuer=issuer,
                traded_date=traded,
                published_date=published,
                tx_type=tx_type,
                size_range=size_range,
                price=price,
            )
        )
    return trades


def fetch_trades(
    url: str, politician_id: str, politician_name: str
) -> list[PoliticianTrade]:
    html = fetch_trades_html(url)
    return parse_trades(html, politician_id, politician_name)


def chronological(trades: Iterable[PoliticianTrade]) -> list[PoliticianTrade]:
    """Capitol Trades returns newest-first; we want oldest-first for processing
    so buys precede their corresponding sells when applicable."""
    return sorted(trades, key=lambda t: (t.traded_date, t.published_date))
