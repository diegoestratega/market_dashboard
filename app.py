"""
Pre-Market / On-Demand Macro Context Dashboard (v4 — bug fixes)
---------------------------------------------------------------------------
Fixes vs v3:
  - ES=F missing: added retry logic (yfinance is occasionally flaky on the
    first hit) + a cash-index fallback (^GSPC/^IXIC/^RUT) if the future
    itself still fails after retries, so a section never silently drops.
  - Credit stress & DXY charts: removed `fill="tozeroy"`, which was forcing
    Plotly's autorange down to 0 and flattening the visible variation.
    Charts now autorange tightly around the actual data range.
  - Rates chart: fixed series order to 2Y -> 10Y -> 30Y (was 10Y -> 2Y -> 30Y).
  - Market read: now writes an extended, analyst-style explanation for each
    active red flag (why it matters, what to watch), staying brief only
    when nothing is flagged.
"""

import datetime as dt
import time

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
import yfinance as yf

st.set_page_config(page_title="Macro Context Dashboard", layout="wide")

UNUSUAL_MOVE_PCT = 3.0
FRED_API_KEY = st.secrets.get("FRED_API_KEY", "")

FUTURES = {"ES=F": "S&P 500 (ES)", "NQ=F": "Nasdaq 100 (NQ)", "RTY=F": "Russell 2000 (RTY)"}
FUTURES_FALLBACK = {"ES=F": ("^GSPC", "S&P 500 (cash, futures unavailable)"),
                     "NQ=F": ("^IXIC", "Nasdaq Composite (cash, futures unavailable)"),
                     "RTY=F": ("^RUT", "Russell 2000 (cash, futures unavailable)")}
COMMODITIES = {"CL=F": "Crude Oil (WTI)", "GC=F": "Gold", "SI=F": "Silver", "HG=F": "Copper"}
VIX_TERM = ["^VIX9D", "^VIX", "^VIX3M", "^VIX6M"]
DXY_TICKER = "DX-Y.NYB"

FF_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
CALENDAR_KEYWORDS = ["CPI", "PCE", "Non-Farm", "NFP", "Nonfarm", "FOMC",
                     "Fed Funds", "Interest Rate", "Unemployment Claims", "Employment"]

BG_APP = "#0f1116"
BG_CARD = "#171a21"
BG_PLOT = "#1b1f28"
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

