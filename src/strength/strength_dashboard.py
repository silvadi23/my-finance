#!/usr/bin/env python3
"""
Streamlit dashboard for sector/industry relative strength.

Reads the CSVs produced by screener_strength.py (in this same folder):
  - strength_results.csv           : snapshot ranking (one row per sector/industry)
  - strength_history.csv           : tidy bi-weekly rs_2w series (joins on `id`)
  - strength_companies.csv         : per-industry top companies (drill-down)
  - strength_companies_history.csv : bi-weekly rs_2w per company

Run:
  python -m streamlit run strength_dashboard.py
"""
from pathlib import Path
import io
import base64
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import streamlit as st

HERE = Path(__file__).parent
RESULTS_CSV = HERE / "strength_results.csv"
HISTORY_CSV = HERE / "strength_history.csv"
COMPANIES_CSV = HERE / "strength_companies.csv"
COMPANIES_HISTORY_CSV = HERE / "strength_companies_history.csv"

# Field ordering for the table (left -> right). Detail columns are hidden unless
# the sidebar toggle is on.
BASE_COLUMNS = ['rank', 'name', 'sector', 'level', 'trend', 'rs_score', 'rs_median',
                'breadth_gap', 'rs_1m', 'rs_3m', 'rs_6m', 'rs_1y', 'ret_1y']
DETAIL_COLUMNS = ['n_constituents', 'clipped', 'coverage', 'id']

NUMERIC_COLS = ['rank', 'rs_score', 'rs_median', 'rs_1m', 'rs_3m', 'rs_6m', 'rs_1y',
                'ret_1m', 'ret_3m', 'ret_6m', 'ret_1y', 'n_constituents', 'clipped', 'coverage']

# In-table sparkline shows only the most recent ~6 months of bi-weekly points.
TREND_POINTS = 13

st.set_page_config(page_title="Relative Strength", layout="wide")


@st.cache_data
def load_results(_mtime):
    df = pd.read_csv(RESULTS_CSV)
    for c in NUMERIC_COLS:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors='coerce')
    return df


@st.cache_data
def load_history(_mtime):
    h = pd.read_csv(HISTORY_CSV)
    h['rs_2w'] = pd.to_numeric(h['rs_2w'], errors='coerce')
    return h


def sparkline_map(history):
    """id -> list of rs_2w values in date order (for the inline sparkline)."""
    out = {}
    for cid, g in history.sort_values('date').groupby('id'):
        out[cid] = g['rs_2w'].tolist()
    return out


@st.cache_data
def spark_png(values):
    """Tiny green/red bar sparkline as a data-URI PNG (green >= 0, red < 0).
    Cached per value-tuple so reruns are instant."""
    vals = [v for v in values if v == v]   # drop NaN
    if not vals:
        return None
    fig, ax = plt.subplots(figsize=(2.6, 0.5), dpi=100)
    ax.bar(range(len(vals)), vals, width=0.9,
           color=['#2ca02c' if v >= 0 else '#d62728' for v in vals])
    ax.axhline(0, color='#999999', linewidth=0.5)
    ax.margins(x=0.02, y=0.15)
    ax.set_axis_off()
    buf = io.BytesIO()
    fig.savefig(buf, format='png', bbox_inches='tight', pad_inches=0, transparent=True)
    plt.close(fig)
    return 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode()


@st.cache_data
def load_companies(_mtime):
    df = pd.read_csv(COMPANIES_CSV)
    for c in ['rank', 'weight', 'rs_score', 'rs_1m', 'rs_3m', 'rs_6m', 'rs_1y', 'ret_1y',
              'chg_1w', 'chg_1m', 'chg_3m']:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors='coerce')
    return df


@st.cache_data
def load_companies_history(_mtime):
    h = pd.read_csv(COMPANIES_HISTORY_CSV)
    h['rs_2w'] = pd.to_numeric(h['rs_2w'], errors='coerce')
    return h


def company_sparkline_map(history):
    """(industry_id, symbol) -> list of rs_2w values in date order."""
    out = {}
    for (iid, sym), g in history.sort_values('date').groupby(['industry_id', 'symbol']):
        out[(iid, sym)] = g['rs_2w'].tolist()
    return out


