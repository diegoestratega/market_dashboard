"""
Pre-Market / On-Demand Macro Context Dashboard
---------------------------------------------------------------------------
Nine groups — rates & yield curve (FRED), index futures, oil & metals, agro,
VIX term structure, dollar index, credit stress (HYG/LQD), crypto, and the
macro calendar. Each reports 1D/1W/1M changes plus 1-month and 52-week level
context, which feed the written "market read" and the red-flag list.

Crypto uses 7-/30-day lookbacks rather than 5/21 because it trades 24/7.
Yields come from FRED (daily, published with a lag); everything else comes
from yfinance on a ~15-20 minute delay.

Change history lives in git, not in this docstring.
"""

import datetime as dt
import os
import time
from contextlib import contextmanager

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
import yfinance as yf

st.set_page_config(page_title="Macro Context Dashboard", layout="wide")


def get_secret(name: str, default: str = ""):
    """Read a secret without exploding when there is no secrets.toml.

    st.secrets raises StreamlitSecretNotFoundError (it does not return the
    default) whenever no secrets file exists, which is the normal case for a
    local checkout — so fall back to the environment before giving up.
    """
    try:
        return st.secrets.get(name, "") or os.environ.get(name, default)
    except Exception:
        return os.environ.get(name, default)


# A move is unusual if EITHER test fires — whichever happens first:
#   >= UNUSUAL_MOVE_SIGMA of the series' own daily standard deviation, so a
#     move that is genuinely large for that series counts however small it
#     looks in absolute terms;
#   >= UNUSUAL_MOVE_PCT in absolute terms, so a violent day still flags even
#     after a sustained high-vol stretch has pulled sigma up to meet it.
UNUSUAL_MOVE_PCT = 3.0
UNUSUAL_MOVE_SIGMA = 1.5

# Intraday stamps are shown in US market time and labelled. yfinance returns
# each exchange's own zone (Chicago for ^TNX, New York for equities).
MARKET_TZ = "America/New_York"

# A series whose newest bar lags the rest of the board by a session is stale;
# its "today" figures would be yesterday's while still being labelled today.
STALE_SESSION_LAG = 1

# Two consecutive sessions should be at most this far apart in calendar days
# (a long weekend plus a holiday). Anything wider is a hole in the feed.
MAX_SESSION_GAP_DAYS = 5

# A change over n sessions should span about n*7/5 calendar days. Allow this
# much slack before the figure is unusable. Yahoo stopped publishing ^VIX9D,
# ^VIX3M and ^VIX6M for 54 days in 2026, and their "1-day" change was silently
# a 54-day change -- large, wrong, and indistinguishable from a real move.
SPAN_SLACK_DAYS = 5

# "Near" a 52-week extreme is measured against the series' own 52-week RANGE,
# not as a fixed percentage of price. A flat 1% band treated HYG/LQD — whose
# whole yearly range is a couple of percent — as permanently near its high,
# while being pure noise for crypto.
NEAR_52W_RANGE_FRAC = 0.02

# Moves smaller than this multiple of the series' own daily standard deviation
# are called flat rather than given a direction. Stops a -0.02% tick on the
# credit ratio being reported as "widening".
FLAT_SIGMA_FRAC = 0.3

FRED_API_KEY = get_secret("FRED_API_KEY")

FUTURES = {"ES=F": "S&P 500 (ES)", "NQ=F": "Nasdaq 100 (NQ)", "RTY=F": "Russell 2000 (RTY)"}
FUTURES_FALLBACK = {"ES=F": ("^GSPC", "S&P 500 (cash, futures unavailable)"),
                     "NQ=F": ("^IXIC", "Nasdaq Composite (cash, futures unavailable)"),
                     "RTY=F": ("^RUT", "Russell 2000 (cash, futures unavailable)")}
COMMODITIES = {"CL=F": "Crude Oil (WTI)", "GC=F": "Gold", "SI=F": "Silver", "HG=F": "Copper"}
AGRO = {"ZW=F": "Wheat", "ZC=F": "Corn", "ZS=F": "Soybeans"}
CRYPTO = {"BTC-USD": "Bitcoin (BTC)", "ETH-USD": "Ethereum (ETH)"}
VIX_TERM = ["^VIX9D", "^VIX", "^VIX3M", "^VIX6M"]
DXY_TICKER = "DX-Y.NYB"

# Changes at or inside this band read as noise and are greyed rather than
# coloured, so a -0.02% tick does not look like a meaningful down day.
NOISE_PCT = 0.05

# Quarterly roll for equity index futures: third Friday of these months. A gap
# only counts as a roll if it also diverges from the cash index by more than
# ROLL_DIVERGENCE_PP, so a genuine overnight move is never mistaken for one.
ROLL_MONTHS = (3, 6, 9, 12)
ROLL_WINDOW_DAYS = 4
ROLL_DIVERGENCE_PP = 0.75

# Commodity and grain contracts roll on their own schedules, so there is no
# date window to gate on and no cash index to anchor to. A continuously traded
# ETF stands in as the reference instead.
COMMODITY_PROXY = {"CL=F": "USO", "GC=F": "GLD", "SI=F": "SLV", "HG=F": "CPER",
                   "ZW=F": "WEAT", "ZC=F": "CORN", "ZS=F": "SOYB"}

# Divergence from the proxy alone is NOT sufficient evidence of a roll: over
# two years it flagged 95 days across these seven contracts, of which only 35
# were real. A roll is a PERSISTENT step in the futures/proxy ratio, where
# tracking noise reverts, so history is judged on that. Measured on the
# current bar the two are inseparable (noise reaches 22.9% against a median
# roll of 3.1%), so today is settled by looking at the contracts themselves.
ROLL_PROXY_DIVERGENCE_PP = 2.0
ROLL_PERSIST_STEP_PCT = 1.5
ROLL_PERSIST_WINDOW = 5

MONTH_CODES = "FGHJKMNQUVXZ"
FUTURES_ROOTS = {"CL=F": ("CL", "NYM"), "GC=F": ("GC", "CMX"), "SI=F": ("SI", "CMX"),
                 "HG=F": ("HG", "CMX"), "ZW=F": ("ZW", "CBT"), "ZC=F": ("ZC", "CBT"),
                 "ZS=F": ("ZS", "CBT")}

# Live yield proxies. Yahoo publishes intraday indices for the 5Y, 10Y and 30Y
# but has nothing for the 2Y: ^UST2Y/^US2Y do not exist, and 2YY=F (CBOT 2-Year
# Yield futures) is too thinly quoted to use — one 15-minute bar in five days,
# 14-20 bps away from the official yield, and a correlation of daily CHANGES
# against the official 2Y of just 0.05.
#
# So the 2Y is carried forward instead: take FRED's settled 2Y and add the 5Y's
# move since that settle, scaled by beta. Measured over 495 sessions the 2Y
# moves 0.894 bps per bp of 5Y (correlation 0.89, intercept ~0, and the beta
# held between 0.85 and 0.98 across six consecutive sub-periods). Median error
# 1.2 bps, 2.6 bps at the 80th percentile. It is an estimate and is labelled
# as one.
LIVE_YIELD_TICKERS = {"5Y": "^FVX", "10Y": "^TNX", "30Y": "^TYX"}
TWO_YEAR_BETA_ON_5Y = 0.894
TWO_YEAR_EST_ERR_BPS = 2.6

STANDARD_CFG = dict(week_n=5, month_n=21, month_window=21, year_window=252)
CRYPTO_CFG = dict(week_n=7, month_n=30, month_window=30, year_window=365)

SANITY_CAP_1D = {"vix": 20.0}

FF_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
CALENDAR_KEYWORDS = ["CPI", "PCE", "Non-Farm", "NFP", "Nonfarm", "FOMC",
                     "Fed Funds", "Interest Rate", "Unemployment Claims", "Employment"]

BG_APP = "#0f1116"
BG_CARD = "#171a21"
BG_PLOT = "#1b1f28"
BG_BOX = "#1c212b"
GRID = "#2a2f3a"
TEXT = "#e8e9ec"
MUTED = "#9aa0ab"
POS = "#34d399"
NEG = "#f87171"
ACCENT = "#2dd4bf"
ACCENT2 = "#f59e0b"
ACCENT3 = "#60a5fa"
ACCENT4 = "#c084fc"
PALETTE = [ACCENT, ACCENT2, ACCENT3, ACCENT4]

CHART_HEIGHT = 230

