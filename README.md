# Strength and Price Dashboards

## Purpose

Top-down view of where market strength is: rank all **11 GICS sectors** and **~145 industries** by
**relative strength (RS) vs SPY** over 1m / 3m / 6m / 1y, then **drill into each industry's strongest
companies**, and **visualize** it.

- **`screener_strength.py`** — computes RS, writes the CSVs.
- **`strength_dashboard.py`** — Streamlit app over those CSVs (table + heatmap + sparklines + drill-down).

The company drill-down's basic-strength filter spec is documented in this file (see below).

## Run it

Collect data:

```bash
python screener_strength.py
```

Open dashboard:

```bash
python -m streamlit run strength_dashboard.py
```

## Data source

[yfinance](https://github.com/ranaroussi/yfinance):

- `yf.Sector(key)` → sector index + `.industries` (industry keys, symbols, weights).
- `yf.Industry(key).top_companies` → constituents with market weights (yfinance caps this at **~50** per industry).
- `yf.download(...)` → bulk ~1y daily closes (batched HTTP).
- `yf.EquityQuery` / `yf.screen` → the size/price/liquidity universe for the company filter.

## Core metric

**Relative strength (RS)** for a window = `asset return − SPY return` over the same window.
**Composite `rs_score`** = weighted blend across windows: `1m 0.20, 3m 0.30, 6m 0.30, 1y 0.20`
(favours the intermediate term — long enough to be a real trend, recent enough to be actionable).

## How each level is measured (and why)

### Sectors → SPDR ETF proxy

Uses the clean SPDR sector ETFs (`XLB, XLC, XLY, XLP, XLE, XLF, XLV, XLI, XLRE, XLK, XLU`).
**Not** the Yahoo `^YH…` sector/industry indices — those are *aggregate market-cap levels* that
step-jump on index reconstitution (e.g. a phantom +1032% one-day "move"), which corrupts returns.
That approach was tried and discarded.

### Industries → constituent aggregation

No clean industry ETF exists, so aggregate the constituents:

- **Cap-weighted**, but each weight is **capped at `MAX_WEIGHT = 0.15`** and redistributed, so a single
  mega-cap (NVDA is ~46% of Semiconductors) can't *be* the industry's reading.
- Each constituent's RS is **winsorised to `±RS_CLIP = 200%`**, neutralising bad-data names (SNDK's
  fake 47×) so one corrupt high-weight name can't detonate the average.
- **`rs_median`** = weight-free median of constituents' RS — a breadth cross-check. A large
  `rs_score − rs_median` gap means the score is driven by a few names, not broad strength.

### Bi-weekly trend (`rs_2w`) — for sparklines

For each consecutive ~2-week (10 trading-day) bucket: the **median constituent excess return**
(sector = the ETF's own excess), winsorised. A per-entity time series for charting.

### Company drill-down — per industry

Each industry's constituents that pass the **basic-strength filter**, then the **top 25 by their own
`rs_score`** (winsorised). This filter is the canonical spec (it previously lived in a separate
`basic_strength_screen_params.md`, now inlined here):

| Criterion | Threshold | Enforced via |
| --- | --- | --- |
| Market cap | > 2B | `yf.screen` membership |
| Price | > $7 | `yf.screen` membership |
| Avg volume | > 1M (≈60-day) | `yf.screen` membership — uses `avgdailyvol3m` (~63-day) as the proxy; no exact 60-day field exists |
| Price vs SMA50 | price > SMA50 | screen payload `fiftyDayAverageChangePercent > 0` |
| Price vs SMA200 | price > SMA200 | screen payload `twoHundredDayAverageChangePercent > 0` |
| 6-month price change | > 10% | `period_return(closes, 126)` from history |

`TOP_N_CONSTITUENTS = 100` (effectively ~50 due to the yfinance cap) gives a deeper *pool* so the
displayed top-25 are genuinely the strongest, not just the largest.

## Outputs (CSVs, written to this folder; `id` = yfinance slug, the join key)

- **`strength_results.csv`** — `rank, id, level, name, sector, proxy, n_constituents, coverage,
  clipped, ret_1m..1y, rs_1m..1y, rs_score, rs_median` (sectors then industries).
- **`strength_history.csv`** — `id, level, name, sector, date, rs_2w` (tidy/long, bi-weekly).
- **`strength_companies.csv`** — `industry_id, rank, symbol, name, weight, rs_score, rs_1m..1y, ret_1y`.
- **`strength_companies_history.csv`** — `industry_id, symbol, date, rs_2w`.

## Dashboard (`strength_dashboard.py`)

Streamlit. Reads the four CSVs (cached on file mtime; "Reload data" clears the cache).

- Sortable sector/industry table; **RS-window heatmap** (rs_1m..1y on one shared red→green scale so
  left→right is comparable); `rs_score`/`rs_median` color-scaled; a breadth **Gap** column.
- **Green/red 6-month bar sparkline** per row — rendered as a tiny matplotlib PNG via `ImageColumn`
  (Streamlit's chart columns can't color bars by sign), showing the last `TREND_POINTS = 13`
  bi-weekly points; cached.
- **Row click** → detail line chart + metrics; selecting an **industry** also renders a **"Top
  companies"** sub-table with the same column treatment. Detail columns (`n/clip/cov/id`) hidden by default.

```bash
python -m streamlit run strength_dashboard.py
```

Requires `streamlit`, `pandas`, `matplotlib`.

## Major choices (summary)

- Dropped the corrupted `^YH` indices → **ETF for sectors, constituents for industries**.
- **Cap-weight (15%) + winsorize (200%)** for robustness + de-concentration; **median** for breadth transparency.
- Company size/price/vol filter via **one `yf.screen`** (avoids thousands of `.info` calls); SMA from
  payload; 6m change from history.
- **Image sparklines** (not `BarChartColumn`) to get green/red bars.

## Known caveats

- `top_companies` caps at ~50/industry → pool ceiling; strong small-caps outside an industry's top ~50 won't appear.
- Full run fetches ~2,700+ symbols → multi-minute runtime.
- Two RS notions coexist: snapshot `rs_score` (trailing-window blend) vs `rs_2w` (per-period) — same
  spirit, **not** comparable in magnitude (snapshot ranks, series trends).
- A few delisted tickers may log "no price data" — harmless, skipped.

## Tunable constants (`screener_strength.py`, top of file)

`PERIODS`, `WEIGHTS`, `TOP_N_CONSTITUENTS` (100), `RS_CLIP` (2.0), `MAX_WEIGHT` (0.15); dashboard:
`TREND_POINTS` (13).
