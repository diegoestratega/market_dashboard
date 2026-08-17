"""
Pre-Market / On-Demand Macro Context Dashboard (v2 — compact dark layout)
---------------------------------------------------------------------------
Same 8 data modules as v1, restructured into a compact 3-column card grid
with normalized (% change) charts, a dark theme, and an analyst-style
summary with explicit red flags at the top of the page.
"""

import datetime as dt

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
import yfinance as yf

st.set_page_config(page_title="Macro Context Dashboard", layout="wide")

UNUSUAL_MOVE_PCT = 3.0
FRED_API_KEY = st.secrets.get("FRED_API_KEY", "")

FUTURES = {"ES=F": "S&P 500 (ES)", "NQ=F": "Nasdaq 100 (NQ)", "RTY=F": "Russell 2000 (RTY)"}
COMMODITIES = {"CL=F": "Crude Oil (WTI)", "GC=F": "Gold", "SI=F": "Silver", "HG=F": "Copper"}
VIX_TERM = ["^VIX9D", "^VIX", "^VIX3M", "^VIX6M"]
DXY_TICKER = "DX-Y.NYB"

FF_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
CALENDAR_KEYWORDS = ["CPI", "PCE", "Non-Farm", "NFP", "Nonfarm", "FOMC",
                     "Fed Funds", "Interest Rate", "Unemployment Claims", "Employment"]

CHART_HEIGHT = 190
DARK_BG = "#0e1117"
PLOT_BG = "#161a23"
GRID = "#2a2f3a"
ACCENT_COLORS = ["#4fd1c5", "#f6ad55", "#63b3ed", "#fc8181", "#b794f4", "#68d391"]