.card {{
    background-color: {BG_CARD}; border-radius: 12px; padding: 20px 22px;
    margin-bottom: 20px; border-left: 4px solid {ACCENT};
}}
.card-flag {{ border-left: 4px solid {NEG}; }}
.card h4 {{
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
def fetch_yf_history(ticker: str, period: str = "3mo", retries: int = 3):
    """Fetch daily closes with retries — yfinance is occasionally flaky on
    the first request for a given ticker (empty frame / transient timeout)."""
    for attempt in range(retries):
        try:
            hist = yf.Ticker(ticker).history(period=period, interval="1d")
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
    """Try the futures ticker; if it still fails after retries, fall back to
    the corresponding cash index rather than silently dropping the row."""
    hist, as_of = fetch_yf_history(ticker)
    if hist is not None:
        return hist, label
    fb_ticker, fb_label = FUTURES_FALLBACK.get(ticker, (None, None))
    if fb_ticker:
        hist, as_of = fetch_yf_history(fb_ticker)
        if hist is not None:
            return hist, fb_label
    return None, label


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
    """Zoom the y-axis tightly around the data instead of Plotly defaulting
    toward 0 — this is what makes small % variations actually visible."""
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


# ---------------------------------------------------------------------------
# Phase 1 — fetch & compute
# ---------------------------------------------------------------------------

data = {}
red_flags = []
notes = []
flagged = {"rates": False, "futures": False, "commodities": False,
           "vix": False, "dxy": False, "credit": False}

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
            red_flags.append(f"2s10s curve is inverted ({slope_10s2s:.0f} bps).")
            flagged["rates"] = True
        if trend_10s2s:
            notes.append(f"the 2s10s curve is {trend_10s2s} ({slope_10s2s:.0f} bps)")

fut_hist = {}
fut_rows = []
for t, n in FUTURES.items():
    h, label = fetch_future_with_fallback(t, n)
    if h is not None:
        d = pct_changes(h)
        fut_hist[label] = h.tail(22)
        fut_rows.append({"name": label, **d})
if fut_rows:
    data["futures"] = fut_rows
    valid = [r["chg_1d"] for r in fut_rows if r["chg_1d"] is not None]
    if valid:
        dispersion = max(valid) - min(valid)
        leader = max(fut_rows, key=lambda r: r["chg_1d"] or -999)
        laggard = min(fut_rows, key=lambda r: r["chg_1d"] or 999)
        data["futures_dispersion"] = dispersion
        data["futures_leader"], data["futures_laggard"] = leader["name"], laggard["name"]
        notes.append(f"equity futures show {'broad, aligned' if dispersion < 0.5 else 'narrow, divergent'} "
                     f"participation ({leader['name']} leading, {laggard['name']} lagging)")
        if dispersion >= 1.0:
            red_flags.append(f"Wide dispersion across index futures ({dispersion:.1f} pts).")
            flagged["futures"] = True

com_hist = {}
com_rows = []
for t, n in COMMODITIES.items():
    h, _ = fetch_yf_history(t)
    if h is not None:
        d = pct_changes(h)
        com_hist[n] = h.tail(22)
        com_rows.append({"name": n, **d})
        if is_unusual(d["chg_1d"]):
            red_flags.append(f"{n} moved {d['chg_1d']:+.1f}% today (≥{UNUSUAL_MOVE_PCT:.0f}% threshold).")
            flagged["commodities"] = True
if com_rows:
    data["commodities"] = com_rows

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
        red_flags.append("VIX term structure is inverted/backwardated.")
        flagged["vix"] = True
    else:
        notes.append("the VIX term structure is in normal contango (calm)")

dxy_hist, _ = fetch_yf_history(DXY_TICKER)
if dxy_hist is not None:
    d = pct_changes(dxy_hist)
    data["dxy"] = d
    if d["chg_1d"] is not None and abs(d["chg_1d"]) >= 0.5:
        red_flags.append(f"Dollar Index moved {d['chg_1d']:+.2f}% today.")
        flagged["dxy"] = True
    notes.append(f"the dollar is {'up' if (d['chg_1d'] or 0) >= 0 else 'down'} {abs(d['chg_1d'] or 0):.2f}% on the day")

hyg_hist, _ = fetch_yf_history("HYG")
lqd_hist, _ = fetch_yf_history("LQD")
if hyg_hist is not None and lqd_hist is not None:
    joined = pd.concat([hyg_hist, lqd_hist], axis=1, join="inner")
    joined.columns = ["HYG", "LQD"]
    ratio = joined["HYG"] / joined["LQD"]
    d = pct_changes(ratio)
    data["credit"] = dict(ratio=ratio, **d)
    if d["chg_1d"] is not None and d["chg_1d"] <= -0.5:
        red_flags.append(f"Credit stress proxy (HYG/LQD) fell {d['chg_1d']:.2f}% today.")
        flagged["credit"] = True
    notes.append(f"credit conditions ({'widening' if (d['chg_1d'] or 0) < 0 else 'stable-to-easing'})")

events, fetched_at = fetch_ff_calendar()
data["calendar"] = events
high_impact_soon = [e for e in events if str(e.get("impact", "")).lower() in ("high", "3")]
if high_impact_soon:
    titles = ", ".join(sorted({e.get("title", "") for e in high_impact_soon}))
    notes.append(f"high-impact releases on deck this week ({titles})")


# ---------------------------------------------------------------------------
# Market read (extended when flags are active, brief when clean)
# ---------------------------------------------------------------------------

def build_narrative():
    base = "Taking stock of the tape right now: " + ("; ".join(notes) + "." if notes else "data is limited.")
    if not red_flags:
        base += (" Nothing here is flashing outside of normal ranges — context is clean, no single factor "
                 "demands a defensive posture today.")
        return base

    parts = [base, "", "A few things stand out enough to break down in more detail:"]

    if flagged["rates"]:
        r = data["rates"]
        parts.append(
            f"The 2s10s Treasury spread has moved into inversion, sitting at {r['slope_10s2s']:.0f} bps. "
            "Curve inversions have historically preceded economic slowdowns or Fed policy pivots by several "
            "quarters — it doesn't mean a recession is imminent, but it reflects the bond market pricing in "
            "slower growth or future rate cuts relative to today's short-end pricing. Worth tracking whether "
            "this deepens or reverses over the coming sessions rather than reacting to one print."
        )
    if flagged["futures"]:
        parts.append(
            f"Index futures show a same-day dispersion of {data.get('futures_dispersion', 0):.2f} points between "
            f"{data.get('futures_leader', 'the leader')} and {data.get('futures_laggard', 'the laggard')}. That "
            "kind of spread means today's move is concentrated in a specific market segment (large-cap growth vs. "
            "small-cap/cyclicals, for instance) rather than a broad, healthy risk-on move — it's worth checking "
            "what's driving the split (rates, a single mega-cap name, sector rotation) before reading directional "
            "conviction into the headline index number."
        )
    if flagged["commodities"]:
        moves = [f"{r['name']} {r['chg_1d']:+.1f}%" for r in data.get("commodities", []) if is_unusual(r["chg_1d"])]
        parts.append(
            f"One or more commodities crossed the {UNUSUAL_MOVE_PCT:.0f}% single-day threshold ({', '.join(moves)}). "
            "Moves of this size in a single session tend to bleed into inflation expectations, input costs, and "
            "related equity sectors (energy, materials, miners) — worth identifying the catalyst (supply shock, "
            "dollar move, geopolitical headline, positioning unwind) rather than dismissing it as noise."
        )
    if flagged["vix"]:
        parts.append(
            "The VIX term structure has flipped into backwardation — near-term implied volatility (VIX9D) is now "
            "pricing higher fear than the longer-dated tenors (VIX3M/VIX6M). This is the opposite of the normal, "
            "calm upward-sloping shape, and historically this pattern shows up right before or during acute "
            "equity drawdowns, since it reflects options markets pricing an immediate, near-term shock rather than "
            "steady-state risk. Of everything on this board, this is typically the most actionable single flag."
        )
    if flagged["dxy"]:
        d = data["dxy"]
        parts.append(
            f"The Dollar Index moved {d['chg_1d']:+.2f}% in a single session — an unusually large one-day move for "
            "DXY. Swings of this size tend to ripple through commodities (which are dollar-denominated and move "
            "inversely), emerging-market assets, and multinational earnings translation. Worth cross-referencing "
            "against today's macro calendar for a rate decision, Fed commentary, or major data surprise as the likely driver."
        )
    if flagged["credit"]:
        d = data["credit"]
        parts.append(
            f"The HYG/LQD credit stress proxy fell {d['chg_1d']:.2f}% today, meaning high-yield bonds "
            "underperformed investment-grade. Credit markets often lead equities at inflection points — a "
            "persistent widening trend here over the next several sessions would be a more reliable early-warning "
            "signal than equity price action alone, so this is worth monitoring for follow-through rather than a one-off."
        )
    return "<br><br>".join(parts)


# ---------------------------------------------------------------------------
# Header + summary
# ---------------------------------------------------------------------------

top_l, top_r = st.columns([5, 1])
with top_l:
    st.title("Macro Context Dashboard")
    st.caption("On-demand snapshot — not a live stream · ~20 min delay tolerated on market data · daily on yields")
with top_r:
    st.write("")
    if st.button("🔄 Refresh", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

st.markdown("#### Market read")
st.markdown(f"<div class='summary-box'>{build_narrative()}</div>", unsafe_allow_html=True)

if red_flags:
    flag_html = f"<div class='summary-box' style='border-left-color:{NEG}'><b>🚩 Red flags</b><br><br>"
    flag_html += "<br>".join(f"<span class='flag-red'>• {f}</span>" for f in red_flags)
    flag_html += "</div>"
    st.markdown(flag_html, unsafe_allow_html=True)
else:
    st.markdown("<span class='flag-ok'>✓ No red flags triggered across rates, futures, commodities, "
                "VIX shape, DXY, or credit at current thresholds.</span>", unsafe_allow_html=True)

st.write("")

# ---------------------------------------------------------------------------
# Phase 2 — two-column layout
# ---------------------------------------------------------------------------

left, right = st.columns(2, gap="large")

with left:
    cls = "card card-flag" if flagged["futures"] else "card"
    st.markdown(f"<div class='{cls}'><h4>Index Futures</h4>", unsafe_allow_html=True)
    if "futures" in data:
        df = pd.DataFrame(data["futures"]).sort_values("chg_1d", ascending=False)
        disp = df.rename(columns={"name": "Future", "last": "Last", "chg_1d": "1D",
                                   "chg_1w": "1W", "chg_1m": "1M"})
        for c in ["1D", "1W", "1M"]:
            disp[c] = disp[c].apply(fmt_pct)
        disp["Last"] = disp["Last"].apply(lambda x: f"{x:,.2f}")
        st.dataframe(disp, hide_index=True, use_container_width=True, height=145)
        st.plotly_chart(normalized_chart(fut_hist, PALETTE), use_container_width=True,
                         config={"displayModeBar": False})
        st.markdown(f"<div class='small-caption'>1D dispersion: {data.get('futures_dispersion', 0):.2f} pts "
                    f"&nbsp;·&nbsp; yfinance, ~15-20 min delay</div>", unsafe_allow_html=True)
    else:
        st.error("Could not load futures data.")
    st.markdown("</div>", unsafe_allow_html=True)

    cls = "card card-flag" if flagged["rates"] else "card"
    st.markdown(f"<div class='{cls}'><h4>Rates & Yield Curve</h4>", unsafe_allow_html=True)
    if "rates" in data:
        r = data["rates"]
        c1, c2, c3 = st.columns(3)
        c1.metric("2Y", f"{r['y2'].iloc[-1]:.2f}%", fmt_bps(r["c2"]["chg_1d"]))
        c2.metric("10Y", f"{r['y10'].iloc[-1]:.2f}%", fmt_bps(r["c10"]["chg_1d"]))
        c3.metric("30Y", f"{r['y30'].iloc[-1]:.2f}%", fmt_bps(r["c30"]["chg_1d"]))
        st.markdown(f"<div class='small-caption'>10s2s: {r['slope_10s2s']:.0f} bps "
                    f"({r['trend_10s2s'] or '—'}) &nbsp;·&nbsp; 30s10s: {r['slope_30s10s']:.0f} bps</div>",
                    unsafe_allow_html=True)
        st.write("")
        chart_series = {"2Y": r["y2"].tail(66), "10Y": r["y10"].tail(66), "30Y": r["y30"].tail(66)}
        st.plotly_chart(multi_level_chart(chart_series, PALETTE, ticksuffix="%"),
                         use_container_width=True, config={"displayModeBar": False})
        st.markdown(f"<div class='small-caption'>As of {r['as_of']} · FRED, official daily par yields</div>",
                    unsafe_allow_html=True)
    else:
        st.warning("Add FRED_API_KEY in Secrets to enable this section.")
    st.markdown("</div>", unsafe_allow_html=True)

    cls = "card card-flag" if flagged["commodities"] else "card"
    st.markdown(f"<div class='{cls}'><h4>Oil & Metals</h4>", unsafe_allow_html=True)
    if "commodities" in data:
        df = pd.DataFrame(data["commodities"])
        disp = df.rename(columns={"name": "Asset", "last": "Last", "chg_1d": "1D",
                                   "chg_1w": "1W", "chg_1m": "1M"})
        for c in ["1D", "1W", "1M"]:
            disp[c] = disp[c].apply(fmt_pct)
        disp["Last"] = disp["Last"].apply(lambda x: f"{x:,.2f}")
        st.dataframe(disp, hide_index=True, use_container_width=True, height=175)
        st.plotly_chart(normalized_chart(com_hist, PALETTE), use_container_width=True,
                         config={"displayModeBar": False})
        st.markdown(f"<div class='small-caption'>Unusual-move threshold: ±{UNUSUAL_MOVE_PCT:.0f}% (1-day)</div>",
                    unsafe_allow_html=True)
    else:
        st.error("Could not load commodity data.")
    st.markdown("</div>", unsafe_allow_html=True)

with right:
    cls = "card card-flag" if flagged["vix"] else "card"
    st.markdown(f"<div class='{cls}'><h4>VIX Term Structure</h4>", unsafe_allow_html=True)
    if "vix" in data:
        df = pd.DataFrame(data["vix"]["rows"])
        disp = df.rename(columns={"name": "Index", "last": "Last", "chg_1d": "1D",
                                   "chg_1w": "1W", "chg_1m": "1M"})
        for c in ["1D", "1W", "1M"]:
            disp[c] = disp[c].apply(fmt_pct)
        disp["Last"] = disp["Last"].apply(lambda x: f"{x:,.2f}")
        st.dataframe(disp, hide_index=True, use_container_width=True, height=175)
        st.plotly_chart(normalized_chart(vix_hist, PALETTE), use_container_width=True,
                         config={"displayModeBar": False})
        shape_txt = "Contango (calm)" if data["vix"]["ordered"] else "⚠️ Inverted / backwardated (risk-off)"
        st.markdown(f"<div class='small-caption'>Shape: {shape_txt}</div>", unsafe_allow_html=True)
    else:
        st.error("Could not load VIX data.")
    st.markdown("</div>", unsafe_allow_html=True)

    cls = "card card-flag" if flagged["credit"] else "card"
    st.markdown(f"<div class='{cls}'><h4>Credit Stress (HYG / LQD)</h4>", unsafe_allow_html=True)
    if "credit" in data:
        d = data["credit"]
        c1, c2, c3 = st.columns(3)
        c1.metric("Ratio", f"{d['last']:.3f}", fmt_pct(d["chg_1d"]))
        c2.metric("1W", fmt_pct(d["chg_1w"]))
        c3.metric("1M", fmt_pct(d["chg_1m"]))
        st.write("")
        st.plotly_chart(level_chart(d["ratio"].tail(22), color=ACCENT3),
                         use_container_width=True, config={"displayModeBar": False})
        note = "Ratio falling → high-yield underperforming IG → credit stress widening." if (d["chg_1d"] or 0) < 0 \
            else "Ratio rising → high-yield outperforming IG → credit conditions easing."
        st.markdown(f"<div class='small-caption'>{note}</div>", unsafe_allow_html=True)
    else:
        st.error("Could not load credit data.")
    st.markdown("</div>", unsafe_allow_html=True)

    cls = "card card-flag" if flagged["dxy"] else "card"
    st.markdown(f"<div class='{cls}'><h4>Dollar Index (DXY)</h4>", unsafe_allow_html=True)
    if "dxy" in data:
        d = data["dxy"]
        c1, c2, c3 = st.columns(3)
        c1.metric("DXY", f"{d['last']:.2f}", fmt_pct(d["chg_1d"]))
        c2.metric("1W", fmt_pct(d["chg_1w"]))
        c3.metric("1M", fmt_pct(d["chg_1m"]))
        st.write("")
        st.plotly_chart(level_chart(dxy_hist.tail(22), color=ACCENT2),
                         use_container_width=True, config={"displayModeBar": False})
    else:
        st.error("Could not load DXY.")
    st.markdown("</div>", unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Full-width Macro Calendar
# ---------------------------------------------------------------------------

st.write("")
st.markdown("<div class='card'><h4>Macro Calendar — CPI / NFP / FOMC / PCE (USD, this week)</h4>",
            unsafe_allow_html=True)
if data.get("calendar"):
    rows = []
    for e in data["calendar"]:
        rows.append({
            "When": format_event_datetime(e.get("date", "")),
            "Event": e.get("title", ""),
            "Impact": str(e.get("impact", "")),
        })
    cal_df = pd.DataFrame(rows)
    st.dataframe(cal_df, hide_index=True, use_container_width=True, height=190)
else:
    st.info("No matching USD events this week, or the calendar feed is unavailable.")
st.markdown(f"<div class='small-caption'>Fetched {fetched_at} · ForexFactory public calendar feed, cached ~6h</div>",
            unsafe_allow_html=True)
st.markdown("</div>", unsafe_allow_html=True)