def rs_styler(d, score_cols, window_cols):
    """Red->green background. Each score col is scaled to its own symmetric range;
    the window cols share ONE symmetric scale so the left->right read compares
    (same colour = same magnitude)."""
    sty = d.style
    for c in score_cols:
        if c in d.columns and d[c].notna().any():
            m = float(d[c].abs().max()) or 1.0
            sty = sty.background_gradient(cmap='RdYlGn', subset=[c], vmin=-m, vmax=m)
    wcols = [c for c in window_cols if c in d.columns]
    if wcols and d[wcols].notna().any().any():
        wm = float(d[wcols].abs().max().max())
        if not (wm > 0):
            wm = 1.0
        sty = sty.background_gradient(cmap='RdYlGn', subset=wcols, vmin=-wm, vmax=wm, axis=None)
    return sty


# --- load ------------------------------------------------------------------
if not RESULTS_CSV.exists():
    st.error(f"Missing `{RESULTS_CSV.name}` in {HERE}.\n\nGenerate it first:\n\n"
             "```\npython screener_strength.py\n```")
    st.stop()

results = load_results(RESULTS_CSV.stat().st_mtime)
history = (load_history(HISTORY_CSV.stat().st_mtime) if HISTORY_CSV.exists()
           else pd.DataFrame(columns=['id', 'date', 'rs_2w']))
_sparks = sparkline_map(history)
results['trend'] = results['id'].map(
    lambda cid: spark_png(tuple((_sparks.get(cid) or [])[-TREND_POINTS:])))
# Breadth gap: a big positive gap means the cap-weighted score is driven by a few
# names rather than broad strength (score high, median low).
results['breadth_gap'] = results['rs_score'] - results['rs_median']

# Company drill-down data (optional; produced by screener_strength.py).
companies = (load_companies(COMPANIES_CSV.stat().st_mtime) if COMPANIES_CSV.exists()
             else pd.DataFrame())
companies_hist = (load_companies_history(COMPANIES_HISTORY_CSV.stat().st_mtime)
                  if COMPANIES_HISTORY_CSV.exists()
                  else pd.DataFrame(columns=['industry_id', 'symbol', 'date', 'rs_2w']))
if not companies.empty:
    _csparks = company_sparkline_map(companies_hist)
    companies['trend'] = companies.apply(
        lambda r: spark_png(tuple((_csparks.get((r['industry_id'], r['symbol'])) or [])[-TREND_POINTS:])),
        axis=1)

# Enrich companies with sector + industry name (for the all-companies view).
if not companies.empty:
    _id_name = dict(zip(results['id'], results['name']))
    _id_sector = dict(zip(results['id'], results['sector']))
    companies['industry'] = companies['industry_id'].map(_id_name).fillna(companies['industry_id'])
    companies['sector'] = companies['industry_id'].map(_id_sector)


def company_table(cdf, show_industry=False, height=None, sort_by=None):
    """Render a company table (RS-window heatmap, colored rs_score, green/red
    trend). `#` is the GLOBAL rs_score rank (independent of display order).
    `sort_by` is an optional list of (column, ascending) pairs for a multi-column
    display sort; defaults to rs_score descending. `height` (px) fixes the table
    height (None = auto)."""
    cdf = cdf.copy()
    if cdf.empty:
        st.info("No companies match.")
        return
    # '#' = global RS-score rank, assigned before the (possibly multi-key) display sort.
    cdf = cdf.sort_values('rs_score', ascending=False)
    cdf['rank'] = range(1, len(cdf) + 1)
    if sort_by:
        scols, sasc = zip(*sort_by)
        cdf = cdf.sort_values(list(scols), ascending=list(sasc), kind='stable')
    cdf = cdf.reset_index(drop=True)
    cols = (['rank', 'symbol', 'name'] + (['industry'] if show_industry else [])
            + ['trend', 'chg_1w', 'chg_1m', 'chg_3m',
               'rs_score', 'rs_1m', 'rs_3m', 'rs_6m', 'rs_1y', 'ret_1y'])
    cfg = {
        'rank':     st.column_config.NumberColumn("#", format="%d"),
        'symbol':   st.column_config.TextColumn("Symbol"),
        'name':     st.column_config.TextColumn("Name", width="medium"),
        'industry': st.column_config.TextColumn("Industry", width="medium"),
        'trend':    st.column_config.ImageColumn("RS trend (6m)", width="medium"),
        'chg_1w':   st.column_config.NumberColumn("Chg 1w", format="%.1f", help="Price change %, last 1 week"),
        'chg_1m':   st.column_config.NumberColumn("Chg 1m", format="%.1f", help="Price change %, last 1 month"),
        'chg_3m':   st.column_config.NumberColumn("Chg 3m", format="%.1f", help="Price change %, last 3 months"),
        'rs_score': st.column_config.NumberColumn("RS score", format="%.1f"),
        'rs_1m':    st.column_config.NumberColumn("RS 1m", format="%.1f"),
        'rs_3m':    st.column_config.NumberColumn("RS 3m", format="%.1f"),
        'rs_6m':    st.column_config.NumberColumn("RS 6m", format="%.1f"),
        'rs_1y':    st.column_config.NumberColumn("RS 1y", format="%.1f"),
        'ret_1y':   st.column_config.NumberColumn("Ret 1y", format="%.1f"),
    }
    st.dataframe(
        rs_styler(cdf, ('rs_score', 'chg_1w', 'chg_1m', 'chg_3m'),
                  ('rs_1m', 'rs_3m', 'rs_6m', 'rs_1y')),
        hide_index=True, use_container_width=True, height=height,
        column_order=[c for c in cols if c in cdf.columns], column_config=cfg,
    )


