"""
Pre-Market / On-Demand Macro Context Dashboard
------------------------------------------------
Modules:
1. Rates: 2Y/10Y/30Y Treasury yields + curve slope        (source: FRED)
2. Index futures: ES / NQ / RTY leadership & dispersion    (source: yfinance)
3. Oil (WTI)                                                (source: yfinance)
4. Metals: Gold / Silver / Copper                           (source: yfinance)
5. VIX term structure: VIX9D / VIX / VIX3M / VIX6M          (source: yfinance)
6. Dollar Index (DXY)                                       (source: yfinance)
7. Credit stress proxy: HYG / LQD ratio                     (source: yfinance)
8. Macro calendar: CPI / NFP / FOMC / PCE (USD, this week)   (source: ForexFactory)

No brokerage account or streaming connection required. All data is either
free/keyless (yfinance, ForexFactory) or free with a self-serve key (FRED).
Refresh is on-demand (button), not live-streaming. Typical delay tolerated: ~20 min.
"""

import datetime as dt

import pandas as pd
import requests
import streamlit as st
import yfinance as yf

st.set_page_config(page_title="Macro Context Dashboard", layout="wide")

UNUSUAL_MOVE_PCT = 3.0  # threshold requested: flag moves of 3%+ on commodities

FRED_API_KEY = st.secrets.get("FRED_API_KEY", "")

FUTURES = {"ES=F": "S&P 500 (ES)", "NQ=F": "Nasdaq 100 (NQ)", "RTY=F": "Russell 2000 (RTY)"}
COMMODITIES = {"CL=F": "Crude Oil (WTI)", "GC=F": "Gold", "SI=F": "Silver", "HG=F": "Copper"}
VIX_TERM = ["^VIX9D", "^VIX", "^VIX3M", "^VIX6M"]
DXY_TICKER = "DX-Y.NYB"
CREDIT_TICKERS = {"HYG": "High Yield (HYG)", "LQD": "Investment Grade (LQD)"}

FF_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
CALENDAR_KEYWORDS = ["CPI", "PCE", "Non-Farm", "NFP", "Nonfarm", "FOMC",
                     "Fed Funds", "Interest Rate", "Unemployment Claims", "Employment"]


# ---------------------------------------------------------------------------
# Data fetchers (cached to respect free-tier limits and the ~20 min tolerance)
# ---------------------------------------------------------------------------

@st.cache_data(ttl=6 * 3600, show_spinner=False)
def fetch_fred_series(series_id: str, limit: int = 90):
    """Daily official Treasury par yield series from FRED."""
    if not FRED_API_KEY:
        return None, None
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {
        "series_id": series_id,
        "api_key": FRED_API_KEY,
        "file_type": "json",
        "sort_order": "desc",
        "limit": limit,
    }
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    obs = r.json().get("observations", [])
    rows = [(o["date"], o["value"]) for o in obs if o["value"] != "."]
    rows.reverse()
    df = pd.DataFrame(rows, columns=["date", "value"]).astype({"value": float})
    df["date"] = pd.to_datetime(df["date"])
    as_of = df["date"].iloc[-1].strftime("%Y-%m-%d") if not df.empty else None
    return df.set_index("date")["value"], as_of