# ---------------------------------------------------------------------------
# Custom CSS — compact cards + dark polish (works with any Streamlit theme)
# ---------------------------------------------------------------------------
st.markdown("""
<style>
.block-container {padding-top: 1.2rem; padding-bottom: 2rem; max-width: 1400px;}
div[data-testid="stMetric"] {background-color: #161a23; border-radius: 8px;
    padding: 8px 10px; border: 1px solid #262b36;}
div[data-testid="stMetricLabel"] {font-size: 0.75rem; opacity: 0.75;}
div[data-testid="stMetricValue"] {font-size: 1.1rem;}
.card {background-color: #12151c; border: 1px solid #262b36; border-radius: 10px;
       padding: 14px 16px; margin-bottom: 14px;}
.card h4 {margin-top: 0; margin-bottom: 8px; font-size: 0.95rem; opacity: 0.9;}
.small-caption {font-size: 0.72rem; opacity: 0.55; margin-top: 2px;}
.flag-red {color: #fc8181; font-weight: 600;}
.flag-ok {color: #68d391;}
</style>
""", unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------

@st.cache_data(ttl=6 * 3600, show_spinner=False)
def fetch_fred_series(series_id: str, limit: int = 90):
    if not FRED_API_KEY:
        return None, None
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {"series_id": series_id, "api_key": FRED_API_KEY, "file_type": "json",
              "sort_order": "desc", "limit": limit}
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    obs = r.json().get("observations", [])
    rows = [(o["date"], o["value"]) for o in obs if o["value"] != "."]
    rows.reverse()
    df = pd.DataFrame(rows, columns=["date", "value"]).astype({"value": float})
    df["date"] = pd.to_datetime(df["date"])
    as_of = df["date"].iloc[-1].strftime("%Y-%m-%d") if not df.empty else None
    return df.set_index("date")["value"], as_of


@st.cache_data(ttl=1200, show_spinner=False)
def fetch_yf_history(ticker: str, period: str = "3mo"):
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
    try:
        r = requests.get(FF_CALENDAR_URL, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        events = r.json()
    except Exception:
        return [], None
    filtered = [e for e in events if e.get("country") == "USD"
                and any(k.lower() in e.get("title", "").lower() for k in CALENDAR_KEYWORDS)]
    return filtered, dt.datetime.now().strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------
# Calculations
# ---------------------------------------------------------------------------

def pct_changes(series: pd.Series):
    s = series.dropna()
    if s.empty:
        return {"last": None, "chg_1d": None, "chg_1w": None, "chg_1m": None}
    last = s.iloc[-1]

    def chg(n):
        if len(s) > n and s.iloc[-1 - n] != 0:
            return (last / s.iloc[-1 - n] - 1) * 100
        return None

    return {"last": last, "chg_1d": chg(1), "chg_1w": chg(5), "chg_1m": chg(21)}


def bps_changes(series: pd.Series):
    s = series.dropna()
    if s.empty:
        return {"last": None, "chg_1d": None, "chg_1w": None, "chg_1m": None}
    last = s.iloc[-1]

    def chg(n):
        return (last - s.iloc[-1 - n]) * 100 if len(s) > n else None

    return {"last": last, "chg_1d": chg(1), "chg_1w": chg(5), "chg_1m": chg(21)}


def fmt_pct(x):
    return "—" if x is None else f"{x:+.2f}%"


def fmt_bps(x):
    return "—" if x is None else f"{x:+.0f} bps"


def is_unusual(chg_1d_pct):
    return chg_1d_pct is not None and abs(chg_1d_pct) >= UNUSUAL_MOVE_PCT


def normalized_chart(series_dict: dict, height=CHART_HEIGHT, y_title="% change"):
    """Compact dark chart normalizing every series to % change from window start."""
    fig = go.Figure()
    for i, (name, s) in enumerate(series_dict.items()):
        s = s.dropna()
        if s.empty:
            continue
        norm = (s / s.iloc[0] - 1) * 100
        fig.add_trace(go.Scatter(x=norm.index, y=norm.values, mode="lines", name=name,
                                  line=dict(width=2, color=ACCENT_COLORS[i % len(ACCENT_COLORS)])))
    fig.update_layout(
        height=height, margin=dict(l=4, r=4, t=4, b=4),
        paper_bgcolor=DARK_BG, plot_bgcolor=PLOT_BG,
        font=dict(color="#d7dae0", size=11),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, font=dict(size=10)),
        xaxis=dict(showgrid=False, tickfont=dict(size=9)),
        yaxis=dict(showgrid=True, gridcolor=GRID, ticksuffix="%", tickfont=dict(size=9)),
    )
    return fig


def level_chart(series: pd.Series, height=CHART_HEIGHT, color=ACCENT_COLORS[0], ticksuffix=""):
    """Chart on the raw scale (used for yields, single-series levels)."""
    fig = go.Figure()
    s = series.dropna()
    fig.add_trace(go.Scatter(x=s.index, y=s.values, mode="lines",
                              line=dict(width=2, color=color)))
    fig.update_layout(
        height=height, margin=dict(l=4, r=4, t=4, b=4),
        paper_bgcolor=DARK_BG, plot_bgcolor=PLOT_BG,
        font=dict(color="#d7dae0", size=11),
        xaxis=dict(showgrid=False, tickfont=dict(size=9)),
        yaxis=dict(showgrid=True, gridcolor=GRID, ticksuffix=ticksuffix, tickfont=dict(size=9)),
    )
    return fig


# ---------------------------------------------------------------------------
# Phase 1 — fetch & compute everything up front (feeds both summary + cards)
# ---------------------------------------------------------------------------

data = {}
red_flags = []
notes = []

# Rates
if FRED_API_KEY:
    y2, as_of_2 = fetch_fred_series("DGS2")
    y10, as_of_10 = fetch_fred_series("DGS10")
    y30, as_of_30 = fetch_fred_series("DGS30")
    if y2 is not None and y10 is not None and y30 is not None:
        c2, c10, c30 = bps_changes(y2), bps_changes(y10), bps_changes(y30)
        slope_10s2s = (y10.iloc[-1] - y2.iloc[-1]) * 100
        slope_30s10s = (y30.iloc[-1] - y10.iloc[-1]) * 100
        slope_10s2s_1d = (y10.iloc[-2] - y2.iloc[-2]) * 100 if len(y10) > 1 and len(y2) > 1 else None
        trend_10s2s = None
        if slope_10s2s_1d is not None:
            trend_10s2s = "steepening" if slope_10s2s > slope_10s2s_1d else "flattening"
        data["rates"] = dict(y2=y2, y10=y10, y30=y30, c2=c2, c10=c10, c30=c30,
                              slope_10s2s=slope_10s2s, slope_30s10s=slope_30s10s,
                              trend_10s2s=trend_10s2s, as_of=as_of_10)
        if slope_10s2s < 0:
            red_flags.append(f"2s10s curve is **inverted** ({slope_10s2s:.0f} bps) — classic late-cycle/recession-risk signal.")
        if trend_10s2s:
            notes.append(f"the 2s10s curve is {trend_10s2s} ({slope_10s2s:.0f} bps)")

# Futures
fut_hist = {}
fut_rows = []
for t, n in FUTURES.items():
    h, _ = fetch_yf_history(t)
    if h is not None:
        d = pct_changes(h)
        fut_hist[n] = h.tail(22)
        fut_rows.append({"name": n, **d})
if fut_rows:
    data["futures"] = fut_rows
    valid = [r["chg_1d"] for r in fut_rows if r["chg_1d"] is not None]
    if valid:
        dispersion = max(valid) - min(valid)
        leader = max(fut_rows, key=lambda r: r["chg_1d"] or -999)
        laggard = min(fut_rows, key=lambda r: r["chg_1d"] or 999)
        data["futures_dispersion"] = dispersion
        notes.append(f"equity futures show {'broad, aligned' if dispersion < 0.5 else 'narrow, divergent'} "
                     f"participation ({leader['name']} leading, {laggard['name']} lagging)")
        if dispersion >= 1.0:
            red_flags.append(f"Wide dispersion across index futures ({dispersion:.1f} pts) — rally/selloff is narrow, not broad-based.")

# Commodities
com_hist = {}
com_rows = []
for t, n in COMMODITIES.items():
    h, _ = fetch_yf_history(t)
    if h is not None:
        d = pct_changes(h)
        com_hist[n] = h.tail(22)
        com_rows.append({"name": n, **d})
        if is_unusual(d["chg_1d"]):
            red_flags.append(f"{n} moved {d['chg_1d']:+.1f}% today — unusual (≥{UNUSUAL_MOVE_PCT:.0f}%) move.")
if com_rows:
    data["commodities"] = com_rows

# VIX term structure
vix_hist = {}
vix_rows = []
vix_levels = {}
for t in VIX_TERM:
    h, _ = fetch_yf_history(t)
    if h is not None:
        d = pct_changes(h)
        label = t.replace("^", "")
        vix_hist[label] = h.tail(22)
        vix_rows.append({"name": label, **d})
        vix_levels[t] = d["last"]
if vix_rows:
    ordered = all(vix_levels.get(a, 0) <= vix_levels.get(b, 0) for a, b in
                  zip(VIX_TERM, VIX_TERM[1:]) if a in vix_levels and b in vix_levels)
    data["vix"] = dict(rows=vix_rows, ordered=ordered)
    if not ordered:
        red_flags.append("VIX term structure is inverted/backwardated — front-end fear rising faster than the back end.")
    else:
        notes.append("the VIX term structure is in normal contango (calm)")

# DXY
dxy_hist, _ = fetch_yf_history(DXY_TICKER)
if dxy_hist is not None:
    d = pct_changes(dxy_hist)
    data["dxy"] = d
    if d["chg_1d"] is not None and abs(d["chg_1d"]) >= 0.5:
        red_flags.append(f"Dollar Index moved {d['chg_1d']:+.2f}% today — a sizeable one-day FX move.")
    notes.append(f"the dollar is {'up' if (d['chg_1d'] or 0) >= 0 else 'down'} {abs(d['chg_1d'] or 0):.2f}% on the day")

# Credit stress (HYG/LQD)
hyg_hist, _ = fetch_yf_history("HYG")
lqd_hist, _ = fetch_yf_history("LQD")
if hyg_hist is not None and lqd_hist is not None:
    joined = pd.concat([hyg_hist, lqd_hist], axis=1, join="inner")
    joined.columns = ["HYG", "LQD"]
    ratio = joined["HYG"] / joined["LQD"]
    d = pct_changes(ratio)
    data["credit"] = dict(ratio=ratio, **d)
    if d["chg_1d"] is not None and d["chg_1d"] <= -0.5:
        red_flags.append(f"Credit stress proxy (HYG/LQD) fell {d['chg_1d']:.2f}% today — high-yield underperforming, a risk-off tell.")
    notes.append(f"credit conditions ({'widening' if (d['chg_1d'] or 0) < 0 else 'stable-to-easing'})")

# Calendar
events, fetched_at = fetch_ff_calendar()
data["calendar"] = events
high_impact_soon = [e for e in events if str(e.get("impact", "")).lower() in ("high", "3")]
if high_impact_soon:
    titles = ", ".join(sorted({e.get("title", "") for e in high_impact_soon}))
    notes.append(f"high-impact releases on deck this week ({titles})")


# ---------------------------------------------------------------------------
# Summary (analyst-style) — built from the flags/notes computed above
# ---------------------------------------------------------------------------

st.title("Macro Context Dashboard")
if st.button("🔄 Refresh now"):
    st.cache_data.clear()
    st.rerun()

st.markdown("### Market read")

if notes:
    narrative = ("Taking stock of the tape right now: " + "; ".join(notes) + ". " +
                 ("No readings are outside normal ranges beyond what's flagged below." if red_flags
                  else "Nothing here is flashing outside of normal ranges — context is clean, no single factor "
                       "demands a defensive posture today."))
else:
    narrative = "Not enough data loaded yet to form a read — check that your FRED key is set and refresh."

st.markdown(f"<div class='card'>{narrative}</div>", unsafe_allow_html=True)

if red_flags:
    st.markdown("**🚩 Red flags**")
    for f in red_flags:
        st.markdown(f"<div class='flag-red'>• {f}</div>", unsafe_allow_html=True)
else:
    st.markdown("<span class='flag-ok'>✓ No red flags triggered across rates, futures, commodities, "
                "VIX shape, DXY, or credit at current thresholds.</span>", unsafe_allow_html=True)

st.divider()

# ---------------------------------------------------------------------------
# Phase 2 — compact 3-column card grid
# ---------------------------------------------------------------------------

col1, col2, col3 = st.columns(3, gap="medium")

# --- Column 1: Rates + DXY -----------------------------------------------
with col1:
    st.markdown("<div class='card'><h4>1 · Rates & Curve</h4>", unsafe_allow_html=True)
    if "rates" in data:
        r = data["rates"]
        c1, c2, c3 = st.columns(3)
        c1.metric("2Y", f"{r['y2'].iloc[-1]:.2f}%", fmt_bps(r["c2"]["chg_1d"]))
        c2.metric("10Y", f"{r['y10'].iloc[-1]:.2f}%", fmt_bps(r["c10"]["chg_1d"]))
        c3.metric("30Y", f"{r['y30'].iloc[-1]:.2f}%", fmt_bps(r["c30"]["chg_1d"]))
        st.caption(f"10s2s: {r['slope_10s2s']:.0f} bps ({r['trend_10s2s'] or '—'}) · "
                    f"30s10s: {r['slope_30s10s']:.0f} bps")
        st.plotly_chart(go.Figure(
            data=[go.Scatter(x=r['y10'].tail(66).index, y=r['y10'].tail(66).values, name="10Y",
                              line=dict(color=ACCENT_COLORS[0], width=2)),
                  go.Scatter(x=r['y2'].tail(66).index, y=r['y2'].tail(66).values, name="2Y",
                              line=dict(color=ACCENT_COLORS[1], width=2)),
                  go.Scatter(x=r['y30'].tail(66).index, y=r['y30'].tail(66).values, name="30Y",
                              line=dict(color=ACCENT_COLORS[2], width=2))]
        ).update_layout(height=CHART_HEIGHT, margin=dict(l=4, r=4, t=4, b=4),
                          paper_bgcolor=DARK_BG, plot_bgcolor=PLOT_BG,
                          font=dict(color="#d7dae0", size=10),
                          legend=dict(orientation="h", y=1.15, x=0, font=dict(size=9)),
                          xaxis=dict(showgrid=False, tickfont=dict(size=9)),
                          yaxis=dict(showgrid=True, gridcolor=GRID, ticksuffix="%", tickfont=dict(size=9))),
                        use_container_width=True, config={"displayModeBar": False})
        st.markdown(f"<div class='small-caption'>As of {r['as_of']} · FRED</div>", unsafe_allow_html=True)
    else:
        st.warning("Add FRED_API_KEY in Secrets to enable this section.")
    st.markdown("</div>", unsafe_allow_html=True)

    st.markdown("<div class='card'><h4>6 · Dollar Index (DXY)</h4>", unsafe_allow_html=True)
    if "dxy" in data:
        d = data["dxy"]
        c1, c2, c3 = st.columns(3)
        c1.metric("DXY", f"{d['last']:.2f}", fmt_pct(d["chg_1d"]))
        c2.metric("1W", fmt_pct(d["chg_1w"]))
        c3.metric("1M", fmt_pct(d["chg_1m"]))
        st.plotly_chart(level_chart(dxy_hist.tail(22), color=ACCENT_COLORS[3]),
                         use_container_width=True, config={"displayModeBar": False})
    else:
        st.error("Could not load DXY.")
    st.markdown("</div>", unsafe_allow_html=True)

# --- Column 2: Futures + VIX ---------------------------------------------
with col2:
    st.markdown("<div class='card'><h4>2 · Index Futures</h4>", unsafe_allow_html=True)
    if "futures" in data:
        df = pd.DataFrame(data["futures"]).sort_values("chg_1d", ascending=False)
        disp = df.copy()
        for c in ["chg_1d", "chg_1w", "chg_1m"]:
            disp[c] = disp[c].apply(fmt_pct)
        disp = disp.rename(columns={"name": "Future", "last": "Last",
                                     "chg_1d": "1D", "chg_1w": "1W", "chg_1m": "1M"})
        st.dataframe(disp, hide_index=True, use_container_width=True, height=140)
        st.plotly_chart(normalized_chart(fut_hist), use_container_width=True, config={"displayModeBar": False})
        st.caption(f"Dispersion (1D): {data.get('futures_dispersion', 0):.2f} pts")
    else:
        st.error("Could not load futures data.")
    st.markdown("</div>", unsafe_allow_html=True)

    st.markdown("<div class='card'><h4>5 · VIX Term Structure</h4>", unsafe_allow_html=True)
    if "vix" in data:
        df = pd.DataFrame(data["vix"]["rows"])
        disp = df.copy()
        for c in ["chg_1d", "chg_1w", "chg_1m"]:
            disp[c] = disp[c].apply(fmt_pct)
        disp = disp.rename(columns={"name": "Index", "last": "Last",
                                     "chg_1d": "1D", "chg_1w": "1W", "chg_1m": "1M"})
        st.dataframe(disp, hide_index=True, use_container_width=True, height=170)
        st.plotly_chart(normalized_chart(vix_hist), use_container_width=True, config={"displayModeBar": False})
        st.caption("Contango (calm)" if data["vix"]["ordered"] else "⚠️ Inverted (risk-off)")
    else:
        st.error("Could not load VIX data.")
    st.markdown("</div>", unsafe_allow_html=True)

# --- Column 3: Commodities + Credit + Calendar ----------------------------
with col3:
    st.markdown("<div class='card'><h4>3-4 · Oil & Metals</h4>", unsafe_allow_html=True)
    if "commodities" in data:
        df = pd.DataFrame(data["commodities"])
        disp = df.copy()
        for c in ["chg_1d", "chg_1w", "chg_1m"]:
            disp[c] = disp[c].apply(fmt_pct)
        disp = disp.rename(columns={"name": "Asset", "last": "Last",
                                     "chg_1d": "1D", "chg_1w": "1W", "chg_1m": "1M"})
        st.dataframe(disp, hide_index=True, use_container_width=True, height=170)
        st.plotly_chart(normalized_chart(com_hist), use_container_width=True, config={"displayModeBar": False})
    else:
        st.error("Could not load commodity data.")
    st.markdown("</div>", unsafe_allow_html=True)

    st.markdown("<div class='card'><h4>7 · Credit Stress (HYG/LQD)</h4>", unsafe_allow_html=True)
    if "credit" in data:
        d = data["credit"]
        c1, c2, c3 = st.columns(3)
        c1.metric("Ratio", f"{d['last']:.3f}", fmt_pct(d["chg_1d"]))
        c2.metric("1W", fmt_pct(d["chg_1w"]))
        c3.metric("1M", fmt_pct(d["chg_1m"]))
        st.plotly_chart(level_chart(d["ratio"].tail(22), color=ACCENT_COLORS[4]),
                         use_container_width=True, config={"displayModeBar": False})
    else:
        st.error("Could not load credit data.")
    st.markdown("</div>", unsafe_allow_html=True)

    st.markdown("<div class='card'><h4>8 · Macro Calendar</h4>", unsafe_allow_html=True)
    if data.get("calendar"):
        cal_df = pd.DataFrame([{"Date": e.get("date", ""), "Event": e.get("title", ""),
                                 "Impact": e.get("impact", "")} for e in data["calendar"]])
        st.dataframe(cal_df, hide_index=True, use_container_width=True, height=170)
    else:
        st.info("No matching USD events this week, or feed unavailable.")
    st.markdown(f"<div class='small-caption'>Fetched {fetched_at}</div>", unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)