def render_sectors_industries():
    """View 1: sortable sector/industry table + row-click detail and drill-down."""
    level = st.sidebar.radio("Level", ["All", "sector", "industry"], horizontal=True)
    pick_sectors = st.sidebar.multiselect("Sector", sorted(results['sector'].dropna().unique()))
    search = st.sidebar.text_input("Search name")
    valid = results['rs_score'].dropna()
    lo = float(valid.min()) if not valid.empty else -100.0
    hi = float(valid.max()) if not valid.empty else 100.0
    if hi <= lo:
        hi = lo + 1.0
    score_min = st.sidebar.slider("Min RS score", lo, hi, value=lo)
    top_n = st.sidebar.number_input("Top N (0 = all)", min_value=0, value=0, step=10)
    show_detail = st.sidebar.checkbox("Show detail columns (n, clip, cov, id)", value=False)

    df = results.copy()
    if level != "All":
        df = df[df['level'] == level]
    if pick_sectors:
        df = df[df['sector'].isin(pick_sectors)]
    if search:
        df = df[df['name'].str.contains(search, case=False, na=False)]
    df = df[df['rs_score'].fillna(-1e9) >= score_min]
    df = df.sort_values('rs_score', ascending=False, na_position='last').reset_index(drop=True)
    if top_n:
        df = df.head(int(top_n))

    st.markdown("#### Sector & Industry Relative Strength")
    st.caption(f"Relative strength vs SPY · {len(df)} rows · source: {RESULTS_CSV.name} + {HISTORY_CSV.name}")

    column_order = [c for c in (BASE_COLUMNS + (DETAIL_COLUMNS if show_detail else [])) if c in df.columns]
    column_config = {
        'rank':           st.column_config.NumberColumn("#", format="%d"),
        'name':           st.column_config.TextColumn("Name", width="medium"),
        'trend':          st.column_config.ImageColumn("RS trend (6m)", width="medium"),
        'rs_score':       st.column_config.NumberColumn("RS score", format="%.1f"),
        'rs_median':      st.column_config.NumberColumn("RS median", format="%.1f"),
        'breadth_gap':    st.column_config.NumberColumn("Gap", format="%.1f",
                                                        help="rs_score - rs_median; large = a few names drive it"),
        'rs_1m':          st.column_config.NumberColumn("RS 1m", format="%.1f"),
        'rs_3m':          st.column_config.NumberColumn("RS 3m", format="%.1f"),
        'rs_6m':          st.column_config.NumberColumn("RS 6m", format="%.1f"),
        'rs_1y':          st.column_config.NumberColumn("RS 1y", format="%.1f"),
        'ret_1y':         st.column_config.NumberColumn("Ret 1y", format="%.1f"),
        'n_constituents': st.column_config.NumberColumn("n", format="%d"),
        'clipped':        st.column_config.NumberColumn("clip", format="%d"),
        'coverage':       st.column_config.NumberColumn("cov", format="%.2f"),
        'id':             st.column_config.TextColumn("id"),
    }
    styler = rs_styler(df, ('rs_score', 'rs_median'), ('rs_1m', 'rs_3m', 'rs_6m', 'rs_1y'))
    event = st.dataframe(
        styler, hide_index=True, use_container_width=True,
        column_order=column_order, column_config=column_config,
        on_select="rerun", selection_mode="single-row", key="rs_table",
    )

    if df.empty:
        st.info("No rows match the current filters.")
        return
    rows = event.selection.rows
    sel_id = df.iloc[rows[0]]['id'] if rows else df.iloc[0]['id']
    row = results[results['id'] == sel_id].iloc[0]
    st.caption(f"Selected: **{sel_id}** — click a row above to change.")

    # Top companies (industry drill-down) -- shown first.
    if row['level'] == 'industry':
        st.markdown(f"#### Top companies — {row['name']}")
        if companies.empty:
            st.info("No company data. Re-run screener_strength.py to generate strength_companies.csv.")
        else:
            company_table(companies[companies['industry_id'] == sel_id])
    elif row['level'] == 'sector':
        st.caption("Select an industry to drill into its top companies.")

    # Trend detail -- shown after.
    st.subheader("Trend detail")
    c1, c2, c3 = st.columns(3)
    c1.metric("RS score", f"{row['rs_score']:.1f}" if pd.notna(row['rs_score']) else "—")
    c2.metric("RS median", f"{row['rs_median']:.1f}" if pd.notna(row['rs_median']) else "—")
    c3.metric("Return 1y", f"{row['ret_1y']:.1f}%" if pd.notna(row['ret_1y']) else "—")
    h = history[history['id'] == sel_id].sort_values('date')
    if h.empty:
        st.info("No bi-weekly history for this id.")
    else:
        st.line_chart(h, x='date', y='rs_2w')


