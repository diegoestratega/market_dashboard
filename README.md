# Macro Context Dashboard

An on-demand macro snapshot — not a live stream. One Streamlit page that pulls
the current state of rates, equity futures, commodities, grains, volatility,
the dollar, credit and crypto, then writes a plain-English read of the tape and
a ranked list of what is worth a second look.

Live: <https://marketdash1.streamlit.app/>

## What it shows

| Card | Source | Notes |
|---|---|---|
| Rates & yield curve | FRED + yfinance | Live 5Y/10Y/30Y lead the card (`^FVX`/`^TNX`/`^TYX`); FRED `DGS2/5/10/30` is the settled reference. The 2Y is estimated — see below |
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
- **The 2Y yield is an estimate.** Yahoo publishes no live 2Y index, and
  `2YY=F` is too thinly quoted to use (one 15-minute bar in five days, 14–20 bps
  off the official yield, and a correlation of daily *changes* against it of
  0.05). So the 2Y shown is FRED's settle carried forward by the 5Y's move since
  that settle, scaled by β = 0.894 — measured over 495 sessions, correlation
  0.89, intercept ≈ 0, β stable between 0.85 and 0.98 across six consecutive
  sub-periods. Median error 1.2 bps, 2.6 bps at the 80th percentile. It is
  labelled `(est)` wherever it appears.
- **Credit ratio uses raw, not dividend-adjusted, prices.** HYG yields ~6.1% and
  LQD ~4.7%, so adjusted history drifts the ratio upward by the carry
  differential rather than by credit conditions.
- **Index futures are roll-adjusted.** Yahoo's `=F` series switches contract
  without back-adjusting, so the calendar spread lands as a one-day price jump —
  measured against the cash index these reached 3.2 percentage points, a 3σ
  phantom move. On a roll day (third Friday of Mar/Jun/Sep/Dec) the contract's
  economic return is the cash index's return, so it is substituted and the
  series rebuilt to end on the true current price.
- **Noise band** — changes at or inside ±0.05% are greyed rather than coloured,
  so a −0.02% tick does not read as a meaningful down day.
- **Feed gaps** — every change is checked against the calendar distance it
  actually spans. If the source is missing sessions, the figure is withheld
  rather than shown over the wrong window, and σ is computed only from
  genuinely consecutive sessions. Levels are still shown, since they are
  current. This is not hypothetical: Yahoo stopped publishing `^VIX9D`,
  `^VIX3M` and `^VIX6M` for 54 days in 2026 while continuing to report a
  current level, so their "1-day" change was really a 54-day change.
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
