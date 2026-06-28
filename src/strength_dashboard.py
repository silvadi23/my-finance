#!/usr/bin/env python3
"""
Streamlit dashboard for sector/industry relative strength.

Reads the CSVs produced by screener_strength.py (written to ./data_results/):
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
DATA = HERE / "data_results"
RESULTS_CSV = DATA / "strength_results.csv"
HISTORY_CSV = DATA / "strength_history.csv"
COMPANIES_CSV = DATA / "strength_companies.csv"
COMPANIES_HISTORY_CSV = DATA / "strength_companies_history.csv"

# Field ordering for the table (left -> right). Detail columns are hidden unless
# the sidebar toggle is on.
BASE_COLUMNS = ['rank', 'name', 'sector', 'level', 'trend', 'state', 'rot', 'rs_momentum',
                'rs_score', 'rs_median', 'breadth_gap', 'rs_1m', 'rs_3m', 'rs_6m', 'rs_1y', 'ret_1y']
DETAIL_COLUMNS = ['n_constituents', 'clipped', 'coverage', 'id']

NUMERIC_COLS = ['rank', 'rs_score', 'rs_median', 'rs_1m', 'rs_3m', 'rs_6m', 'rs_1y',
                'ret_1m', 'ret_3m', 'ret_6m', 'ret_1y', 'n_constituents', 'clipped', 'coverage']

# In-table sparkline shows only the most recent ~6 months of bi-weekly points.
TREND_POINTS = 13

# --- relative rotation (RRG) knobs -----------------------------------------
# The RS-Ratio is the strength *level* (where it is); the RS-Momentum is its rate
# of change (which way it is moving). A turn shows up in momentum before the level
# crosses over, so these are the early/confirmation dial.
RATIO_SMOOTH = 3       # buckets to smooth the cumulative RS line (the RS-Ratio)
MOM_LOOKBACK = 3       # buckets over which momentum (rate of change) is measured
MOM_SMOOTH = 2         # extra smoothing on momentum so the y-axis stops zig-zagging
CONFIRM_BUCKETS = 2    # consecutive same-sign momentum buckets to mark "confirmed"
TAIL_POINTS = 8        # max trajectory length retained (the scatter has a length slider)
ROT_LOOKBACK = 2       # buckets (~4 weeks) over which the rotation arrow measures momentum change
ROT_EPS = 0.1          # |mom_delta| below this shows flat (->), not up/down
# Sidebar Rotation filter: label -> arrow glyph in the `rot` column.
ROT_FILTER = {'Rising ↑': '↑', 'Falling ↓': '↓', 'Flat →': '→'}

# Cache-key token for the RRG functions. st.cache_data only invalidates on the decorated
# function's own source, NOT its callees — so when _rrg_from_wide's output shape/logic
# changes, bump this to force a recompute (otherwise a long-running process can serve a
# stale cached frame that's missing new columns).
_RRG_VERSION = 2

# Fixed display color per state (used in both the table and the scatter).
STATE_ORDER = ['Improving', 'Leading', 'Weakening', 'Lagging']
STATE_COLORS = {
    'Leading':   '#2ca02c',   # confirmed strong
    'Improving': '#1f77b4',   # early bullish turn (still lagging on level)
    'Weakening': '#ff7f0e',   # early topping
    'Lagging':   '#d62728',   # confirmed weak
}

st.set_page_config(page_title="Relative Strength", layout="wide")


@st.cache_data
def load_results(mtime):
    df = pd.read_csv(RESULTS_CSV)
    for c in NUMERIC_COLS:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors='coerce')
    return df


# NOTE: `mtime` (the CSV's st_mtime) is part of the cache key on purpose — it must NOT be
# underscore-prefixed, or st.cache_data would drop it from the hash and never see file changes.
@st.cache_data
def load_history(mtime):
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
def load_companies(mtime):
    df = pd.read_csv(COMPANIES_CSV)
    for c in ['rank', 'weight', 'rs_score', 'rs_1m', 'rs_3m', 'rs_6m', 'rs_1y', 'ret_1y',
              'chg_1w', 'chg_1m', 'chg_3m']:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors='coerce')
    return df


@st.cache_data
def load_companies_history(mtime):
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


def _state_bg(v):
    """Background color for a state cell (handles the trailing ' ✓' marker)."""
    if not isinstance(v, str) or not v:
        return ''
    c = STATE_COLORS.get(v.replace(' ✓', ''))
    return f'background-color: {c}; color: white' if c else ''


def _rot_arrow(d):
    """Rotation arrow from the recent change in RS-Momentum: rising / falling / flat."""
    if d is None or d != d:   # None or NaN
        return ''
    return '↑' if d > ROT_EPS else '↓' if d < -ROT_EPS else '→'


def _rot_style(v):
    """Green for ↑, red for ↓, grey for flat — text colour only."""
    return {'↑': 'color: #2ca02c', '↓': 'color: #d62728'}.get(v, 'color: #999999')


def _zscore_rows(frame):
    """Cross-sectional z-score per date (row): (x - row mean) / row std. A 0 std
    row (or fewer than 2 values) yields NaN, which callers drop."""
    mean = frame.mean(axis=1)
    std = frame.std(axis=1).replace(0, float('nan'))
    return frame.sub(mean, axis=0).div(std, axis=0)


_RRG_COLS = ['rs_ratio', 'rs_momentum', 'mom_delta', 'state', 'confirmed', 'tail']


def _rrg_from_wide(wide):
    """Core relative-rotation computation from a date x id wide matrix of rs_2w.

    rs_2w is a 2-week *excess return*, so its cumulative sum is the relative line;
    we smooth that into the RS-Ratio (level) and take its rate of change as
    RS-Momentum (direction). Both are z-scored per date so the quadrant origin is
    the cohort average (the RRG '100' centre).

    Returns a DataFrame indexed by the columns of `wide` with:
      rs_ratio     latest RS-Ratio  (z; >0 = above cohort average)
      rs_momentum  latest RS-Momentum (z; >0 = strengthening, leads the level)
      state        Improving / Leading / Weakening / Lagging
      confirmed    momentum held its sign for >= CONFIRM_BUCKETS buckets
      tail         [(rs_ratio, rs_momentum)] for the last TAIL_POINTS dates
    Empty if fewer than 2 dates or 2 ids (a cross-sectional z-score needs both)."""
    if wide.shape[0] < 2 or wide.shape[1] < 2:
        return pd.DataFrame(columns=_RRG_COLS)

    line = wide.fillna(0).cumsum()
    ratio = line.rolling(RATIO_SMOOTH, min_periods=1).mean()
    momentum = ratio.diff(MOM_LOOKBACK).rolling(MOM_SMOOTH, min_periods=1).mean()
    ratio_z = _zscore_rows(ratio)
    mom_z = _zscore_rows(momentum)

    out = {}
    for cid in wide.columns:
        rz = ratio_z[cid].dropna()
        mz = mom_z[cid].dropna()
        if rz.empty or mz.empty:
            continue
        r_last, m_last = float(rz.iloc[-1]), float(mz.iloc[-1])
        if m_last >= 0:
            state = 'Leading' if r_last >= 0 else 'Improving'
        else:
            state = 'Weakening' if r_last >= 0 else 'Lagging'
        sign = m_last >= 0
        run = 0
        for v in reversed(mz.tolist()):
            if (v >= 0) == sign:
                run += 1
            else:
                break
        # Rotation direction: change in RS-Momentum over the last ~ROT_LOOKBACK buckets.
        # Negative while Leading = rolling over (the on-chart tail heading down).
        mom_delta = float(m_last - mz.iloc[-1 - min(ROT_LOOKBACK, len(mz) - 1)])
        common = rz.index.intersection(mz.index)[-TAIL_POINTS:]
        tail = [(round(float(ratio_z[cid][d]), 3), round(float(mom_z[cid][d]), 3))
                for d in common]
        out[cid] = {'rs_ratio': round(r_last, 2), 'rs_momentum': round(m_last, 2),
                    'mom_delta': round(mom_delta, 2), 'state': state,
                    'confirmed': run >= CONFIRM_BUCKETS, 'tail': tail}
    return pd.DataFrame.from_dict(out, orient='index') if out else pd.DataFrame(columns=_RRG_COLS)


@st.cache_data
def rrg_frame(mtime, ids, version=_RRG_VERSION):
    """Relative-rotation coordinates per sector/industry id, normalised
    cross-sectionally within the cohort `ids` (a tuple). See `_rrg_from_wide`.
    `version` only participates in the cache key (see _RRG_VERSION)."""
    h = load_history(mtime)
    ids = [i for i in ids if i in set(h['id'])]
    if not ids:
        return pd.DataFrame(columns=_RRG_COLS)
    wide = (h[h['id'].isin(ids)]
            .pivot_table(index='date', columns='id', values='rs_2w', aggfunc='last')
            .sort_index())
    return _rrg_from_wide(wide)


@st.cache_data
def company_rrg_frame(mtime, version=_RRG_VERSION):
    """Per-company RRG state, z-scored across the WHOLE company universe (every
    company in companies_hist), so a company's state reflects its RS trajectory
    versus the market-wide screened set. Returns a flat DataFrame:
    industry_id, symbol, rs_ratio, rs_momentum, mom_delta, state, confirmed."""
    cols = ['industry_id', 'symbol', 'rs_ratio', 'rs_momentum', 'mom_delta', 'state', 'confirmed']
    h = load_companies_history(mtime)
    if h.empty:
        return pd.DataFrame(columns=cols)
    # Composite key so a symbol listed under two industries stays distinct.
    sep = '\x1f'
    keyed = h.assign(_k=h['industry_id'].astype(str) + sep + h['symbol'].astype(str))
    wide = (keyed.pivot_table(index='date', columns='_k', values='rs_2w', aggfunc='last')
            .sort_index())
    r = _rrg_from_wide(wide)
    if r.empty:
        return pd.DataFrame(columns=cols)
    parts = r.index.to_series().str.split(sep, n=1, expand=True)
    r['industry_id'] = parts[0].values
    r['symbol'] = parts[1].values
    return r[cols].reset_index(drop=True)


# --- load ------------------------------------------------------------------
if not RESULTS_CSV.exists():
    st.error(f"Missing `{RESULTS_CSV.name}` in {DATA}.\n\nGenerate it first:\n\n"
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

# Relative-rotation state. Normalised per level so sectors are ranked among sectors
# and industries among industries (a stable signal independent of the table filters).
_hist_mtime = HISTORY_CSV.stat().st_mtime if HISTORY_CSV.exists() else 0
_RRG_FIELDS = ('rs_ratio', 'rs_momentum', 'mom_delta', 'state', 'confirmed')
if not history.empty:
    _rrg = pd.concat([rrg_frame(_hist_mtime, tuple(results[results['level'] == lv]['id']), _RRG_VERSION)
                      for lv in ('sector', 'industry')])
    # Defensive: merge only the fields actually present (a stale cache could lack a new one),
    # then backfill any missing field so downstream code never KeyErrors.
    _have = [c for c in _RRG_FIELDS if c in _rrg.columns]
    results = results.merge(_rrg[_have], left_on='id', right_index=True, how='left')
    for c in _RRG_FIELDS:
        if c not in results.columns:
            results[c] = None
    # Tag confirmed turns with a check so the table shows tentative vs confirmed.
    results['state'] = [f"{s} ✓" if (isinstance(s, str) and c) else s
                        for s, c in zip(results['state'], results['confirmed'])]
    results['rot'] = results['mom_delta'].map(_rot_arrow)
else:
    for c in _RRG_FIELDS + ('rot',):
        results[c] = None

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

# Relative-rotation state per company, normalised across the whole company universe
# (mirrors the per-level sector/industry block above).
if not companies.empty and not companies_hist.empty:
    _crrg = company_rrg_frame(COMPANIES_HISTORY_CSV.stat().st_mtime, _RRG_VERSION)
    _cfields = ['rs_momentum', 'mom_delta', 'state', 'confirmed']
    _chave = ['industry_id', 'symbol'] + [c for c in _cfields if c in _crrg.columns]
    companies = companies.merge(_crrg[_chave], on=['industry_id', 'symbol'], how='left')
    for c in _cfields:
        if c not in companies.columns:
            companies[c] = None
    companies['state'] = [f"{s} ✓" if (isinstance(s, str) and c) else s
                          for s, c in zip(companies['state'], companies['confirmed'])]
    companies['rot'] = companies['mom_delta'].map(_rot_arrow)
elif not companies.empty:
    for _c in ('rs_momentum', 'mom_delta', 'state', 'confirmed', 'rot'):
        companies[_c] = None


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
            + ['trend', 'state', 'rot', 'rs_momentum', 'chg_1w', 'chg_1m', 'chg_3m',
               'rs_score', 'rs_1m', 'rs_3m', 'rs_6m', 'rs_1y', 'ret_1y'])
    cfg = {
        'rank':     st.column_config.NumberColumn("#", format="%d"),
        'symbol':   st.column_config.TextColumn("Symbol"),
        'name':     st.column_config.TextColumn("Name", width="medium"),
        'industry': st.column_config.TextColumn("Industry", width="medium"),
        'trend':    st.column_config.ImageColumn("RS trend (6m)", width="medium"),
        'state':    st.column_config.TextColumn("State",
                                                help="RRG rotation vs the whole company universe: "
                                                     "Improving/Leading/Weakening/Lagging (✓ = momentum confirmed)"),
        'rot':      st.column_config.TextColumn("Rot", help="RS-Momentum rising ↑ / falling ↓ over "
                                                            "~4 weeks; ↓ while Leading = rolling over"),
        'rs_momentum': st.column_config.NumberColumn("RS mom", format="%.2f",
                                                     help="RS-Momentum (z): >0 strengthening, <0 weakening — leads the level"),
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
    styler = rs_styler(cdf, ('rs_score', 'rs_momentum', 'chg_1w', 'chg_1m', 'chg_3m'),
                       ('rs_1m', 'rs_3m', 'rs_6m', 'rs_1y'))
    if 'state' in cdf.columns:
        styler = styler.map(_state_bg, subset=['state'])
    if 'rot' in cdf.columns:
        styler = styler.map(_rot_style, subset=['rot'])
    # Pass height only when set: Streamlit >=1.58 rejects height=None (must be a positive
    # int / 'stretch' / 'content'), and omitting it uses the auto default on all versions.
    extra = {'height': height} if height is not None else {}
    st.dataframe(
        styler, hide_index=True, width='stretch',
        column_order=[c for c in cols if c in cdf.columns], column_config=cfg, **extra,
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
    pick_states = st.sidebar.multiselect("State (RRG)", STATE_ORDER)
    pick_rot = st.sidebar.multiselect("Rotation", list(ROT_FILTER))
    top_n = st.sidebar.number_input("Top N (0 = all)", min_value=0, value=0, step=10)
    show_detail = st.sidebar.checkbox("Show detail columns (n, clip, cov, id)", value=False)

    df = results.copy()
    if level != "All":
        df = df[df['level'] == level]
    if pick_sectors:
        df = df[df['sector'].isin(pick_sectors)]
    if search:
        df = df[df['name'].str.contains(search, case=False, na=False)]
    if pick_states and 'state' in df.columns:
        df = df[df['state'].fillna('').str.replace(' ✓', '', regex=False).isin(pick_states)]
    if pick_rot and 'rot' in df.columns:
        df = df[df['rot'].isin([ROT_FILTER[x] for x in pick_rot])]
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
        'state':          st.column_config.TextColumn("State",
                                                      help="RRG rotation: Improving/Leading/Weakening/Lagging "
                                                           "(✓ = momentum confirmed). Improving/Weakening are early turns."),
        'rot':            st.column_config.TextColumn("Rot", help="RS-Momentum rising ↑ / falling ↓ over "
                                                                  "~4 weeks; ↓ while Leading = rolling over"),
        'rs_momentum':    st.column_config.NumberColumn("RS mom", format="%.2f",
                                                        help="RS-Momentum (z): >0 strengthening, <0 weakening — leads the level"),
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
    styler = rs_styler(df, ('rs_score', 'rs_median', 'rs_momentum'), ('rs_1m', 'rs_3m', 'rs_6m', 'rs_1y'))
    if 'state' in df.columns:
        styler = styler.map(_state_bg, subset=['state'])
    if 'rot' in df.columns:
        styler = styler.map(_rot_style, subset=['rot'])
    event = st.dataframe(
        styler, hide_index=True, width='stretch',
        column_order=column_order, column_config=column_config,
        on_select="rerun", selection_mode="single-row", key="rs_table",
    )

    if df.empty:
        st.info("No rows match the current filters.")
        return
    rows = event.selection.rows
    # A selection index can outlive the rows it pointed at if the filters shrink the table
    # on a later rerun; fall back to the first row when it's now out of range.
    sel_pos = rows[0] if (rows and rows[0] < len(df)) else 0
    sel_id = df.iloc[sel_pos]['id']
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
    # Scope the industry list to the picked sector(s) so it stays short (~145 otherwise).
    _ind_pool = companies[companies['sector'].isin(pick_sectors)] if pick_sectors else companies
    pick_industries = st.sidebar.multiselect("Industry", sorted(_ind_pool['industry'].dropna().unique()))
    search = st.sidebar.text_input("Search symbol / name")
    valid = companies['rs_score'].dropna()
    lo = float(valid.min()) if not valid.empty else -100.0
    hi = float(valid.max()) if not valid.empty else 100.0
    if hi <= lo:
        hi = lo + 1.0
    score_min = st.sidebar.slider("Min RS score", lo, hi, value=lo)
    pick_states = st.sidebar.multiselect("State (RRG)", STATE_ORDER)
    pick_rot = st.sidebar.multiselect("Rotation", list(ROT_FILTER))
    top_n = st.sidebar.number_input("Top N by RS (0 = all)", min_value=0, value=0, step=25)

    # Multi-column sort (priority = selection order). Streamlit's header-click sort
    # is single-column, so this pre-sorts the frame. Text cols A->Z, numeric high->low.
    sort_opts = ['rs_score', 'rs_momentum', 'mom_delta', 'state', 'rs_1m', 'rs_3m', 'rs_6m', 'rs_1y',
                 'ret_1y', 'chg_1w', 'chg_1m', 'chg_3m', 'industry', 'sector', 'symbol', 'name']
    sort_cols = st.sidebar.multiselect("Sort by (priority order)", sort_opts, default=['rs_score'])
    text_cols = {'industry', 'sector', 'symbol', 'name', 'state'}
    sort_by = [(c, c in text_cols) for c in sort_cols] or [('rs_score', False)]

    cdf = companies.copy()
    if pick_sectors:
        cdf = cdf[cdf['sector'].isin(pick_sectors)]
    if pick_industries:
        cdf = cdf[cdf['industry'].isin(pick_industries)]
    if search:
        cdf = cdf[cdf['symbol'].str.contains(search, case=False, na=False)
                  | cdf['name'].str.contains(search, case=False, na=False)]
    if pick_states and 'state' in cdf.columns:
        cdf = cdf[cdf['state'].fillna('').str.replace(' ✓', '', regex=False).isin(pick_states)]
    if pick_rot and 'rot' in cdf.columns:
        cdf = cdf[cdf['rot'].isin([ROT_FILTER[x] for x in pick_rot])]
    cdf = cdf[cdf['rs_score'].fillna(-1e9) >= score_min]
    if top_n:  # pick the strongest N by RS, then apply the display sort
        cdf = cdf.sort_values('rs_score', ascending=False).head(int(top_n))

    st.caption(f"{len(cdf)} stocks across all industries · sort: "
               f"{', '.join(c for c, _ in sort_by)} · source: {COMPANIES_CSV.name}")
    company_table(cdf, show_industry=True, height=820, sort_by=sort_by)


def render_rrg():
    """View 3: relative-rotation scatter — RS-Ratio (level) vs RS-Momentum
    (direction) — with trajectory tails, so turns are visible before the level
    crosses over. Improving (top-left) and Weakening (bottom-right) are the early
    quadrants; Leading/Lagging are the confirmed ones."""
    st.markdown("#### Relative Rotation")
    st.caption("RS-Ratio (strength level, x) vs RS-Momentum (rate of change, y), z-scored "
               "within the cohort. The dot is the current position; the grey trail fades "
               "from older to now and the arrow points the way it's rotating. Use the "
               "sidebar to shorten tails or highlight names. "
               "Window: bi-weekly relative strength over ~1y (snapshot from the last "
               "screener run) — the level (x) reflects the full year, momentum (y) ~the "
               "last 6–10 weeks, and each tail point is ~2 weeks.")
    if history.empty:
        st.info("No bi-weekly history available. Re-run screener_strength.py.")
        return

    cohort = st.sidebar.radio("Cohort", ["Sectors", "Industries in a sector"])
    if cohort == "Sectors":
        sub = results[results['level'] == 'sector']
        title = "Sectors"
    else:
        sec = st.sidebar.selectbox("Sector", sorted(results['sector'].dropna().unique()))
        sub = results[(results['level'] == 'industry') & (results['sector'] == sec)]
        title = f"{sec} industries"

    rrg = rrg_frame(_hist_mtime, tuple(sub['id']), _RRG_VERSION)
    if rrg.empty:
        st.info("Not enough history to compute rotation for this cohort (need ≥2 members).")
        return
    names = dict(zip(results['id'], results['name']))

    tail_len = st.sidebar.slider("Tail length", 0, TAIL_POINTS, min(5, TAIL_POINTS),
                                 help="0 = current positions only (declutters the chart)")
    focus = st.sidebar.multiselect("Highlight", [names.get(i, i) for i in rrg.index],
                                   help="Draw tails only for these (others show as dots)")
    focus_ids = {i for i in rrg.index if names.get(i, i) in focus}

    fig, ax = plt.subplots(figsize=(9, 7))
    lim = 0.0
    for cid, r in rrg.iterrows():
        color = STATE_COLORS.get(r['state'], '#888888')
        show_tail = tail_len and (not focus_ids or cid in focus_ids)
        tail = (r['tail'] or [])[-(tail_len + 1):] if show_tail else []
        xs = [p[0] for p in tail]
        ys = [p[1] for p in tail]
        lim = max([lim] + [abs(v) for v in xs + ys if v == v]
                  + [abs(r['rs_ratio']), abs(r['rs_momentum'])])
        # Fading neutral-gray comet trail (old = faint/thin -> now = solid) so trails
        # don't tangle by colour; an arrowhead at the current point shows direction.
        n = len(tail)
        for i in range(1, n):
            f = i / n
            ax.plot(xs[i - 1:i + 1], ys[i - 1:i + 1], '-', color='#9e9e9e',
                    alpha=0.10 + 0.45 * f, linewidth=0.6 + 1.4 * f,
                    solid_capstyle='round', zorder=1)
        if n >= 2:
            ax.annotate('', xy=(xs[-1], ys[-1]), xytext=(xs[-2], ys[-2]),
                        arrowprops=dict(arrowstyle='-|>', color=color, lw=1.4, alpha=0.9),
                        zorder=2)
        dim = bool(focus_ids) and cid not in focus_ids
        ax.scatter([r['rs_ratio']], [r['rs_momentum']], color=color,
                   s=70 if dim else 120, zorder=3, edgecolors='white', linewidths=0.8,
                   alpha=0.35 if dim else 1.0)
        if not dim:
            ax.annotate(names.get(cid, cid), (r['rs_ratio'], r['rs_momentum']),
                        fontsize=8, xytext=(6, 4), textcoords='offset points', zorder=4,
                        bbox=dict(boxstyle='round,pad=0.12', fc='white', ec='none', alpha=0.6))
    lim = (lim or 1.0) * 1.15
    ax.set_xlim(-lim, lim)
    ax.set_ylim(-lim, lim)
    ax.axhline(0, color='#999', linewidth=0.8)
    ax.axvline(0, color='#999', linewidth=0.8)
    # Faint quadrant tints + corner labels.
    ax.axhspan(0, lim, xmin=0.5, xmax=1.0, color=STATE_COLORS['Leading'], alpha=0.05)
    ax.axhspan(-lim, 0, xmin=0.5, xmax=1.0, color=STATE_COLORS['Weakening'], alpha=0.05)
    ax.axhspan(-lim, 0, xmin=0.0, xmax=0.5, color=STATE_COLORS['Lagging'], alpha=0.05)
    ax.axhspan(0, lim, xmin=0.0, xmax=0.5, color=STATE_COLORS['Improving'], alpha=0.05)
    for qx, qy, txt in [(0.98, 0.98, 'Leading'), (0.98, 0.02, 'Weakening'),
                        (0.02, 0.02, 'Lagging'), (0.02, 0.98, 'Improving')]:
        ax.text(qx, qy, txt, transform=ax.transAxes, color=STATE_COLORS[txt], fontsize=10,
                ha='right' if qx > 0.5 else 'left', va='top' if qy > 0.5 else 'bottom',
                alpha=0.75, fontweight='bold')
    ax.set_xlabel("RS-Ratio  (relative strength level, z)")
    ax.set_ylabel("RS-Momentum  (rate of change, z)")
    ax.set_title(f"Relative rotation — {title}")
    st.pyplot(fig)
    plt.close(fig)

    # Actionable list: early turns (Improving/Weakening) first.
    tbl = rrg.copy()
    tbl['name'] = [names.get(i, i) for i in tbl.index]
    rank = {s: i for i, s in enumerate(STATE_ORDER)}
    tbl['_o'] = tbl['state'].map(rank)
    tbl['state'] = [f"{s} ✓" if c else s for s, c in zip(tbl['state'], tbl['confirmed'])]
    tbl = tbl.sort_values(['_o', 'rs_momentum'], ascending=[True, False])
    show = tbl[['name', 'state', 'rs_ratio', 'rs_momentum']]
    sty = show.style.map(_state_bg, subset=['state'])
    mm = float(show['rs_momentum'].abs().max() or 0)
    if mm > 0:
        sty = sty.background_gradient(cmap='RdYlGn', subset=['rs_momentum'], vmin=-mm, vmax=mm)
    st.dataframe(sty, hide_index=True, width='stretch', column_config={
        'name':        st.column_config.TextColumn("Name", width="medium"),
        'state':       st.column_config.TextColumn("State"),
        'rs_ratio':    st.column_config.NumberColumn("RS-Ratio", format="%.2f"),
        'rs_momentum': st.column_config.NumberColumn("RS-Momentum", format="%.2f"),
    })


# --- header navigation + view dispatch -------------------------------------
# Views live as tabs in the top header (st.navigation position="top"); the sidebar
# is left for each page's own filters. The reload control is shared across pages.
if st.sidebar.button("🔄 Reload data"):
    st.cache_data.clear()
    st.rerun()

pages = [
    st.Page(render_sectors_industries, title="Sectors & Industries", icon="📊", default=True),
    st.Page(render_companies,          title="All companies",        icon="🏢"),
    st.Page(render_rrg,                title="Rotation (RRG)",        icon="🔄"),
]
st.navigation(pages, position="top").run()
