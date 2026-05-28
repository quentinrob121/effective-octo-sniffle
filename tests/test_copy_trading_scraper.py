from datetime import date

from copy_trading_bot.scraper import chronological, parse_trades

SAMPLE_HTML = """
<table>
  <tr><th>Politician</th><th>Issuer</th><th>Published</th><th>Traded</th>
      <th>Filed</th><th>Owner</th><th>Type</th><th>Size</th><th>Price</th><th></th></tr>
  <tr>
    <td>Tim Moore</td>
    <td>AT&amp;T Inc | T:US</td>
    <td>20 May 2026</td><td>18 May 2026</td><td>2 days</td>
    <td>Undisclosed</td><td>buy</td><td>15K&ndash;50K</td><td>$24.43</td><td></td>
  </tr>
  <tr>
    <td>Tim Moore</td>
    <td>Some London Co | LDN:GB</td>
    <td>20 May 2026</td><td>18 May 2026</td><td>2 days</td>
    <td>Undisclosed</td><td>buy</td><td>1K&ndash;15K</td><td>$10.00</td><td></td>
  </tr>
  <tr>
    <td>Tim Moore</td>
    <td>NVIDIA Corporation | NVDA:US</td>
    <td>29 Apr 2026</td><td>24 Mar 2026</td><td>36 days</td>
    <td>Undisclosed</td><td>sell</td><td>15K&ndash;50K</td><td>$175.20</td><td></td>
  </tr>
</table>
"""


def test_parse_drops_non_us_tickers():
    trades = parse_trades(SAMPLE_HTML, "M001236", "Tim Moore")
    tickers = [t.ticker for t in trades]
    assert "T" in tickers
    assert "NVDA" in tickers
    assert "LDN" not in tickers
    assert len(trades) == 2


def test_parse_extracts_dates_type_size_price():
    trades = parse_trades(SAMPLE_HTML, "M001236", "Tim Moore")
    by_ticker = {t.ticker: t for t in trades}
    t_att = by_ticker["T"]
    assert t_att.traded_date == date(2026, 5, 18)
    assert t_att.published_date == date(2026, 5, 20)
    assert t_att.tx_type == "buy"
    assert t_att.size_range.startswith("15K")
    assert t_att.price == 24.43

    nvda = by_ticker["NVDA"]
    assert nvda.tx_type == "sell"
    assert nvda.price == 175.20


def test_signature_is_stable_and_distinguishes_trades():
    trades = parse_trades(SAMPLE_HTML, "M001236", "Tim Moore")
    signatures = {t.signature for t in trades}
    assert len(signatures) == len(trades)
    # Re-parsing yields identical signatures.
    again = parse_trades(SAMPLE_HTML, "M001236", "Tim Moore")
    assert {t.signature for t in again} == signatures


def test_chronological_orders_oldest_first():
    trades = parse_trades(SAMPLE_HTML, "M001236", "Tim Moore")
    ordered = chronological(trades)
    assert ordered[0].traded_date <= ordered[-1].traded_date


def test_parse_empty_html():
    assert parse_trades("<html></html>", "X", "X") == []