def render_companies():
    """View 2: every company across all industries, ordered by RS score."""
    st.markdown("#### All companies by RS score")
    if companies.empty:
        st.info("No company data. Re-run screener_strength.py to generate strength_companies.csv.")
        return
    pick_sectors = st.sidebar.multiselect("Sector", sorted(companies['sector'].dropna().unique()))
    search = st.sidebar.text_input("Search symbol / name")
    valid = companies['rs_score'].dropna()
    lo = float(valid.min()) if not valid.empty else -100.0
    hi = float(valid.max()) if not valid.empty else 100.0
    if hi <= lo:
        hi = lo + 1.0
    score_min = st.sidebar.slider("Min RS score", lo, hi, value=lo)
    top_n = st.sidebar.number_input("Top N by RS (0 = all)", min_value=0, value=0, step=25)

    # Multi-column sort (priority = selection order). Streamlit's header-click sort
    # is single-column, so this pre-sorts the frame. Text cols A->Z, numeric high->low.
    sort_opts = ['rs_score', 'rs_1m', 'rs_3m', 'rs_6m', 'rs_1y', 'ret_1y',
                 'chg_1w', 'chg_1m', 'chg_3m', 'industry', 'sector', 'symbol', 'name']
    sort_cols = st.sidebar.multiselect("Sort by (priority order)", sort_opts, default=['rs_score'])
    text_cols = {'industry', 'sector', 'symbol', 'name'}
    sort_by = [(c, c in text_cols) for c in sort_cols] or [('rs_score', False)]

    cdf = companies.copy()
    if pick_sectors:
        cdf = cdf[cdf['sector'].isin(pick_sectors)]
    if search:
        cdf = cdf[cdf['symbol'].str.contains(search, case=False, na=False)
                  | cdf['name'].str.contains(search, case=False, na=False)]
    cdf = cdf[cdf['rs_score'].fillna(-1e9) >= score_min]
    if top_n:  # pick the strongest N by RS, then apply the display sort
        cdf = cdf.sort_values('rs_score', ascending=False).head(int(top_n))

    st.caption(f"{len(cdf)} stocks across all industries · sort: "
               f"{', '.join(c for c, _ in sort_by)} · source: {COMPANIES_CSV.name}")
    company_table(cdf, show_industry=True, height=820, sort_by=sort_by)


# --- sidebar + view dispatch -----------------------------------------------
st.sidebar.header("Filters")
if st.sidebar.button("🔄 Reload data"):
    st.cache_data.clear()
    st.rerun()
view = st.sidebar.radio("View", ["Sectors & Industries", "All companies"])

if view == "Sectors & Industries":
    render_sectors_industries()
else:
    render_companies()
