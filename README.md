# Alpaca Paper-Trading Starter

A minimal Python starter for the [Alpaca](https://alpaca.markets/) trading API:
connect, check your account, pull market data, and place paper orders.

Built on the official [`alpaca-py`](https://github.com/alpacahq/alpaca-py) SDK.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # then fill in your keys
```

Get paper-trading keys from the
[Alpaca paper dashboard](https://app.alpaca.markets/paper/dashboard/overview).

### Environment variables

| Variable             | Description                                              |
| -------------------- | -------------------------------------------------------- |
| `ALPACA_API_KEY`     | API key ID                                               |
| `ALPACA_API_SECRET`  | API secret key                                           |
| `ALPACA_BASE_URL`    | `https://paper-api.alpaca.markets/v2` (paper) or the live URL |

> **Never commit `.env`.** It is git-ignored. Keys transmitted in plaintext
> should be rotated from the dashboard.

## Usage

```bash
python main.py account            # account summary
python main.py quote AAPL         # latest quote
python main.py bars AAPL          # recent daily bars
python main.py orders             # list open orders
python main.py buy AAPL 1         # market buy, 1 share (paper)
python main.py buy AAPL 1 --limit 150
```

## Layout

```
alpaca_starter/
  config.py        # loads + validates credentials from env / .env
  client.py        # builds Trading and Market-Data clients
  account.py       # account info helpers
  market_data.py   # quotes and historical bars
  orders.py        # market/limit orders, list/cancel
main.py            # demo CLI tying it together
```

## Note on market data

Quotes and bars use Alpaca's free IEX feed by default. With only IEX data,
quotes outside market hours (or for thinly traded symbols) may be empty.
Trading endpoints work against your paper account at any time.