st.markdown(f"""
<style>
.block-container {{padding-top: 1.4rem; padding-bottom: 3rem; max-width: 1500px;}}

div[data-testid="stMetric"] {{
    background-color: {BG_CARD}; border-radius: 8px; padding: 10px 12px;
    border: 1px solid #262b36;
}}
div[data-testid="stMetricLabel"] p {{
    font-size: 0.78rem !important; color: {MUTED} !important; font-weight: 500;
}}
div[data-testid="stMetricValue"] {{
    font-size: 1.5rem !important; color: {TEXT} !important; font-weight: 600;
}}
div[data-testid="stMetricDelta"] svg {{ display: none; }}

/* Cards are real st.container() blocks keyed by card()/cardflag(), so the
   styling lands on the element that actually contains the charts and tables.
   The `st-key-card` prefix matches both the plain and flagged keys. */
div[class*="st-key-card"] {{
    background-color: {BG_CARD} !important;
    border: none !important; border-left: 4px solid {ACCENT} !important;
    border-radius: 12px !important; padding: 20px 22px !important;
    margin-bottom: 20px;
}}
div[class*="st-key-cardflag-"] {{ border-left: 4px solid {NEG} !important; }}
.card-title {{
    margin-top: 0; margin-bottom: 14px; font-size: 1.05rem; font-weight: 600;
    color: {TEXT}; letter-spacing: 0.2px;
}}
.small-caption {{ font-size: 0.75rem; color: {MUTED}; margin-top: 6px; }}
.flag-red {{ color: {NEG}; font-weight: 600; line-height: 1.6; }}
.flag-ok {{ color: {POS}; font-weight: 500; }}
.summary-box {{
    background-color: {BG_CARD}; border-radius: 12px; padding: 20px 24px;
    margin-bottom: 12px; font-size: 0.98rem; line-height: 1.75; color: {TEXT};
    border-left: 4px solid {ACCENT};
}}
.impact-high {{ color: {NEG}; font-weight: 600; }}
.impact-medium {{ color: {ACCENT2}; font-weight: 600; }}
.impact-low {{ color: {MUTED}; }}
[data-testid="stDataFrame"] {{ border-radius: 8px; overflow: hidden; }}

.yield-box {{
    background-color: {BG_BOX}; border: 1px solid #262b36; border-radius: 8px;
    padding: 10px 12px; min-height: 88px;
}}
.yield-label {{ font-size: 0.78rem; color: {MUTED}; font-weight: 500; }}
.yield-value {{ font-size: 1.5rem; font-weight: 600; color: {TEXT}; }}
.yield-delta {{ font-size: 0.78rem; font-weight: 600; padding: 1px 6px; border-radius: 5px; margin-left: 6px; }}
.yield-delta-pos {{ background-color: rgba(52,211,153,0.15); color: {POS}; }}
.yield-delta-neg {{ background-color: rgba(248,113,113,0.15); color: {NEG}; }}
.yield-sub {{ font-size: 0.68rem; color: {MUTED}; margin-top: 6px; line-height: 1.4; }}
</style>
""", unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Layout helpers
# ---------------------------------------------------------------------------

@contextmanager
def card(title: str, flagged: bool = False):
    """A dashboard card that actually contains what follows it.

    The previous version emitted a bare <div class='card'> through st.markdown
    and closed it with a second st.markdown. Streamlit renders every element
    into its own DOM subtree, so that div self-closed immediately and the card
    never wrapped its charts or tables — only the title.
    """
    slug = "".join(ch if ch.isalnum() else "-" for ch in title.lower()).strip("-")
    with st.container(border=True, key=f"{'cardflag' if flagged else 'card'}-{slug}"):
        st.markdown(f"<h4 class='card-title'>{title}</h4>", unsafe_allow_html=True)
        yield


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------

@st.cache_data(ttl=6 * 3600, show_spinner=False)
def fetch_fred_series(series_id: str, limit: int = 270):
    """Returns (series, as_of). Never raises: an expired or mistyped key would
    otherwise take the whole page down on raise_for_status()."""
    if not FRED_API_KEY:
        return None, None
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {"series_id": series_id, "api_key": FRED_API_KEY, "file_type": "json",
              "sort_order": "desc", "limit": limit}
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        obs = r.json().get("observations", [])
    except Exception:
        return None, None
    rows = [(o["date"], o["value"]) for o in obs if o["value"] != "."]
    rows.reverse()
    if not rows:
        return None, None
    df = pd.DataFrame(rows, columns=["date", "value"]).astype({"value": float})
    df["date"] = pd.to_datetime(df["date"])
    as_of = df["date"].iloc[-1].strftime("%b %d")
    return df.set_index("date")["value"], as_of


def _clean_history(hist: pd.DataFrame) -> pd.DataFrame:
    """Collapse a DAILY history to one row per session. Never call this on
    intraday bars — it keeps only the last bar of each day, which silently
    discarded 130 of 135 fifteen-minute bars where it used to be applied."""
    if hist is None or hist.empty:
        return hist
    hist = hist.copy()
    hist.index = pd.to_datetime(hist.index).tz_localize(None) if hist.index.tz is not None \
        else pd.to_datetime(hist.index)
    hist["_date"] = hist.index.normalize()
    hist = hist.sort_index()
    hist = hist[~hist["_date"].duplicated(keep="last")]
    return hist.drop(columns="_date")


@st.cache_data(ttl=1200, show_spinner=False)
def fetch_yf_history(ticker: str, period: str = "1y", retries: int = 3, adjusted: bool = True):
    """One year by default, so a single fetch serves both the 1D/1W/1M columns
    and the 52-week level test. Fetching 3mo and 1y separately doubled the
    request count (38 calls instead of 19) for identical numbers.

    `adjusted=False` returns raw closes. Needed wherever the figure of interest
    is price action rather than total return — see the credit ratio.
    """
    for attempt in range(retries):
        try:
            hist = yf.Ticker(ticker).history(period=period, interval="1d",
                                             auto_adjust=adjusted)
            hist = _clean_history(hist)
            if hist is not None and not hist.empty:
                closes = hist["Close"].dropna()
                if not closes.empty:
                    as_of = closes.index[-1].strftime("%Y-%m-%d %H:%M")
                    return closes, as_of
        except Exception:
            pass
        if attempt < retries - 1:
            time.sleep(0.7)
    return None, None


@st.cache_data(ttl=300, show_spinner=False)
def fetch_intraday_quote(ticker: str):
    try:
        hist = yf.Ticker(ticker).history(period="5d", interval="15m")
        if hist is not None and not hist.empty:
            closes = hist["Close"].dropna()
            if not closes.empty:
                return float(closes.iloc[-1]), format_market_time(closes.index[-1])
    except Exception:
        pass
    hist, as_of = fetch_yf_history(ticker, period="5d")
    if hist is not None:
        return float(hist.iloc[-1]), as_of
    return None, None


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def fetch_ff_calendar():
    try:
        r = requests.get(FF_CALENDAR_URL, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        events = r.json()
    except Exception:
        return [], None
    filtered = [e for e in events if e.get("country") == "USD"
                and any(k.lower() in e.get("title", "").lower() for k in CALENDAR_KEYWORDS)]
    return filtered, dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def format_event_datetime(raw_date: str):
    try:
        d = dt.datetime.fromisoformat(raw_date)
        return d.strftime("%b %d, %I:%M %p").replace(" 0", " ")
    except Exception:
        return raw_date


def fetch_future_with_fallback(ticker: str, label: str):
    hist, as_of = fetch_yf_history(ticker)
    if hist is not None:
        return hist, label, ticker
    fb_ticker, fb_label = FUTURES_FALLBACK.get(ticker, (None, None))
    if fb_ticker:
        hist, as_of = fetch_yf_history(fb_ticker)
        if hist is not None:
            return hist, fb_label, fb_ticker
    return None, label, ticker


# ---------------------------------------------------------------------------
# Calculations
# ---------------------------------------------------------------------------

def pct_changes(series: pd.Series, cap: float = None, week_n: int = 5, month_n: int = 21):
    s = series.dropna()
    if s.empty:
        return {"last": None, "chg_1d": None, "chg_1w": None, "chg_1m": None,
                "suspect_1d": False, "sigma": None, "gapped": False}
    last = s.iloc[-1]
    gapped = False

    def chg(n):
        """None when the two observations are too far apart to mean what the
        column header says — a hole in the feed, not a real move."""
        nonlocal gapped
        if len(s) <= n or s.iloc[-1 - n] == 0:
            return None
        span = (s.index[-1] - s.index[-1 - n]).days
        if span > n * 7 / 5 + SPAN_SLACK_DAYS:
            gapped = True
            return None
        return (last / s.iloc[-1 - n] - 1) * 100

    chg_1d = chg(1)
    suspect = cap is not None and chg_1d is not None and abs(chg_1d) > cap
    return {"last": last, "chg_1d": (None if suspect else chg_1d),
            "chg_1w": chg(week_n), "chg_1m": chg(month_n), "suspect_1d": suspect,
            "sigma": daily_sigma(s), "gapped": gapped}


def bps_changes(series: pd.Series):
    s = series.dropna()
    if s.empty:
        return {"last": None, "chg_1d": None, "chg_1w": None, "chg_1m": None, "sigma": None}
    last = s.iloc[-1]

    def chg(n):
        return (last - s.iloc[-1 - n]) * 100 if len(s) > n else None

    diffs = (s.diff().dropna() * 100).tail(63)
    sd = float(diffs.std()) if len(diffs) >= 12 else None
    return {"last": last, "chg_1d": chg(1), "chg_1w": chg(5), "chg_1m": chg(21),
            "sigma": sd if sd else None}


def _third_friday(year, month):
    d = dt.date(year, month, 15)
    while d.weekday() != 4:
        d += dt.timedelta(days=1)
    return d


def in_roll_window(ts):
    """Equity index futures roll on the third Friday of Mar/Jun/Sep/Dec."""
    d = ts.date()
    for m in ROLL_MONTHS:
        tf = _third_friday(d.year, m)
        if 0 <= (d - tf).days <= ROLL_WINDOW_DAYS:
            return True
    return False


def backadjust_rolls(fut: pd.Series, cash: pd.Series):
    """Strip quarterly roll discontinuities out of a continuous futures series.

    Yahoo's `=F` series switches to the next contract without back-adjusting,
    so the calendar spread lands as a one-day price jump. Measured against the
    cash index those gaps reached 3.2 percentage points — a 3-sigma phantom
    move that trips the flagging system, inflates sigma, and leaves the
    52-week range describing prices that were never traded on one contract.

    On a roll day the contract's economic return is the cash index's return,
    so substitute it there and rebuild the series so it still ends on the true
    current price.
    """
    if fut is None or cash is None or fut.empty or cash.empty:
        return fut, 0
    j = pd.concat([fut.rename("f"), cash.rename("c")], axis=1, join="inner").dropna()
    if len(j) < 30:
        return fut, 0
    rf, rc = j["f"].pct_change(), j["c"].pct_change()
    rolls = ((rf - rc).abs() * 100 > ROLL_DIVERGENCE_PP) & \
            pd.Series([in_roll_window(t) for t in j.index], index=j.index)
    if not rolls.any():
        return fut, 0
    adj = rf.where(~rolls, rc).fillna(0)
    growth = (1 + adj).cumprod()
    rebuilt = growth / growth.iloc[-1] * float(j["f"].iloc[-1])
    return rebuilt, int(rolls.sum())


def contract_symbols(root, exchange, count=6, start=None):
    """The next few listed months for a futures root, Yahoo style (CLX26.NYM).

    Months a contract does not trade simply return no data and are skipped,
    which is cheaper than encoding each product's delivery cycle.
    """
    d = start or dt.date.today()
    out = []
    for k in range(count):
        m0 = d.month - 1 + k
        year, month = d.year + m0 // 12, m0 % 12
        out.append(f"{root}{MONTH_CODES[month]}{year % 100:02d}.{exchange}")
    return out


def confirm_roll(front: pd.Series, ticker: str):
    """Did the continuous series change contract on its most recent bar?

    Definitive where the proxy test is not: if the last two closes belong to
    two different delivery months, the move between them is the calendar
    spread, not a price change. Returns the return of the contract actually
    held into the roll, which is the real move for that session.
    """
    roots = FUTURES_ROOTS.get(ticker)
    if roots is None or len(front) < 2:
        return None
    root, exchange = roots
    today_v, prev_v = float(front.iloc[-1]), float(front.iloc[-2])
    held = matched_today = None
    for sym in contract_symbols(root, exchange):
        c, _ = fetch_yf_history(sym, period="1mo")
        if c is None or len(c) < 2:
            continue
        if abs(float(c.iloc[-1]) - today_v) < 1e-6:
            matched_today = sym
        if abs(float(c.iloc[-2]) - prev_v) < 1e-6:
            held = (sym, c)
    if held and matched_today and held[0] != matched_today:
        hc = held[1]
        return (float(hc.iloc[-1]) / float(hc.iloc[-2]) - 1) * 100
    return None


def backadjust_proxy_rolls(fut: pd.Series, proxy: pd.Series, ticker: str):
    """Remove contract-roll steps from a commodity or grain series.

    History is judged by persistence — a roll leaves a permanent step in the
    futures/proxy ratio, tracking noise reverts within days. The most recent
    bars have no "after" window to test, so the latest one is settled against
    the contracts themselves and the rest are left alone rather than guessed.
    """
    if fut is None or proxy is None or fut.empty or proxy.empty:
        return fut, 0, None
    j = pd.concat([fut.rename("f"), proxy.rename("p")], axis=1, join="inner").dropna()
    if len(j) < 40:
        return fut, 0, None
    ratio = j["f"] / j["p"]
    div = (j["f"].pct_change() - j["p"].pct_change()).abs() * 100
    rets = j["f"].pct_change()
    w = ROLL_PERSIST_WINDOW
    rolls = pd.Series(False, index=j.index)
    for i in range(w, len(j) - w):
        if div.iloc[i] <= ROLL_PROXY_DIVERGENCE_PP:
            continue
        before = ratio.iloc[i - w:i].median()
        after = ratio.iloc[i + 1:i + 1 + w].median()
        if before and abs(after / before - 1) * 100 > ROLL_PERSIST_STEP_PCT:
            rolls.iloc[i] = True

    # The newest bar: only the contracts can settle it.
    live_roll_return = None
    if div.iloc[-1] > ROLL_PROXY_DIVERGENCE_PP:
        live_roll_return = confirm_roll(j["f"], ticker)
        if live_roll_return is not None:
            rolls.iloc[-1] = True
            rets.iloc[-1] = live_roll_return / 100

    if not rolls.any():
        return fut, 0, None
    adj = rets.where(~rolls, j["p"].pct_change())
    if live_roll_return is not None:
        adj.iloc[-1] = live_roll_return / 100      # the held contract's own move
    adj = adj.fillna(0)
    growth = (1 + adj).cumprod()
    rebuilt = growth / growth.iloc[-1] * float(j["f"].iloc[-1])
    return rebuilt, int(rolls.sum()), live_roll_return


def estimate_live_2y(fred_2y, fvx_daily, fvx_live):
    """FRED's settled 2Y carried forward by the 5Y's move since that settle.

    Anchored on the same session FRED last published, so the estimate is the
    settle plus only the move that has happened since it.
    """
    if fred_2y is None or fvx_live is None or fvx_daily is None or fvx_daily.empty:
        return None
    settle_date = fred_2y.index[-1].normalize()
    prior = fvx_daily[fvx_daily.index.normalize() <= settle_date]
    if prior.empty:
        return None
    return float(fred_2y.iloc[-1] + TWO_YEAR_BETA_ON_5Y * (fvx_live - prior.iloc[-1]))


def staleness(last_bar: dict, always_on=()):
    """Series whose newest bar lags the rest of the board by a session.

    Calibrated against the newest bar actually observed rather than against the
    calendar, so weekends and market holidays need no special handling. Assets
    that trade 24/7 are held out of the consensus, since at a weekend they
    legitimately run ahead of everything that trades in sessions.
    """
    session = {k: v for k, v in last_bar.items() if k not in always_on}
    if not session:
        return []
    newest = max(session.values())
    return sorted(k for k, v in session.items()
                  if (newest - v).days >= STALE_SESSION_LAG)


def daily_sigma(series: pd.Series, window: int = 63):
    """Standard deviation of recent daily percent returns.

    Gives every threshold a unit that scales with how volatile the series
    actually is, so the same rule can be applied to the credit ratio and to
    crypto without meaning wildly different things.
    """
    s = series.dropna()
    if len(s) < 12:
        return None
    w = s.tail(window)
    rets = w.pct_change() * 100
    # Only differences between genuinely consecutive sessions. A hole in the
    # feed otherwise enters as one enormous "daily" return: ^VIX9D's 54-day
    # gap put its sigma at 16.1% a day, which made it unflaggable.
    spans = w.index.to_series().diff().dt.days
    rets = rets[spans <= MAX_SESSION_GAP_DAYS].dropna()
    if len(rets) < 10:
        return None
    sd = float(rets.std())
    return sd if sd > 0 else None


def level_status(series: pd.Series, month_window: int = 21, year_window: int = 252,
                  range_frac: float = NEAR_52W_RANGE_FRAC):
    """1-month high/low (informational) and 52-week near/breach.

    "Near" is a position within the 52-week range rather than a fixed
    percentage of price: a series sitting in the top `range_frac` of its own
    high-low band is near its high. That keeps the test comparable across a
    credit ratio that moves 2% a year and a coin that moves 100%.
    """
    s = series.dropna()
    if len(s) < 6:
        return None
    last = s.iloc[-1]
    mw = s.tail(min(month_window, len(s)))
    result = {"month": None, "year": None, "have_year": False, "range_pos": None}
    if last >= mw.max():
        result["month"] = "high"
    elif last <= mw.min():
        result["month"] = "low"
    have_year = len(s) >= int(year_window * 0.6)
    result["have_year"] = have_year
    if have_year:
        yw = s.tail(min(year_window, len(s)))
        y_max, y_min = float(yw.max()), float(yw.min())
        span = y_max - y_min
        if span > 0:
            result["range_pos"] = (last - y_min) / span
        if last >= y_max:
            result["year"] = "breach_high"
        elif last <= y_min:
            result["year"] = "breach_low"
        elif span > 0 and result["range_pos"] >= 1 - range_frac:
            result["year"] = "near_high"
        elif span > 0 and result["range_pos"] <= range_frac:
            result["year"] = "near_low"
    return result


def _missing(x):
    """None survives a plain dict, but pandas turns it into NaN inside a float
    column — and NaN is not None, so a withheld figure printed as "+nan%"."""
    return x is None or (isinstance(x, float) and pd.isna(x))


def fmt_pct(x, suspect=False):
    if suspect:
        return "⚠️ check"
    return "—" if _missing(x) else f"{x:+.2f}%"


def fmt_bps(x):
    return "—" if _missing(x) else f"{x:+.0f} bps"


def is_unusual(chg_1d_pct, sigma=None):
    """Either test firing is enough: big for this series, or big outright.

    A fixed percentage alone never fired on a genuinely extreme day in
    something quiet like the credit ratio; a sigma test alone would go silent
    on a violent day once a high-vol stretch had raised sigma to match it.
    """
    if chg_1d_pct is None:
        return False
    if abs(chg_1d_pct) >= UNUSUAL_MOVE_PCT:
        return True
    z = z_score(chg_1d_pct, sigma)
    return z is not None and z >= UNUSUAL_MOVE_SIGMA


def market_today():
    """Today's date in US market time."""
    return pd.Timestamp.now(tz=MARKET_TZ).normalize().tz_localize(None)


def session_word(session_date):
    """"today" only when the newest bar really is today's session.

    Before the open there is no bar for the current day, so a "1-day" change is
    the previous session's — which the dashboard used to narrate as "today"
    regardless. Naming the session instead of asserting "today" is also correct
    on weekends and holidays, and needs no trading calendar.
    """
    if session_date is None:
        return "in the latest session"
    return "today" if session_date >= market_today() else f"on {session_date:%b %d}"


def move_phrase(name, chg_pct, sigma, when="today"):
    """'Corn moved +3.4% today (2.1σ)' — the sigma makes the size legible."""
    z = z_score(chg_pct, sigma)
    tail = f" ({z:.1f}σ)" if z is not None else ""
    return f"{name} moved {chg_pct:+.1f}% {when}{tail}."


def format_market_time(ts):
    """Intraday stamps in US market time, explicitly labelled.

    The previous code ran intraday timestamps through tz_localize(None), which
    kept the wall-clock reading of whichever exchange zone yfinance returned
    and then displayed it with no zone at all — a 14:50 ET bar showed as 13:50.
    """
    ts = pd.Timestamp(ts)
    if ts.tz is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert(MARKET_TZ).strftime("%H:%M ET")


def z_score(chg_pct, sigma):
    """Today's move expressed in the series' own daily standard deviations."""
    if chg_pct is None or not sigma:
        return None
    return abs(chg_pct) / sigma


def is_flat(chg_pct, sigma):
    """True when a move is too small to deserve a direction word."""
    if chg_pct is None:
        return True
    if not sigma:
        return abs(chg_pct) < 0.05
    return abs(chg_pct) < FLAT_SIGMA_FRAC * sigma


def flag(red_flags, text, severity):
    """Record a red flag with a severity so the list can be ranked.

    Severity is in daily-sigma units where the signal has a magnitude, and a
    hand-set score for structural signals (curve inversion, backwardation)
    that have no natural one. Without this every flag ranked equally, so a
    0.5% drift to a marginal new high sat level with a 4% move in crude.
    """
    red_flags.append({"text": text, "severity": float(severity)})


def severity_tier(sev):
    return "high" if sev >= 3 else ("moderate" if sev >= 1.5 else "low")


def base_layout(fig, height, legend=True, y_range=None):
    layout_kwargs = dict(
        height=height, margin=dict(l=6, r=10, t=(34 if legend else 8), b=6),
        paper_bgcolor=BG_CARD, plot_bgcolor=BG_PLOT,
        font=dict(color=TEXT, size=12),
        xaxis=dict(showgrid=False, tickfont=dict(size=10, color=MUTED), linecolor=GRID),
        yaxis=dict(showgrid=True, gridcolor=GRID, tickfont=dict(size=10, color=MUTED),
                    zerolinecolor=GRID),
        hovermode="x unified",
    )
    if legend:
        layout_kwargs["legend"] = dict(orientation="h", yanchor="bottom", y=1.02, x=0,
                                        font=dict(size=11, color=MUTED))
    if y_range:
        layout_kwargs["yaxis"]["range"] = y_range
    fig.update_layout(**layout_kwargs)
    return fig


def tight_range(series: pd.Series, pad_frac: float = 0.12):
    lo, hi = float(series.min()), float(series.max())
    span = hi - lo
    if span == 0:
        span = abs(hi) * 0.02 or 1.0
    pad = span * pad_frac
    return [lo - pad, hi + pad]


def normalized_chart(series_dict: dict, colors, height=CHART_HEIGHT):
    fig = go.Figure()
    all_vals = []
    for i, (name, s) in enumerate(series_dict.items()):
        s = s.dropna()
        if s.empty:
            continue
        norm = (s / s.iloc[0] - 1) * 100
        all_vals.append(norm)
        fig.add_trace(go.Scatter(x=norm.index, y=norm.values, mode="lines", name=name,
                                  line=dict(width=2.2, color=colors[i % len(colors)])))
    fig.update_yaxes(ticksuffix="%")
    y_range = tight_range(pd.concat(all_vals)) if all_vals else None
    return base_layout(fig, height, legend=True, y_range=y_range)


def level_chart(series: pd.Series, color=ACCENT, height=CHART_HEIGHT, ticksuffix="", tickprefix=""):
    fig = go.Figure()
    s = series.dropna()
    fig.add_trace(go.Scatter(x=s.index, y=s.values, mode="lines",
                              line=dict(width=2.4, color=color)))
    fig.update_yaxes(ticksuffix=ticksuffix, tickprefix=tickprefix)
    y_range = tight_range(s) if not s.empty else None
    return base_layout(fig, height, legend=False, y_range=y_range)


def multi_level_chart(series_dict: dict, colors, height=CHART_HEIGHT, ticksuffix=""):
    fig = go.Figure()
    all_vals = []
    for i, (name, s) in enumerate(series_dict.items()):
        s = s.dropna()
        all_vals.append(s)
        fig.add_trace(go.Scatter(x=s.index, y=s.values, mode="lines", name=name,
                                  line=dict(width=2.2, color=colors[i % len(colors)])))
    fig.update_yaxes(ticksuffix=ticksuffix)
    y_range = tight_range(pd.concat(all_vals)) if all_vals else None
    return base_layout(fig, height, legend=True, y_range=y_range)


def change_colour(v):
    """Grey inside the noise band, otherwise green or red."""
    if _missing(v) or abs(v) <= NOISE_PCT:
        return MUTED
    return POS if v > 0 else NEG


def render_change_table(rows, name_header, last_fmt="{:,.2f}", height=145,
                        change_cols=("chg_1d", "chg_1w", "chg_1m"),
                        headers=("1D", "1W", "1M"), sort_by=None):
    """One styled table for every group, so changes are encoded the same way
    everywhere: coloured by direction, greyed when inside the noise band."""
    df = pd.DataFrame(rows)
    if sort_by and sort_by in df.columns:
        df = df.sort_values(sort_by, ascending=False)
    df = df.reset_index(drop=True)
    out = pd.DataFrame({name_header: df["name"],
                        "Last": df["last"].map(lambda v: last_fmt.format(v) if v is not None else "—")})
    for col, hdr in zip(change_cols, headers):
        if "suspect_1d" in df.columns and col == "chg_1d":
            out[hdr] = [fmt_pct(v, bool(s)) for v, s in zip(df[col], df["suspect_1d"])]
        else:
            out[hdr] = df[col].map(fmt_pct)

    def paint(col):
        src = df[change_cols[list(headers).index(col.name)]]
        return [f"color: {change_colour(v)}" for v in src]

    styled = out.style.apply(paint, subset=list(headers), axis=0) \
                      .hide(axis="index")
    st.dataframe(styled, width="stretch", height=height, hide_index=True)
    if "gapped" in df.columns and df["gapped"].any():
        shown = ", ".join(df.loc[~df["gapped"], "name"]) or "none"
        st.markdown(f"<div class='small-caption'>Levels are current for all. Changes shown "
                    f"for {shown} only — the rest are missing sessions upstream.</div>",
                    unsafe_allow_html=True)


def render_change_box(label, chg_pct):
    """One period's change, coloured. Used for 1D/1W/1M alike.

    These three used to be encoded three different ways in the same card: 1D
    as a small coloured st.metric delta, 1W and 1M as large plain values, so
    the same quantity looked like different kinds of thing.
    """
    if _missing(chg_pct):
        body = f"<div class='yield-value' style='color:{MUTED}'>—</div>"
    else:
        body = (f"<div class='yield-value' style='color:{change_colour(chg_pct)}'>"
                f"{chg_pct:+.2f}%</div>")
    return (f"<div class='yield-box' style='min-height:70px'>"
            f"<div class='yield-label'>{label}</div>{body}</div>")


def render_yield_box(label, live_val, live_ts, settled_val, settled_as_of, estimated=False):
    """Lead with the freshest number available, not the settled one.

    FRED publishes a day in arrears, so the old layout put a stale figure in
    the headline slot and hid the live quote in the caption. Here the live
    yield is the headline, the chip is how far it has moved since the settle,
    and the settled value is demoted to the sub-line for reference.
    """
    if live_val is not None:
        move = (live_val - settled_val) * 100 if settled_val is not None else None
        cls = "yield-delta-pos" if (move or 0) >= 0 else "yield-delta-neg"
        chip = f"<span class='yield-delta {cls}'>{fmt_bps(move)}</span>" if move is not None else ""
        tag = " · est" if estimated else ""
        head = f"{live_val:.2f}%{chip}"
        sub = f"{live_ts}{tag}"
        if settled_val is not None:
            sub += f" &nbsp;·&nbsp; settled {settled_val:.2f}% ({settled_as_of})"
    else:
        head = f"{settled_val:.2f}%" if settled_val is not None else "—"
        sub = f"settled {settled_as_of} — no live quote"
    label_html = f"{label}<span style='color:{MUTED}'>{' (est)' if estimated else ''}</span>"
    return (f"<div class='yield-box'><div class='yield-label'>{label_html}</div>"
            f"<div class='yield-value'>{head}</div>"
            f"<div class='yield-sub'>{sub}</div></div>")


def collect_level_notes(rows, group_key, flagged, red_flags, big_picture):
    """rows: dicts with 'name', 'level' (from level_status) and optionally
    'chg_1d'/'sigma', used to size how emphatic a 52-week breach really is.

    A 52-week signal implies the 1-month one, so only the stronger of the two
    is reported — the narrative used to say both about the same asset.
    """
    band = f"{NEAR_52W_RANGE_FRAC * 100:.0f}%"
    for r in rows:
        ls = r.get("level")
        name = r["name"]
        if not ls:
            continue
        year = ls.get("year")
        # The comparison is inclusive, so this fires on matching the prior
        # extreme as well as exceeding it — "is at" is true in both cases,
        # where "just broke out to a fresh high" would not be.
        if year == "breach_high":
            msg = f"{name} is at a 52-week high"
        elif year == "breach_low":
            msg = f"{name} is at a 52-week low"
        elif year == "near_high":
            big_picture.append(f"{name} is in the top {band} of its 52-week range")
            continue
        elif year == "near_low":
            big_picture.append(f"{name} is in the bottom {band} of its 52-week range")
            continue
        else:
            if ls["month"] == "high":
                big_picture.append(f"{name} is at a 1-month high")
            elif ls["month"] == "low":
                big_picture.append(f"{name} is at a 1-month low")
            continue

        # A breach carries only as much weight as the move that produced it.
        z = z_score(r.get("chg_1d"), r.get("sigma"))
        big_picture.append(msg)
        if z is None:
            flag(red_flags, msg + ".", 1.5)
        else:
            unit = r.get("unit", "%")
            flag(red_flags, f"{msg} (on a {abs(r['chg_1d']):.1f}{unit} move, {z:.1f}σ).", z)
        flagged[group_key] = True


# ---------------------------------------------------------------------------
# Phase 1 — fetch & compute
# ---------------------------------------------------------------------------

data = {}
red_flags = []
notes = []
big_picture = []
last_bar = {}          # name -> newest session in that series, for staleness
gapped_series = set()  # names whose history has holes, so changes are withheld
flagged = {"rates": False, "futures": False, "commodities": False, "agro": False,
           "vix": False, "dxy": False, "credit": False, "crypto": False}

# --- Rates ---
if FRED_API_KEY:
    y2, as_of_2 = fetch_fred_series("DGS2")
    y5, as_of_5 = fetch_fred_series("DGS5")
    y10, as_of_10 = fetch_fred_series("DGS10")
    y30, as_of_30 = fetch_fred_series("DGS30")
    if y2 is not None and y10 is not None and y30 is not None:
        c2, c10, c30 = bps_changes(y2), bps_changes(y10), bps_changes(y30)
        c5 = bps_changes(y5) if y5 is not None else None
        slope_10s2s = (y10.iloc[-1] - y2.iloc[-1]) * 100
        slope_30s10s = (y30.iloc[-1] - y10.iloc[-1]) * 100
        slope_10s2s_1d = (y10.iloc[-2] - y2.iloc[-2]) * 100 if len(y10) > 1 and len(y2) > 1 else None
        trend_10s2s = None
        if slope_10s2s_1d is not None:
            trend_10s2s = "steepening" if slope_10s2s > slope_10s2s_1d else "flattening"
        data["rates"] = dict(y2=y2, y5=y5, y10=y10, y30=y30, c2=c2, c5=c5, c10=c10, c30=c30,
                              slope_10s2s=slope_10s2s, slope_30s10s=slope_30s10s,
                              trend_10s2s=trend_10s2s, as_of=as_of_10, as_of_2=as_of_2,
                              as_of_5=as_of_5, as_of_30=as_of_30)
        if slope_10s2s < 0:
            flag(red_flags, f"2s10s curve is inverted ({slope_10s2s:.0f} bps).", 3.5)
            flagged["rates"] = True
        if trend_10s2s:
            notes.append(f"the 2s10s curve is {trend_10s2s} ({slope_10s2s:.0f} bps)")

        # Yields carry their bps move and bps volatility so a 52-week level
        # gets ranked on the same footing as everything else.
        rate_rows = [{"name": "2Y", "level": level_status(y2),
                      "chg_1d": c2["chg_1d"], "sigma": c2["sigma"], "unit": " bps"},
                     {"name": "5Y", "level": level_status(y5) if y5 is not None else None,
                      "chg_1d": c5["chg_1d"] if c5 else None,
                      "sigma": c5["sigma"] if c5 else None, "unit": " bps"},
                     {"name": "10Y", "level": level_status(y10),
                      "chg_1d": c10["chg_1d"], "sigma": c10["sigma"], "unit": " bps"},
                     {"name": "30Y", "level": level_status(y30),
                      "chg_1d": c30["chg_1d"], "sigma": c30["sigma"], "unit": " bps"}]
        collect_level_notes(rate_rows, "rates", flagged, red_flags, big_picture)

# Live yields. These lead the rates card; FRED is the settled reference.
live_yield = {}
for tenor, tk in LIVE_YIELD_TICKERS.items():
    val, ts = fetch_intraday_quote(tk)
    live_yield[tenor] = {"value": val, "ts": ts}

# The 2Y has no live index, so carry the settle forward on the 5Y's move.
fvx_daily, _ = fetch_yf_history(LIVE_YIELD_TICKERS["5Y"])
live_yield["2Y"] = {
    "value": estimate_live_2y(data.get("rates", {}).get("y2"), fvx_daily,
                              live_yield["5Y"]["value"]),
    "ts": live_yield["5Y"]["ts"],
    "estimated": True,
}

# --- Index futures ---
fut_hist = {}
fut_rows = []
rolls_removed = 0
rolled_today = set()
for t, n in FUTURES.items():
    h, label, used_ticker = fetch_future_with_fallback(t, n)
    if h is not None:
        # Only the futures contracts carry roll gaps; if we already fell back
        # to the cash index there is nothing to adjust.
        if used_ticker.endswith("=F"):
            cash_tk = FUTURES_FALLBACK.get(t, (None, None))[0]
            cash_h, _ = fetch_yf_history(cash_tk) if cash_tk else (None, None)
            h, n_rolls = backadjust_rolls(h, cash_h)
            rolls_removed += n_rolls
        d = pct_changes(h, **{k: v for k, v in STANDARD_CFG.items() if k in ("week_n", "month_n")})
        d["level"] = level_status(h, STANDARD_CFG["month_window"], STANDARD_CFG["year_window"])
        fut_hist[label] = h.tail(22)
        last_bar[label] = h.index[-1].normalize()
        if d.get("gapped"): gapped_series.add(label)
        fut_rows.append({"name": label, **d})
if fut_rows:
    data["futures"] = fut_rows
    # Compare only contracts that actually reported. `r["chg_1d"] or -999`
    # treated a genuinely flat 0.00% as missing and mis-picked the leader.
    scored = [r for r in fut_rows if r["chg_1d"] is not None]
    if scored:
        dispersion = max(r["chg_1d"] for r in scored) - min(r["chg_1d"] for r in scored)
        leader = max(scored, key=lambda r: r["chg_1d"])
        laggard = min(scored, key=lambda r: r["chg_1d"])
        partial = len(scored) < len(fut_rows)
        coverage = f" across {len(scored)} of {len(fut_rows)} contracts" if partial else ""
        data["futures_dispersion"] = dispersion
        data["futures_partial"] = partial
        data["futures_coverage"] = coverage
        data["futures_leader"], data["futures_laggard"] = leader["name"], laggard["name"]
        notes.append(f"equity futures show {'broad, aligned' if dispersion < 0.5 else 'narrow, divergent'} "
                     f"participation ({leader['name']} leading, {laggard['name']} lagging)")
        if dispersion >= 1.0:
            flag(red_flags,
                 f"Wide dispersion across index futures "
                 f"({dispersion:.1f} percentage points{coverage}).",
                 dispersion / 0.5)
            flagged["futures"] = True
    collect_level_notes(fut_rows, "futures", flagged, red_flags, big_picture)

# --- Oil & Metals ---
com_hist = {}
com_rows = []
for t, n in COMMODITIES.items():
    h, _ = fetch_yf_history(t)
    if h is not None:
        proxy, _ = fetch_yf_history(COMMODITY_PROXY[t]) if t in COMMODITY_PROXY else (None, None)
        h, n_rolls, live_roll = backadjust_proxy_rolls(h, proxy, t)
        rolls_removed += n_rolls
        if live_roll is not None:
            rolled_today.add(n)
        d = pct_changes(h)
        d["level"] = level_status(h)
        com_hist[n] = h.tail(22)
        last_bar[n] = h.index[-1].normalize()
        if d.get("gapped"): gapped_series.add(n)
        com_rows.append({"name": n, **d})
        if is_unusual(d["chg_1d"], d["sigma"]):
            flag(red_flags, move_phrase(n, d["chg_1d"], d["sigma"],
                                        session_word(h.index[-1].normalize())),
                 z_score(d["chg_1d"], d["sigma"]) or 2.0)
            flagged["commodities"] = True
if com_rows:
    data["commodities"] = com_rows
    collect_level_notes(com_rows, "commodities", flagged, red_flags, big_picture)

# --- Agro ---
agro_hist = {}
agro_rows = []
for t, n in AGRO.items():
    h, _ = fetch_yf_history(t)
    if h is not None:
        proxy, _ = fetch_yf_history(COMMODITY_PROXY[t]) if t in COMMODITY_PROXY else (None, None)
        h, n_rolls, live_roll = backadjust_proxy_rolls(h, proxy, t)
        rolls_removed += n_rolls
        if live_roll is not None:
            rolled_today.add(n)
        d = pct_changes(h)
        d["level"] = level_status(h)
        agro_hist[n] = h.tail(22)
        last_bar[n] = h.index[-1].normalize()
        if d.get("gapped"): gapped_series.add(n)
        agro_rows.append({"name": n, **d})
        if is_unusual(d["chg_1d"], d["sigma"]):
            flag(red_flags, move_phrase(n, d["chg_1d"], d["sigma"],
                                        session_word(h.index[-1].normalize())),
                 z_score(d["chg_1d"], d["sigma"]) or 2.0)
            flagged["agro"] = True
if agro_rows:
    data["agro"] = agro_rows
    collect_level_notes(agro_rows, "agro", flagged, red_flags, big_picture)

# --- VIX term structure ---
vix_hist = {}
vix_rows = []
vix_levels = {}
for t in VIX_TERM:
    h, _ = fetch_yf_history(t)
    if h is not None:
        d = pct_changes(h, cap=SANITY_CAP_1D["vix"])
        d["level"] = level_status(h)
        label = t.replace("^", "")
        vix_hist[label] = h.tail(22)
        last_bar[label] = h.index[-1].normalize()
        if d.get("gapped"): gapped_series.add(label)
        vix_rows.append({"name": label, **d})
        vix_levels[t] = d["last"]
if vix_rows:
    ordered = all(vix_levels.get(a, 0) <= vix_levels.get(b, 0) for a, b in
                  zip(VIX_TERM, VIX_TERM[1:]) if a in vix_levels and b in vix_levels)
    data["vix"] = dict(rows=vix_rows, ordered=ordered)
    if not ordered:
        flag(red_flags, "VIX term structure is inverted/backwardated.", 3.5)
        flagged["vix"] = True
    else:
        notes.append("the VIX term structure is in normal contango (calm)")
    collect_level_notes(vix_rows, "vix", flagged, red_flags, big_picture)

# --- Dollar Index ---
dxy_hist, _ = fetch_yf_history(DXY_TICKER)
if dxy_hist is not None:
    d = pct_changes(dxy_hist)
    d["level"] = level_status(dxy_hist)
    last_bar["DXY"] = dxy_hist.index[-1].normalize()
    if d.get("gapped"): gapped_series.add("DXY")
    data["dxy"] = d
    if d["chg_1d"] is not None and abs(d["chg_1d"]) >= 0.5:
        flag(red_flags, f"Dollar Index moved {d['chg_1d']:+.2f}% "
                        f"{session_word(dxy_hist.index[-1].normalize())}.",
             z_score(d["chg_1d"], d["sigma"]) or 2.0)
        flagged["dxy"] = True
    if is_flat(d["chg_1d"], d["sigma"]):
        notes.append("the dollar is little changed on the day")
    else:
        notes.append(f"the dollar is {'up' if d['chg_1d'] >= 0 else 'down'} "
                     f"{abs(d['chg_1d']):.2f}% on the day")
    collect_level_notes([{"name": "DXY", "level": d["level"], "chg_1d": d["chg_1d"],
                          "sigma": d["sigma"]}], "dxy", flagged, red_flags, big_picture)

# --- Credit stress ---
# Raw closes, not dividend-adjusted. HYG yields ~6.1% and LQD ~4.7%, so
# auto-adjusted history depresses HYG's past by ~1.4pp more than LQD's, which
# drifts the ratio upward for reasons that are carry, not credit stress. On
# adjusted data the 1-month change read +0.11% where the actual price ratio was
# -0.02%; the 52-week range position moved 0.914 -> 0.950. The 1-day change is
# unaffected either way, and matches an independent quote source exactly.
hyg_hist, _ = fetch_yf_history("HYG", adjusted=False)
lqd_hist, _ = fetch_yf_history("LQD", adjusted=False)
if hyg_hist is not None and lqd_hist is not None:
    joined = pd.concat([hyg_hist, lqd_hist], axis=1, join="inner")
    joined.columns = ["HYG", "LQD"]
    ratio = joined["HYG"] / joined["LQD"]
    d = pct_changes(ratio)
    d["level"] = level_status(ratio)
    last_bar["HYG/LQD"] = ratio.index[-1].normalize()
    if d.get("gapped"): gapped_series.add("HYG/LQD")
    data["credit"] = dict(ratio=ratio, **d)
    if d["chg_1d"] is not None and d["chg_1d"] <= -0.5:
        flag(red_flags, f"Credit stress proxy (HYG/LQD) fell {d['chg_1d']:.2f}% "
                        f"{session_word(ratio.index[-1].normalize())}.",
             z_score(d["chg_1d"], d["sigma"]) or 2.0)
        flagged["credit"] = True
    if is_flat(d["chg_1d"], d["sigma"]):
        notes.append("credit conditions are steady")
    else:
        notes.append(f"credit conditions are {'widening' if d['chg_1d'] < 0 else 'easing'}")
    collect_level_notes([{"name": "HYG/LQD ratio", "level": d["level"], "chg_1d": d["chg_1d"],
                          "sigma": d["sigma"]}], "credit", flagged, red_flags, big_picture)

# --- Crypto ---
crypto_hist = {}
crypto_rows = []
for t, n in CRYPTO.items():
    h, _ = fetch_yf_history(t)
    if h is not None:
        d = pct_changes(h, week_n=CRYPTO_CFG["week_n"], month_n=CRYPTO_CFG["month_n"])
        d["level"] = level_status(h, CRYPTO_CFG["month_window"], CRYPTO_CFG["year_window"])
        crypto_hist[n] = h.tail(30)
        last_bar[n] = h.index[-1].normalize()
        if d.get("gapped"): gapped_series.add(n)
        crypto_rows.append({"name": n, **d})
        if is_unusual(d["chg_1d"], d["sigma"]):
            flag(red_flags, move_phrase(n, d["chg_1d"], d["sigma"],
                                        session_word(h.index[-1].normalize())),
                 z_score(d["chg_1d"], d["sigma"]) or 2.0)
            flagged["crypto"] = True
if crypto_rows:
    data["crypto"] = crypto_rows
    collect_level_notes(crypto_rows, "crypto", flagged, red_flags, big_picture)

# --- Calendar ---
events, fetched_at = fetch_ff_calendar()
data["calendar"] = events
high_impact_soon = [e for e in events if str(e.get("impact", "")).lower() in ("high", "3")]
if high_impact_soon:
    titles = ", ".join(sorted({e.get("title", "") for e in high_impact_soon}))
    notes.append(f"high-impact releases on deck this week ({titles})")

# --- Data health ---
# Two distinct failures. Staleness is a series frozen at an old bar. Gaps are
# holes in the middle of an otherwise current series: every tenor of the VIX
# term structure except ^VIX itself lost 54 days of history in 2026 while
# still reporting a current level, so their "1-day" change was really a 54-day
# change. The last-bar check cannot see that, because the last bar is fine.
stale_names = staleness(last_bar, always_on=set(CRYPTO.values()))
data["stale"] = stale_names
if stale_names:
    flag(red_flags,
         f"Stale data — {', '.join(stale_names)} "
         f"{'is' if len(stale_names) == 1 else 'are'} behind the rest of the board; "
         f"the 1-day figures shown for them are not today's.", 3.0)
    notes.append(f"note that {', '.join(stale_names)} did not report a fresh bar")

# What session the board as a whole is reporting. Crypto trades around the
# clock so it always has a current bar; excluding it keeps this honest about
# whether the session instruments have printed yet.
SESSION_DATE = max((v for k, v in last_bar.items() if k not in set(CRYPTO.values())),
                   default=None)
SESSION_WORD = session_word(SESSION_DATE)
PRE_SESSION = SESSION_DATE is not None and SESSION_DATE < market_today()
data["session_date"], data["pre_session"] = SESSION_DATE, PRE_SESSION

# Deliberately not a red flag or a narrative note. A hole in someone else's
# history is a standing condition, not news — it would fire identically every
# day for as long as the gap exists, which is the alert fatigue the ranked
# flags exist to avoid. The affected card says which series are withheld, at
# the point where the missing figures actually are.
data["gapped"] = sorted(gapped_series)


# ---------------------------------------------------------------------------
# Market read
# ---------------------------------------------------------------------------

def build_narrative():
    base = "Taking stock of the tape right now: " + ("; ".join(notes) + "." if notes else "data is limited.")
    if big_picture:
        base += " On the bigger picture: " + "; ".join(sorted(set(big_picture))) + "."

    if not red_flags:
        base += (" Nothing here is flashing outside of normal ranges — context is clean, no single factor "
                 "demands a defensive posture right now.")
        return base

    parts = [base, "", "A few things stand out enough to break down in more detail:"]

    if flagged["rates"]:
        r = data["rates"]
        parts.append(
            f"The 2s10s Treasury spread is at {r['slope_10s2s']:.0f} bps. "
            "Curve moves — especially inversions — have historically preceded economic slowdowns or Fed "
            "policy pivots by several quarters, and a 52-week extreme on any tenor is worth tracking for "
            "follow-through rather than reacting to one print."
        )
    if flagged["futures"]:
        parts.append(
            f"Index futures show a same-day dispersion of {data.get('futures_dispersion', 0):.2f} percentage points between "
            f"{data.get('futures_leader', 'the leader')} and {data.get('futures_laggard', 'the laggard')} — "
            f"the move is concentrated in a specific market segment rather than broad-based."
        )
    if flagged["commodities"]:
        moves = [f"{r['name']} {r['chg_1d']:+.1f}%" for r in data.get("commodities", []) if is_unusual(r["chg_1d"])]
        if moves:
            parts.append(
                f"One or more commodities crossed the {UNUSUAL_MOVE_PCT:.0f}% single-day threshold ({', '.join(moves)}), "
                "worth tracing back to a specific catalyst rather than dismissing as noise."
            )
        else:
            parts.append("A commodity in this group is sitting at a 52-week extreme — worth a closer look at the driver.")
    if flagged["agro"]:
        moves = [f"{r['name']} {r['chg_1d']:+.1f}%" for r in data.get("agro", []) if is_unusual(r["chg_1d"])]
        if moves:
            parts.append(
                f"Grains moved more than usual {SESSION_WORD} ({', '.join(moves)}) — large single-day moves here can "
                "bleed into food inflation and related equity sectors."
            )
        else:
            parts.append("A grain contract is sitting at a 52-week extreme — worth a closer look at the driver.")
    if flagged["vix"]:
        parts.append(
            "The VIX term structure has flipped into backwardation — near-term implied volatility is pricing "
            "higher fear than longer-dated tenors, the classic signature of acute, immediate risk perception."
        )
    if flagged["dxy"]:
        d = data["dxy"]
        if d["chg_1d"] is not None and abs(d["chg_1d"]) >= 0.5:
            parts.append(
                f"The Dollar Index moved {d['chg_1d']:+.2f}% in a single session — worth cross-referencing against "
                "today's macro calendar for a rate or data-driven catalyst."
            )
        else:
            parts.append("The Dollar Index is sitting at a 52-week extreme — worth watching for follow-through.")
    if flagged["credit"]:
        d = data["credit"]
        if d["chg_1d"] is not None and d["chg_1d"] <= -0.5:
            parts.append(
                f"The HYG/LQD credit stress proxy fell {d['chg_1d']:.2f}% {SESSION_WORD} — worth monitoring for "
                "follow-through over the next few sessions rather than treating a single-day move as conclusive."
            )
        else:
            parts.append("The HYG/LQD credit ratio is sitting at a 52-week extreme — a genuine shift in relative credit risk appetite.")
    if flagged["crypto"]:
        moves = [f"{r['name']} {r['chg_1d']:+.1f}%" for r in data.get("crypto", []) if is_unusual(r["chg_1d"])]
        if moves:
            parts.append(f"Crypto moved sharply today ({', '.join(moves)}) — treat with the usual grain of salt given crypto's baseline volatility is naturally higher than the other groups here.")
        else:
            parts.append("BTC or ETH is sitting at a 52-week extreme.")
    return "<br><br>".join(parts)


# ---------------------------------------------------------------------------
# Header + summary
# ---------------------------------------------------------------------------

top_l, top_r = st.columns([5, 1])
with top_l:
    st.title("Macro Context Dashboard")
    # State the session the change columns refer to. Left implicit, a "1D"
    # read before the open silently meant the previous session.
    if SESSION_DATE is None:
        session_txt = "session data unavailable"
    elif PRE_SESSION:
        session_txt = (f"**1D = the session of {SESSION_DATE:%a %b %d}** — today's bar "
                       f"has not printed yet, so the change columns are not today's move")
    else:
        session_txt = f"1D = today's session ({SESSION_DATE:%a %b %d}), in progress"
    st.caption(f"On-demand snapshot — not a live stream · ~20 min delay tolerated on market "
               f"data · daily on yields  \n{session_txt}")
with top_r:
    st.write("")
    if st.button("🔄 Refresh", width="stretch"):
        st.cache_data.clear()
        st.rerun()

st.markdown("#### Market read")
st.markdown(f"<div class='summary-box'>{build_narrative()}</div>", unsafe_allow_html=True)

if red_flags:
    # Ranked by severity so the biggest move leads, instead of whatever group
    # happened to be computed first.
    ranked = sorted(red_flags, key=lambda f: f["severity"], reverse=True)
    tier_color = {"high": NEG, "moderate": ACCENT2, "low": MUTED}
    flag_html = f"<div class='summary-box' style='border-left-color:{NEG}'><b>🚩 Red flags</b><br><br>"
    flag_html += "<br>".join(
        f"<span class='flag-red' style='color:{tier_color[severity_tier(f['severity'])]}'>"
        f"• {f['text']}</span>" for f in ranked)
    flag_html += (f"<div class='small-caption' style='margin-top:10px;'>Ranked by size relative to each "
                  f"series' own daily volatility · red ≥3σ, amber ≥1.5σ, grey below</div>")
    flag_html += "</div>"
    st.markdown(flag_html, unsafe_allow_html=True)
else:
    st.markdown("<span class='flag-ok'>✓ No red flags triggered across any group at current thresholds.</span>",
                unsafe_allow_html=True)

st.write("")

# ---------------------------------------------------------------------------
# Phase 2 — two-column layout
# ---------------------------------------------------------------------------

left, right = st.columns(2, gap="large")

with left:
    with card("Index Futures", flagged["futures"]):
        if "futures" in data:
            render_change_table(data["futures"], "Future", height=145, sort_by="chg_1d")
            st.plotly_chart(normalized_chart(fut_hist, PALETTE), width="stretch",
                             config={"displayModeBar": False})
            st.markdown(f"<div class='small-caption'>1D dispersion: {data.get('futures_dispersion', 0):.2f} pp "
                        f"&nbsp;·&nbsp; yfinance, ~15-20 min delay</div>", unsafe_allow_html=True)
        else:
            st.error("Could not load futures data.")

    with card("Rates & Yield Curve", flagged["rates"]):
        if "rates" in data:
            r = data["rates"]
            settled = {"2Y": (r["y2"].iloc[-1], r["as_of_2"]),
                       "5Y": (r["y5"].iloc[-1], r["as_of_5"]) if r.get("y5") is not None else (None, None),
                       "10Y": (r["y10"].iloc[-1], r["as_of"]),
                       "30Y": (r["y30"].iloc[-1], r["as_of_30"])}
            for col, tenor in zip(st.columns(4), ["2Y", "5Y", "10Y", "30Y"]):
                lv = live_yield.get(tenor, {})
                s_val, s_as_of = settled[tenor]
                with col:
                    st.markdown(render_yield_box(tenor, lv.get("value"), lv.get("ts"),
                                                 s_val, s_as_of, lv.get("estimated", False)),
                                unsafe_allow_html=True)

            # Curve from the live quotes where they exist, settled alongside.
            live_2y, live_10y = live_yield.get("2Y", {}).get("value"), live_yield.get("10Y", {}).get("value")
            live_30y = live_yield.get("30Y", {}).get("value")
            curve_bits = [f"10s2s: <b>{r['slope_10s2s']:.0f} bps</b> settled ({r['trend_10s2s'] or '—'})"]
            if live_2y is not None and live_10y is not None:
                curve_bits.append(f"~{(live_10y - live_2y) * 100:.0f} bps live (est)")
            curve_bits.append(f"30s10s: <b>{r['slope_30s10s']:.0f} bps</b> settled")
            if live_10y is not None and live_30y is not None:
                curve_bits.append(f"~{(live_30y - live_10y) * 100:.0f} bps live")
            st.markdown(f"<div class='small-caption' style='margin-top:10px;'>"
                        f"{' &nbsp;·&nbsp; '.join(curve_bits)}<br>"
                        f"Live 5Y/10Y/30Y from ^FVX/^TNX/^TYX (~15 min delay). The 2Y has no live "
                        f"index, so it is FRED's settle carried forward on the 5Y's move "
                        f"(β={TWO_YEAR_BETA_ON_5Y}, typical error ±{TWO_YEAR_EST_ERR_BPS:.1f} bps).</div>",
                        unsafe_allow_html=True)
            st.write("")
            chart_series = {"2Y": r["y2"].tail(66), "10Y": r["y10"].tail(66), "30Y": r["y30"].tail(66)}
            if r.get("y5") is not None:
                chart_series = {"2Y": r["y2"].tail(66), "5Y": r["y5"].tail(66),
                                "10Y": r["y10"].tail(66), "30Y": r["y30"].tail(66)}
            st.plotly_chart(multi_level_chart(chart_series, PALETTE, ticksuffix="%"),
                             width="stretch", config={"displayModeBar": False})
        else:
            if FRED_API_KEY:
                st.error("FRED request failed — the key may be expired or rate-limited. "
                         "Everything else on this page is unaffected.")
            else:
                st.warning("Add FRED_API_KEY in Secrets to enable this section.")

    with card("Oil & Metals", flagged["commodities"]):
        if "commodities" in data:
            render_change_table(data["commodities"], "Asset", height=175)
            st.plotly_chart(normalized_chart(com_hist, PALETTE), width="stretch",
                             config={"displayModeBar": False})
            st.markdown(f"<div class='small-caption'>Unusual move: ≥{UNUSUAL_MOVE_SIGMA:.1f}σ of this series' own daily vol, or ≥±{UNUSUAL_MOVE_PCT:.0f}% outright</div>",
                        unsafe_allow_html=True)
            rolled = [r['name'] for r in data['commodities'] if r['name'] in rolled_today]
            if rolled:
                st.markdown(f"<div class='small-caption'>{', '.join(rolled)} rolled contract today; the change shown is the held contract's own move, not the calendar spread.</div>",
                            unsafe_allow_html=True)
        else:
            st.error("Could not load commodity data.")

    with card("Agro (Wheat / Corn / Soybeans)", flagged["agro"]):
        if "agro" in data:
            render_change_table(data["agro"], "Asset", height=145)
            st.plotly_chart(normalized_chart(agro_hist, PALETTE), width="stretch",
                             config={"displayModeBar": False})
            st.markdown(f"<div class='small-caption'>Unusual move: ≥{UNUSUAL_MOVE_SIGMA:.1f}σ of this series' own daily vol, or ≥±{UNUSUAL_MOVE_PCT:.0f}% outright "
                        f"&nbsp;·&nbsp; CBOT futures, yfinance</div>", unsafe_allow_html=True)
            rolled = [r['name'] for r in data['agro'] if r['name'] in rolled_today]
            if rolled:
                st.markdown(f"<div class='small-caption'>{', '.join(rolled)} rolled contract today; the change shown is the held contract's own move, not the calendar spread.</div>",
                            unsafe_allow_html=True)
        else:
            st.error("Could not load agro data.")

with right:
    with card("VIX Term Structure", flagged["vix"]):
        if "vix" in data:
            df = pd.DataFrame(data["vix"]["rows"])
            render_change_table(data["vix"]["rows"], "Index", height=175)
            if df.get("suspect_1d", pd.Series(dtype=bool)).any():
                st.markdown("<div class='small-caption'>One or more 1D changes exceeded the sanity threshold "
                            f"(±{SANITY_CAP_1D['vix']:.0f}%) and were suppressed as likely data glitches.</div>",
                            unsafe_allow_html=True)
            # Only plot tenors with continuous history. A normalised line drawn
            # across a hole in the feed is a straight jump, not an evolution.
            plot_series = {k: v for k, v in vix_hist.items() if k not in gapped_series}
            if plot_series:
                st.plotly_chart(normalized_chart(plot_series, PALETTE), width="stretch",
                                 config={"displayModeBar": False})
                if len(plot_series) < len(vix_hist):
                    st.markdown(f"<div class='small-caption'>Chart: "
                                f"{', '.join(plot_series)} only.</div>", unsafe_allow_html=True)
            shape_txt = "Contango (calm)" if data["vix"]["ordered"] else "⚠️ Inverted / backwardated (risk-off)"
            st.markdown(f"<div class='small-caption'>Shape: {shape_txt}</div>", unsafe_allow_html=True)
        else:
            st.error("Could not load VIX data.")

    with card("Credit Stress (HYG / LQD)", flagged["credit"]):
        if "credit" in data:
            d = data["credit"]
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Ratio", f"{d['last']:.3f}")
            for col, lab, key in ((c2, "1 day", "chg_1d"), (c3, "1 week", "chg_1w"),
                                  (c4, "1 month", "chg_1m")):
                col.markdown(render_change_box(lab, d[key]), unsafe_allow_html=True)
            st.write("")
            st.plotly_chart(level_chart(d["ratio"].tail(22), color=ACCENT3),
                             width="stretch", config={"displayModeBar": False})
            if is_flat(d["chg_1d"], d["sigma"]):
                note = "Ratio flat → high-yield and IG moving together → no signal."
            elif d["chg_1d"] < 0:
                note = "Ratio falling → high-yield underperforming IG → credit stress widening."
            else:
                note = "Ratio rising → high-yield outperforming IG → credit conditions easing."
            st.markdown(f"<div class='small-caption'>{note}</div>", unsafe_allow_html=True)
        else:
            st.error("Could not load credit data.")

    with card("Dollar Index (DXY)", flagged["dxy"]):
        if "dxy" in data:
            d = data["dxy"]
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("DXY", f"{d['last']:.2f}")
            for col, lab, key in ((c2, "1 day", "chg_1d"), (c3, "1 week", "chg_1w"),
                                  (c4, "1 month", "chg_1m")):
                col.markdown(render_change_box(lab, d[key]), unsafe_allow_html=True)
            st.write("")
            st.plotly_chart(level_chart(dxy_hist.tail(22), color=ACCENT2),
                             width="stretch", config={"displayModeBar": False})
        else:
            st.error("Could not load DXY.")

    with card("Crypto (BTC / ETH)", flagged["crypto"]):
        if "crypto" in data:
            render_change_table(data["crypto"], "Asset", height=110)
            st.plotly_chart(normalized_chart(crypto_hist, PALETTE), width="stretch",
                             config={"displayModeBar": False})
            st.markdown(f"<div class='small-caption'>Unusual move: ≥{UNUSUAL_MOVE_SIGMA:.1f}σ of this series' own daily vol, or ≥±{UNUSUAL_MOVE_PCT:.0f}% outright "
                        f"&nbsp;·&nbsp; 1W/1M use 7-/30-day lookbacks (24/7 trading)</div>", unsafe_allow_html=True)
        else:
            st.error("Could not load crypto data.")

# ---------------------------------------------------------------------------
# Full-width Macro Calendar
# ---------------------------------------------------------------------------

st.write("")
with card("Macro Calendar — CPI / NFP / FOMC / PCE (USD, this week)"):
    if data.get("calendar"):
        rows = []
        for e in data["calendar"]:
            rows.append({
                "When": format_event_datetime(e.get("date", "")),
                "Event": e.get("title", ""),
                "Impact": str(e.get("impact", "")),
            })
        cal_df = pd.DataFrame(rows)
        st.dataframe(cal_df, hide_index=True, width="stretch", height=190)
    else:
        st.info("No matching USD events this week, or the calendar feed is unavailable.")
    fetched_note = f"Fetched {fetched_at}" if fetched_at else "Feed unreachable — last fetch failed"
    st.markdown(f"<div class='small-caption'>{fetched_note} · ForexFactory public calendar feed, cached ~6h</div>",
                unsafe_allow_html=True)
