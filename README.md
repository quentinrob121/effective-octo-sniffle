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

## Dip-buying ladder (`dip_ladder/`)

A separate, scale-in-on-dips helper: place GTC limit buys at -15%, -25%,
-35%, -50% off a reference price, with 1:2:3:5 sizing (deeper dips =
bigger adds). The ladder is just GTC limit orders, so it needs no daemon
— Alpaca fills the rungs when (if) price gets there.

### Standalone CLI

```bash
# Preview only (also works without ALPACA creds when --ref-price is set)
python -m dip_ladder plan NFLX --ref-price 360 --base-shares 10

# Place the ladder (uses the live quote if --ref-price is omitted)
python -m dip_ladder place NFLX --base-shares 10
python -m dip_ladder place NVDA --base-usd 500 --ref-price 175

# Custom geometry
python -m dip_ladder place TSLA --base-shares 5 \
    --levels 10,20,30,40,50 --weights 1,2,3,4,5

# Inspect / tear down
python -m dip_ladder status
python -m dip_ladder status NFLX
python -m dip_ladder cancel NFLX
```

Ladder orders are tagged via Alpaca's `client_order_id` (`ladder-<sym>-…`),
so `status` and `cancel` only touch ladder rungs — they leave your other
orders alone.

### Plugged into the copy-trader

Set `COPY_ENABLE_LADDER=true` (env or repo variable). After every
successful mirror-buy, the bot drops a ladder under it sized off the
mirror's own dollar budget. `COPY_LADDER_MAX_USD` caps the cumulative
notional per ladder.

## Position manager (`position_manager/`)

Walks every open long position and, if it has no protective SELL order
attached, places a trailing-stop SELL at `PSMGR_TRAIL_PCT`% below the high-
water mark. Alpaca trails the stop server-side, so "moving the floor up"
happens automatically — this module's job is just to make sure the trailing
stop *exists* on every position.

Optionally, set `PSMGR_REENTRY_PCT > 0` to also drop a GTC limit BUY
`reentry_pct`% below the current price (re-enters automatically if the
stop fires and price recovers to the bid).

### CLI

```bash
python -m position_manager run --dry-run     # show what'd happen
python -m position_manager run               # attach stops
python -m position_manager status            # list managed orders
python -m position_manager cancel AAPL       # cancel managed orders for AAPL
python -m position_manager cancel            # cancel all managed orders
```

### Scheduling

`.github/workflows/manage-positions.yml` runs every 30 min during US
market hours. The op is idempotent (no-op when the stop already exists),
so over-scheduling is harmless.

## Wheel bot (`wheel_bot/`)

Runs the classic options "wheel" strategy on a configurable basket of
tickers: sell cash-secured puts, take assignment if they finish ITM, sell
covered calls against the assigned shares, repeat. The bot tracks every
ticker's stage (`IDLE`/`PUT_OPEN`/`HOLDING`/`CALL_OPEN`/`DISABLED`) in
`wheel_bot/state/wheel_state.json`, persists after every order, and
reconciles against Alpaca on each run so that an assignment or call-away
that happens between cron ticks is picked up automatically.

**Hard correctness rules** (designed around past failure modes):

- **Cash-secured guard**: before every put STO, subtracts collateral
  already reserved by *every* open put (wheel-tagged or not) from options
  buying power; refuses if the result can't cover the new put's notional.
- **Never below basis**: covered calls won't be sold at a strike below
  the effective per-share basis (avg cost minus call premium already
  collected against the same lot).
- **Per-trade durability**: state is saved immediately after every
  successful order so a mid-loop crash never replays an STO.
- **Adopt pre-existing positions**: if a ticker is `IDLE` in state but
  has equity at Alpaca on first run, the bot adopts the position into
  `HOLDING` using Alpaca's `avg_entry_price` as basis (and logs a
  warning so you can override via `wheel_bot adopt`).
- **Deeply underwater = HOLD**: when every available call strike sits
  below basis, the lot stays in `HOLDING` and is surfaced in the daily
  summary rather than being sold for a loss.

### CLI

```bash
python -m wheel_bot status                  # state per ticker
python -m wheel_bot status PLTR             # one ticker
python -m wheel_bot run --dry-run           # one full cycle, no orders
python -m wheel_bot run                     # one full cycle (live)
python -m wheel_bot summary                 # write today's markdown summary
python -m wheel_bot close PLTR              # force BTC the open wheel contract
python -m wheel_bot pause PLTR              # stop opening new positions
python -m wheel_bot resume PLTR
python -m wheel_bot reset PLTR              # wipe local state for ticker
python -m wheel_bot adopt PLTR --basis 38.5 --shares 100
```

### Scheduling

`.github/workflows/wheel.yml` runs every 15 min during US market hours
(`*/15 13-21 * * 1-5` UTC, covering both EDT and EST). After each run it
commits `wheel_bot/state/` back to the branch (state file, audit log,
daily summary) with the same pull-rebase + retry pattern used by the
copy-trade workflow. The commit step runs `if: always()` so partial state
writes from a mid-loop crash still get persisted.

### Caveats

- **Options L3 required** — the Alpaca account must be approved for
  selling cash-secured puts and covered calls. Paper accounts can be
  upgraded for free in the dashboard.
- **Free IEX option data is sparse off-hours**. The bot will simply skip
  a ticker for the cycle if the chain comes back empty rather than
  guessing.
- **NANC has monthly options only**. The DTE-window picker returns
  `None` rather than picking a 40-DTE monthly when the target is 21-DTE,
  so NANC gets STO'd at most ~once a month, and only when the cron tick
  lands inside the window.
- **STO orders use GTC limits at the bid**. They may not fill instantly;
  the next cron tick won't double-submit because state is updated as
  soon as Alpaca accepts the order, regardless of fill.
- **BTC uses market orders**. Once we've decided the contract is decayed
  enough to close, parking a GTC limit at ask risks the option expiring
  unfilled. The decision gate guarantees we only do this when the cost
  is below the configured profit target.

## Note on market data

Quotes and bars use Alpaca's free IEX feed by default. With only IEX data,
quotes outside market hours (or for thinly traded symbols) may be empty.
Trading endpoints work against your paper account at any time.