@st.cache_data(ttl=1200, show_spinner=False)  # 20 min, matches tolerated delay
def fetch_yf_history(ticker: str, period: str = "3mo"):
    """Daily close history for a single ticker via yfinance."""
    try:
        hist = yf.Ticker(ticker).history(period=period, interval="1d")
        if hist.empty:
            return None, None
        closes = hist["Close"].dropna()
        as_of = closes.index[-1].strftime("%Y-%m-%d %H:%M")
        return closes, as_of
    except Exception:
        return None, None


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def fetch_ff_calendar():
    """This week's USD macro events (CPI/NFP/FOMC/PCE) from ForexFactory's free feed."""
    try:
        r = requests.get(FF_CALENDAR_URL, timeout=15,
                          headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        events = r.json()
    except Exception:
        return [], None

    filtered = []
    for e in events:
        title = e.get("title", "")
        country = e.get("country", "")
        if country == "USD" and any(k.lower() in title.lower() for k in CALENDAR_KEYWORDS):
            filtered.append(e)
    fetched_at = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    return filtered, fetched_at


# ---------------------------------------------------------------------------
# Calculations
# ---------------------------------------------------------------------------

def pct_changes(series: pd.Series):
    """Return last value + %-change over 1D/1W(5 obs)/1M(21 obs)."""
    s = series.dropna()
    if s.empty:
        return {"last": None, "chg_1d": None, "chg_1w": None, "chg_1m": None}
    last = s.iloc[-1]

    def chg(n):
        if len(s) > n:
            past = s.iloc[-1 - n]
            if past != 0:
                return (last / past - 1) * 100
        return None

    return {"last": last, "chg_1d": chg(1), "chg_1w": chg(5), "chg_1m": chg(21)}


def bps_changes(series: pd.Series):
    """Return last value + bps-change over 1D/1W/1M for a yield series."""
    s = series.dropna()
    if s.empty:
        return {"last": None, "chg_1d": None, "chg_1w": None, "chg_1m": None}
    last = s.iloc[-1]

    def chg(n):
        if len(s) > n:
            return (last - s.iloc[-1 - n]) * 100  # percentage points -> bps
        return None

    return {"last": last, "chg_1d": chg(1), "chg_1w": chg(5), "chg_1m": chg(21)}


def fmt_pct(x):
    return "—" if x is None else f"{x:+.2f}%"


def fmt_bps(x):
    return "—" if x is None else f"{x:+.0f} bps"


def is_unusual(chg_1d_pct):
    return chg_1d_pct is not None and abs(chg_1d_pct) >= UNUSUAL_MOVE_PCT


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

st.title("Macro Context Dashboard")
st.caption("On-demand snapshot — not a live stream. Data tolerances: ~20 min for market data, "
           "daily for Treasury yields, weekly refresh for the calendar feed.")

if st.button("🔄 Refresh now"):
    st.cache_data.clear()
    st.rerun()

st.divider()

# ---- 1. Rates & curve --------------------------------------------------
st.header("1. Rates & Yield Curve")
if not FRED_API_KEY:
    st.warning("No FRED_API_KEY found in secrets — add it in Streamlit Cloud "
               "under App settings → Secrets to enable this section.")
else:
    y2, as_of_2 = fetch_fred_series("DGS2")
    y10, as_of_10 = fetch_fred_series("DGS10")
    y30, as_of_30 = fetch_fred_series("DGS30")

    if y2 is not None and y10 is not None and y30 is not None:
        c2, c10, c30 = bps_changes(y2), bps_changes(y10), bps_changes(y30)
        cols = st.columns(3)
        for col, label, data in zip(cols, ["2Y", "10Y", "30Y"], [c2, c10, c30]):
            with col:
                st.metric(label, f"{data['last']:.2f}%", fmt_bps(data["chg_1d"]) + " (1D)")
                st.caption(f"1W: {fmt_bps(data['chg_1w'])} | 1M: {fmt_bps(data['chg_1m'])}")

        slope_10s2s = (y10.iloc[-1] - y2.iloc[-1]) * 100
        slope_30s10s = (y30.iloc[-1] - y10.iloc[-1]) * 100
        slope_10s2s_1d = (y10.iloc[-2] - y2.iloc[-2]) * 100 if len(y10) > 1 and len(y2) > 1 else None
        slope_30s10s_1d = (y30.iloc[-2] - y10.iloc[-2]) * 100 if len(y30) > 1 and len(y10) > 1 else None

        c1, c2_ = st.columns(2)
        with c1:
            trend = ""
            if slope_10s2s_1d is not None:
                trend = "steepening ↗" if slope_10s2s > slope_10s2s_1d else "flattening ↘"
            st.metric("10s2s Spread", f"{slope_10s2s:.0f} bps", trend)
        with c2_:
            trend = ""
            if slope_30s10s_1d is not None:
                trend = "steepening ↗" if slope_30s10s > slope_30s10s_1d else "flattening ↘"
            st.metric("30s10s Spread", f"{slope_30s10s:.0f} bps", trend)

        st.line_chart(pd.DataFrame({"2Y": y2, "10Y": y10, "30Y": y30}).tail(66))
        st.caption(f"As of: {as_of_10} (FRED, official daily par yields)")
    else:
        st.error("Could not load Treasury yield data — check your FRED_API_KEY.")

st.divider()

# ---- 2. Index futures ---------------------------------------------------
st.header("2. Index Futures — Leadership & Dispersion")
rows = []
sparklines = {}
for ticker, name in FUTURES.items():
    hist, as_of = fetch_yf_history(ticker)
    if hist is not None:
        d = pct_changes(hist)
        rows.append({"Future": name, "Last": round(d["last"], 2),
                     "1D %": d["chg_1d"], "1W %": d["chg_1w"], "1M %": d["chg_1m"]})
        sparklines[name] = hist.tail(22)

if rows:
    df = pd.DataFrame(rows).sort_values("1D %", ascending=False)
    disp_df = df.copy()
    for c in ["1D %", "1W %", "1M %"]:
        disp_df[c] = disp_df[c].apply(fmt_pct)
    st.dataframe(disp_df, use_container_width=True, hide_index=True)

    valid_1d = [r["1D %"] for r in rows if r["1D %"] is not None]
    if valid_1d:
        dispersion = max(valid_1d) - min(valid_1d)
        st.caption(f"Dispersion (max - min, 1D): **{dispersion:.2f} pts** — "
                   f"{'narrow, concentrated move' if dispersion < 0.5 else 'broad divergence between indices'}")
    st.line_chart(pd.DataFrame(sparklines))
    _, as_of = fetch_yf_history("ES=F")
    st.caption(f"As of: {as_of} (yfinance, ~15-20 min delay typical)")
else:
    st.error("Could not load index futures data.")

st.divider()

# ---- 3 & 4. Oil, Gold, Silver, Copper -----------------------------------
st.header("3-4. Oil & Metals")
rows = []
sparklines = {}
for ticker, name in COMMODITIES.items():
    hist, as_of = fetch_yf_history(ticker)
    if hist is not None:
        d = pct_changes(hist)
        flag = "⚠️ Unusual" if is_unusual(d["chg_1d"]) else ""
        rows.append({"Asset": name, "Last": round(d["last"], 2),
                     "1D %": d["chg_1d"], "1W %": d["chg_1w"], "1M %": d["chg_1m"], "Flag": flag})
        sparklines[name] = hist.tail(22)

if rows:
    df = pd.DataFrame(rows)
    disp_df = df.copy()
    for c in ["1D %", "1W %", "1M %"]:
        disp_df[c] = disp_df[c].apply(fmt_pct)
    st.dataframe(disp_df, use_container_width=True, hide_index=True)
    st.caption(f"Unusual-move threshold: ±{UNUSUAL_MOVE_PCT:.0f}% (1-day).")
    st.line_chart(pd.DataFrame(sparklines))
    _, as_of = fetch_yf_history("CL=F")
    st.caption(f"As of: {as_of} (yfinance, ~15-20 min delay typical)")
else:
    st.error("Could not load commodity data.")

st.divider()

# ---- 5. VIX term structure ----------------------------------------------
st.header("5. VIX Term Structure")
rows = []
sparklines = {}
levels = {}
for ticker in VIX_TERM:
    hist, as_of = fetch_yf_history(ticker)
    if hist is not None:
        d = pct_changes(hist)
        rows.append({"Index": ticker.replace("^", ""), "Last": round(d["last"], 2),
                     "1D %": d["chg_1d"], "1W %": d["chg_1w"], "1M %": d["chg_1m"]})
        sparklines[ticker.replace("^", "")] = hist.tail(22)
        levels[ticker] = d["last"]

if rows:
    df = pd.DataFrame(rows)
    disp_df = df.copy()
    for c in ["1D %", "1W %", "1M %"]:
        disp_df[c] = disp_df[c].apply(fmt_pct)
    st.dataframe(disp_df, use_container_width=True, hide_index=True)

    ordered = all(levels.get(a, 0) <= levels.get(b, 0) for a, b in
                  zip(VIX_TERM, VIX_TERM[1:]) if a in levels and b in levels)
    if ordered:
        st.success("Shape: normal upward slope (VIX9D ≤ VIX ≤ VIX3M ≤ VIX6M) — calm/complacent term structure.")
    else:
        st.error("Shape: inverted/backwardated — front-end fear rising faster than back-end. Watch closely.")

    st.line_chart(pd.DataFrame(sparklines))
    _, as_of = fetch_yf_history("^VIX")
    st.caption(f"As of: {as_of} (yfinance, ~15-20 min delay typical)")
else:
    st.error("Could not load VIX term structure data.")

st.divider()

# ---- 6. Dollar Index ------------------------------------------------------
st.header("6. Dollar Index (DXY)")
hist, as_of = fetch_yf_history(DXY_TICKER)
if hist is not None:
    d = pct_changes(hist)
    c1, c2_, c3 = st.columns(3)
    c1.metric("DXY", f"{d['last']:.2f}", fmt_pct(d["chg_1d"]) + " (1D)")
    c2_.metric("1W", fmt_pct(d["chg_1w"]))
    c3.metric("1M", fmt_pct(d["chg_1m"]))
    st.line_chart(hist.tail(22))
    st.caption(f"As of: {as_of} (yfinance, ~15-20 min delay typical)")
else:
    st.error("Could not load DXY data.")

st.divider()

# ---- 7. Credit stress proxy (HYG/LQD) -------------------------------------
st.header("7. Credit Stress Proxy (HYG / LQD)")
hyg_hist, as_of_hyg = fetch_yf_history("HYG")
lqd_hist, as_of_lqd = fetch_yf_history("LQD")
if hyg_hist is not None and lqd_hist is not None:
    joined = pd.concat([hyg_hist, lqd_hist], axis=1, join="inner")
    joined.columns = ["HYG", "LQD"]
    ratio = joined["HYG"] / joined["LQD"]
    d = pct_changes(ratio)
    c1, c2_, c3 = st.columns(3)
    c1.metric("HYG/LQD Ratio", f"{d['last']:.4f}", fmt_pct(d["chg_1d"]) + " (1D)")
    c2_.metric("1W", fmt_pct(d["chg_1w"]))
    c3.metric("1M", fmt_pct(d["chg_1m"]))
    if d["chg_1d"] is not None and d["chg_1d"] < 0:
        st.caption("Ratio falling → high-yield underperforming investment-grade → credit stress widening.")
    elif d["chg_1d"] is not None:
        st.caption("Ratio rising → high-yield outperforming investment-grade → credit conditions easing.")
    st.line_chart(ratio.tail(22))
    st.caption(f"As of: {as_of_hyg} (yfinance, ~15-20 min delay typical)")
else:
    st.error("Could not load HYG/LQD data.")

st.divider()

# ---- 8. Macro calendar -----------------------------------------------------
st.header("8. Macro Calendar (CPI / NFP / FOMC / PCE)")
events, fetched_at = fetch_ff_calendar()
if events:
    cal_rows = [{"Date": e.get("date", ""), "Time": e.get("time", ""),
                 "Event": e.get("title", ""), "Impact": e.get("impact", "")}
                for e in events]
    st.dataframe(pd.DataFrame(cal_rows), use_container_width=True, hide_index=True)
else:
    st.info("No matching USD macro events found for this week, or the calendar feed is unavailable.")
st.caption(f"Fetched: {fetched_at} (ForexFactory public calendar feed, cached ~6h)")
