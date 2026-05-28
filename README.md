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
python main.py sell AAPL 1        # market sell (paper)
python main.py sell AAPL 1 --limit 160

# Bracket order: entry + take-profit + stop-loss legs
python main.py bracket AAPL 1 --take-profit 200 --stop-loss 140
python main.py bracket AAPL 1 --take-profit 200 --stop-loss 140 --stop-limit 139

# Standalone protective stop (sell)
python main.py stop AAPL 1 --stop-price 140

# Positions
python main.py positions          # list open positions
python main.py close AAPL         # close a single position
python main.py close --all        # close every position (cancels open orders)

# Live trade feed over websockets (Ctrl+C to stop)
python main.py stream AAPL MSFT
```

## Layout

```
alpaca_starter/
  config.py        # loads + validates credentials from env / .env
  client.py        # builds Trading and Market-Data clients
  account.py       # account info helpers
  market_data.py   # quotes and historical bars
  orders.py        # market/limit/stop/trailing-stop/bracket, list/cancel
  positions.py     # list, summarize, close one / close all
  stream.py        # live websocket trade feed
main.py            # demo CLI tying it together
tests/             # pytest suite (mocked clients, no network)
```

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

The suite mocks the Alpaca clients, so it runs offline and needs no API keys.
GitHub Actions runs it on every push and pull request across Python 3.10–3.12
(see `.github/workflows/ci.yml`).

## Copy-trading bot (politician mirror)

`copy_trading_bot/` mirrors a congressional politician's disclosed trades from
[Capitol Trades](https://www.capitoltrades.com/) into your Alpaca account.

**Defaults** (override via env vars — see `.env.example`):

- Target: **Tim Moore (R-NC, House)** — Capitol Trades' highest-volume active
  trader at the time of setup (200+ trades, stocks only, recent activity).
- Sizing: **$500 per copied buy** (mirrored sells close the held position).
- Mode: **paper** (so long as `ALPACA_BASE_URL` is the paper endpoint).

### Important caveats

- **Disclosures lag the actual trade by up to 45 days** (STOCK Act rules).
  This is a delayed signal, not a live front-run.
- **Options trades are dropped.** Capitol Trades rarely discloses strike or
  expiry, so faithful options copying isn't possible. Tim Moore trades stocks
  only, so this is moot for the default target.
- **Non-US-listed tickers are dropped** (e.g. `:GB`, `:DE`).
- **Sell signals close the held position**; the bot never shorts.

### CLI

```bash
python -m copy_trading_bot preview            # show scraped trades, do nothing
python -m copy_trading_bot run --dry-run      # decide + log, no orders
python -m copy_trading_bot run                # actually mirror
```

### Scheduling (GitHub Actions)

`.github/workflows/copy-trade.yml` runs the bot hourly during US market hours
on a cron schedule and commits `copy_trading_bot/state/processed_trades.json`
back to the branch so processed disclosures aren't replayed. To enable it:

1. Repo **Settings → Secrets and variables → Actions** → add secrets
   `ALPACA_API_KEY`, `ALPACA_API_SECRET` (and optionally `ALPACA_BASE_URL`).
2. Optionally override the politician or sizing via repo **Variables**:
   `COPY_POLITICIAN_ID`, `COPY_POLITICIAN_NAME`, `COPY_TRADE_USD`,
   `COPY_MAX_TRADE_USD`.
3. Trigger a one-off run from the **Actions** tab → *Copy Trade* →
   *Run workflow* (toggle "Preview only" for a dry run).

A locally-installed cron is intentionally *not* provided — running the bot
from a laptop means it stops working as soon as the laptop sleeps.

## Note on market data

Quotes and bars use Alpaca's free IEX feed by default. With only IEX data,
quotes outside market hours (or for thinly traded symbols) may be empty.
Trading endpoints work against your paper account at any time.
