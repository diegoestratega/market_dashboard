# Macro Context Dashboard

An on-demand macro snapshot — not a live stream. One Streamlit page that pulls
the current state of rates, equity futures, commodities, grains, volatility,
the dollar, credit and crypto, then writes a plain-English read of the tape and
a ranked list of what is worth a second look.

Live: <https://marketdash1.streamlit.app/>

## What it shows

| Card | Source | Notes |
|---|---|---|
| Rates & yield curve | FRED (`DGS2`/`DGS10`/`DGS30`) | Daily, published with a lag. Intraday reference from `^TNX`/`^TYX` |
| Index futures | yfinance | ES / NQ / RTY, falling back to the cash index if a contract is unavailable |
| Oil & metals | yfinance | WTI, gold, silver, copper |
| Agro | yfinance | CBOT wheat, corn, soybeans |
| VIX term structure | yfinance | 9D / 30D / 3M / 6M, with a contango-vs-backwardation read |
| Dollar index | yfinance | `DX-Y.NYB` |
| Credit stress | yfinance | HYG/LQD ratio as a high-yield-vs-investment-grade proxy |
| Crypto | yfinance | BTC and ETH, on 7-/30-day lookbacks since it trades 24/7 |
| Macro calendar | ForexFactory feed | CPI / NFP / FOMC / PCE for the current week |

## How the signals work

Thresholds scale to each series rather than being fixed percentages, so the
same rule means something comparable for a credit ratio that moves 6% a year
and a coin that moves 65%.

- **Unusual move** — flags when a day is either **≥1.5σ** of that series' own
  daily volatility, **or ≥3%** outright. Either test firing is enough.
- **Volatility (σ)** — the standard deviation of the last **63 sessions'**
  daily returns, recomputed on every load. Nothing is cached or hard-coded, so
  the bar moves with the market's regime.
- **52-week levels** — "near an extreme" is a position within the series' own
  52-week range (top or bottom 2%), not a fixed percentage of price.
- **Direction words** — a move must clear 0.3σ before it is called up, down,
  widening or easing; below that it reads as flat.
- **Red flags** — ranked by size relative to that series' volatility, coloured
  red ≥3σ, amber ≥1.5σ, grey below.
- **Stale data** — any series whose newest bar lags the rest of the board by a
  session is called out, so old figures are never presented as today's.

All tunable at the top of `app.py`.

## Running it locally

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe -m streamlit run app.py
```

Then open <http://localhost:8501>.

The rates card needs a free [FRED API key](https://fredaccount.stlouisfed.org/apikeys).
Everything else works without one. Provide it either way:

- `.streamlit/secrets.toml` with `FRED_API_KEY = "your-key"` — gitignored, and
  the file must never be committed; or
- a `FRED_API_KEY` environment variable.

Without a key the rates card shows a prompt and the rest of the page renders
normally.

## Deploying

Streamlit Cloud watches `main`, so **a push to `main` is a production deploy**.
The `FRED_API_KEY` lives in the app's Streamlit Cloud secrets settings, not in
this repo. Verify locally before pushing.
