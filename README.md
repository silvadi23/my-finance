# Sector & Industry Relative-Strength Dashboard

A top-down view of **where market strength is**: it ranks the 11 GICS sectors and ~145
industries by their relative strength versus the S&P 500 (SPY), lets you drill into each
industry's strongest companies, and shows where everything is **rotating** next.

---

## Quick start

**Live app:** <https://my-finance-silvadi23.streamlit.app>
(runs off a committed data snapshot — see *Refresh the data* below to update it).

**Run locally:**

```bash
pip install -r requirements.txt
python -m streamlit run src/strength_dashboard.py
```

**Refresh the data** (optional — needs internet and `yfinance`; takes a few minutes):

```bash
pip install yfinance
python src/screener_strength.py        # rewrites the CSVs in src/data_results/
```

The dashboard reads pre-computed CSVs, so it starts instantly; you only re-run the screener
when you want fresher numbers. Inside the app, **🔄 Reload data** picks up new CSVs.

---

## What you see

The app has three tabs in the top header.

### 📊 Sectors & Industries

A sortable, color-coded ranking of every sector and industry. Click any row to open a **trend
chart**; clicking an *industry* also lists its **top companies**. Reading a row:

- **RS heatmap** (`RS 1m / 3m / 6m / 1y`) — red→green on one shared scale, so left-to-right
  comparison is meaningful.
- **State** — the rotation phase (see *Reading the signals*).
- **RS mom** — how fast strength is changing (the early-warning number).
- **RS score / RS median** — the composite strength level, and a breadth cross-check.
- **Gap** — `RS score − RS median`; a big gap means a few names drive the score, not the whole group.
- **Trend** — a green/red 6-month sparkline of the bi-weekly relative strength.

Sidebar filters: Level, Sector, name search, Min RS score, State, Top N.

### 🏢 All companies

Every screened company across all industries, ranked by RS score, with the same column
treatment plus recent price changes (`Chg 1w/1m/3m`). Sidebar filters: **Sector → Industry**
(the industry list narrows to the chosen sectors), State, symbol/name search, Min RS score,
Top N, and a multi-column **Sort by** (priority = selection order).

### 🔄 Rotation (RRG)

A relative-rotation scatter: **RS-Ratio** (strength level) on the x-axis vs **RS-Momentum**
(rate of change) on the y-axis, with fading "comet" tails and an arrowhead showing each name's
direction. Pick the cohort (all **Sectors**, or the industries within one sector), shorten the
tails, or highlight specific names. The four quadrants map to the four states below.

---

## Reading the signals

**Relative strength (RS)** = an asset's return minus SPY's return over the same window. Positive
= outperforming the market. The composite **RS score** blends 1m/3m/6m/1y, favouring the
intermediate term.

**State** combines the strength *level* with its *momentum* — and momentum turns before the
level does, so it flags moves early:

| State | Level | Momentum | Meaning |
|-----------|---------|----------|--------------------------------|
| Improving | below avg | rising | **early** turn up (still lagging) |
| Leading | above avg | rising | confirmed strong |
| Weakening | above avg | falling | **early** topping |
| Lagging | below avg | falling | confirmed weak |

A **✓** next to a state means the momentum direction has held for at least two periods
(confirmed, not a one-off blip).

---

## Under the hood

Technical detail for the curious — not needed to use the app.

### Data source

[yfinance](https://github.com/ranaroussi/yfinance):

- `yf.Sector(key).industries` — the sector → industry map (keys, symbols, weights).
- `yf.Industry(key).top_companies` — constituents with market weights (capped at ~50/industry).
- `yf.download(...)` — bulk ~1y daily closes.
- `yf.EquityQuery` / `yf.screen` — the size/price/liquidity universe for the company filter.

### How each level is measured

- **Sectors → SPDR ETF proxy** (`XLB, XLC, XLY, XLP, XLE, XLF, XLV, XLI, XLRE, XLK, XLU`). Clean
  tradable prices — *not* the Yahoo `^YH…` indices, whose levels step-jump on reconstitution and
  produce phantom returns.
- **Industries → constituent aggregation** (no clean industry ETF exists):
  - Cap-weighted, but each weight is **capped at 15%** and redistributed, so one mega-cap (e.g.
    NVDA in Semiconductors) can't *be* the reading.
  - Each constituent's RS is **winsorised to ±200%**, so a bad-data name can't detonate the average.
  - **RS median** = weight-free median of constituents' RS, a breadth cross-check.
- **Company drill-down** — an industry's constituents that pass a **basic-strength filter**, then
  the top 25 by their own RS score:

  | Criterion | Threshold | Source |
  | --- | --- | --- |
  | Market cap | > $2B | `yf.screen` |
  | Price | > $7 | `yf.screen` |
  | Avg volume | > 1M | `yf.screen` (`avgdailyvol3m` proxy) |
  | Price vs SMA50 | above | screen payload |
  | Price vs SMA200 | above | screen payload |
  | 6-month price change | > 10% | price history |

### Rotation (RRG) math

Each bi-weekly relative-strength point is a 2-week excess return, so its cumulative sum is the
relative line. Smoothing that gives **RS-Ratio** (level); its rate of change gives **RS-Momentum**
(direction). Both are z-scored *within the cohort* per date, so the quadrant origin is the cohort
average. Sectors and industries are normalised per level (sectors vs sectors, industries vs
industries); companies are normalised across the whole company universe. Over the ~1y window the
**level** reflects the full year, **momentum** roughly the last 6–10 weeks, and each tail point is
~2 weeks.

### Output CSVs (`src/data_results/`, `id` = yfinance slug join key)

- `strength_results.csv` — one row per sector/industry: `rank, id, level, name, sector, proxy,
  n_constituents, coverage, clipped, ret_1m..1y, rs_1m..1y, rs_score, rs_median`.
- `strength_history.csv` — tidy bi-weekly series: `id, level, name, sector, date, rs_2w`.
- `strength_companies.csv` — per-industry top companies: `industry_id, rank, symbol, name, weight,
  rs_score, rs_1m..1y, ret_1y, chg_1w/1m/3m`.
- `strength_companies_history.csv` — `industry_id, symbol, date, rs_2w`.

### Tunables & caveats

- Tunable at the top of `src/screener_strength.py`: `PERIODS`, `WEIGHTS`, `TOP_N_CONSTITUENTS`
  (100), `RS_CLIP` (2.0), `MAX_WEIGHT` (0.15); RRG knobs in `src/strength_dashboard.py`
  (`RATIO_SMOOTH`, `MOM_LOOKBACK`, `MOM_SMOOTH`, `CONFIRM_BUCKETS`, `TAIL_POINTS`, `TREND_POINTS`).
- yfinance caps `top_companies` at ~50/industry, so strong names outside that pool won't appear.
- A full screener run fetches ~2,700+ symbols and takes several minutes; a few delisted tickers
  log "no price data" and are skipped.
- Two RS notions coexist — the snapshot `rs_score` (trailing-window blend) and the bi-weekly
  `rs_2w` (per-period) — same spirit, **not** comparable in magnitude.

> `src/drafts/` holds experimental single-stock quality/SMC screeners with their own README; they
> are separate from this dashboard.
